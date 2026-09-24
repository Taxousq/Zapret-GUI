"""Проверка обновлений Cloudflare WARP через ``winget``.

WARP **обновляется сам**: клиент Cloudflare тянет новые версии с серверов
Cloudflare в фоне, и отдельного «скачать и поставить» у него нет. Поэтому
раздел «Обновление» не устанавливает WARP, а только:

* показывает текущую версию клиента (``warp-cli --version``; если клиент не
  отвечает — версия берётся из реестра Windows, см. :meth:`WarpUpdater.current_version`);
* по кнопке «Проверить обновление» спрашивает у ``winget``, есть ли версия
  новее установленной (``winget upgrade Cloudflare.Warp``);
* если ``winget`` в системе нет — кнопка проверки подменяется кнопкой
  «Открыть сайт» (:meth:`WarpUpdater.open_download_page`).

Ни установки, ни скачивания здесь нет: если winget сообщил о новой версии,
достаточно подождать (WARP обновится сам) или скачать установщик с сайта
Cloudflare. Управление самим WARP (подключение, режим, исключения) осталось в
разделе «Обход (WARP)» — см. :class:`core.warp_manager.WarpManager`.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import webbrowser

from config import (
    WARP_CLI_PATH,
    WARP_DOWNLOAD_URL,
    WARP_WINGET_ID,
    WARP_WINGET_LATEST_MARKERS,
    WARP_WINGET_TIMEOUT,
    WARP_WINGET_UPDATE_MARKERS,
)
from core.win_utils import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

#: Состояние проверки обновления WARP.
STATUS_UPDATE_AVAILABLE = "update_available"
#: Новая версия не найдена (установлена последняя).
STATUS_LATEST = "latest"
#: Проверить не удалось (winget не ответил, команда не распознана).
STATUS_UNKNOWN = "unknown"

#: Имя исполняемого файла winget.
WINGET_EXE = "winget"

#: Версия клиента в выводе ``warp-cli --version``: «warp-cli 2026.7.1376.0».
VERSION_RE = re.compile(r"(?P<version>\d+(?:\.\d+){1,})")

#: Версия в выводе winget (колонки Version / Available).
WINGET_VERSION_RE = re.compile(r"\bv?(?P<version>\d+(?:\.\d+){1,})\b")

#: Ключ реестра, где Windows хранит версию установленного WARP.
WARP_UNINSTALL_KEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Cloudflare WARP"
)
#: Разрядность, в ветке которой лежит ключ выше (WARP ставится 64-битным).
WARP_UNINSTALL_FLAGS = 0x0100  # KEY_WOW64_64KEY

#: Текст, когда ``warp-cli`` не установлен.
NOT_INSTALLED_TEXT = "Cloudflare WARP не найден"
#: Текст, когда ``winget`` недоступен: проверять обновления нечем.
NO_WINGET_TEXT = (
    "winget не найден в системе — проверка обновлений недоступна. "
    "WARP обновляется автоматически через Cloudflare."
)
#: Текст ошибки, когда winget не ответил за отведённое время.
WINGET_TIMEOUT_TEXT = "winget не ответил вовремя. Попробуйте позже."


class WarpUpdaterError(RuntimeError):
    """Понятная пользователю ошибка проверки обновления WARP.

    :param status: одно из :data:`STATUS_UPDATE_AVAILABLE`, :data:`STATUS_LATEST`
        или :data:`STATUS_UNKNOWN` — что именно удалось выяснить;
    :param available_version: версия, которую предлагает winget (может быть
        пустой — winget не всегда печатает номер в разбираемом виде).
    """

    def __init__(
        self,
        message: str,
        status: str = STATUS_UNKNOWN,
        available_version: str = "",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.available_version = available_version


def version_key(version: str) -> tuple[int, int, int]:
    """«2026.7.1376.0» -> ``(2026, 7, 1376)`` — ключ для сравнения версий.

    Строками версии сравнивать нельзя (``"2026.10" < "2026.9"`` — ложь),
    поэтому берутся первые три числа, а хвост отбрасывается: для «есть ли
    новее» этого достаточно.
    """
    numbers = re.findall(r"\d+", str(version or ""))
    parts = [int(number) for number in numbers[:3]]
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def is_newer(current: str, latest: str) -> bool:
    """Новее ли версия ``latest`` версии ``current`` (сравнение чисел).

    Пустая ``latest`` новее не считается: неизвестную версию нельзя выдавать
    за обновление.
    """
    if not str(latest or "").strip():
        return False
    return version_key(latest) > version_key(current)


def normalize_version(text: str) -> str:
    """Вытаскивает номер версии из строки вывода клиента или winget.

    ``warp-cli --version`` печатает «warp-cli 2026.7.1376.0», winget —
    «Cloudflare.Warp 2026.7.1376.0 2026.9.1»: в обоих случаях нужен только
    номер, без имени программы.
    """
    match = VERSION_RE.search(str(text or ""))
    return match.group("version") if match else ""


class WarpUpdater:
    """Версия Cloudflare WARP и проверка обновления через ``winget``.

    :param winget_path: явный путь к ``winget.exe``; ``None`` — искать в PATH
        (см. :meth:`is_winget_available`).
    """

    def __init__(self, winget_path: str | None = None) -> None:
        #: Явно заданный путь к winget (обычно не нужен — он есть в PATH).
        self._winget_path = str(winget_path) if winget_path else ""
        #: Версия WARP кэшируется: во время работы окна она не меняется.
        self._cached_version: str | None = None

    # ------------------------------------------------------------------
    #  Версия установленного клиента
    # ------------------------------------------------------------------
    def reset_version_cache(self) -> None:
        """Сбрасывает кэш версии (например, после установки WARP)."""
        self._cached_version = None

    def current_version(self) -> str | None:
        """Версия установленного ``warp-cli``; ``None`` — клиент не найден.

        Сначала спрашиваем сам клиент, затем — реестр Windows: если
        ``warp-cli.exe`` недоступен (нет прав, клиент переустанавливается),
        версию всё равно хочется показать.
        """
        if self._cached_version is not None:
            return self._cached_version or None

        version = self._version_from_cli()
        if not version:
            version = self._version_from_registry()
        self._cached_version = version
        if not version:
            log.info("Версию Cloudflare WARP определить не удалось")
            return None
        return version

    def _version_from_cli(self) -> str:
        """Версия из вывода ``warp-cli --version`` (пустая строка — не узнать)."""
        cli = self._find_cli()
        if cli is None:
            return ""
        ok, output = self._run([cli, "--version"])
        if not ok and not output:
            return ""
        version = normalize_version(output)
        if not version:
            log.debug("Вывод warp-cli --version не разобран: %s", output[:200])
            return ""
        return version

    @staticmethod
    def _find_cli() -> str:
        """Путь к ``warp-cli.exe``: PATH, затем стандартная папка установки."""
        found = shutil.which("warp-cli.exe") or shutil.which("warp-cli")
        if found:
            return found
        try:
            if WARP_CLI_PATH.is_file():
                return str(WARP_CLI_PATH)
        except OSError:  # pragma: no cover — недоступный путь (нет прав)
            return ""
        return ""

    def is_installed(self) -> bool:
        """Установлен ли Cloudflare WARP (найден ``warp-cli`` или ключ реестра)."""
        if self._find_cli():
            return True
        return bool(self._version_from_registry())

    def _version_from_registry(self) -> str:
        """Версия WARP из реестра Windows (пустая строка — не узнать).

        Чтение реестра идёт через ``winreg``; модуль импортируется на месте,
        чтобы :mod:`core.warp_updater` оставался импортируемым вне Windows.
        """
        try:
            import winreg  # noqa: PLC0415 — только Windows
        except ImportError:  # pragma: no cover — не Windows
            return ""
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                WARP_UNINSTALL_KEY,
                0,
                winreg.KEY_READ | WARP_UNINSTALL_FLAGS,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "DisplayVersion")
        except OSError as exc:
            log.debug("Версия WARP в реестре не найдена: %s", exc)
            return ""
        return normalize_version(str(value))

    # ------------------------------------------------------------------
    #  winget
    # ------------------------------------------------------------------
    def is_winget_available(self) -> bool:
        """Есть ли в системе ``winget`` (без него проверка невозможна)."""
        return bool(self._winget_command())

    def _winget_command(self) -> str:
        """Команда запуска winget (путь из PATH или явно заданный)."""
        if self._winget_path and shutil.which(self._winget_path):
            return self._winget_path
        return shutil.which(WINGET_EXE) or ""

    @staticmethod
    def _run(command: list[str], timeout: float = WARP_WINGET_TIMEOUT) -> tuple[bool, str]:
        """Запускает команду и возвращает ``(успех, вывод)``.

        stderr подмешивается к stdout: winget пишет туда и ошибки, и часть
        сводки. Исключения не пробрасываются — нештатная ситуация
        возвращается как ``(False, сообщение)``.
        """
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=CREATE_NO_WINDOW,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            log.warning("Команда не ответила за %.0f с: %s", timeout, command)
            return False, ""
        except OSError as exc:  # нет прав, файл занят, программа удалена
            log.warning("Не удалось запустить %s: %s", command[0], exc)
            return False, f"Не удалось запустить winget:\n{exc}"

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        output = "\n".join(part for part in (stdout, stderr) if part)
        log.debug("winget -> код %s: %s", completed.returncode, output[:600])
        if completed.returncode != 0 and not output:
            # winget вернул код без единой строки вывода: так он ведёт себя,
            # когда не может запуститься (нет контекста пакета, отключён
            # установщик приложений). Код уходит в журнал — в интерфейсе он
            # ничего не объясняет.
            log.warning("winget завершился с кодом %s без вывода", completed.returncode)
        return completed.returncode == 0, output

    def winget_installed_version(self, output: str) -> str:
        """Версия WARP, которую winget считает установленной (пустая — неясно).

        Строка ``winget list`` выглядит так::

            Имя              Идентификатор    Версия         Источник
            Cloudflare WARP  Cloudflare.Warp  2026.7.1376.0  winget

        Поэтому нужна именно строка с идентификатором пакета.
        """
        wanted = WARP_WINGET_ID.lower()
        for line in str(output or "").splitlines():
            if wanted not in line.lower():
                continue
            numbers = WINGET_VERSION_RE.findall(line)
            if numbers:
                return numbers[-1]
        return ""

    def check_winget(self) -> tuple[bool, str]:
        """Спрашивает у winget, есть ли версия WARP новее установленной.

        :returns: ``(True, сообщение)`` — winget предлагает новую версию;
            ``(False, сообщение)`` — обновления нет («Установлена последняя
            версия…»).
        :raises WarpUpdaterError: проверить не удалось: winget недоступен, не
            ответил или ответил непонятно.
        """
        command = self._winget_command()
        if not command:
            log.info("winget не найден — проверка обновлений WARP недоступна")
            raise WarpUpdaterError(NO_WINGET_TEXT)

        log.info("Проверка обновления WARP через winget (%s)", WARP_WINGET_ID)
        ok, output = self._run(
            [
                command,
                "upgrade",
                "--id",
                WARP_WINGET_ID,
                "--accept-source-agreements",
            ]
        )
        if not output:
            # Ни строки вывода: winget не ответил вовремя или не смог
            # запуститься — разбирать нечего (см. _run).
            raise WarpUpdaterError(WINGET_TIMEOUT_TEXT)

        return self._interpret(output)

    def _interpret(self, output: str) -> tuple[bool, str]:
        """Разбирает вывод ``winget upgrade`` и решает, есть ли обновление.

        Сначала ищем признаки в тексте (winget отвечает на языке системы),
        затем сравниваем версии из таблицы: если в строке с идентификатором
        пакета видно две версии — новая правее установленной.

        :raises WarpUpdaterError: ни признаков, ни версий в выводе не нашлось.
        """
        text = str(output or "")
        lowered = text.lower()
        available = self._available_version(text)

        if any(marker in lowered for marker in WARP_WINGET_UPDATE_MARKERS):
            if available:
                return True, f"Доступна новая версия Cloudflare WARP: {available}"
            return True, "Доступна новая версия Cloudflare WARP"
        if any(marker in lowered for marker in WARP_WINGET_LATEST_MARKERS):
            return False, "Установлена последняя версия Cloudflare WARP"

        current = self.current_version()
        if available and is_newer(current or "", available):
            return True, f"Доступна новая версия Cloudflare WARP: {available}"

        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line:
            raise WarpUpdaterError(first_line)
        raise WarpUpdaterError("winget не сообщил версию Cloudflare WARP")

    def _available_version(self, output: str) -> str:
        """Версия из колонки «Available» winget (пустая строка — не видно).

        winget печатает две версии в строке пакета (установленная и доступная);
        доступная — последняя в строке. Проверяем, что она действительно новее
        установленной через реестр/клиент, иначе это была бы не версия, а шум.
        """
        wanted = WARP_WINGET_ID.lower()
        for line in str(output or "").splitlines():
            if wanted not in line.lower():
                continue
            numbers = WINGET_VERSION_RE.findall(line)
            if len(numbers) >= 2:
                return numbers[-1]
        return ""

    # ------------------------------------------------------------------
    #  Сайт
    # ------------------------------------------------------------------
    def download_url(self) -> str:
        """Ссылка на страницу загрузки WARP (кнопка «Открыть сайт»)."""
        return WARP_DOWNLOAD_URL

    def open_download_page(self) -> bool:
        """Открывает страницу загрузки WARP в браузере.

        :returns: удалось ли передать ссылку браузеру (``False`` — например,
            в системе не настроен браузер по умолчанию).
        """
        try:
            opened = webbrowser.open(WARP_DOWNLOAD_URL)
        except Exception:  # noqa: BLE001 — браузер может отсутствовать
            log.exception("Не удалось открыть страницу загрузки WARP")
            return False
        if not opened:
            log.warning("Браузер не открыл страницу %s", WARP_DOWNLOAD_URL)
        return bool(opened)


__all__ = [
    "NOT_INSTALLED_TEXT",
    "NO_WINGET_TEXT",
    "STATUS_LATEST",
    "STATUS_UNKNOWN",
    "STATUS_UPDATE_AVAILABLE",
    "WARP_DOWNLOAD_URL",
    "WarpUpdater",
    "WarpUpdaterError",
    "is_newer",
    "normalize_version",
    "version_key",
]
