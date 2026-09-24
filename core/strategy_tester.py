"""Запуск встроенного тестера стратегий (``utils\\test zapret.ps1``).

Свой тестер не пишется: GUI запускает скрипт запрета и разбирает его
консольный вывод. Скрипт интерактивный (меню ``Read-Host``), поэтому GUI
сам отвечает за пользователя на его вопросы, отправляя номера пунктов в
stdin. Последовательность ответов такая::

    1                       — тип теста (Standard tests, HTTP/ping)
    1                       — режим: все конфиги
    2 + <номер конфига>     — режим: выбранные конфиги (одна стратегия)

Вывод читается построчно в отдельном потоке (:class:`threading.Thread`),
чтобы его можно было показывать в реальном времени и останавливать тест.
Разбор строк даёт таблицу результатов и прогресс; строка ``Best config:``
задаёт лучшую стратегию.

Особенности, проверенные по коду скрипта и на живом запуске:

* тестер требует прав администратора и отсутствия службы ``zapret`` —
  ошибки распознаются и превращаются в понятные сообщения
  (:func:`describe_problems`);
* ``[System.Console]::ReadKey`` в конце скрипта не работает при
  перенаправленном stdin: PowerShell пишет исключение в stderr, но скрипт
  доходит до конца. Поэтому stderr подмешивается к stdout, а строка с
  исключением отбрасывается (:func:`is_noise`);
* цвета консоли в PowerShell 5.1 через ``Write-Host`` в поток не попадают,
  но ANSI-последовательности всё равно вырезаются.

Модуль намеренно не зависит от Qt: строки отдаются обычными колбэками,
а в GUI их переносит ``gui.widgets.Worker``.
"""

from __future__ import annotations

import codecs
import logging
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from config import (
    ANSI_RE,
    COMMAND_TIMEOUT,
    RISKY_PATH_CHARS,
    TEST_ADMIN_ERROR_RE,
    TEST_ANALYTICS_HEADER_RE,
    TEST_ANALYTICS_RE,
    TEST_BEST_RE,
    TEST_COMPLETED_RE,
    TEST_CONFIG_HEADER_RE,
    TEST_CONFIG_ITEM_RE,
    TEST_CURL_ERROR_RE,
    TEST_METRIC_RE,
    TEST_MODE_ALL,
    TEST_MODE_SELECTED,
    TEST_NO_CONFIGS_RE,
    TEST_OUTPUT_TIMEOUT,
    TEST_SCRIPT,
    TEST_SELECT_PROMPT_RE,
    TEST_SERVICE_ERROR_RE,
    TEST_STOP_TIMEOUT,
    TEST_TARGETS_FILE,
    TEST_TOTAL_RE,
    TEST_TYPE_STANDARD,
    UTILS_DIR,
    ZAPRET_PATH,
    tester_script,
)
from core.win_utils import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Разбор консольного вывода
# ---------------------------------------------------------------------------
_ANSI_RE = re.compile(ANSI_RE)
_ANALYTICS_RE = re.compile(TEST_ANALYTICS_RE)
_ANALYTICS_HEADER_RE = re.compile(TEST_ANALYTICS_HEADER_RE)
_METRIC_RE = re.compile(TEST_METRIC_RE)
_CONFIG_HEADER_RE = re.compile(TEST_CONFIG_HEADER_RE)
_CONFIG_ITEM_RE = re.compile(TEST_CONFIG_ITEM_RE)
_TOTAL_RE = re.compile(TEST_TOTAL_RE)
_BEST_RE = re.compile(TEST_BEST_RE)
_SELECT_PROMPT_RE = re.compile(TEST_SELECT_PROMPT_RE)
_COMPLETED_RE = re.compile(TEST_COMPLETED_RE)
_ADMIN_ERROR_RE = re.compile(TEST_ADMIN_ERROR_RE)
_SERVICE_ERROR_RE = re.compile(TEST_SERVICE_ERROR_RE)
_CURL_ERROR_RE = re.compile(TEST_CURL_ERROR_RE)
_NO_CONFIGS_RE = re.compile(TEST_NO_CONFIGS_RE)

#: Строки PowerShell-исключения из-за ReadKey при перенаправленном stdin.
#: Они не являются результатом теста и только засоряют журнал и консоль GUI.
_NOISE_MARKERS = (
    "readkey",
    "cannot read keys",
    "console input has been redirected",
    "categoryinfo",
    "fullyqualifiederrorid",
    "fullyqualifiederror",
    "at line:",
    "char:",
    "parentcontainserrorrecordexception",
    "remoteexception",
    "nativ ecommanderror",
    "nativecommanderror",
    "exception calling",
)

#: Метрики аналитики, которые складываются в колонку «FAIL».
_FAIL_KEYS = ("FAIL", "ERR", "ERROR", "PINGFAIL", "BLOCKED", "LIKELY_BLOCKED")

#: Метрики, которые тестер печатает в строке аналитики. Проверка по списку
#: нужна, чтобы не спутать аналитику со строкой результата по цели вида
#: ``  YouTube Web   TLS1.3:OK  | Ping: 24ms`` — в ней тоже есть «метрика: число».
_ANALYTICS_KEYS = frozenset(
    {
        "OK",
        "FAIL",
        "ERR",
        "ERROR",
        "UNSUP",
        "UNSUPPORTED",
        "BLOCKED",
        "LIKELY BLOCKED",
        "HTTP OK",
        "PING OK",
        "PINGFAIL",
        "PING",
    }
)


def decode_output(data: bytes) -> str:
    """Декодирует строку вывода тестера.

    PowerShell пишет в кодировке OEM-консоли (cp866 на русской Windows) либо
    в UTF-8, если скрипт переключил кодовую страницу. Поэтому сначала
    проверяется UTF-8, и только потом cp866 — для чистой латиницы оба
    варианта совпадают.
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


def clean_line(raw: bytes | str) -> str:
    """Убирает ANSI-последовательности, ``\\r`` и хвостовые пробелы."""
    text = raw if isinstance(raw, str) else decode_output(raw)
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "").replace("\x00", "")
    return text.rstrip()


def is_noise(line: str) -> bool:
    """Служебная ли строка (исключение ReadKey и т. п.)."""
    lowered = line.lower()
    return any(marker in lowered for marker in _NOISE_MARKERS)


def strip_bat_suffix(name: str) -> str:
    """``general (ALT11).bat`` -> ``general (ALT11)``."""
    text = name.strip()
    if text.lower().endswith(".bat"):
        text = text[:-4]
    return text.strip()


@dataclass(frozen=True)
class ParsedLine:
    """Разобранная значащая строка вывода тестера."""

    kind: str
    """``config-header`` | ``config-item`` | ``analytics`` | ``total`` |
    ``best`` | ``completed``."""

    config: str = ""
    index: int | None = None
    total: int | None = None
    metrics: dict[str, int] = field(default_factory=dict)


def parse_line(line: str, profile_analytics: bool = False) -> ParsedLine | None:
    """Разбирает одну строку вывода. ``None`` — строка не несёт данных.

    Порядок проверок важен: строка ``  [3] general (ALT11).bat`` похожа на
    строку аналитики, поэтому сначала проверяются самые специфичные шаблоны
    (заголовки, элементы списка), и только потом — аналитика.

    :param profile_analytics: ``True`` внутри блока ``=== ANALYTICS ===``.
        Там имя конфига и метрики разделены двоеточием, а сама аналитика —
        это ``general.bat : OK: 17, FAIL: 9`` (см. :func:`parse_profile_analytics`).
    """
    if not line or not line.strip():
        return None

    completed = _COMPLETED_RE.search(line)
    if completed:
        return ParsedLine(kind="completed")

    best = _BEST_RE.search(line)
    if best:
        return ParsedLine(kind="best", config=best.group("config").strip())

    total = _TOTAL_RE.search(line)
    if total:
        return ParsedLine(kind="total", total=int(total.group("total")))

    header = _CONFIG_HEADER_RE.match(line)
    if header:
        return ParsedLine(
            kind="config-header",
            config=header.group("config").strip(),
            index=int(header.group("index")),
            total=int(header.group("total")),
        )

    item = _CONFIG_ITEM_RE.match(line)
    if item:
        return ParsedLine(
            kind="config-item",
            config=item.group("config").strip(),
            index=int(item.group("index")),
        )

    if profile_analytics:
        # Внутри блока аналитики строки результатов по целям не печатаются,
        # поэтому здесь достаточно формата «имя : метрики».
        bare = parse_profile_analytics(line)
        if bare is not None:
            config, metrics = bare
            return ParsedLine(kind="analytics", config=config, metrics=metrics)
    else:
        analytics = parse_analytics(line)
        if analytics is not None:
            config, metrics = analytics
            return ParsedLine(kind="analytics", config=config, metrics=metrics)

    return None


def parse_profile_analytics(line: str) -> tuple[str, dict[str, int]] | None:
    """Разбирает строку аналитики вида ``general.bat : OK: 17, FAIL: 9``.

    Тестер печатает имя конфига и метрики, а самый длинный конфиг — вообще без
    выравнивания (``PadRight`` ничего не добавляет). Поэтому имя здесь не
    обязательно выглядит как «general (ALT11).bat»: важно лишь, что строка
    состоит из имени, двоеточия и пар «метрика: число».

    Проверяется и то, что после последней метрики ничего нет: строка
    результата по цели (``  YouTube Web  TLS1.3:OK | Ping: 24ms``) так не
    заканчивается, а строка с «Ping: Timeout» не подходит из-за нечислового
    значения.
    """
    matches = list(_METRIC_RE.finditer(line))
    if not matches:
        return None
    if line[matches[-1].end() :].strip(" ,\t"):
        return None

    config = line[: matches[0].start()].rstrip(" :\t").strip()
    if not config:
        return None

    metrics: dict[str, int] = {}
    previous_end = matches[0].end()
    for index, match in enumerate(matches):
        key = match.group("key").strip().upper()
        if key not in _ANALYTICS_KEYS:
            return None
        if index > 0 and line[previous_end : match.start()].strip(" ,\t"):
            return None
        metrics[key] = int(match.group("value"))
        previous_end = match.end()
    return config, metrics


def parse_analytics(line: str) -> tuple[str, dict[str, int]] | None:
    """Разбирает строку аналитики тестера (см. :func:`parse_profile_analytics`).

    Отдельная проверка нужна для строки результата по одной цели из подробного
    отчёта (``  YouTube Web   HTTP:OK  TLS1.2:OK  | Ping: 24ms``): она тоже
    содержит «метрика: число», но это не аналитика по стратегии.

    :return: ``(имя конфига, метрики)`` или ``None``, если строка не подходит.
    """
    matches = list(_METRIC_RE.finditer(line))
    if not matches:
        return None

    config = line[: matches[0].start()].rstrip(" :\t").strip()
    if not config:
        return None

    metrics: dict[str, int] = {}
    previous_end = matches[0].end()
    for index, match in enumerate(matches):
        key = match.group("key").strip().upper()
        # Строка результата по цели («... TLS1.3:OK | Ping: 24ms») выглядит как
        # аналитика, но её метрики не из списка — такие строки отсеиваются.
        if key not in _ANALYTICS_KEYS:
            return None
        if index > 0:
            # Между парами допускаются только запятые и пробелы.
            gap = line[previous_end : match.start()]
            if gap.strip(" ,\t"):
                return None
        metrics[key] = int(match.group("value"))
        previous_end = match.end()

    return config, metrics


def metrics_to_counts(metrics: dict[str, int]) -> tuple[int, int]:
    """Превращает метрики аналитики в ``(OK, FAIL)`` для таблицы."""
    ok = metrics.get("OK", metrics.get("HTTP OK", 0))
    fail = sum(metrics.get(key, 0) for key in _FAIL_KEYS)
    return ok, fail


@dataclass
class StrategyTestResult:
    """Результат тестирования одной стратегии."""

    filename: str
    """Имя .bat-файла (как его печатает тестер), например ``general (ALT11).bat``."""

    metrics: dict[str, int] = field(default_factory=dict)
    raw: str = ""
    """Строка аналитики целиком — показывается во всплывающей подсказке."""

    @property
    def display_name(self) -> str:
        return strip_bat_suffix(self.filename)

    @property
    def ok_count(self) -> int:
        return metrics_to_counts(self.metrics)[0]

    @property
    def fail_count(self) -> int:
        return metrics_to_counts(self.metrics)[1]

    @property
    def is_best(self) -> bool:
        return False  # заполняется через TestReport.best_config

    def tooltip(self) -> str:
        if not self.raw:
            return self.filename
        return f"{self.filename}\n{self.raw}"


@dataclass
class TestProgress:
    """Текущий прогресс тестирования."""

    current: int
    total: int
    config: str = ""

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, int(self.current * 100 / self.total)))


@dataclass
class TestReport:
    """Итог одного запуска тестера."""

    mode: str
    results: list[StrategyTestResult] = field(default_factory=list)
    best_config: str | None = None
    requested_strategy: str | None = None
    exit_code: int | None = None
    stopped: bool = False
    stdout_tail: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.exit_code == 0 and not self.stopped and not self.problems

    @property
    def best_name(self) -> str | None:
        """Имя лучшей стратегии без расширения (``general (ALT11)``)."""
        return strip_bat_suffix(self.best_config) if self.best_config else None

    def best_display(self, name: str | None = None) -> str | None:
        """Имя лучшей стратегии для интерфейса.

        Для ``general.bat`` имя без расширения («general») малопонятно, поэтому
        рядом с ним показывается и имя файла: ``general (general.bat)``.

        :param name: имя стратегии из списка .bat-файлов, если оно найдено.
        """
        if not self.best_config:
            return None
        if name and name != self.best_name:
            return f"{name} ({self.best_config})"
        return self.best_name

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
#  Проблемы окружения
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EnvironmentProblem:
    """Проблема окружения, найденная до запуска тестера.

    ``kind``: ``tester`` — нет файла тестера (запуск невозможен);
    ``winws`` — нет winws.exe в папке запрета (запуск невозможен);
    ``path`` — предупреждение о пробелах/кириллице в пути (запуск возможен).
    """

    kind: str
    message: str

    @property
    def blocking(self) -> bool:
        """Мешает ли проблема запустить тестер."""
        return self.kind in ("tester", "winws")


def check_environment(
    script: Path | None = None, zapret_path: Path | None = None
) -> list[EnvironmentProblem]:
    """Проверяет окружение до запуска и возвращает список проблем.

    Пустой список — можно запускать. Проверки дешёвые и не заменяют проверки
    самого тестера: они лишь дают понятное сообщение раньше него.
    """
    problems: list[EnvironmentProblem] = []
    zapret_path = Path(zapret_path) if zapret_path else Path(ZAPRET_PATH)
    # Путь к тестеру считается от папки запрета, а не от замороженной
    # константы TEST_SCRIPT: в сборке PyInstaller та указывала во временную
    # папку (``%TEMP%\\zapret\\utils\\test zapret.ps1``).
    script = Path(script) if script else tester_script(zapret_path)

    try:
        if not script.is_file():
            problems.append(
                EnvironmentProblem(
                    "tester",
                    f"Тестер не найден:\n{script}\n\n"
                    "Укажите папку запрета в разделе «Настройки» "
                    "— ожидается файл utils\\test zapret.ps1.",
                )
            )
    except OSError as exc:
        problems.append(
            EnvironmentProblem("tester", f"Не удалось проверить файл тестера:\n{exc}")
        )

    try:
        if not (zapret_path / "bin" / "winws.exe").is_file():
            problems.append(
                EnvironmentProblem(
                    "winws",
                    f"Не найден winws.exe в папке запрета:\n"
                    f"{zapret_path / 'bin' / 'winws.exe'}\n\n"
                    "Укажите папку запрета в разделе «Настройки».",
                )
            )
    except OSError as exc:
        problems.append(
            EnvironmentProblem("winws", f"Не удалось проверить папку запрета:\n{exc}")
        )

    risk = risky_path_reason(zapret_path)
    if risk:
        problems.append(EnvironmentProblem("path", risk))
    return problems


def risky_path_reason(zapret_path: Path | None = None) -> str | None:
    """Предупреждение о пробелах и кириллице в пути (тестер их не любит)."""
    path = Path(zapret_path) if zapret_path else Path(ZAPRET_PATH)
    text = str(path)
    issues: list[str] = []
    if any(char in text for char in RISKY_PATH_CHARS):
        issues.append("пробелы")
    if any("\u0400" <= char <= "\u04ff" for char in text):
        issues.append("кириллица")
    if not issues:
        return None
    return (
        "В пути к папке запрета есть " + " и ".join(issues) + f":\n{text}\n\n"
        "Тестер и .bat-стратегии могут не найти файлы. Рекомендуется перенести "
        "запрет в путь без пробелов и кириллицы, например C:\\zapret."
    )


def describe_problems(lines: Sequence[str], exit_code: int | None = None) -> list[str]:
    """Превращает строки вывода тестера в понятные сообщения об ошибках."""
    joined = "\n".join(lines)
    problems: list[str] = []

    if _ADMIN_ERROR_RE.search(joined):
        problems.append(
            "Для тестирования требуются права администратора.\n\n"
            "Перезапустите приложение от имени администратора "
            "(правый клик по ярлыку → «Запуск от имени администратора»)."
        )
    if _SERVICE_ERROR_RE.search(joined):
        problems.append(
            "Служба zapret установлена, а тестеру нужна удалённая служба.\n\n"
            "Удалите службу (кнопка «Удалить службу» или автоматическое "
            "удаление перед тестом) и запустите тест снова."
        )
    if _CURL_ERROR_RE.search(joined):
        problems.append(
            "Тестер не нашёл curl.exe.\n\n"
            "curl входит в состав Windows 10/11 — проверьте, что он доступен "
            "в PATH (команда curl --version в cmd)."
        )
    if _NO_CONFIGS_RE.search(joined):
        problems.append(
            "В папке запрета не найдено ни одной стратегии general*.bat.\n\n"
            "Проверьте, что запрет распакован полностью."
        )
    if not problems and exit_code not in (None, 0):
        tail = joined.strip().splitlines()[-12:]
        problems.append(
            "Тестер завершился с ошибкой (код "
            f"{exit_code}).\n\n" + ("\n".join(tail) or "Вывод пуст.")
        )
    return problems


# ---------------------------------------------------------------------------
#  Запуск тестера
# ---------------------------------------------------------------------------
class TestError(RuntimeError):
    """Тестер не удалось запустить или он неожиданно прервался."""


class StrategyTester:
    """Запускает ``utils\\test zapret.ps1`` и разбирает его вывод.

    Все методы рассчитаны на вызов из одного фонового потока
    (``gui.widgets.Worker``); единственное исключение — :meth:`stop`, который
    можно звать из главного потока: он только снимает процесс и не ждёт.

    :param on_line: вызывается для каждой строки вывода (уже очищенной).
    :param on_progress: вызывается при смене текущего конфига.
    :param on_result: вызывается, когда для стратегии появился результат.
    """

    def __init__(
        self,
        zapret_path: Path | None = None,
        script: Path | None = None,
        on_line: Callable[[str], None] | None = None,
        on_progress: Callable[[TestProgress], None] | None = None,
        on_result: Callable[[StrategyTestResult], None] | None = None,
    ) -> None:
        self.zapret_path = Path(zapret_path) if zapret_path else Path(ZAPRET_PATH)
        # Тестер лежит внутри папки запрета: путь берём от неё, а не из
        # константы TEST_SCRIPT — в сборке PyInstaller она указывала во
        # временную папку (``%TEMP%\\zapret``) и давала ошибку «Тестер не найден».
        self.script = Path(script) if script else tester_script(self.zapret_path)

        self._on_line = on_line
        self._on_progress = on_progress
        self._on_result = on_result

        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stdin_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._finished = threading.Event()

        # Состояние разбора.
        self._results: dict[str, StrategyTestResult] = {}
        self._order: list[str] = []
        self._items: dict[int, str] = {}
        self._current_file = ""
        self._current_index = 0
        self._total = 0
        self._best: str | None = None
        self._completed_marker = False
        self._error_lines: list[str] = []
        self._tail: list[str] = []
        self._requested: str | None = None
        self._inputs: dict[str, str] = {}
        self._sent: set[str] = set()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._selected_sent = False
        self._select_pending = False
        self._select_requested_at: float | None = None
        self._in_analytics = False

    # -- состояние ---------------------------------------------------------
    @property
    def running(self) -> bool:
        """Идёт ли тестирование прямо сейчас."""
        process = self._process
        return process is not None and process.poll() is None

    @property
    def results(self) -> list[StrategyTestResult]:
        """Результаты в порядке появления."""
        return [self._results[filename] for filename in self._order]

    # -- запуск ------------------------------------------------------------
    def run(
        self,
        mode: str = "all",
        strategy_filename: str | None = None,
    ) -> TestReport:
        """Запускает тестер и ждёт его завершения.

        :param mode: ``"all"`` — все стратегии, ``"single"`` — одна.
        :param strategy_filename: имя .bat-файла для режима ``"single"``.
        :raises TestError: если тестер не удалось запустить.
        """
        mode = "single" if mode == "single" else "all"
        if mode == "single" and not strategy_filename:
            raise TestError("Не выбрана стратегия для тестирования.")

        try:
            if not self.script.is_file():
                raise TestError(
                    f"Тестер не найден. Проверьте путь к запрету в config.py:\n{self.script}"
                )
        except OSError as exc:
            raise TestError(f"Не удалось открыть тестер:\n{exc}") from exc

        self._reset(mode, strategy_filename)
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
        ]
        log.info("Запуск тестера стратегий: %s (режим %s)", self.script, mode)

        try:
            process = subprocess.Popen(
                command,
                cwd=str(self.zapret_path),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            log.exception("Не удалось запустить тестер")
            raise TestError(f"Не удалось запустить тестер:\n{exc}") from exc

        self._process = process
        self._reader = threading.Thread(
            target=self._read_output, name="zapret-tester-reader", daemon=True
        )
        self._reader.start()
        self._feed_initial_inputs()

        self._pump()
        return self._finish()

    # -- внутреннее --------------------------------------------------------
    def _reset(self, mode: str, strategy_filename: str | None) -> None:
        self._results = {}
        self._order = []
        self._items = {}
        self._current_file = ""
        self._current_index = 0
        self._total = 0
        self._best = None
        self._completed_marker = False
        self._error_lines = []
        self._tail = []
        self._requested = strategy_filename
        self._selected_sent = False
        self._select_pending = False
        self._select_requested_at = None
        self._in_analytics = False
        self._sent = set()
        # Очередь создаётся до запуска потока чтения: он в неё пишет.
        self._queue = queue.Queue()
        self._stop_requested.clear()
        self._finished.clear()
        self._inputs = {
            "test-type": TEST_TYPE_STANDARD,
            "mode": TEST_MODE_SELECTED if mode == "single" else TEST_MODE_ALL,
            "select": TEST_MODE_ALL,
        }

    def _read_output(self) -> None:
        """Поток чтения stdout: чистит строки и кладёт их в очередь."""
        process = self._process
        if process is None or process.stdout is None:
            self._finished.set()
            return
        try:
            for raw in process.stdout:
                line = clean_line(raw)
                if not line:
                    self._queue.put(line)
                    continue
                if is_noise(line):
                    log.debug("Тестер (служебное): %s", line)
                    continue
                self._queue.put(line)
        except (OSError, ValueError) as exc:  # поток закрылся при остановке
            log.debug("Чтение вывода тестера прервано: %s", exc)
        finally:
            self._finished.set()
            self._queue.put(None)

    def _pump(self) -> None:
        """Читает очередь вывода, отвечает на меню тестера и ждёт завершения.

        Здесь же ловится зависание: если тестер молчит дольше
        ``TEST_OUTPUT_TIMEOUT``, процесс снимается, а не ждёт вечно.
        """
        deadline = time.monotonic() + TEST_OUTPUT_TIMEOUT

        while True:
            if self._stop_requested.is_set():
                break
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                item = ""

            if item is None:  # поток чтения закончил — процесс закрыл stdout
                break
            if item and self._handle_line(item):
                deadline = time.monotonic() + TEST_OUTPUT_TIMEOUT

            if time.monotonic() > deadline:
                self._error_lines.append(
                    f"Тестер не выводил данные более {TEST_OUTPUT_TIMEOUT:g} с — запуск прерван."
                )
                log.error("Тестер завис: нет вывода")
                self._stop_requested.set()
                self._kill_escalating()
                break

            # Тестер попросил выбрать конфиги: в режиме одной стратегии ответ
            # отправляется, как только в выводе появится список конфигов.
            if self._select_pending and not self._selected_sent:
                if self._select_and_send():
                    self._select_pending = False

        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=2.0)
        while True:  # дочитываем хвост очереди, чтобы не потерять результаты
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                break
            self._handle_line(item)

    def _kill_escalating(self) -> None:
        """Гасит процесс тестера: soft → terminate → kill.

        ``terminate()`` на Windows — это ``TerminateProcess`` (жёстко), но
        тестер успевает выполнить ``finally`` при закрытии stdin, поэтому
        сначала пробуем именно его и лишь затем добиваем процесс.
        """
        process = self._process
        if process is None or process.poll() is not None:
            return

        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        except OSError as exc:
            log.debug("Не удалось закрыть stdin тестера: %s", exc)

        try:
            process.terminate()
            process.wait(timeout=3)
            return
        except (OSError, subprocess.TimeoutExpired):
            log.warning("Тестер не завершился после terminate, завершаю принудительно")

        try:
            process.kill()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("Не удалось завершить процесс тестера: %s", exc)

    def _handle_line(self, line: str) -> bool:
        """Обрабатывает строку. ``True`` — строка была значащей."""
        self._tail.append(line)
        if len(self._tail) > 400:
            del self._tail[:100]

        if self._on_line is not None:
            try:
                self._on_line(line)
            except Exception:  # noqa: BLE001 — сбой колбэка не должен ронять тест
                log.exception("Ошибка в обработчике строки тестера")

        if self._error_marker(line):
            return False

        if _ANALYTICS_HEADER_RE.search(line):
            # Дальше идут строки аналитики: их формат отличается от подробного
            # отчёта по целям, и разбирать их нужно иначе.
            self._in_analytics = True
            return True

        self._answer_menu(line)

        parsed = parse_line(line, profile_analytics=self._in_analytics)
        if parsed is None:
            return False

        if parsed.kind == "total" and parsed.total:
            self._total = parsed.total
            self._emit_progress()
        elif parsed.kind == "config-header":
            # Строка вида «[3/17] general (ALT11).bat»: тестер начал эту
            # стратегию. Строку в таблицу добавляем только с результатом,
            # иначе в ней останутся стратегии, которые даже не запустились.
            self._current_file = parsed.config
            self._current_index = parsed.index or 0
            if parsed.total:
                self._total = parsed.total
            self._emit_progress()
        elif parsed.kind == "config-item":
            if parsed.index is not None:
                self._items[parsed.index] = parsed.config
        elif parsed.kind == "analytics":
            if parsed.metrics:
                self._emit_result(parsed.config, parsed.metrics, line)
        elif parsed.kind == "best":
            self._best = parsed.config or None
            log.info("Лучшая стратегия по версии тестера: %s", self._best)
        elif parsed.kind == "completed":
            self._completed_marker = True
            self._emit_progress()
        return True

    def _error_marker(self, line: str) -> bool:
        """Запоминает строки с ошибками тестера (``[ERROR]``/``[WARN]``)."""
        stripped = line.strip()
        if stripped.startswith("[ERROR]") or stripped.startswith("[WARN]"):
            self._error_lines.append(stripped)
            return True
        return False

    def _answer_menu(self, line: str) -> None:
        """Досылает ответы по заголовкам меню, если они попали в вывод.

        Основные ответы отправляются заранее (:meth:`_feed_initial_inputs`),
        потому что при перенаправленном stdin ``Read-Host`` печатает
        приглашение не в stdout, а прямо в консоль. Этот метод — страховка на
        случай, когда заголовки всё же попадают в поток: повторный ответ на
        один и тот же вопрос не отправляется (см. :meth:`_feed`).

        Отдельный случай — список конфигов: номер выбранной стратегии
        известен только после того, как тестер его напечатает, поэтому здесь
        только отмечается, что ответ нужно подготовить.
        """
        stripped = line.strip()
        for name, header in self._menu_headers():
            if stripped.startswith(header):
                if name in ("select-list", "select"):
                    if self._inputs.get("mode") != TEST_MODE_SELECTED:
                        self._feed("select")
                    else:
                        self._select_pending = True
                        self._select_requested_at = time.monotonic()
                else:
                    self._feed(name)
                return

    @staticmethod
    def _menu_headers() -> tuple[tuple[str, str], ...]:
        """Заголовки меню тестера → название ожидаемого ответа."""
        return (
            ("select-list", "Available configs:"),
            ("select", "Enter numbers"),
            ("test-type", "Select test type"),
            ("mode", "Select test run mode"),
        )

    def _feed_initial_inputs(self) -> None:
        """Отправляет ответы, известные до запуска теста.

        Приглашения ``Read-Host`` в stdout не попадают, а тестер ждёт ввод
        только когда до него дойдёт, поэтому ответы можно положить в stdin
        сразу: он прочитает их по очереди (``Read-Host`` вызывается строго
        один раз на каждый вопрос). Порядок ответов и есть порядок вопросов:
        тип теста → режим → (в режиме «все») список конфигов.
        """
        self._feed("test-type")
        self._feed("mode")
        if self._inputs.get("mode") == TEST_MODE_ALL:
            self._feed("select")

    def _feed(self, name: str) -> None:
        """Отправляет ответ на очередной вопрос тестера (один раз на вопрос)."""
        if name in self._sent:
            return
        value = self._inputs.get(name, "")
        if not value:
            return
        self._sent.add(name)
        if name == "select":
            self._selected_sent = True
        self._write_stdin(value)

    def _write_stdin(self, value: str) -> None:
        """Пишет ответ в stdin тестера (поток открыт в бинарном режиме)."""
        process = self._process
        if process is None or process.stdin is None:
            return
        try:
            with self._stdin_lock:
                process.stdin.write(value.encode("ascii", errors="replace") + b"\n")
                process.stdin.flush()
            log.debug("Тестеру отправлено: %r", value)
        except (OSError, ValueError) as exc:
            log.debug("Не удалось отправить ответ тестеру: %s", exc)

    def _select_and_send(self) -> bool:
        """Отправляет номер выбранной стратегии, когда список конфигов напечатан.

        Номер берётся из самого вывода тестера: он нумерует конфиги в своём
        порядке, поэтому вычислять его в Python не нужно и не надёжно.

        Если стратегии в списке нет (файл переименовали во время теста), через
        паузу отправляется «0» — это то же, что «All configs».
        """
        if not self._requested:
            return False
        wanted = self._requested.strip().lower()
        for index, name in sorted(self._items.items()):
            if name.strip().lower() == wanted:
                self._write_stdin(str(index))
                self._selected_sent = True
                log.info("Для теста выбрана стратегия %s (пункт %s)", name, index)
                return True

        # Список печатается построчно: пока не пришли все строки, номер
        # отправлять нельзя — скрипт прочитает его как часть ещё не
        # напечатанного списка. Завершение списка видно по общему числу.
        complete = self._total > 0 and len(self._items) >= self._total
        if complete and self._select_requested_at is not None and (
            time.monotonic() - self._select_requested_at > 30.0
        ):
            log.warning(
                "Стратегия %s не найдена в списке тестера — запускаю все конфиги",
                self._requested,
            )
            self._error_lines.append(
                f"Стратегия «{self._requested}» не найдена в списке тестера — "
                "протестированы все стратегии."
            )
            self._write_stdin(TEST_MODE_ALL)
            self._selected_sent = True
            return True
        return False

    def _emit_progress(self) -> None:
        if self._on_progress is None:
            return
        progress = TestProgress(
            current=self._current_index,
            total=self._total,
            config=self._current_file,
        )
        try:
            self._on_progress(progress)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в обработчике прогресса тестера")

    def _emit_result(
        self, filename: str, metrics: dict[str, int] | None, raw: str = ""
    ) -> None:
        if not filename:
            return
        result = self._results.get(filename)
        if result is None:
            result = StrategyTestResult(filename=filename)
            self._results[filename] = result
            self._order.append(filename)
        if metrics is not None:
            result.metrics = metrics
            result.raw = raw
        if self._on_result is not None:
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка в обработчике результата тестера")

    def _finish(self) -> TestReport:
        process = self._process
        exit_code: int | None = None
        if process is not None:
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_escalating()
                try:
                    exit_code = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    exit_code = None
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None and not stream.closed:
                        stream.close()
                except OSError:
                    pass

        self._finished.wait(timeout=2.0)

        if self._completed_marker and not self._stop_requested.is_set():
            self._current_index = self._total or self._current_index
            self._emit_progress()

        report = TestReport(
            mode="single" if self._inputs.get("mode") == TEST_MODE_SELECTED else "all",
            results=self.results,
            best_config=self._best,
            requested_strategy=self._requested,
            exit_code=exit_code,
            stopped=self._stop_requested.is_set(),
            stdout_tail="\n".join(self._tail[-120:]),
            problems=describe_problems(self._error_lines, exit_code),
        )
        log.info(
            "Тестер завершён: код %s, стратегий %d, лучшая %s, остановлен %s",
            report.exit_code,
            len(report.results),
            report.best_config,
            report.stopped,
        )
        return report

    def stop(self) -> None:
        """Просит тестер остановиться. Не блокирует вызывающий поток.

        Процесс снимается сразу. ``terminate()`` на Windows — это жёсткое
        ``TerminateProcess``: блок ``finally`` скрипта выполнить не удаётся,
        поэтому за тестером подчищает сам GUI — гасит оставшийся winws.exe
        (см. :meth:`wait`), а службу возвращает вызывающая сторона.
        """
        if self._stop_requested.is_set():
            return
        log.info("Запрошена остановка тестера")
        self._stop_requested.set()
        self._kill_escalating()

    def wait(self, timeout: float = TEST_STOP_TIMEOUT) -> bool:
        """Ждёт завершения тестера и его процессов. ``True`` — всё остановлено."""
        reader = self._reader
        if reader is not None:
            reader.join(timeout=timeout)

        process = self._process
        stopped = True
        if process is not None:
            try:
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                stopped = False

        if stopped:
            # Тестер запускает winws.exe через cmd.exe; после жёсткого
            # завершения процесс winws остаётся висеть и мешает установить
            # службу. Тестер гасит его в своём finally — повторяем это здесь.
            stop_running_winws()
        return stopped


def stop_running_winws() -> None:
    """Гасит оставшиеся winws.exe — так делает и сам тестер в ``finally``."""
    from core.service_manager import run_command

    code, output = run_command(["taskkill.exe", "/IM", "winws.exe", "/F"], timeout=COMMAND_TIMEOUT)
    if code == 0:
        log.info("Процессы winws.exe остановлены после теста.")


def targets_preview(path: Path | None = None, limit: int = 5) -> list[str]:
    """Первые строки ``utils\\targets.txt`` — для подсказки в интерфейсе."""
    file = Path(path) if path else Path(TEST_TARGETS_FILE)
    names: list[str] = []
    try:
        with codecs.open(file, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = re.match(r'^\s*(\w+)\s*=\s*"', line)
                if match:
                    names.append(match.group(1))
                    if len(names) >= limit:
                        break
    except OSError as exc:
        log.debug("Не удалось прочитать %s: %s", file, exc)
    return names


def tester_available(script: Path | None = None) -> bool:
    """Есть ли файл тестера на месте."""
    file = Path(script) if script else Path(TEST_SCRIPT)
    try:
        return file.is_file()
    except OSError:
        return False


def utils_dir() -> Path:
    """Папка utils запрета (для подсказок в интерфейсе)."""
    return Path(UTILS_DIR)
