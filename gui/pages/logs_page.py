"""Страница «Логи».

Показывает записи журнала событий Windows о службе zapret. Записи читаются
через ``wevtutil`` (см. :func:`gui.logs_window.fetch_service_logs`) — это
занимает секунды, поэтому чтение идёт в фоновом потоке, а результат
подставляется в текстовое поле.

Автообновления по таймеру нет: журнал читается один раз при открытии страницы
и по кнопке «Обновить». Опрос ``wevtutil`` раз в 5 секунд запускал лишний
процесс и читал журнал System — на слабых машинах это заметно грузило систему.

Отдельное окно :class:`gui.logs_window.LogsWindow` больше не используется, но
оставлено в проекте на случай, если понадобится открыть логи отдельным окном.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QWidget,
)

from config import LOGS_EVENT_LIMIT
from gui.logs_window import fetch_service_logs
from gui.pages.base import Page, WindowApi
from gui.widgets import ModernCard, Worker

log = logging.getLogger(__name__)


class LogsPage(Page):
    """Страница с журналом событий: чтение по кнопке и при открытии."""

    #: Ошибка чтения журнала: (заголовок, сообщение).
    failed = pyqtSignal(str, str)

    def __init__(self, window: WindowApi, parent: QWidget | None = None) -> None:
        super().__init__(window, "Логи", parent=parent)
        self._worker: Worker | None = None
        self._status_color = "muted"

        self.card = ModernCard(
            "Логи службы zapret",
            "Источник: журнал событий Windows (wevtutil). Записи о запуске и "
            "остановке службы появляются с задержкой в несколько секунд. "
            "Обновление — по кнопке «Обновить».",
        )
        self._build()
        self.add_card(self.card)
        self.finish_layout()
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)

        self.refresh_button = self.make_button("Обновить")
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)

        self.copy_button = self.make_button("Копировать")
        self.copy_button.clicked.connect(self._copy_to_clipboard)
        toolbar.addWidget(self.copy_button)

        self.clear_button = self.make_button("Очистить", tooltip="Очистить поле вывода")
        self.clear_button.clicked.connect(self._clear)
        toolbar.addWidget(self.clear_button)

        toolbar.addStretch(1)

        self.status_label = QLabel("—")
        self.status_label.setObjectName("Muted")
        toolbar.addWidget(self.status_label)
        self.card.add_layout(toolbar)

        self.text_area = QPlainTextEdit()
        self.text_area.setReadOnly(True)
        self.text_area.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text_area.setPlaceholderText("Нажмите «Обновить», чтобы прочитать журнал.")
        self.text_area.setMinimumHeight(320)
        self.card.add_widget(self.text_area)

    # ------------------------------------------------------------------
    #  Чтение журнала
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Запускает чтение логов в фоне (если оно уже идёт — ничего не делает)."""
        if self._worker is not None and self._worker.isRunning():
            return

        self.refresh_button.setEnabled(False)
        self.status_label.setText("Чтение журнала...")
        self._set_status_color("muted")
        self.window.set_status("Чтение журнала событий...")

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
            self._set_status_color("success")
            self.window.set_status("Логи обновлены", 4000)
        else:
            self.text_area.setPlainText(error or "Пусто.")
            self.status_label.setText("Нет данных")
            self._set_status_color("muted")
            self.window.set_status("Записи о службе не найдены", 5000)

    def _on_logs_error(self, message: str) -> None:
        log.error("Ошибка чтения логов: %s", message)
        self.text_area.setPlainText(f"Не удалось получить логи.\n\n{message}")
        self.status_label.setText("Ошибка")
        self._set_status_color("error")
        self.failed.emit("Логи", message)

    # ------------------------------------------------------------------
    #  Прочее
    # ------------------------------------------------------------------
    def _copy_to_clipboard(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text_area.toPlainText())
            self.status_label.setText("Скопировано в буфер")
            self._set_status_color("success")

    def _clear(self) -> None:
        self.text_area.clear()
        self.status_label.setText("Очищено")
        self._set_status_color("muted")

    def showEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """Читает журнал один раз при открытии страницы (таймера здесь нет)."""
        super().showEvent(event)
        self.refresh()

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def _set_status_color(self, key: str) -> None:
        self._status_color = key
        self.status_label.setStyleSheet(f"color: {self.colors().get(key, key)};")

    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.card.refresh_theme()
        self._set_status_color(self._status_color)
