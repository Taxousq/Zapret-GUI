"""Страница «Служба».

Управление службой Windows zapret: состояние, текущая стратегия, старт, стоп,
перезапуск, автозапуск приложения при входе в систему и переход к логам.

Вся логика работы со службой осталась в :class:`core.service_manager.ServiceManager`;
страница только показывает состояние и просит главное окно выполнить операцию
в фоне (``window.run_async``).
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QWidget

from core.service_manager import AutostartManager, ServiceManager, ServiceState, is_admin
from gui.pages.base import Page, WindowApi
from gui.widgets import ModernCard

log = logging.getLogger(__name__)


class ServicePage(Page):
    """Карточка «Служба» с кнопками управления."""

    #: Служба изменила состояние — главное окно обновляет трей и просит статус.
    service_changed = pyqtSignal()
    #: Пользователь просит открыть раздел «Логи».
    logs_requested = pyqtSignal()
    #: Ошибка операции: (заголовок, сообщение) — показывает главное окно.
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        window: WindowApi,
        service_manager: ServiceManager,
        autostart: AutostartManager,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(window, "Служба", parent=parent)
        self.service_manager = service_manager
        self.autostart = autostart

        self._last_state: ServiceState = ServiceState.UNKNOWN
        self._autostart_guard = False
        self._jobs: dict[str, tuple[object, tuple]] = {}

        self.card = ModernCard(
            "Служба",
            "Служба Windows zapret: запуск, остановка, перезапуск.",
        )
        self._build()
        self.add_card(self.card)
        self.finish_layout()

        self._set_autostart_checkbox(self.autostart.is_enabled())
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.state_value = self.kv_row(self.card.body, "Состояние:")
        self.strategy_value = self.kv_row(self.card.body, "Текущая стратегия:")
        self.admin_value = self.kv_row(self.card.body, "Права администратора:")
        self.admin_value.setText("Да" if is_admin() else "Нет")

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = self.make_button("Старт", "primary")
        self.start_button.clicked.connect(self.on_start_service)
        self.stop_button = self.make_button("Стоп")
        self.stop_button.clicked.connect(self.on_stop_service)
        self.restart_button = self.make_button("Перезапуск")
        self.restart_button.clicked.connect(self.on_restart_service)
        self.logs_button = self.make_button("Логи", tooltip="Открыть раздел «Логи»")
        self.logs_button.clicked.connect(self.logs_requested.emit)

        for button in (self.start_button, self.stop_button, self.restart_button, self.logs_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.card.add_layout(buttons)

        self.autostart_check = QCheckBox("Запускать Zapret GUI при входе в систему")
        self.autostart_check.toggled.connect(self._on_autostart_toggled)
        self.card.add_widget(self.autostart_check)

    # ------------------------------------------------------------------
    #  Состояние службы
    # ------------------------------------------------------------------
    def set_service_state(self, state: ServiceState) -> None:
        """Применяет состояние, полученное главным окном от ServiceManager."""
        self._last_state = state

        self.state_value.setText(state.label)
        self.state_value.setStyleSheet(f"color: {state.color};")

        running = state.is_running
        installed = state.is_installed
        busy = self._busy("service")
        self.start_button.setEnabled(not running and not busy)
        self.stop_button.setEnabled(running and not busy)
        self.restart_button.setEnabled(installed and not busy)

    def set_current_strategy(self, name: str | None) -> None:
        """Показывает стратегию, с которой установлена служба."""
        self.strategy_value.setText(name or "—")

    def _set_busy(self, group: str, busy: bool) -> None:
        """Блокирует кнопки группы, пока операция выполняется."""
        if group == "service":
            if busy:
                for button in (self.start_button, self.stop_button, self.restart_button):
                    button.setEnabled(False)
            else:
                self.set_service_state(self._last_state)

    def _busy(self, group: str) -> bool:
        return group in self._jobs

    # ------------------------------------------------------------------
    #  Автозапуск
    # ------------------------------------------------------------------
    def _set_autostart_checkbox(self, enabled: bool) -> None:
        self._autostart_guard = True
        self.autostart_check.setChecked(enabled)
        self._autostart_guard = False

    def _on_autostart_toggled(self, checked: bool) -> None:
        if self._autostart_guard:
            return
        self.autostart_check.setEnabled(False)
        action = self.autostart.enable if checked else self.autostart.disable
        self._run(
            "autostart",
            action,
            on_success=lambda result, want=checked: self._on_autostart_done(result, want),
            on_error=self._on_autostart_error,
            busy_message="Настройка автозапуска...",
        )

    def _on_autostart_done(self, result, wanted: bool) -> None:
        self.autostart_check.setEnabled(True)
        ok, message = result
        if ok:
            self.window.set_status(message.splitlines()[0], 6000)
            log.info("Автозапуск: %s", message)
        else:
            self.failed.emit("Автозапуск", message)
            self._set_autostart_checkbox(not wanted)

    def _on_autostart_error(self, message: str) -> None:
        self.autostart_check.setEnabled(True)
        self._set_autostart_checkbox(self.autostart.is_enabled())
        self.failed.emit("Автозапуск", message)

    # ------------------------------------------------------------------
    #  Действия со службой
    # ------------------------------------------------------------------
    def on_start_service(self) -> None:
        self._run_service(self.service_manager.start, "Запуск службы...")

    def on_stop_service(self) -> None:
        self._run_service(self.service_manager.stop, "Остановка службы...")

    def on_restart_service(self) -> None:
        self._run_service(self.service_manager.restart, "Перезапуск службы...")

    def run_service_task(self, action, busy_message: str, success_prefix: str = "") -> None:
        """Выполняет операцию со службой от имени другой страницы.

        Нужна дашборду: кнопка «Перезапустить» для неустановленной службы
        ставит её заново, а блокировка кнопок и обновление состояния должны
        остаться здесь — на странице, которая владеет этими кнопками.
        """
        self._run_service(action, busy_message, success_prefix)

    def _run_service(self, action, busy_message: str, success_prefix: str = "") -> None:
        if self._busy("service"):
            return
        self._run(
            "service",
            action,
            on_success=lambda result: self._handle_service_result(
                result, "Служба", success_prefix
            ),
            busy_message=busy_message,
        )

    def _handle_service_result(self, result, title: str, success_prefix: str = "") -> None:
        ok, message = result
        if ok:
            log.info("%s: %s", title, message)
            self.window.set_status((success_prefix or message).splitlines()[0], 6000)
        else:
            self.failed.emit(title, message)
        # Состояние обновляем в любом случае: даже при ошибке оно могло измениться.
        self.service_changed.emit()

    # ------------------------------------------------------------------
    #  Общая обвязка фоновых операций
    # ------------------------------------------------------------------
    def _run(
        self,
        group: str,
        fn,
        *,
        on_success=None,
        on_error=None,
        busy_message: str | None = None,
    ) -> None:
        """Выполняет операцию в фоне, блокируя кнопки группы."""
        worker = self.window.run_async(fn, busy_message=busy_message)
        self._jobs[group] = (worker, (on_success, on_error))
        self._set_busy(group, True)

        def done(result, name=group, handler=on_success) -> None:
            self._jobs.pop(name, None)
            try:
                if handler is not None:
                    handler(result)
            except Exception as exc:  # noqa: BLE001 — ошибка обработчика видна пользователю
                log.exception("Ошибка обработки результата операции «%s»", name)
                self.failed.emit("Ошибка", str(exc))
            finally:
                self._set_busy(name, False)

        def broken(message: str, name=group, handler=on_error) -> None:
            self._jobs.pop(name, None)
            try:
                if handler is not None:
                    handler(message)
                else:
                    self.failed.emit("Ошибка", message)
            finally:
                self._set_busy(name, False)

        worker.finished_signal.connect(done)
        worker.error_signal.connect(broken)

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.card.refresh_theme()
        self.set_service_state(self._last_state)
        self.admin_value.setStyleSheet(
            f"color: {self.colors()['text'] if is_admin() else self.colors()['warning']};"
        )
