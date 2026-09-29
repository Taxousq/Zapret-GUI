"""Проверка обновлений и замена exe TG WS Proxy.

`TG WS Proxy <https://github.com/Flowseal/tg-ws-proxy>`_ — отдельное приложение
со своим трей-приложением, поэтому обновляется оно иначе, чем запрет: релизы
публикуются в репозитории Flowseal, из ассетов скачивается готовый exe, и он
просто кладётся на место старого. Никакой распаковки архивов и замены папки
здесь нет.

Последовательность (см. :meth:`TgwsUpdater.latest_release`,
:meth:`TgwsUpdater.download_release`, :meth:`TgwsUpdater.install_release`):

1. проверить последний релиз через GitHub API и найти в нём ассет с
   :data:`config.TGWS_ASSET_HINT` (``windows``) и расширением ``.exe``
   (обычно ``TgWsProxy_windows.exe``);
2. скачать ассет во временную папку и убедиться, что он не пустой
   (:data:`config.TGWS_MIN_SIZE`);
3. **закрыть процесс** — иначе Windows не даст перезаписать занятый exe.
   Решение о закрытии принимает страница (диалог подтверждения), а
   :meth:`TgwsUpdater.install_release` лишь отказывается работать с живым
   процессом;
4. скопировать новый exe поверх старого и запустить его заново.

Отдельного «остановить и не запускать» здесь нет: раздел «TG WS Proxy» не
останавливает прокси в обычной работе — пользователь управляет им через
собственный трей прокси. ``taskkill`` вызывается только на время обновления
(см. :meth:`core.tgws_manager.TgwsManager.kill_process`).
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from config import (
    CHECK_USER_AGENT,
    GITHUB_ACCEPT,
    TGWS_ASSET_HINT,
    TGWS_DOWNLOAD_TIMEOUT,
    TGWS_GITHUB_API_LATEST,
    TGWS_GITHUB_RELEASES_URL,
    TGWS_GITHUB_REPO,
    TGWS_MIN_SIZE,
    UPDATE_TIMEOUT,
)
from core.tgws_manager import NOT_INSTALLED_HINT, TgwsManager

log = logging.getLogger(__name__)

#: Размер порции при скачивании exe (байт).
CHUNK_SIZE = 64 * 1024

#: Прогресс скачивания: ``(скачано байт, всего байт)``. ``0`` во втором
#: параметре означает «общий размер неизвестен».
ProgressCallback = Callable[[int, int], None]

#: Текст ошибки при исчерпанном лимите запросов GitHub API.
RATE_LIMIT_TEXT = "Превышен лимит запросов GitHub, попробуйте позже."

#: Текст ошибки, когда GitHub недоступен.
NETWORK_TEXT = "Не удалось проверить обновления. Проверьте подключение к интернету."

#: Текст ошибки, когда в релизе нет exe для Windows.
NO_ASSET_TEXT = "В релизе нет .exe для Windows"


class TgwsUpdaterError(RuntimeError):
    """Понятная пользователю ошибка проверки или установки обновления."""


@dataclass(frozen=True)
class TgwsRelease:
    """Сведения о последнем релизе TG WS Proxy на GitHub."""

    #: Версия без ведущей ``v`` (``tag_name`` релиза).
    version: str
    #: Имя скачиваемого файла (``TgWsProxy_windows.exe``).
    asset_name: str
    #: Ссылка на ассет (``browser_download_url``).
    asset_url: str
    #: Описание релиза (``body``) — показывается как «что нового».
    notes: str = ""


def version_key(version: str) -> tuple[int, ...]:
    """«v1.10.3» -> ``(1, 10, 3)`` — ключ для сравнения версий.

    Сравнивать версии строками нельзя: ``"1.10.0" < "1.9.0"`` — ложь.
    Ведущая ``v`` срезается, суффиксы вроде ``1.0.1-beta`` отбрасываются: для
    обновления важны только числа.
    """
    text = str(version or "").strip().lstrip("vV")
    return tuple(int(number) for number in re.findall(r"\d+", text))


def is_newer(current: str, latest: str) -> bool:
    """Новее ли версия ``latest`` версии ``current`` (сравнение по числам).

    Пустая ``latest`` новее не считается: неизвестную версию нельзя выдавать
    за обновление.
    """
    if not str(latest or "").strip():
        return False
    return version_key(latest) > version_key(current)


def _is_windows_exe_asset(name: str) -> bool:
    """Ассет для Windows: в имени есть ``windows`` и оно кончается на ``.exe``.

    Регистр не важен и ``.exe`` проверяется по расширению: в релизах могут
    появиться ``TgWsProxy_windows.exe``, ``TgWsProxy_Windows_amd64.exe`` и
    подобные имена, а ``.zip`` или ``.exe.sha256`` ассетом не считаются.
    """
    text = str(name or "").strip().lower()
    return TGWS_ASSET_HINT in text and text.endswith(".exe")


class TgwsUpdater:
    """Проверка релизов TG WS Proxy, скачивание и замена exe.

    :param timeout: таймаут запросов к GitHub API (секунды);
    :param download_timeout: таймаут скачивания exe (секунды);
    :param manager: менеджер приложения — по нему берётся путь к exe и
        проверяется, запущен ли процесс. ``None`` — создать свой.
    """

    def __init__(
        self,
        timeout: int = UPDATE_TIMEOUT,
        download_timeout: int = TGWS_DOWNLOAD_TIMEOUT,
        manager: TgwsManager | None = None,
    ) -> None:
        self.timeout = int(timeout)
        self.download_timeout = int(download_timeout)
        #: Менеджер приложения: через него узнаётся путь к exe и состояние
        #: процесса. Главное окно передаёт свой экземпляр, чтобы не создавать
        #: второй (оба ищут exe в одних и тех же местах).
        self.manager = manager if manager is not None else TgwsManager()

    # ------------------------------------------------------------------
    #  Версии
    # ------------------------------------------------------------------
    def current_version(self, exe_path: Path | None = None) -> str:
        """Версия установленного TG WS Proxy (пустая — не узнать).

        :param exe_path: явный путь к exe; ``None`` — взять у менеджера
            (:meth:`core.tgws_manager.TgwsManager.tgws_exe_path`). Запускает
            процесс, поэтому страница зовёт это из фона.

        Если сборка уже показала, что флага ``--version`` у неё нет, команда
        не повторяется: у таких сборок версия остаётся неизвестной, и это
        штатная ситуация (см. :meth:`core.tgws_manager.TgwsManager.version`).
        """
        if exe_path is None:
            exe_path = self.manager.tgws_exe_path()
        if exe_path is None:
            return ""
        manager = TgwsManager(exe_path)
        if not manager.version_supported():
            return ""
        return manager.version()

    def has_update(self, current: str, latest: str) -> bool:
        """Есть ли в релизе версия новее установленной."""
        return is_newer(current, latest)

    # ------------------------------------------------------------------
    #  Сеть
    # ------------------------------------------------------------------
    @staticmethod
    def _headers() -> dict[str, str]:
        """Заголовки запросов: GitHub требует User-Agent с именем приложения."""
        return {
            "Accept": GITHUB_ACCEPT,
            "User-Agent": CHECK_USER_AGENT,
        }

    def latest_release(self) -> TgwsRelease:
        """Спрашивает у GitHub последний релиз TG WS Proxy.

        :raises TgwsUpdaterError: сеть недоступна, лимит запросов исчерпан,
            GitHub ответил ошибкой, релизов нет или в релизе нет exe для
            Windows.
        """
        try:
            response = requests.get(
                TGWS_GITHUB_API_LATEST, headers=self._headers(), timeout=self.timeout
            )
        except requests.exceptions.Timeout as exc:
            raise TgwsUpdaterError(
                "GitHub не ответил вовремя. Проверьте подключение к интернету."
            ) from exc
        except requests.exceptions.RequestException as exc:
            log.warning("Не удалось проверить обновления TG WS Proxy: %s", exc)
            raise TgwsUpdaterError(NETWORK_TEXT) from exc

        if response.status_code in (403, 429):
            raise TgwsUpdaterError(RATE_LIMIT_TEXT)
        if response.status_code == 404:
            raise TgwsUpdaterError(
                f"У {TGWS_GITHUB_REPO} нет опубликованных релизов. "
                f"Проверьте страницу: {TGWS_GITHUB_RELEASES_URL}"
            )
        if response.status_code != 200:
            raise TgwsUpdaterError(
                f"GitHub вернул код {response.status_code}. Попробуйте позже."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise TgwsUpdaterError("GitHub вернул ответ в неизвестном формате.") from exc

        assets = data.get("assets") or []
        asset = next(
            (item for item in assets if _is_windows_exe_asset(item.get("name"))),
            None,
        )
        tag = str(data.get("tag_name") or "")
        if asset is None:
            names = ", ".join(str(item.get("name")) for item in assets) or "нет"
            log.warning(
                "В релизе %s нет exe для Windows (файлы: %s)", tag or "?", names
            )
            raise TgwsUpdaterError(NO_ASSET_TEXT)

        asset_url = str(asset.get("browser_download_url") or "")
        if not asset_url:
            raise TgwsUpdaterError("У файла релиза нет ссылки для скачивания.")

        version = tag.strip().lstrip("vV") or str(data.get("name") or "")
        log.info("Последний релиз TG WS Proxy: %s (%s)", version, asset.get("name"))
        return TgwsRelease(
            version=version,
            asset_name=str(asset.get("name") or ""),
            asset_url=asset_url,
            notes=str(data.get("body") or ""),
        )

    # ------------------------------------------------------------------
    #  Скачивание
    # ------------------------------------------------------------------
    def download_release(
        self,
        release: TgwsRelease,
        target_dir: Path,
        on_progress: ProgressCallback | None = None,
    ) -> Path:
        """Скачивает exe релиза в ``target_dir`` и возвращает путь к файлу.

        Скачанный файл проверяется по размеру (:data:`config.TGWS_MIN_SIZE`):
        оборванная загрузка или HTML-заглушка вместо exe не должны попасть на
        место рабочего приложения.

        :param on_progress: вызывается из **фонового** потока с парой
            ``(скачано байт, всего байт)``; ``0`` во втором параметре — размер
            неизвестен.
        :raises TgwsUpdaterError: папка недоступна, сеть оборвалась, сервер
            ответил ошибкой или файл подозрительно мал.
        """
        directory = Path(target_dir)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.exception("Не удалось создать папку для загрузки TG WS Proxy")
            raise TgwsUpdaterError(f"Не удалось создать папку для загрузки:\n{exc}") from exc

        name = release.asset_name or Path(release.asset_url).name
        destination = directory / name

        log.info("Скачиваю TG WS Proxy %s: %s", release.version, release.asset_url)
        try:
            with requests.get(
                release.asset_url,
                headers=self._headers(),
                stream=True,
                timeout=self.download_timeout,
            ) as response:
                if response.status_code != 200:
                    raise TgwsUpdaterError(
                        f"Не удалось скачать файл (код {response.status_code})."
                    )
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if on_progress is not None:
                            on_progress(downloaded, total)
        except TgwsUpdaterError:
            _remove_file(destination)
            raise
        except requests.exceptions.RequestException as exc:
            log.warning("Скачивание TG WS Proxy прервано: %s", exc)
            _remove_file(destination)
            raise TgwsUpdaterError(f"Скачивание прервано:\n{exc}") from exc
        except OSError as exc:
            log.exception("Не удалось сохранить exe TG WS Proxy")
            _remove_file(destination)
            raise TgwsUpdaterError(f"Не удалось сохранить файл:\n{exc}") from exc

        if not _is_exe(destination):
            raise TgwsUpdaterError("Скачанный файл не является .exe.")
        try:
            size = destination.stat().st_size
        except OSError as exc:
            raise TgwsUpdaterError(f"Не удалось проверить скачанный файл:\n{exc}") from exc
        if size < TGWS_MIN_SIZE:
            _remove_file(destination)
            log.warning("Скачанный exe TG WS Proxy слишком мал: %d байт", size)
            raise TgwsUpdaterError(
                "Скачанный файл повреждён или это не приложение "
                f"({size} байт). Повторите попытку позже."
            )

        log.info("TG WS Proxy скачан: %s (%.1f МБ)", destination, size / 1024 / 1024)
        return destination

    # ------------------------------------------------------------------
    #  Установка
    # ------------------------------------------------------------------
    def install_release(
        self,
        exe_path: Path,
        new_exe: Path,
        manager: TgwsManager | None = None,
        version: str = "",
    ) -> tuple[bool, str]:
        """Заменяет exe приложения скачанным файлом и запускает его заново.

        Процесс **должен быть уже закрыт**: метод лишь отказывается работать,
        если TG WS Proxy ещё запущен. Диалог подтверждения и ``taskkill`` —
        задача страницы (см. :meth:`core.tgws_manager.TgwsManager.kill_process`):
        замена занятого файла на Windows всё равно не удалась бы.

        :param exe_path: путь к установленному exe (заменяется);
        :param new_exe: скачанный файл релиза;
        :param manager: менеджер для запуска; ``None`` — взять свой;
        :param version: версия релиза — попадает в сообщение об успехе.
        :returns: ``(успех, сообщение)``; при успехе приложение запущено заново.
        """
        target_manager = manager if manager is not None else self.manager

        if target_manager.is_running():
            return False, "TG WS Proxy запущен — сначала закройте его"

        # Старый exe сначала отводится в сторону (``.bak``): прерывание копии
        # оставило бы на его месте обрубок, и запускать было бы нечего. Если
        # копия не удалась, файл возвращается на место.
        backup = exe_path.with_name(f"{exe_path.name}.bak")
        try:
            if backup.is_file():
                backup.unlink()
            shutil.move(str(exe_path), str(backup))
        except OSError as exc:
            log.exception("Не удалось подготовить замену exe TG WS Proxy")
            return False, (
                "Не удалось заменить файл приложения "
                f"(закройте TG WS Proxy и повторите):\n{exc}"
            )

        try:
            shutil.copy2(str(new_exe), str(exe_path))
        except OSError as exc:
            log.exception("Не удалось заменить exe TG WS Proxy")
            _restore_backup(backup, exe_path)
            return False, (
                "Не удалось заменить файл приложения "
                f"(старый файл возвращён на место):\n{exc}"
            )

        _remove_file(backup)
        log.info("Exe TG WS Proxy заменён: %s", exe_path)
        started, message = target_manager.start()
        if not started:
            return False, f"Файл обновлён, но приложение не запустилось: {message}"

        suffix = f" до {version}" if str(version or "").strip() else ""
        return True, f"Обновлено{suffix}. Приложение запущено заново."


def _restore_backup(backup: Path, target: Path) -> None:
    """Возвращает отведённый в сторону exe на место (после неудачной замены)."""
    try:
        shutil.move(str(backup), str(target))
        log.warning("Старый exe TG WS Proxy возвращён на место: %s", target)
    except OSError as exc:  # восстановить не удалось — хотя бы скажем об этом
        log.error(
            "Не удалось вернуть старый exe TG WS Proxy (%s -> %s): %s",
            backup,
            target,
            exc,
        )


def _is_exe(path: Path) -> bool:
    """Существует ли файл (недоступный путь — «нет», а не исключение)."""
    try:
        return path.is_file()
    except OSError:  # pragma: no cover — нет прав на путь
        return False


def _remove_file(path: Path) -> None:
    """Удаляет неполный файл (ошибку удаления только логируем)."""
    try:
        if path.is_file():
            path.unlink()
    except OSError as exc:
        log.debug("Не удалось удалить неполный файл %s: %s", path, exc)


__all__ = [
    "NETWORK_TEXT",
    "NO_ASSET_TEXT",
    "NOT_INSTALLED_HINT",
    "ProgressCallback",
    "RATE_LIMIT_TEXT",
    "TgwsRelease",
    "TgwsUpdater",
    "TgwsUpdaterError",
    "is_newer",
    "version_key",
]
