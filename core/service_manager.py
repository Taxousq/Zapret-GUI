"""Управление службой Windows ``zapret`` через sc/net/reg, плюс автозапуск.

Запрет ставится как обычная служба Windows (service.bat -> sc create), поэтому
GUI работает не с процессом, а со службой: ``sc query/start/stop/delete``.

Ключевой момент — установка службы с выбранной стратегией. Файлы стратегий
(``general (ALT11).bat``) сами службу не создают: они запускают ``winws.exe``
как отдельный процесс. Службу создаёт ``service.bat``, разбирая .bat-стратегию
и передавая её аргументы в ``sc create``. Здесь тот же разбор повторён на
Python, потому что интерактивное меню service.bat (set /p + pause) для GUI
непригодно. Разбор проверен сравнением с реальным ImagePath установленной
службы — результат совпадает.

Кодировка вывода Windows-команд — cp866 (русская Windows), см. decode_output.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from config import (
    AUTOSTART_CMD_NAME,
    AUTOSTART_LNK_NAME,
    COMMAND_TIMEOUT,
    SERVICE_DELETE_DELAY_SEC,
    SERVICE_DESCRIPTION,
    SERVICE_DISPLAY_NAME,
    SERVICE_NAME,
    SERVICE_REGISTRY_KEY,
    STARTUP_DIR,
    STATE_WAIT_TIMEOUT,
    STATE_COLORS,
    STRATEGY_REGISTRY_VALUE,
    ZAPRET_PATH,
)
from core.win_utils import CREATE_NO_WINDOW
from core.zapret_locator import is_zapret_dir

log = logging.getLogger(__name__)

#: Текст, по которому распознаётся «нет прав администратора».
_ACCESS_DENIED_MARKERS = (
    "отказано в доступе",
    "access is denied",
    "failed 5:",
    "error 5",
    "access denied",
)

#: Текст, по которому распознаётся «служба не установлена» (код 1060).
_NOT_INSTALLED_MARKERS = (
    "1060",
    "не установлена",
    "does not exist as an installed service",
    "указанная служба не установлена",
)

#: Состояния службы в выводе sc.exe всегда печатаются по-английски,
#: даже на русской Windows (локализуется только подпись «СОСТОЯНИЕ»).
_STATE_NAMES = (
    "RUNNING",
    "STOPPED",
    "START_PENDING",
    "STOP_PENDING",
    "CONTINUE_PENDING",
    "PAUSE_PENDING",
    "PAUSED",
)
_STATE_RE = re.compile(r"\b(" + "|".join(_STATE_NAMES) + r")\b")

#: Аргументы winws.exe, значение которых в service.bat склеивается через «=».
_ARGS_WITH_VALUE = ("sni", "host", "altorder")

#: Имя главного .bat-файла запрета (упоминается в подсказках пользователю).
SERVICE_BAT_NAME = "service.bat"

#: cmd-шный `for %%i in (...)` делит строку по пробелам, запятым, «;», «=» и табу,
#: но кавычки сохраняют группу целиком. Повторяем это поведение.
_TOKEN_RE = re.compile(r'"[^"]*"|[^ \t,;=]+')

#: Подстановки переменных батника. Ключи в нижнем регистре.
_BATCH_VAR_RE = re.compile(r"%([^%]{1,40})%")


# ---------------------------------------------------------------------------
#  Запуск внешних команд
# ---------------------------------------------------------------------------
def decode_output(data: bytes) -> str:
    """Декодирует вывод Windows-команд.

    Основная кодировка — cp866 (OEM-кодировка русской Windows, как и требует
    ТЗ), но многие .bat-файлы запрета переключают консоль в UTF-8
    (``chcp 65001``). Поэтому сначала проверяем, является ли вывод корректным
    UTF-8 — иначе русский текст превратился бы в кракозябры. Для чистой
    латиницы оба варианта дают одинаковый результат.
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in ("cp866", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("cp866", errors="replace")


def run_command(
    args: Sequence[str],
    cwd: Path | None = None,
    timeout: float = COMMAND_TIMEOUT,
) -> tuple[int, str]:
    """Запускает внешнюю команду и возвращает ``(код возврата, вывод)``.

    ``args`` — обязательно **список** аргументов: ``["sc.exe", "start", "zapret"]``.
    Строка вида ``"sc.exe start zapret"`` недопустима: ``subprocess`` на Windows
    принимает её как одну команду, а построчный разбор превратил бы её в
    последовательность символов и попытался запустить программу «s». Поэтому
    строка отвергается явной ошибкой, а `shell=True` не используется нигде —
    иначе пробелы в аргументах снова стали бы разделителями.

    stderr подмешивается к stdout: sc/net пишут ошибки именно туда, а нам
    нужен их текст для понятного сообщения пользователю.

    ``creationflags=CREATE_NO_WINDOW`` — чтобы Windows не создавала дочернему
    процессу отдельное консольное окно (иначе оно мигает на экране при каждом
    опросе статуса службы). На других платформах флаг равен 0.
    """
    if isinstance(args, (str, bytes)):
        raise TypeError(
            "run_command() принимает список аргументов, а не строку: "
            f"{args!r}. Пример: ['sc.exe', 'query', SERVICE_NAME]"
        )
    command = [str(a) for a in args]
    log.debug("Запуск команды: %s (cwd=%s)", command, cwd)
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            timeout=timeout,
            shell=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        message = f"Команда не ответила за {timeout:g} с: {' '.join(command)}"
        log.error(message)
        return 1, message
    except FileNotFoundError:
        message = f"Не найдена команда: {command[0]}"
        log.error(message)
        return 1, message
    except OSError as exc:
        message = f"Не удалось выполнить {' '.join(command)}: {exc}"
        log.error(message)
        return 1, message

    output = decode_output(completed.stdout) + decode_output(completed.stderr)
    output = output.strip()
    log.debug("Код %s, вывод: %s", completed.returncode, output[:2000])
    return completed.returncode, output


def is_access_denied(output: str) -> bool:
    lowered = (output or "").lower()
    return any(marker in lowered for marker in _ACCESS_DENIED_MARKERS)


def is_admin() -> bool:
    """Запущено ли приложение с правами администратора."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 — проверка не должна ронять приложение
        log.debug("Не удалось определить права администратора", exc_info=True)
        return False


ADMIN_HINT = (
    "Операция требует прав администратора.\n\n"
    "Закройте приложение и запустите его заново от имени администратора "
    "(правый клик по ярлыку → «Запуск от имени администратора»)."
)


# ---------------------------------------------------------------------------
#  Состояние службы
# ---------------------------------------------------------------------------
class ServiceState(Enum):
    """Состояние службы zapret."""

    RUNNING = "Работает"
    STOPPED = "Остановлена"
    NOT_INSTALLED = "Не установлена"
    UNKNOWN = "Неизвестно"

    @property
    def label(self) -> str:
        """Текст для интерфейса."""
        return self.value

    @property
    def color(self) -> str:
        return STATE_COLORS.get(self.name, STATE_COLORS["UNKNOWN"])

    @property
    def is_installed(self) -> bool:
        return self in (ServiceState.RUNNING, ServiceState.STOPPED)

    @property
    def is_running(self) -> bool:
        return self is ServiceState.RUNNING


@dataclass
class ServiceStatus:
    """Подробный статус службы."""

    state: ServiceState
    raw: str = ""
    message: str = ""

    @property
    def label(self) -> str:
        return self.state.label

    @property
    def color(self) -> str:
        return self.state.color


# ---------------------------------------------------------------------------
#  Разбор стратегии -> аргументы winws.exe
# ---------------------------------------------------------------------------
def game_filter_variables(zapret_path: Path) -> dict[str, str]:
    """Повторяет :game_switch_status из service.bat.

    Если файла ``utils\\game_filter.enabled`` нет, игровой фильтр выключен и
    подставляется «12» (так же делает service.bat).
    """
    disabled = {"GameFilter": "12", "GameFilterTCP": "12", "GameFilterUDP": "12"}
    flag = zapret_path / "utils" / "game_filter.enabled"
    try:
        if not flag.is_file():
            return dict(disabled)
        mode = ""
        for line in flag.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if stripped:
                mode = stripped
                break
    except OSError as exc:
        log.warning("Не удалось прочитать %s: %s", flag, exc)
        return dict(disabled)

    full = "1024-65535"
    if mode.lower() == "all":
        return {"GameFilter": full, "GameFilterTCP": full, "GameFilterUDP": full}
    if mode.lower() == "tcp":
        return {"GameFilter": full, "GameFilterTCP": full, "GameFilterUDP": "12"}
    # В service.bat ветка else — это UDP (и любое неизвестное значение).
    return {"GameFilter": full, "GameFilterTCP": "12", "GameFilterUDP": full}


def expand_batch_variables(text: str, variables: dict[str, str]) -> str:
    """Подставляет значения переменных батника (аналог ``call set "line=!line!"``).

    Неизвестные ``%VAR%`` превращаются в пустую строку — ровно так ведёт себя cmd.
    """

    def replace(match: re.Match[str]) -> str:
        return variables.get(match.group(1).strip().lower(), "")

    return _BATCH_VAR_RE.sub(replace, text)


def _quote_argument(arg: str, root: str) -> str:
    """Повторяет обработку кавычек из service.bat (строки 304-314)."""
    if not arg.startswith('"'):
        return arg
    inner = arg[1:-1] if len(arg) >= 2 else ""
    if ":" in inner:
        return f'"{inner}"'
    if inner.startswith("@"):
        return f'"@{root}{inner[1:]}"'
    return f'"{root}{inner}"'


def reassemble_args(tokens: Sequence[str], zapret_path: Path) -> str:
    """Склеивает токены обратно в строку аргументов (порт service.bat 296-343)."""
    root = str(zapret_path)
    if not root.endswith("\\"):
        root += "\\"

    args = ""
    merge_args = 0  # 0 — обычный токен, 1 — продолжение через «,», 2 — после «--x», 3 — через «=»
    for token in tokens:
        # Каретки-переносы строк в батнике — просто выбрасываются.
        if token in ("^", "^^"):
            continue
        arg = _quote_argument(token, root)

        if arg.startswith("--") and merge_args != 0:
            merge_args = 0

        if merge_args == 1:
            args += "," + arg
        elif merge_args == 3:
            args += "=" + arg
            merge_args = 1
        else:
            args += " " + arg

        if arg.startswith("--"):
            merge_args = 2
        elif merge_args >= 1:
            if merge_args == 2:
                merge_args = 1
            if arg.lower() in _ARGS_WITH_VALUE:
                merge_args = 3
    return args.strip()


def build_winws_args(strategy_path: Path, zapret_path: Path = ZAPRET_PATH) -> str:
    """Извлекает строку аргументов winws.exe из .bat-файла стратегии.

    :raises OSError: если файл стратегии недоступен.
    :raises ValueError: если в файле нет вызова winws.exe.
    """
    text = strategy_path.read_text(encoding="utf-8", errors="replace")

    variables = {
        "~dp0": str(zapret_path).rstrip("\\") + "\\",
        "~n0": strategy_path.stem,
        "~f0": str(strategy_path),
        "bin": str(zapret_path).rstrip("\\") + "\\bin\\",
        "lists": str(zapret_path).rstrip("\\") + "\\lists\\",
    }
    for key, value in game_filter_variables(zapret_path).items():
        variables[key.lower()] = value

    tokens: list[str] = []
    captured = False
    stripped = False
    for raw_line in text.splitlines():
        line = expand_batch_variables(raw_line, variables)
        if "winws.exe" in line.lower():
            captured = True
        if not captured:
            continue
        if not stripped:
            # Отрезаем всё до winws.exe вместе с закрывающей кавычкой:
            # `start "..." /min "%BIN%winws.exe" ^` -> ` ^`
            line = re.sub(r"^.*?winws\.exe\"?", "", line, count=1, flags=re.IGNORECASE | re.DOTALL)
            stripped = True
        tokens.extend(_TOKEN_RE.findall(line))

    args = reassemble_args(tokens, zapret_path)
    if not args:
        raise ValueError(
            f"В файле стратегии не найдены аргументы winws.exe: {strategy_path.name}"
        )
    log.debug("Аргументы winws.exe из %s: %s", strategy_path.name, args[:400])
    return args


# ---------------------------------------------------------------------------
#  Менеджер службы
# ---------------------------------------------------------------------------
class ServiceManager:
    """Управляет службой Windows ``zapret``."""

    def __init__(
        self,
        service_name: str = SERVICE_NAME,
        zapret_path: Path | None = None,
    ) -> None:
        self.service_name = service_name
        self.zapret_path = Path(zapret_path) if zapret_path else Path(ZAPRET_PATH)

    # -- вспомогательное ---------------------------------------------------
    def _run(
        self, args: Sequence[str], cwd: Path | None = None, timeout: float = COMMAND_TIMEOUT
    ) -> tuple[int, str]:
        return run_command(args, cwd=cwd, timeout=timeout)

    def _sc(self, *args: str) -> tuple[int, str]:
        return self._run(["sc.exe", *args])

    # -- статус ------------------------------------------------------------
    def get_status(self) -> ServiceStatus:
        """Текущее состояние службы (никогда не выбрасывает исключений)."""
        # Без папки запрета опрашивать нечего: служба могла остаться от прежней
        # установки, но управлять ею нельзя — пользователю нужна подсказка.
        if not is_zapret_dir(self.zapret_path):
            return ServiceStatus(
                ServiceState.NOT_INSTALLED,
                message=(
                    "Запрет не найден: "
                    f"{self.zapret_path}\n"
                    "Укажите папку в разделе «Настройки»."
                ),
            )

        code, output = self._sc("query", self.service_name)
        lowered = output.lower()

        if code != 0:
            if any(marker in lowered for marker in _NOT_INSTALLED_MARKERS):
                return ServiceStatus(
                    ServiceState.NOT_INSTALLED,
                    raw=output,
                    message="Служба zapret не установлена.",
                )
            if is_access_denied(output):
                return ServiceStatus(
                    ServiceState.UNKNOWN,
                    raw=output,
                    message="Нет прав для опроса службы. " + ADMIN_HINT,
                )
            return ServiceStatus(
                ServiceState.UNKNOWN,
                raw=output,
                message=output or f"sc query вернул код {code}.",
            )

        # Имя состояния sc.exe печатает по-английски даже на русской Windows.
        match = _STATE_RE.search(output)
        state_name = match.group(1) if match else ""
        if state_name == "RUNNING":
            return ServiceStatus(ServiceState.RUNNING, raw=output)
        if state_name == "STOPPED":
            return ServiceStatus(ServiceState.STOPPED, raw=output)
        if state_name in ("START_PENDING", "CONTINUE_PENDING"):
            return ServiceStatus(
                ServiceState.RUNNING, raw=output, message=f"Служба запускается ({state_name})."
            )
        if state_name in ("STOP_PENDING", "PAUSE_PENDING", "PAUSED"):
            return ServiceStatus(
                ServiceState.STOPPED, raw=output, message=f"Служба останавливается ({state_name})."
            )
        return ServiceStatus(
            ServiceState.UNKNOWN, raw=output, message="Не удалось разобрать состояние службы."
        )

    def _wait_for_state(self, target: ServiceState, timeout: float = STATE_WAIT_TIMEOUT) -> bool:
        """Ждёт перехода службы в нужное состояние."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.get_status().state is target:
                return True
            time.sleep(0.4)
        return self.get_status().state is target

    def _wait_until_uninstalled(self, timeout: float = STATE_WAIT_TIMEOUT) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.get_status().state is ServiceState.NOT_INSTALLED:
                return True
            time.sleep(0.4)
        return self.get_status().state is ServiceState.NOT_INSTALLED

    # -- старт / стоп ------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        """Запускает службу."""
        status = self.get_status()
        if status.state is ServiceState.NOT_INSTALLED:
            return False, "Служба zapret не установлена. Сначала примените стратегию."
        if status.state is ServiceState.RUNNING and not status.message:
            return True, "Служба уже запущена."

        code, output = self._sc("start", self.service_name)
        if code != 0 and "1056" not in output:  # 1056 — уже запущена
            if is_access_denied(output):
                return False, ADMIN_HINT
            return False, f"Не удалось запустить службу:\n{output}"

        if self._wait_for_state(ServiceState.RUNNING):
            return True, "Служба запущена."
        return False, f"Служба не перешла в состояние «Работает».\n{output}"

    def stop(self) -> tuple[bool, str]:
        """Останавливает службу."""
        status = self.get_status()
        if status.state is ServiceState.NOT_INSTALLED:
            return False, "Служба zapret не установлена."
        if status.state is ServiceState.STOPPED and not status.message:
            return True, "Служба уже остановлена."

        code, output = self._sc("stop", self.service_name)
        if code != 0 and "1062" not in output:  # 1062 — ещё не запущена
            if is_access_denied(output):
                return False, ADMIN_HINT
            return False, f"Не удалось остановить службу:\n{output}"

        if self._wait_for_state(ServiceState.STOPPED):
            return True, "Служба остановлена."
        return False, f"Служба не перешла в состояние «Остановлена».\n{output}"

    def restart(self) -> tuple[bool, str]:
        """Перезапускает службу."""
        status = self.get_status()
        if status.state is ServiceState.NOT_INSTALLED:
            return False, "Служба zapret не установлена. Сначала примените стратегию."

        if status.state is ServiceState.RUNNING:
            ok, message = self.stop()
            if not ok:
                return False, f"Не удалось остановить службу перед перезапуском.\n{message}"
        ok, message = self.start()
        if not ok:
            return False, f"Не удалось запустить службу после остановки.\n{message}"
        return True, "Служба перезапущена."

    # -- текущая стратегия -------------------------------------------------
    def get_current_strategy(self) -> str | None:
        """Имя стратегии, с которой установлена служба (из реестра).

        service.bat пишет в параметр ``zapret-discord-youtube`` имя файла
        стратегии без расширения, например ``general (ALT11)``.

        Именно этот метод читает активную стратегию тестер
        (``core.strategy_tester``): в режиме «Проверить текущую» он проверяет
        её, не переключая службу, а в режиме «Тестировать все» — запоминает,
        чтобы вернуть после теста.
        """
        code, output = self._run(
            [
                "reg.exe",
                "query",
                SERVICE_REGISTRY_KEY,
                "/v",
                STRATEGY_REGISTRY_VALUE,
            ]
        )
        if code != 0:
            log.debug("Не удалось прочитать стратегию из реестра: %s", output)
            return None
        # Вывод: "    zapret-discord-youtube    REG_SZ    general (ALT11)"
        match = re.search(
            rf"{re.escape(STRATEGY_REGISTRY_VALUE)}\s+REG_SZ\s+(.+)", output, re.IGNORECASE
        )
        if not match:
            return None
        return match.group(1).strip() or None

    def get_installed_strategy(self) -> str | None:
        """Прежнее имя :meth:`get_current_strategy` — оставлено для совместимости."""
        return self.get_current_strategy()

    # -- установка / удаление ---------------------------------------------
    def _stop_if_running(self) -> None:
        if self.get_status().state is ServiceState.RUNNING:
            self.stop()

    def _kill_winws(self) -> None:
        """Гасит оставшийся процесс winws.exe (как это делает service.bat)."""
        code, output = self._run(["taskkill.exe", "/IM", "winws.exe", "/F"])
        if code == 0:
            log.info("Процесс winws.exe остановлен принудительно.")

    def remove(self) -> tuple[bool, str]:
        """Останавливает и удаляет службу zapret."""
        if not is_admin():
            return False, ADMIN_HINT

        status = self.get_status()
        if status.state is ServiceState.NOT_INSTALLED:
            return True, "Служба zapret и так не установлена."

        self._stop_if_running()

        code, output = self._sc("delete", self.service_name)
        if code != 0 and not any(m in output.lower() for m in _NOT_INSTALLED_MARKERS):
            if is_access_denied(output):
                return False, ADMIN_HINT
            return False, f"Не удалось удалить службу:\n{output}"

        # Windows помечает службу удалённой не мгновенно.
        time.sleep(SERVICE_DELETE_DELAY_SEC)
        self._wait_until_uninstalled()
        self._kill_winws()

        if self.get_status().state is ServiceState.NOT_INSTALLED:
            return True, "Служба zapret удалена."
        return False, (
            "Служба помечена на удаление, но Windows ещё не завершила удаление.\n"
            "Подождите несколько секунд и обновите статус."
        )

    def install(self, strategy_path: Path | str, display_name: str | None = None) -> tuple[bool, str]:
        """Устанавливает (или переустанавливает) службу с указанной стратегией.

        Порядок важен: сначала удаляется старая служба, затем пауза 2 секунды
        (иначе Windows не даёт создать службу с тем же именем), и только потом
        ``sc create`` с аргументами выбранной стратегии.
        """
        strategy_path = Path(strategy_path)
        if not is_admin():
            return False, ADMIN_HINT
        if not strategy_path.is_file():
            return False, f"Файл стратегии не найден:\n{strategy_path}"

        if not is_zapret_dir(self.zapret_path):
            return False, (
                f"Папка запрета не найдена или не похожа на запрет:\n{self.zapret_path}\n\n"
                "Укажите папку в разделе «Настройки» "
                f"(нужен файл {SERVICE_BAT_NAME} или bin\\winws.exe)."
            )

        winws = self.zapret_path / "bin" / "winws.exe"
        if not winws.is_file():
            return False, (
                f"Не найден winws.exe:\n{winws}\n\n"
                "Проверьте путь к папке запрета в разделе «Настройки»."
            )

        try:
            args = build_winws_args(strategy_path, self.zapret_path)
        except (OSError, ValueError) as exc:
            log.exception("Не удалось разобрать стратегию %s", strategy_path)
            return False, f"Не удалось разобрать стратегию:\n{exc}"

        # 1. Удаляем старую службу.
        self._stop_if_running()
        code, output = self._sc("delete", self.service_name)
        if code != 0 and not any(m in output.lower() for m in _NOT_INSTALLED_MARKERS):
            log.warning("sc delete вернул ошибку: %s", output)
        time.sleep(SERVICE_DELETE_DELAY_SEC)
        self._wait_until_uninstalled()
        self._kill_winws()

        # 2. Создаём службу с аргументами стратегии.
        bin_path = f'"{winws}" {args}'
        code, output = self._sc(
            "create",
            self.service_name,
            "binPath=",
            bin_path,
            "DisplayName=",
            display_name or SERVICE_DISPLAY_NAME,
            "start=",
            "auto",
        )
        if code != 0:
            if is_access_denied(output):
                return False, ADMIN_HINT
            return False, f"Не удалось создать службу:\n{output}"

        self._sc("description", self.service_name, SERVICE_DESCRIPTION)

        # 3. Запоминаем выбранную стратегию — так же, как service.bat.
        code, output = self._run(
            [
                "reg.exe",
                "add",
                SERVICE_REGISTRY_KEY,
                "/v",
                STRATEGY_REGISTRY_VALUE,
                "/t",
                "REG_SZ",
                "/d",
                strategy_path.stem,
                "/f",
            ]
        )
        if code != 0:
            log.warning("Не удалось записать стратегию в реестр: %s", output)

        # 4. Запускаем.
        started, start_message = self.start()
        if not started:
            return False, (
                "Служба создана, но запустить её не удалось.\n"
                f"{start_message}\n\nСтратегия: {strategy_path.stem}"
            )

        return True, (
            f"Служба zapret установлена и запущена.\nСтратегия: {strategy_path.stem}"
        )


# ---------------------------------------------------------------------------
#  Автозапуск
# ---------------------------------------------------------------------------
class AutostartManager:
    """Автозапуск GUI при входе в систему.

    Живёт в core/service_manager.py, потому что это системная интеграция
    (реестр/Startup), а не логика интерфейса, а структура проекта задана
    фиксированным списком файлов.

    Ярлык (.lnk) создаётся через COM-объект WScript.Shell средствами
    PowerShell — это не требует pywin32 и не показывает окно консоли: при
    запуске из исходников цель ярлыка — ``pythonw.exe``, а в собранном
    приложении — сам ``ZapretGUI.exe`` (см. :meth:`_pythonw`). Если PowerShell
    недоступен, создаётся резервный .cmd-файл.
    """

    def __init__(self, app_dir: Path | None = None, startup_dir: Path | None = None) -> None:
        self.app_dir = Path(app_dir) if app_dir else Path(__file__).resolve().parent.parent
        self.startup_dir = Path(startup_dir) if startup_dir else Path(STARTUP_DIR)

    @property
    def lnk_path(self) -> Path:
        return self.startup_dir / AUTOSTART_LNK_NAME

    @property
    def cmd_path(self) -> Path:
        return self.startup_dir / AUTOSTART_CMD_NAME

    def _pythonw(self) -> Path:
        """Что запускать: exe приложения (в сборке) или pythonw.exe (из исходников).

        В собранном PyInstaller-приложении ``sys.executable`` — это сам
        ``ZapretGUI.exe``: запускать его как скрипт (с аргументом ``main.py``)
        нельзя, поэтому в сборке используется он сам и без аргументов.
        """
        if getattr(sys, "frozen", False):
            return Path(sys.executable)
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.is_file():
            return candidate
        return Path(sys.executable)

    @property
    def main_script(self) -> Path | None:
        """Скрипт запуска. ``None`` — сборка exe (запускается сам exe)."""
        if getattr(sys, "frozen", False):
            return None
        return self.app_dir / "main.py"

    def _launch_arguments(self) -> str:
        """Аргументы командной строки для ярлыка/автозапуска."""
        script = self.main_script
        if script is None:
            return ""
        return chr(34) + str(script) + chr(34)

    def is_enabled(self) -> bool:
        try:
            return self.lnk_path.is_file() or self.cmd_path.is_file()
        except OSError:
            return False

    def _create_shortcut(self) -> tuple[bool, str]:
        def ps_quote(value: str) -> str:
            return "'" + str(value).replace("'", "''") + "'"

        script = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$sc = $ws.CreateShortcut({ps_quote(self.lnk_path)}); "
            f"$sc.TargetPath = {ps_quote(self._pythonw())}; "
            f"$sc.Arguments = {ps_quote(self._launch_arguments())}; "
            f"$sc.WorkingDirectory = {ps_quote(self.app_dir)}; "
            f"$sc.Description = {ps_quote('Zapret GUI')}; "
            f"$sc.IconLocation = {ps_quote(str(self._pythonw()) + ',0')}; "
            "$sc.Save()"
        )
        code, output = run_command(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            timeout=30,
        )
        if code != 0 or not self.lnk_path.is_file():
            return False, output or f"PowerShell вернул код {code}."
        return True, ""

    def _create_cmd(self) -> tuple[bool, str]:
        arguments = self._launch_arguments()
        command = f'"{self._pythonw()}"' + (f" {arguments}" if arguments else "")
        content = (
            "@echo off\r\n"
            "rem Автозапуск Zapret GUI (создано приложением Zapret GUI)\r\n"
            f'start "" {command}\r\n'
        )
        try:
            self.cmd_path.write_text(content, encoding="cp866", errors="replace")
        except OSError as exc:
            return False, f"Не удалось создать {self.cmd_path}: {exc}"
        return True, ""

    def enable(self) -> tuple[bool, str]:
        """Включает автозапуск. Возвращает (успех, сообщение)."""
        script = self.main_script
        if script is not None and not script.is_file():
            return False, f"Не найден файл запуска: {script}"
        try:
            self.startup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"Не удалось открыть папку автозапуска:\n{self.startup_dir}\n{exc}"

        self.disable()

        ok, error = self._create_shortcut()
        if ok:
            log.info("Автозапуск включён: %s", self.lnk_path)
            return True, f"Автозапуск включён.\nЯрлык: {self.lnk_path}"

        log.warning("Не удалось создать ярлык (%s), пробую .cmd", error)
        ok, cmd_error = self._create_cmd()
        if ok:
            return True, f"Автозапуск включён (резервный способ).\nФайл: {self.cmd_path}"
        return False, (
            f"Не удалось включить автозапуск.\nЯрлык: {error}\nФайл: {cmd_error}"
        )

    def disable(self) -> tuple[bool, str]:
        """Выключает автозапуск, удаляя созданные файлы."""
        removed: list[str] = []
        errors: list[str] = []
        for path in (self.lnk_path, self.cmd_path):
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(path.name)
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")

        if errors:
            return False, "Не удалось удалить файлы автозапуска:\n" + "\n".join(errors)
        if removed:
            log.info("Автозапуск выключен (%s)", ", ".join(removed))
            return True, "Автозапуск выключен."
        return True, "Автозапуск и так выключен."
