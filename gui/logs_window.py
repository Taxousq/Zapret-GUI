"""Окно логов службы zapret.

Записи читаются из журнала событий Windows через ``wevtutil``:

1. журнал Application с фильтром по поставщику ``zapret``;
2. если там пусто — журнал System с фильтром по ``Service Control Manager``,
   из которого выбираются записи, упоминающие zapret (запуск/остановка службы).

Чтение выполняется в отдельном потоке: wevtutil может отвечать несколько секунд,
а интерфейс подвисать не должен.

Вывод wevtutil читается **байтами**: единой кодировки у него нет. Служебные
поля приходят в cp866 (OEM-кодировка русской Windows), а значения вроде User
или Description могут быть в UTF-8. Поэтому кодировка подбирается по
читаемости текста — см. :func:`_decode_output`.

Автообновления по таймеру здесь нет: журнал читается один раз при открытии
окна и по кнопке «Обновить». Опрос ``wevtutil`` раз в несколько секунд
запускал лишний процесс и читал журнал System без всякой пользы.
"""

from __future__ import annotations

import logging
import re
import subprocess

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import COLORS, LOGS_EVENT_LIMIT, SERVICE_NAME
from core.service_manager import ADMIN_HINT, is_access_denied
from core.win_utils import CREATE_NO_WINDOW
from gui.widgets import Worker

log = logging.getLogger(__name__)

#: Таймаут wevtutil: журнал может отвечать медленно.
_WEVTUTIL_TIMEOUT = 25

#: Кодировки, которыми может «заговорить» wevtutil. Порядок важен: cp866 —
#: основной вариант для русской Windows, utf-8 и cp1251 — запасные, и при
#: равной читаемости побеждает более ранняя.
_ENCODINGS: tuple[str, ...] = ("cp866", "utf-8", "cp1251")

#: Доля «нечитаемых» символов, после которой кодировка считается неподходящей.
_BAD_RATIO = 0.10

#: Признаки неверной кодировки: заглушки вместо букв и кириллические знаки,
#: которых в русском тексте не бывает. Последние — след cp866, прочитанного
#: как cp1251: байты русских букв превращаются в Ђ, Ѓ, Є, Ѕ, І, Ї, Ј, Љ…
_MOJIBAKE_MARKERS = frozenset(
    "\ufffd\u25a1\u2665\u2666"
    "\u0402\u0403\u0405\u0406\u0407\u0408\u0409\u040a\u040b\u040c\u040e\u040f"
    "\u0452\u0453\u0455\u0456\u0457\u0458\u0459\u045a\u045b\u045c\u045e\u045f"
)

_EMPTY_HINT = (
    "Записи о службе zapret в журнале событий не найдены.\n\n"
    "Это нормально: winws.exe не пишет в журнал Windows. Служба всё равно\n"
    "может работать — её состояние видно на главном экране.\n\n"
    "Если служба не запускается, проверьте:\n"
    "  • запущено ли приложение от имени администратора;\n"
    "  • не блокирует ли запуск антивирус или другое средство обхода;\n"
    "  • свободен ли порт и не занят ли winws.exe другим процессом.\n"
)

_EVENT_SPLIT_RE = re.compile(r"(?=^Event\[\d+\]:)", re.MULTILINE)


def _filter_records(text: str, needle: str) -> str:
    """Оставляет только записи журнала, упоминающие needle."""
    records = [r.strip() for r in _EVENT_SPLIT_RE.split(text) if r.strip()]
    matching = [r for r in records if needle.lower() in r.lower()]
    return "\n\n".join(matching)


# ---------------------------------------------------------------------------
#  Кодировка вывода wevtutil
# ---------------------------------------------------------------------------
def _looks_broken(char: str) -> bool:
    """Похож ли символ на след неверной кодировки.

    Проверяются три частых случая: заглушки вместо букв (``?`` и ромбик),
    кириллические знаки, которых в русском тексте не бывает (след cp866,
    прочитанного как cp1251), и псевдографика cp866 — в неё превращается
    русский текст, если однобайтовую кодировку прочитать не той таблицей.
    """
    if char in _MOJIBAKE_MARKERS or char in ("?", "\xa0"):
        return True
    code = ord(char)
    if 0x00A1 <= code <= 0x024F:  # латиница с диакритикой
        return True
    if 0x0370 <= code <= 0x03FF:  # греческий алфавит
        return True
    if 0x2500 <= code <= 0x259F:  # псевдографика и блоки cp866
        return True
    return False


def _bad_ratio(text: str) -> float:
    """Доля символов, выдающих неверную кодировку (0.0 — текст читаемый)."""
    if not text:
        return 0.0
    return sum(1 for char in text if _looks_broken(char)) / len(text)


def _decode_line(line: bytes) -> str:
    """Декодирует строку вывода, выбирая самую читаемую кодировку.

    Выбор именно построчный: wevtutil отдаёт служебные поля в cp866, а
    значения вроде User или Description могут прийти в UTF-8 — общая
    кодировка на весь вывод испортила бы одну из частей.
    """
    best_text = ""
    best_ratio: float | None = None
    for encoding in _ENCODINGS:
        try:
            text = line.decode(encoding)
        except UnicodeDecodeError:
            # Замена «сломанных» байтов на U+FFFD: такой вариант получит
            # штраф в _bad_ratio и проиграет более удачной кодировке.
            text = line.decode(encoding, errors="replace")
        ratio = _bad_ratio(text)
        if best_ratio is None or ratio < best_ratio:
            best_text, best_ratio = text, ratio
    return best_text


def _encoding_undetermined(raw: bytes) -> bool:
    """True, если ни одна из кодировок не даёт читаемого текста."""
    ratios = []
    for encoding in _ENCODINGS:
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            candidate = raw.decode(encoding, errors="replace")
        ratios.append(_bad_ratio(candidate))
    return min(ratios) > _BAD_RATIO


def _decode_output(raw: bytes) -> str:
    """Декодирует вывод wevtutil (см. :func:`_decode_line`)."""
    if not raw:
        return ""
    # BOM называет кодировку однозначно — эвристика не нужна.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    elif raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")

    text = "\n".join(_decode_line(line) for line in raw.split(b"\n"))
    if _encoding_undetermined(raw):
        text += "\n\n(кодировка не определена — текст показан как есть)"
    return text


def _run_wevtutil(args: list[str]) -> tuple[int, bytes]:
    """Запускает wevtutil и возвращает ``(код возврата, вывод в байтах)``.

    Вывод берётся байтами: кодировку определяет :func:`_decode_output`, а не
    subprocess. stderr подмешивается к stdout — ошибки wevtutil пишет туда.

    ``creationflags=CREATE_NO_WINDOW`` — чтобы wevtutil не открывал консольное
    окно (на других платформах флаг равен 0).
    """
    command = ["wevtutil.exe", *args]
    log.debug("Запуск команды: %s", command)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_WEVTUTIL_TIMEOUT,
            shell=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return 1, f"wevtutil не ответил за {_WEVTUTIL_TIMEOUT} с.".encode("utf-8")
    except FileNotFoundError:
        return 1, "Не найдена программа wevtutil.exe.".encode("utf-8")
    except OSError as exc:
        return 1, f"Не удалось выполнить wevtutil.exe: {exc}".encode("utf-8")

    raw = (completed.stdout or b"") + (completed.stderr or b"")
    log.debug("Код %s, вывод: %d байт", completed.returncode, len(raw))
    return completed.returncode, raw


def fetch_service_logs(limit: int = LOGS_EVENT_LIMIT) -> tuple[str, str]:
    """Читает журнал событий.

    :return: ``(текст, ошибка)``. При успехе ошибка — пустая строка.
    """
    # 1. Журнал Application, поставщик zapret.
    code, raw = _run_wevtutil(
        [
            "qe",
            "Application",
            f"/c:{limit}",
            "/rd:true",
            "/f:text",
            "/q:*[System[Provider[@Name='zapret']]]",
        ]
    )
    output = _decode_output(raw).strip()
    if code == 0 and output:
        header = f"=== Журнал Application, поставщик zapret (последние {limit}) ===\n\n"
        return header + output, ""

    application_error = "" if code == 0 else output

    # 2. Резервный вариант: журнал System, Service Control Manager.
    code, raw = _run_wevtutil(
        [
            "qe",
            "System",
            f"/c:{limit * 2}",
            "/rd:true",
            "/f:text",
            "/q:*[System[Provider[@Name='Service Control Manager']]]",
        ]
    )
    output = _decode_output(raw).strip()
    if code == 0 and output:
        filtered = _filter_records(output, SERVICE_NAME)
        if filtered.strip():
            header = (
                "=== Журнал System: Service Control Manager, записи о zapret ===\n\n"
            )
            return header + filtered, ""
        return "", _EMPTY_HINT

    system_error = "" if code == 0 else output
    combined = system_error or application_error

    if is_access_denied(combined):
        return "", (
            "Нет прав для чтения журнала событий.\n\n" + ADMIN_HINT
        )
    return "", (
        "Не удалось прочитать журнал событий Windows.\n\n"
        f"{combined.strip() or 'wevtutil не вернул данных.'}"
    )


class LogsWindow(QWidget):
    """Окно просмотра логов службы.

    Автообновления по таймеру нет: журнал читается один раз при показе окна и
    по кнопке «Обновить». Класс оставлен в проекте как отдельное окно логов,
    хотя основной интерфейс показывает их на странице :class:`gui.pages.LogsPage`.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Логи службы zapret")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setMinimumSize(680, 420)
        self.resize(780, 520)

        self._worker: Worker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        self.refresh_button = QPushButton("Обновить")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)

        self.copy_button = QPushButton("Копировать")
        self.copy_button.clicked.connect(self._copy_to_clipboard)
        toolbar.addWidget(self.copy_button)

        toolbar.addStretch(1)

        self.status_label = QLabel("—")
        self.status_label.setObjectName("Muted")
        toolbar.addWidget(self.status_label)

        layout.addLayout(toolbar)

        self.text_area = QPlainTextEdit()
        self.text_area.setReadOnly(True)
        self.text_area.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_area.setPlaceholderText("Загрузка логов...")
        layout.addWidget(self.text_area, 1)

        self.hint_label = QLabel(
            "Источник: журнал событий Windows (wevtutil). "
            "Записи о запуске и остановке службы появляются с задержкой в несколько секунд. "
            "Обновление — по кнопке «Обновить»."
        )
        self.hint_label.setObjectName("Muted")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

    # -- обновление --------------------------------------------------------
    def refresh(self) -> None:
        """Запускает чтение логов в фоне (если оно уже идёт — ничего не делает)."""
        if self._worker is not None and self._worker.isRunning():
            return

        self.refresh_button.setEnabled(False)
        self.status_label.setText("Чтение журнала...")
        self.status_label.setStyleSheet(f"color: {COLORS['muted']};")

        worker = Worker(fetch_service_logs, LOGS_EVENT_LIMIT, parent=self)
        worker.finished_signal.connect(self._on_logs_loaded)
        worker.error_signal.connect(self._on_logs_error)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self) -> None:
        self.refresh_button.setEnabled(True)
        self._worker = None

    def _on_logs_loaded(self, result: object) -> None:
        try:
            text, error = result  # type: ignore[misc]
        except (TypeError, ValueError):
            log.error("Неожиданный результат чтения логов: %r", result)
            self._on_logs_error("Неожиданный формат ответа при чтении логов.")
            return

        if text:
            self.text_area.setPlainText(text)
            self.text_area.verticalScrollBar().setValue(0)  # самые свежие — сверху
            self.status_label.setText("Обновлено")
            self.status_label.setStyleSheet(f"color: {COLORS['success']};")
        else:
            self.text_area.setPlainText(error or "Пусто.")
            self.status_label.setText("Нет данных")
            self.status_label.setStyleSheet(f"color: {COLORS['muted']};")

    def _on_logs_error(self, message: str) -> None:
        log.error("Ошибка чтения логов: %s", message)
        self.text_area.setPlainText(f"Не удалось получить логи.\n\n{message}")
        self.status_label.setText("Ошибка")
        self.status_label.setStyleSheet(f"color: {COLORS['error']};")

    # -- прочее ------------------------------------------------------------
    def _copy_to_clipboard(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text_area.toPlainText())
            self.status_label.setText("Скопировано в буфер")
            self.status_label.setStyleSheet(f"color: {COLORS['success']};")

    def showEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """Один раз читает журнал при показе окна (это не таймер)."""
        super().showEvent(event)
        self.refresh()
