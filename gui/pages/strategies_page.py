"""Страница «Стратегии».

Список .bat-стратегий обхода из папки запрета, выбор одной из них и установка
службы zapret с её аргументами (а также удаление службы).

Разбор стратегий остался в :class:`core.strategy_parser.StrategyParser`, а сама
установка — в :meth:`core.service_manager.ServiceManager.install`: страница
только показывает список и запускает операции через главное окно.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QSizePolicy, QWidget

from core.service_manager import ADMIN_HINT, ServiceManager, ServiceState, is_admin
from core.strategy_parser import Strategy, StrategyParser
from gui.pages.base import Page, WindowApi
from gui.widgets import ModernCard

log = logging.getLogger(__name__)


class StrategiesPage(Page):
    """Карточка «Стратегия обхода»."""

    #: Список стратегий перечитан: главное окно обновляет страницы и версию.
    strategies_changed = pyqtSignal()
    #: Служба установлена/удалена — нужно обновить её состояние.
    service_changed = pyqtSignal()
    #: Пользователь выбрал другую стратегию в списке.
    selection_changed = pyqtSignal()
    #: Ошибка операции: (заголовок, сообщение).
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        window: WindowApi,
        service_manager: ServiceManager,
        strategy_parser: StrategyParser,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(window, "Стратегии", parent=parent)
        self.service_manager = service_manager
        self.strategy_parser = strategy_parser

        self._strategies: list[Strategy] = []
        self._last_state: ServiceState = ServiceState.UNKNOWN
        self._loading = False
        self._jobs: dict[str, tuple[object, tuple]] = {}
        #: Есть ли папка запрета (её выбирают в разделе «Настройки»).
        self._zapret_available = True

        self.card = ModernCard(
            "Стратегия обхода",
            "Стратегии берутся из .bat-файлов в папке запрета.",
        )
        self._build()
        self.add_card(self.card)
        self.finish_layout()
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.strategy_combo = QComboBox()
        self.strategy_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.strategy_combo.currentIndexChanged.connect(self._on_combo_changed)
        row.addWidget(self.strategy_combo, 1)

        self.reload_button = self.make_button("Обновить список")
        self.reload_button.clicked.connect(self.on_reload_strategies)
        row.addWidget(self.reload_button)
        self.card.add_layout(row)

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("Hint")
        self.hint_label.setWordWrap(True)
        self.card.add_widget(self.hint_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.apply_button = self.make_button("Применить стратегию", "primary")
        self.apply_button.clicked.connect(self.on_apply_strategy)
        self.remove_button = self.make_button("Удалить службу", "danger")
        self.remove_button.clicked.connect(self.on_remove_service)
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.card.add_layout(buttons)

    # ------------------------------------------------------------------
    #  Список стратегий
    # ------------------------------------------------------------------
    def strategies(self) -> list[Strategy]:
        """Найденные стратегии (нужны странице тестирования и главному окну)."""
        return list(self._strategies)

    def current_strategy(self) -> Strategy | None:
        return self.strategy_combo.currentData()

    def on_reload_strategies(self) -> None:
        """Перечитывает список .bat-стратегий."""
        strategies = self.strategy_parser.parse()
        self._strategies = strategies
        self._zapret_available = self.strategy_parser.directory_exists

        self._loading = True
        self.strategy_combo.clear()
        if not strategies:
            self.strategy_combo.addItem(
                "Стратегии не найдены" if self._zapret_available else "Запрет не найден",
                None,
            )
            self.strategy_combo.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.hint_label.setText(self._empty_hint())
            self.hint_label.setVisible(True)
            self._loading = False
            self.window.set_status(
                "Стратегии не найдены" if self._zapret_available else "Запрет не найден",
                6000,
            )
            self.strategies_changed.emit()
            return

        for strategy in strategies:
            self.strategy_combo.addItem(strategy.name, strategy)
            index = self.strategy_combo.count() - 1
            self.strategy_combo.setItemData(index, strategy.tooltip, Qt.ItemDataRole.ToolTipRole)

        self.strategy_combo.setEnabled(True)
        self.apply_button.setEnabled(True)
        self.hint_label.setText(
            f"Найдено стратегий: {len(strategies)}. Папка: {self.strategy_parser.zapret_path}"
        )
        self.hint_label.setVisible(True)
        self._select_installed_strategy()
        self._loading = False
        self.strategies_changed.emit()

    def _empty_hint(self) -> str:
        """Подсказка для пустого списка стратегий."""
        if self._zapret_available:
            return self.strategy_parser.hint()
        return (
            "Запрет не найден. Укажите папку в разделе «Настройки».\n"
            f"Сейчас используется путь: {self.strategy_parser.zapret_path}"
        )

    def set_zapret_path(self, zapret_path) -> None:
        """Смена папки запрета из настроек — перечитывает список стратегий."""
        if zapret_path is None:
            return
        self.strategy_parser.zapret_path = Path(zapret_path)
        self.on_reload_strategies()

    def _select_installed_strategy(self) -> None:
        """Выбирает в комбобоксе стратегию, с которой установлена служба."""
        installed = self.service_manager.get_installed_strategy()
        if not installed:
            return
        key = installed.strip().lower()
        for index in range(self.strategy_combo.count()):
            strategy = self.strategy_combo.itemData(index)
            if strategy is None:
                continue
            if key in (strategy.name.lower(), strategy.path.stem.lower(), strategy.filename.lower()):
                self.strategy_combo.setCurrentIndex(index)
                return

    def _on_combo_changed(self, index: int) -> None:
        if self._loading:
            return
        # Главное окно передаёт выбор странице тестирования.
        self.selection_changed.emit()

    # ------------------------------------------------------------------
    #  Состояние службы
    # ------------------------------------------------------------------
    def set_service_state(self, state: ServiceState) -> None:
        self._last_state = state
        installed = state.is_installed
        self.apply_button.setText("Применить стратегию" if installed else "Установить службу")
        self.remove_button.setEnabled(installed)
        self._set_busy("service", self._busy("service"))

    def _busy(self, group: str) -> bool:
        return group in self._jobs

    def _set_busy(self, group: str, busy: bool) -> None:
        if group != "service":
            return
        self.apply_button.setEnabled(
            not busy and bool(self._strategies) and self.strategy_combo.isEnabled()
        )
        self.remove_button.setEnabled(not busy and self._last_state.is_installed)
        self.reload_button.setEnabled(not busy)

    # ------------------------------------------------------------------
    #  Действия
    # ------------------------------------------------------------------
    def on_apply_strategy(self) -> None:
        """Устанавливает (или переустанавливает) службу с выбранной стратегией."""
        strategy = self.current_strategy()
        if strategy is None:
            QMessageBox.warning(
                self,
                "Стратегия",
                "Стратегии не найдены.\n\n" + self.strategy_parser.hint(),
            )
            return

        if self._last_state.is_installed:
            text = f"Служба zapret будет переустановлена со стратегией «{strategy.name}»."
            informative = (
                "Текущая служба будет остановлена и удалена, затем создана заново "
                "с аргументами выбранной стратегии.\n\n"
                "Это занимает несколько секунд."
            )
        else:
            text = f"Служба zapret будет установлена со стратегией «{strategy.name}»."
            informative = "Служба будет создана и запущена автоматически."

        if not is_admin():
            informative += "\n\n" + ADMIN_HINT

        if not self.window.confirm("Применение стратегии", text, informative):
            return

        self._run_service(
            lambda: self.service_manager.install(strategy.path),
            f"Установка службы со стратегией «{strategy.name}»...",
            success_prefix="Служба установлена",
        )

    def on_remove_service(self) -> None:
        if not self.window.confirm(
            "Удаление службы",
            "Удалить службу zapret?",
            "Служба будет остановлена и удалена из системы. "
            "Файлы запрета при этом не удаляются.",
        ):
            return
        self._run_service(
            self.service_manager.remove,
            "Удаление службы...",
            title="Удаление службы",
        )

    def _run_service(self, fn, busy_message: str, *, title: str = "Установка службы",
                     success_prefix: str = "") -> None:
        """Установка/удаление службы в фоне с блокировкой кнопок."""
        if self._busy("service"):
            return
        worker = self.window.run_async(fn, busy_message=busy_message)
        self._jobs["service"] = (worker, ())
        self._set_busy("service", True)

        def done(result) -> None:
            self._jobs.pop("service", None)
            self._set_busy("service", False)
            ok, message = result
            if ok:
                log.info("%s: %s", title, message)
                self.window.set_status((success_prefix or message).splitlines()[0], 6000)
            else:
                self.failed.emit(title, message)
            # Состояние и список стратегий обновляем в любом случае.
            self.service_changed.emit()

        def broken(message: str) -> None:
            self._jobs.pop("service", None)
            self._set_busy("service", False)
            self.failed.emit(title, message)
            self.service_changed.emit()

        worker.finished_signal.connect(done)
        worker.error_signal.connect(broken)

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.card.refresh_theme()
        self.set_service_state(self._last_state)
