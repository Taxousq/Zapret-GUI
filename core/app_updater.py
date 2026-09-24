"""Автообновление самого приложения через GitHub Releases.

Релизы GUI публикуются в репозитории :data:`config.APP_GITHUB_REPO`. Приложение
спрашивает у GitHub API последний релиз, сравнивает его версию со своей
(:data:`config.APP_VERSION`) и, если версия новее, скачивает установщик Inno
Setup (``ZapretGUI-Setup-*.exe``) во временную папку
(``%TEMP%\\ZapretGUI-update``) и запускает его отдельным процессом. Установщик
запускается «отвязанным» (``DETACHED_PROCESS``), поэтому переживает закрытие
приложения — сразу после запуска GUI завершается (это делает главное окно).

Это **не** то же самое, что :mod:`core.updater`: там обновляется папка запрета
(релизы Flowseal, ``.zip``-архив, файлы копируются поверх существующих). Здесь
обновляется только сам GUI и только через установщик.

Portable-версия автоматически не обновляется: установщик ставит приложение в
``Program Files`` и пишет в реестр, а portable-сборка живёт рядом с exe — такие
файлы установщик не тронет. Пользователю portable-сборки показывается подсказка
скачать новую версию с GitHub вручную.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from config import (
    APP_GITHUB_API_LATEST,
    APP_GITHUB_REPO,
    APP_UPDATE_ASSET_HINT,
    APP_UPDATE_DIR_NAME,
    APP_UPDATE_DOWNLOAD_TIMEOUT,
    APP_UPDATE_MIN_SIZE,
    APP_UPDATE_TIMEOUT,
    APP_VERSION,
    GITHUB_ACCEPT,
)
from core import portable
from core.win_utils import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

#: Размер порции при скачивании установщика (байт).
CHUNK_SIZE = 8 * 1024

#: Прогресс скачивания: процент 0..100 (``0`` — общий размер неизвестен).
ProgressCallback = Callable[[float], None]

#: Сообщение для portable-сборки: автообновление ей недоступно.
PORTABLE_TEXT = (
    "Portable-версия не обновляется автоматически, скачайте новую с GitHub."
)

#: Текст ошибки при исчерпанном лимите запросов GitHub API.
RATE_LIMIT_TEXT = "Превышен лимит запросов GitHub, попробуйте позже."

#: Текст ошибки, когда GitHub недоступен.
NETWORK_TEXT = "Не удалось проверить обновления. Проверьте подключение к интернету."

#: Маска запасного имени установщика: ``ZapretGUI.<версия>.exe``, где версия —
#: только цифры и точки (например ``ZapretGUI.1.0.1.exe``). Маска строгая:
#: ``ZapretGUI-Portable-1.0.1.exe``, ``ZapretGUI.debug.exe`` и прочие имена под
#: неё не подходят. Нужна на случай, если в релиз залит установщик под таким
#: именем (каноническое — ``ZapretGUI-Setup-<версия>.exe``).
_FALLBACK_ASSET_RE = re.compile(r"^zapretgui\.\d+(?:\.\d+)*\.exe$", re.IGNORECASE)


def _is_installer_asset(name: str) -> bool:
    """Канонический установщик: в имени есть ``Setup`` и оно кончается на ``.exe``."""
    text = str(name or "").strip().lower()
    return APP_UPDATE_ASSET_HINT in text and text.endswith(".exe")


def _is_fallback_installer_asset(name: str) -> bool:
    """Запасной установщик: ровно ``ZapretGUI.<цифры и точки>.exe``."""
    return bool(_FALLBACK_ASSET_RE.match(str(name or "").strip()))


class AppUpdaterError(RuntimeError):
    """Понятная пользователю ошибка проверки/скачивания обновления."""


@dataclass
class AppRelease:
    """Сведения о последнем релизе приложения на GitHub."""

    version: str
    tag: str
    name: str
    notes: str
    html_url: str
    asset_name: str
    asset_url: str
    asset_size: int

    @property
    def size_text(self) -> str:
        """Размер установщика строкой (``12.3 МБ`` или ``—``)."""
        if self.asset_size <= 0:
            return "—"
        return f"{self.asset_size / 1024 / 1024:.1f} МБ"


def version_key(version: str) -> tuple[int, int, int]:
    """«v1.10.3» -> ``(1, 10, 3)`` — ключ для сравнения версий.

    Сравнивать версии строками нельзя: ``"1.10.0" < "1.9.0"`` — ложь. Ведущая
    ``v`` срезается, суффиксы вроде ``1.0.1-beta`` отбрасываются: для
    автообновления важны только числа.
    """
    text = str(version or "").strip().lstrip("vV")
    numbers = re.findall(r"\d+", text)
    parts = [int(number) for number in numbers[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _packaging_key(version: str):
    """Разбирает версию через ``packaging``; ``None`` — модуль не справился.

    ``packaging`` (зависимость ``pip``/``setuptools``) понимает PEP 440:
    ``1.0.1-beta`` меньше ``1.0.1``, а ``1.0.1`` меньше ``1.0.1.post1``.
    Импорт делается на месте: в собранном приложении модуля может не быть, и
    это не повод падать — есть запасной разбор (см. :func:`version_key`).
    """
    try:
        from packaging.version import InvalidVersion, Version  # noqa: PLC0415
    except ImportError:  # pragma: no cover — модуля нет в сборке
        return None
    text = str(version or "").strip().lstrip("vV")
    if not text:
        return None
    try:
        return Version(text)
    except InvalidVersion:
        log.debug("Версию %r packaging не разобрал — сравниваю по числам", version)
        return None


def is_newer(current: str, latest: str) -> bool:
    """Новее ли версия ``latest`` версии ``current``.

    Сравнение идёт по числам, а не строками: ``1.0.0 < 1.0.1 < 1.10.0 < 2.0.0``.
    Пустая ``latest`` (релиза нет) новее не считается.

    Если доступен модуль ``packaging``, используется он (PEP 440); иначе —
    собственный разбор по числам (:func:`version_key`).
    """
    if not str(latest or "").strip():
        return False

    parsed_current = _packaging_key(current)
    parsed_latest = _packaging_key(latest)
    if parsed_current is not None and parsed_latest is not None:
        return parsed_latest > parsed_current

    left = version_key(latest)
    right = version_key(current)
    if left != right:
        return left > right
    # Числа совпали (например 1.0.1-beta против 1.0.1): предварительная версия
    # считается старше финальной, поэтому «-beta» обновлением не является.
    return _is_prerelease(current) and not _is_prerelease(latest)


def _is_prerelease(version: str) -> bool:
    """Есть ли в версии суффикс предварительного выпуска (``1.0.1-beta``)."""
    text = str(version or "").strip().lstrip("vV")
    return bool(re.search(r"[-+._]?(?:a|b|rc|alpha|beta|pre|preview|dev)\d*$", text, re.I))


class AppUpdater:
    """Проверяет, скачивает и запускает обновление самого приложения.

    :param current_version: текущая версия приложения (по умолчанию из config);
    :param timeout: таймаут запросов к GitHub API (секунды);
    :param download_timeout: таймаут скачивания установщика (секунды).
    """

    def __init__(
        self,
        current_version: str = APP_VERSION,
        timeout: int = APP_UPDATE_TIMEOUT,
        download_timeout: int = APP_UPDATE_DOWNLOAD_TIMEOUT,
    ) -> None:
        self.current_version = str(current_version or APP_VERSION)
        self.timeout = int(timeout)
        self.download_timeout = int(download_timeout)

    # ------------------------------------------------------------------
    #  Версии
    # ------------------------------------------------------------------
    @staticmethod
    def is_newer(current: str, latest: str) -> bool:
        """Новее ли версия ``latest`` версии ``current`` (см. :func:`is_newer`)."""
        return is_newer(current, latest)

    @staticmethod
    def version_key(version: str) -> tuple[int, int, int]:
        """Ключ сравнения версий (см. :func:`version_key`)."""
        return version_key(version)

    @property
    def portable(self) -> bool:
        """Работает ли приложение в portable-режиме (автообновление запрещено)."""
        return bool(portable.enabled)

    def has_update(self, release: AppRelease | None) -> bool:
        """Есть ли в релизе версия новее текущей."""
        if release is None:
            return False
        return is_newer(self.current_version, release.version)

    # ------------------------------------------------------------------
    #  Сеть
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        """Заголовки запросов: GitHub требует User-Agent с именем приложения."""
        return {
            "Accept": GITHUB_ACCEPT,
            "User-Agent": f"ZapretGUI/{self.current_version}",
        }

    def check_latest(self) -> AppRelease | None:
        """Спрашивает у GitHub последний релиз приложения.

        :returns: :class:`AppRelease` с найденным установщиком; ``None`` — если
            релизов нет (404) или в релизе нет установщика (это не ошибка:
            показывать пользователю нечего, но в журнал пишется предупреждение).
        :raises AppUpdaterError: сеть недоступна, лимит запросов исчерпан или
            GitHub ответил неожиданным кодом.
        """
        try:
            response = requests.get(
                APP_GITHUB_API_LATEST, headers=self._headers(), timeout=self.timeout
            )
        except requests.exceptions.Timeout as exc:
            raise AppUpdaterError(
                "GitHub не ответил вовремя. Проверьте подключение к интернету."
            ) from exc
        except requests.exceptions.RequestException as exc:
            log.warning("Не удалось проверить обновления приложения: %s", exc)
            raise AppUpdaterError(NETWORK_TEXT) from exc

        if response.status_code in (403, 429):
            raise AppUpdaterError(RATE_LIMIT_TEXT)
        if response.status_code == 404:
            # У репозитория ещё нет опубликованных релизов — обновляться некуда.
            log.info("У %s нет опубликованных релизов", APP_GITHUB_REPO)
            return None
        if response.status_code != 200:
            raise AppUpdaterError(
                f"GitHub вернул код {response.status_code}. Попробуйте позже."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AppUpdaterError("GitHub вернул ответ в неизвестном формате.") from exc

        assets = data.get("assets") or []
        # Сначала канонический установщик ZapretGUI-Setup-*.exe; если его в
        # релизе нет — запасной вариант ZapretGUI.<версия>.exe (например
        # ZapretGUI.1.0.1.exe). Приоритет всегда у Setup-варианта.
        asset = next(
            (item for item in assets if _is_installer_asset(item.get("name"))),
            None,
        )
        if asset is None:
            asset = next(
                (item for item in assets if _is_fallback_installer_asset(item.get("name"))),
                None,
            )
        tag = str(data.get("tag_name") or "")
        if asset is None:
            names = ", ".join(str(item.get("name")) for item in assets) or "нет"
            log.warning(
                "В релизе %s нет установщика ZapretGUI-Setup-*.exe или "
                "ZapretGUI.<версия>.exe (файлы: %s) — обновление не предлагается",
                tag or "?",
                names,
            )
            return None

        return AppRelease(
            version=tag or str(data.get("name") or ""),
            tag=tag,
            name=str(data.get("name") or ""),
            notes=str(data.get("body") or ""),
            html_url=str(data.get("html_url") or ""),
            asset_name=str(asset.get("name") or ""),
            asset_url=str(asset.get("browser_download_url") or ""),
            asset_size=int(asset.get("size") or 0),
        )

    def latest_version(self) -> str | None:
        """Версия последнего релиза — быстрая проверка без разбора ассетов.

        Ошибки сети и лимита запросов не выбрасываются: для фоновой проверки
        они не важны, а ``None`` означает «версию узнать не удалось».
        """
        try:
            release = self.check_latest()
        except AppUpdaterError as exc:
            log.info("Версию последнего релиза узнать не удалось: %s", exc)
            return None
        return release.version if release is not None else None

    # ------------------------------------------------------------------
    #  Скачивание
    # ------------------------------------------------------------------
    @staticmethod
    def update_dir() -> Path:
        """Папка для скачанного установщика: ``%TEMP%\\ZapretGUI-update``."""
        base = os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
        return Path(base) / APP_UPDATE_DIR_NAME

    def _clean_update_dir(self, directory: Path) -> None:
        """Убирает прошлые загрузки, чтобы не копить установщики в %TEMP%."""
        try:
            for stale in directory.glob("*.exe"):
                try:
                    stale.unlink()
                except OSError as exc:  # файл занят — не мешает новой загрузке
                    log.debug("Не удалось удалить старый установщик %s: %s", stale, exc)
        except OSError as exc:
            log.debug("Не удалось прочитать папку обновлений %s: %s", directory, exc)

    def download_installer(
        self, url: str, progress_cb: ProgressCallback | None = None
    ) -> tuple[bool, Path | str]:
        """Скачивает установщик во временную папку.

        :param url: ссылка на ассет релиза (``browser_download_url``);
        :param progress_cb: вызывается из **фонового** потока с процентом
            0..100 (``0`` — общий размер неизвестен).
        :returns: ``(True, путь к файлу)`` либо ``(False, текст ошибки)``.
        """
        if self.portable:
            log.info("Portable-режим: автообновление недоступно")
            return False, PORTABLE_TEXT
        if not str(url or "").strip():
            return False, "Не указана ссылка на установщик."

        directory = self.update_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.exception("Не удалось создать папку обновлений")
            return False, f"Не удалось создать папку для загрузки:\n{exc}"

        self._clean_update_dir(directory)
        name = str(url).split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "ZapretGUI-Setup.exe"
        destination = directory / name

        try:
            with requests.get(
                url,
                headers=self._headers(),
                stream=True,
                timeout=self.download_timeout,
            ) as response:
                if response.status_code != 200:
                    return False, (
                        f"Не удалось скачать установщик (код {response.status_code})."
                    )
                total = int(response.headers.get("Content-Length") or 0)
                downloaded = 0
                with destination.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb is not None:
                            progress_cb(downloaded / total * 100 if total else 0.0)
        except requests.exceptions.RequestException as exc:
            log.warning("Скачивание установщика прервано: %s", exc)
            _remove_file(destination)
            return False, f"Скачивание прервано:\n{exc}"
        except OSError as exc:
            log.exception("Не удалось сохранить установщик")
            _remove_file(destination)
            return False, f"Не удалось сохранить установщик:\n{exc}"

        if not destination.is_file():
            return False, "Установщик не сохранился — повторите попытку."
        if destination.suffix.lower() != ".exe":
            _remove_file(destination)
            return False, "Скачанный файл не является установщиком (.exe)."
        size = destination.stat().st_size
        if size < APP_UPDATE_MIN_SIZE:
            _remove_file(destination)
            log.warning("Скачанный установщик слишком мал: %d байт", size)
            return False, (
                "Скачанный файл повреждён или это не установщик "
                f"({size} байт). Повторите попытку позже."
            )

        log.info("Установщик скачан: %s (%.1f МБ)", destination, size / 1024 / 1024)
        return True, destination

    # ------------------------------------------------------------------
    #  Запуск установщика
    # ------------------------------------------------------------------
    def run_installer(self, installer_path: Path) -> tuple[bool, str]:
        """Запускает установщик отдельным процессом.

        Процесс создаётся «отвязанным» от приложения: у него своя группа
        процессов и нет консольного окна, поэтому он переживёт закрытие GUI.
        После успешного запуска приложение обязано завершиться — иначе
        установщик не сможет заменить ``ZapretGUI.exe``.

        :returns: ``(True, сообщение)`` либо ``(False, текст ошибки)``; при
            ошибке приложение закрывать нельзя.
        """
        if self.portable:
            return False, PORTABLE_TEXT

        path = Path(installer_path)
        try:
            if not path.is_file():
                return False, f"Файл установщика не найден:\n{path}"
        except OSError as exc:
            return False, f"Не удалось проверить установщик:\n{exc}"
        if path.suffix.lower() != ".exe":
            return False, "Установщик должен быть .exe-файлом."

        flags = (
            CREATE_NO_WINDOW
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        try:
            subprocess.Popen(
                [str(path)],
                creationflags=flags,
                close_fds=True,
                cwd=str(path.parent),
            )
        except OSError as exc:
            log.exception("Не удалось запустить установщик")
            return False, f"Не удалось запустить установщик:\n{exc}"

        log.info("Установщик запущен: %s", path)
        return True, "Установщик запущен."


def _remove_file(path: Path) -> None:
    """Удаляет частично скачанный файл (ошибку удаления только логируем)."""
    try:
        if path.is_file():
            path.unlink()
    except OSError as exc:
        log.debug("Не удалось удалить неполный файл %s: %s", path, exc)


__all__ = [
    "AppRelease",
    "AppUpdater",
    "AppUpdaterError",
    "PORTABLE_TEXT",
    "ProgressCallback",
    "RATE_LIMIT_TEXT",
    "is_newer",
    "version_key",
]
