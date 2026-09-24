"""Иконка приложения в системном трее.

Сворачивание в трей вместо выхода, контекстное меню управления службой и
смена цвета иконки по состоянию службы (зелёный / красный / синий).
Иконка рисуется программно через QPainter, внешние .ico не нужны.

Цвета иконки берутся из активной темы (:mod:`gui.theme`), но при переключении
темы окна иконка трея не перерисовывается: трей — системная область, и его
фон не зависит от темы приложения.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from config import APP_NAME
from core.service_manager import ServiceState
from gui.icons import make_app_icon
from gui.theme import Theme

log = logging.getLogger(__name__)

#: Причины активации трея, по которым показывается окно приложения.
SHOW_WINDOW_REASONS = (
    QSystemTrayIcon.ActivationReason.DoubleClick,
    QSystemTrayIcon.ActivationReason.Trigger,
)


def _activation_code(reason) -> int | None:
    """Числовой код причины активации трея или ``None``, если его не получить.

    PyQt6 отдаёт enum по-разному: в сборках с Python-enum (PyQt6 6.4+)
    значение лежит в ``.value`` — ``int()`` по самому enum падает с TypeError,
    а в старых sip-сборках enum как раз приводится через ``int()``. Пробуем
    оба способа, чтобы клик работал в любой сборке.
    """
    for candidate in (reason, getattr(reason, "value", None)):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _shows_window(reason) -> bool:
    """Нужно ли показывать окно по этой причине активации.

    Сравниваются числовые коды: ``in`` по кортежу enum'ов в части сборок
    PyQt6 падает с ``TypeError: unable to convert a C++ ... instance``.
    """
    code = _activation_code(reason)
    codes = [_activation_code(item) for item in SHOW_WINDOW_REASONS]
    if code is not None and all(item is not None for item in codes):
        return any(code == item for item in codes)
    # Резервный путь для сборок, где enum не приводится к int: прямое
    # сравнение ``==`` (в отличие от ``in``) работает везде.
    return any(reason == item for item in SHOW_WINDOW_REASONS)


#: Цвета иконки трея по состоянию службы. Трей — системная область, поэтому
#: его цвета (и «тёмный» вид иконки) не зависят от темы окна.
TRAY_STATE_COLORS = {
    "RUNNING": "success",
    "STOPPED": "error",
    "NOT_INSTALLED": "accent",
    "UNKNOWN": "warning",
}


class TrayIcon(QSystemTrayIcon):
    """Иконка в трее с меню управления.

    Виджет не знает о главном окне: он только сообщает о действиях сигналами.
    """

    show_window_requested = pyqtSignal()
    quit_requested = pyqtSignal()
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    restart_requested = pyqtSignal()
    check_requested = pyqtSignal()
    logs_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._icons: dict[str, object] = {}
        self._state = ServiceState.UNKNOWN

        self._build_menu()
        self.set_service_state(ServiceState.UNKNOWN)
        self.activated.connect(self._on_activated)

    # -- иконка ------------------------------------------------------------
    def _icon_for(self, color: str):
        icon = self._icons.get(color)
        if icon is None:
            # Цвет берётся из темы в момент отрисовки; при смене темы окна
            # иконка трея не перерисовывается — трей остаётся тёмным.
            icon = make_app_icon(Theme.color(color, color), "Z", 64)
            self._icons[color] = icon
        return icon

    def set_service_state(self, state: ServiceState) -> None:
        """Меняет цвет иконки и подсказку по состоянию службы."""
        self._state = state
        color = TRAY_STATE_COLORS.get(state.name, TRAY_STATE_COLORS["UNKNOWN"])
        self.setIcon(self._icon_for(color))
        self.setToolTip(f"{APP_NAME}\nСлужба zapret: {state.label}")

        running = state.is_running
        installed = state.is_installed
        self.start_action.setEnabled(not running)
        self.stop_action.setEnabled(running)
        self.restart_action.setEnabled(installed)

    # -- меню --------------------------------------------------------------
    def _build_menu(self) -> None:
        menu = QMenu()

        show_action = menu.addAction("Показать окно")
        show_action.triggered.connect(self.show_window_requested.emit)
        menu.addSeparator()

        self.start_action = menu.addAction("Запустить службу")
        self.start_action.triggered.connect(self.start_requested.emit)

        self.stop_action = menu.addAction("Остановить службу")
        self.stop_action.triggered.connect(self.stop_requested.emit)

        self.restart_action = menu.addAction("Перезапустить службу")
        self.restart_action.triggered.connect(self.restart_requested.emit)

        menu.addSeparator()

        check_action = menu.addAction("Проверить доступность сайтов")
        check_action.triggered.connect(self.check_requested.emit)

        logs_action = menu.addAction("Показать логи")
        logs_action.triggered.connect(self.logs_requested.emit)

        menu.addSeparator()

        quit_action = menu.addAction("Выход")
        quit_action.triggered.connect(self.quit_requested.emit)

        self.setContextMenu(menu)
        self._menu = menu

    # -- события -----------------------------------------------------------
    def _on_activated(self, reason) -> None:
        """Открывает окно по клику в трее и никогда не роняет приложение.

        ``reason`` приходит C++-enum'ом: сравнение набора enum'ов оператором
        ``in`` в части сборок PyQt6 падает с TypeError, поэтому сравниваем
        приведённые к int коды.
        """
        try:
            if _shows_window(reason):
                self.show_window_requested.emit()
        except Exception:  # noqa: BLE001 — сбой обработки клика не должен ронять приложение
            log.debug("Сбой обработки клика по трею", exc_info=True)

    def notify(self, title: str, message: str, warning: bool = False) -> None:
        """Показывает всплывающее уведомление в трее."""
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        try:
            self.showMessage(title, message, icon, 5000)
        except Exception:  # noqa: BLE001 — уведомление не должно ронять приложение
            log.debug("Не удалось показать уведомление в трее", exc_info=True)
