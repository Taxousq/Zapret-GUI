"""Страница «Тестирование».

Запуск встроенного тестера запрета (``utils\\test zapret.ps1``), таблица
результатов по стратегиям, вывод тестера в консоль и применение лучшей
стратегии.

Страница владеет своим состоянием теста (таблица, прогресс, консоль), поэтому
переключение раздела во время прогона ничего не теряет: тест продолжается в
фоновом потоке, а при возврате на страницу прогресс и таблица на месте.
"""

from __future__ import annotations

import logging
import time

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from core.service_manager import ADMIN_HINT, ServiceManager, ServiceState, is_admin
from core.strategy_parser import Strategy
from core.strategy_tester import (
    StrategyTestResult,
    StrategyTester,
    TestProgress,
    TestReport,
    check_environment,
    strip_bat_suffix,
    tester_available,
)
from config import TEST_TARGETS_FILE, TEST_STOP_TIMEOUT, tester_script
from gui.pages.base import Page, WindowApi
from gui.widgets import ModernCard, Worker

log = logging.getLogger(__name__)

#: Сколько строк консоли тестера хранить в окне.
_TEST_CONSOLE_LIMIT = 600
#: Сколько сообщений тестера разбирать за один тик таймера (чтобы не подвисать).
_TEST_FLUSH_LIMIT = 400

#: Колонки таблицы результатов.
TABLE_HEADERS = ("Стратегия", "Результат", "OK", "FAIL", "Время")

#: Номер колонки «Время».
_TIME_COLUMN = 4


class TestingPage(Page):
    """Карточка «Тестирование стратегий»."""

    #: Тест начался — окно показывает прогресс в статус-баре.
    test_started = pyqtSignal()
    #: Тест закончился (успешно или с ошибкой).
    test_finished = pyqtSignal()
    #: Список стратегий в таблице мог измениться — нужно перечитать .bat-файлы.
    strategies_changed = pyqtSignal()
    #: Ошибка операции: (заголовок, сообщение).
    failed = pyqtSignal(str, str)
    #: Нужно обновить состояние службы.
    service_changed = pyqtSignal()
    #: Уведомление в трее: (заголовок, текст) — показывает главное окно.
    notify = pyqtSignal(str, str)

    def __init__(
        self,
        window: WindowApi,
        service_manager: ServiceManager,
        tester: StrategyTester,
        zapret_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(window, "Тестирование", parent=parent)
        self.service_manager = service_manager
        self.tester = tester
        #: Папка запрета, выбранная в настройках (нужна для пути к тестеру).
        self.zapret_path: Path | None = Path(zapret_path) if zapret_path else None

        self._last_state: ServiceState = ServiceState.UNKNOWN
        self._strategies: list[Strategy] = []
        self._test_rows: dict[str, int] = {}
        self._test_progress_queue: list[str] = []
        self._test_starts: dict[str, float] = {}
        self._test_seconds: dict[str, float] = {}

        self._test_worker: Worker | None = None
        self._test_running = False
        self._test_finishing = False
        self._test_restore_needed = False
        self._test_previous_strategy: str | None = None
        self._test_report: TestReport | None = None
        self._test_best: str | None = None
        self._status_color = "muted"

        self.card = ModernCard(
            "Тестирование стратегий",
            "Запуск встроенного тестера запрета (utils\\test zapret.ps1). "
            "Тестер по очереди включает каждую стратегию и проверяет доступность "
            "целей из utils\\targets.txt, после чего показывает лучшую стратегию.",
        )
        self._build()
        self.add_card(self.card)
        self.finish_layout()

        # Поток тестера не трогает виджеты напрямую: его колбэки испускают
        # сигналы окна, а Qt доставляет их в главный поток. Строки консоли
        # копятся в списке и разбираются таймером: перерисовка консоли на
        # каждую строку заметно тормозит окно.
        self._test_flush_timer = QTimer(self)
        self._test_flush_timer.setInterval(150)
        self._test_flush_timer.timeout.connect(self._flush_test_console)

        self._warn_environment()
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.admin_warning = QLabel(
            "⚠ Для тестирования нужны права администратора — "
            "перезапустите приложение от имени администратора."
        )
        self.admin_warning.setObjectName("Warning")
        self.admin_warning.setWordWrap(True)
        self.admin_warning.setVisible(not is_admin())
        self.card.add_widget(self.admin_warning)

        self.tester_warning = QLabel("")
        self.tester_warning.setObjectName("Warning")
        self.tester_warning.setWordWrap(True)
        self.tester_warning.setVisible(False)
        self.card.add_widget(self.tester_warning)
        self._set_tester_warning()

        note = QLabel(
            "Перед тестом служба zapret будет удалена: тестер не работает, пока она "
            "установлена. В режиме одной стратегии выберите её в списке ниже."
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        self.card.add_widget(note)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_label = QLabel("Стратегия для теста:")
        mode_label.setObjectName("Muted")
        self.strategy_combo = QComboBox()
        self.strategy_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self.strategy_combo, 1)
        self.card.add_layout(mode_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.test_selected_button = self.make_button("Тестировать выбранную", "primary")
        self.test_selected_button.clicked.connect(self.on_test_selected)
        self.test_all_button = self.make_button("Тестировать все")
        self.test_all_button.clicked.connect(self.on_test_all)
        self.test_stop_button = self.make_button("Остановить", "danger")
        self.test_stop_button.setEnabled(False)
        self.test_stop_button.clicked.connect(self.on_stop_test)
        self.apply_best_button = self.make_button(
            "Применить лучшую",
            tooltip="Станет доступна, когда тестер определит лучшую стратегию.",
        )
        self.apply_best_button.setEnabled(False)
        self.apply_best_button.clicked.connect(self.on_apply_best_strategy)

        for button in (
            self.test_selected_button,
            self.test_all_button,
            self.test_stop_button,
            self.apply_best_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.card.add_layout(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.card.add_widget(self.progress)

        self.status_label = QLabel("Тестирование не запускалось.")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.card.add_widget(self.status_label)

        self.table = QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(list(TABLE_HEADERS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setMinimumHeight(180)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(TABLE_HEADERS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)
        self.card.add_widget(self.table)

        console_row = QHBoxLayout()
        console_row.setSpacing(8)
        self.console_button = self.make_button(
            "Показать вывод тестера", checkable=True
        )
        self.console_button.toggled.connect(self._on_console_toggled)
        self.console_copy_button = self.make_button("Копировать вывод")
        self.console_copy_button.clicked.connect(self._copy_console)
        console_row.addWidget(self.console_button)
        console_row.addWidget(self.console_copy_button)
        console_row.addStretch(1)
        self.card.add_layout(console_row)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.console.setPlaceholderText("Здесь появится вывод тестера.")
        self.console.setFixedHeight(180)
        self.console.setVisible(False)
        self.card.add_widget(self.console)

    def _warn_environment(self) -> None:
        """Показывает состояние тестера и блокирует запуск, если его нет."""
        self._set_tester_warning()
        if not self._tester_available():
            self.test_selected_button.setEnabled(False)
            self.test_all_button.setEnabled(False)

    def _tester_available(self) -> bool:
        """Есть ли тестер в текущей папке запрета."""
        if self.zapret_path is None:
            return tester_available()
        return tester_available(tester_script(self.zapret_path))

    def _set_tester_warning(self) -> None:
        """Обновляет подсказку о найденном/ненайденном тестере."""
        if self._tester_available():
            self.tester_warning.setVisible(False)
            self.tester_warning.setText("")
            return
        if self.zapret_path is None:
            text = (
                "Запрет не найден. Укажите папку в разделе «Настройки» — "
                "без неё тестирование недоступно."
            )
        else:
            text = (
                f"Тестер не найден: {tester_script(self.zapret_path)}\n"
                "Проверьте папку запрета в разделе «Настройки»."
            )
        self.tester_warning.setText(text)
        self.tester_warning.setVisible(True)

    # ------------------------------------------------------------------
    #  Данные из главного окна
    # ------------------------------------------------------------------
    def set_service_state(self, state: ServiceState) -> None:
        self._last_state = state

    def set_zapret_path(self, zapret_path: Path | None) -> None:
        """Смена папки запрета из настроек: обновляет тестер и подсказки.

        Тестирование одновременно не идёт: смена пути доступна только из
        раздела «Настройки», а он не запускает тест.
        """
        self.zapret_path = Path(zapret_path) if zapret_path else None
        if self.zapret_path is not None:
            self.tester.zapret_path = self.zapret_path
            self.tester.script = tester_script(self.zapret_path)
        self._set_tester_warning()
        self._set_test_busy(self.test_busy())

    def set_strategies(self, strategies: list[Strategy]) -> None:
        """Список стратегий из папки запрета (приходит от страницы «Стратегии»).

        Список для теста наполняется здесь же: он должен быть доступен сразу,
        даже если пользователь не открывал раздел «Стратегии».
        """
        self._strategies = list(strategies)

        current = self.strategy_combo.currentData()
        self.strategy_combo.clear()
        for strategy in self._strategies:
            self.strategy_combo.addItem(strategy.name, strategy)
        if not self._strategies:
            self.strategy_combo.addItem("Стратегии не найдены", None)
        self.strategy_combo.setEnabled(bool(self._strategies))

        if current is not None:
            self.select_strategy(current)
        if not self._tester_available():
            # Тестера нет — запускать нечего, но список показываем.
            self.test_selected_button.setEnabled(False)
            self.test_all_button.setEnabled(False)

    def select_strategy(self, strategy: Strategy | None) -> None:
        """Выбирает в списке ту же стратегию, что и на странице «Стратегии»."""
        if strategy is None:
            return
        for row in range(self.strategy_combo.count()):
            item = self.strategy_combo.itemData(row)
            if item is not None and item.path == strategy.path:
                self.strategy_combo.setCurrentIndex(row)
                return

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.card.refresh_theme()
        self.status_label.setStyleSheet(f"color: {self.colors()[self._status_color]};")
        self.highlight_best_row()
        self.table.viewport().update()

    # ------------------------------------------------------------------
    #  Консоль тестера
    # ------------------------------------------------------------------
    def _on_console_toggled(self, shown: bool) -> None:
        self.console.setVisible(shown)
        self.console_button.setText(
            "Скрыть вывод тестера" if shown else "Показать вывод тестера"
        )

    def _copy_console(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(self.console.toPlainText())
        self.window.set_status("Вывод тестера скопирован в буфер", 4000)

    def append_console(self, text: str) -> None:
        """Добавляет строки в консоль тестера, обрезая слишком длинную историю."""
        if not text:
            return
        self.console.appendPlainText(text)
        document = self.console.document()
        while document.blockCount() > _TEST_CONSOLE_LIMIT:
            cursor = self.console.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

    def on_tester_line(self, line: str) -> None:
        """Строка вывода тестера: копится и разбирается таймером."""
        self._test_progress_queue.append(line)
        if len(self._test_progress_queue) > 5000:
            del self._test_progress_queue[:1000]

    def _flush_test_console(self) -> None:
        """Переносит накопленные строки тестера в консоль."""
        if not self._test_progress_queue:
            return
        batch = self._test_progress_queue[:_TEST_FLUSH_LIMIT]
        del self._test_progress_queue[:_TEST_FLUSH_LIMIT]
        self.append_console("\n".join(batch))

    # ------------------------------------------------------------------
    #  Запуск теста
    # ------------------------------------------------------------------
    def _prepare_test(self, mode: str, strategy: Strategy | None) -> bool:
        """Проверяет окружение и при необходимости предупреждает о службе.

        Возвращает ``True``, если тест можно запускать.
        """
        if self._test_running:
            return False

        problems = check_environment(self.tester.script, self.tester.zapret_path)
        blocking = [problem.message for problem in problems if problem.blocking]
        if blocking:
            self.failed.emit("Тестирование стратегий", "\n\n".join(blocking))
            return False

        warnings = [problem.message for problem in problems if not problem.blocking]
        if warnings:
            # Путь с пробелами/кириллицей — предупреждение, а не запрет:
            # у части пользователей тестер работает и так.
            if not self.window.confirm(
                "Тестирование стратегий", "Продолжить тестирование?", "\n\n".join(warnings)
            ):
                return False

        if not is_admin():
            QMessageBox.critical(
                self,
                "Тестирование стратегий",
                "Для тестирования требуются права администратора.\n\n"
                "Перезапустите приложение от имени администратора "
                "(правый клик по ярлыку → «Запуск от имени администратора»).",
            )
            return False

        if mode == "single":
            if strategy is None:
                QMessageBox.warning(
                    self,
                    "Тестирование стратегий",
                    "Выберите стратегию для тестирования.",
                )
                return False
            question = f"Протестировать стратегию «{strategy.name}»?"
            informative = (
                "Тестер запустит выбранную стратегию и проверит доступность целей "
                f"из {TEST_TARGETS_FILE.name}."
            )
        else:
            question = "Протестировать все стратегии?"
            informative = (
                "Тестер по очереди запустит каждую стратегию и проверит доступность "
                "целей. Это может занять несколько минут."
            )

        previous = self.service_manager.get_installed_strategy()
        installed = self._last_state.is_installed
        if installed:
            informative += (
                "\n\nСлужба zapret сейчас установлена, а тестер с ней не работает: "
                "служба будет удалена. Чтобы не потерять текущую настройку, "
                "после теста её можно будет вернуть."
            )
        if not self.window.confirm("Тестирование стратегий", question, informative):
            return False

        self._test_previous_strategy = previous
        self._test_restore_needed = installed and previous is not None
        return True

    def _start_test(self, mode: str, strategy: Strategy | None) -> None:
        if not self._prepare_test(mode, strategy):
            return

        if mode == "single" and strategy is None:
            return
        strategy_filename = strategy.filename if strategy is not None else None
        start = self._last_state.is_installed
        self._test_report = None
        self._test_best = None
        self._reset_table()

        def job() -> TestReport:
            # Служба мешает тестеру — убираем её перед запуском.
            if start:
                self.service_manager.remove()
            return self.tester.run(mode=mode, strategy_filename=strategy_filename)

        self._async_test(job, on_ready=self._on_test_finished)
        self._set_test_busy(True)
        self.progress.setValue(0)
        self._set_test_status(
            "Подготовка... Удаление службы zapret." if start else "Запуск тестера...",
            "muted",
        )
        self.append_console(
            f"=== Запуск тестера ({'одна стратегия' if mode == 'single' else 'все стратегии'}) ==="
        )
        log.info("Старт тестирования стратегий (режим %s)", mode)

    def on_test_selected(self) -> None:
        """Тестирует стратегию, выбранную в списке страницы."""
        strategy = self.strategy_combo.currentData()
        self._start_test("single", strategy)

    def on_test_all(self) -> None:
        """Тестирует все стратегии."""
        self._start_test("all", None)

    def start_test_all(self) -> None:
        """Запускает тестирование всех стратегий.

        Обёртка над :meth:`on_test_all` для быстрых действий дашборда:
        проверка окружения и подтверждения остаются здесь, в странице.
        """
        self.on_test_all()

    def on_stop_test(self) -> None:
        """Останавливает тест и возвращает исходную стратегию."""
        if not self._test_running or self.tester is None:
            return
        self._test_restore_needed = self._test_previous_strategy is not None
        self._set_test_status("Остановка тестера...", "warning")
        self.progress.setValue(0)
        self.test_stop_button.setEnabled(False)
        self.test_selected_button.setEnabled(False)
        self.test_all_button.setEnabled(False)
        log.info("Пользователь остановил тестирование стратегий")
        tester = self.tester
        self.window.run_async(
            lambda: (tester.stop(), tester.wait(TEST_STOP_TIMEOUT))[1],
            on_success=self._on_test_stopped,
            on_error=lambda message: self._set_test_status(
                f"Не удалось остановить тестер: {message}", "error"
            ),
            busy_message="Остановка тестера...",
        )

    def _on_test_stopped(self, stopped: bool) -> None:
        if not stopped:
            self._set_test_status(
                "Тестер не завершился за "
                f"{TEST_STOP_TIMEOUT:g} с. Дождитесь его завершения.",
                "warning",
            )
            self.test_stop_button.setEnabled(True)

    # ------------------------------------------------------------------
    #  Ход теста
    # ------------------------------------------------------------------
    def _async_test(self, job, on_ready=None) -> Worker:
        """Запускает тест в отдельном потоке и помечает его как активный."""
        if self._test_worker is not None:
            raise RuntimeError("Тестирование уже запущено")

        self._test_running = True
        self._test_finishing = False
        self._test_progress_queue.clear()
        self._test_flush_timer.start()
        self.window.set_status("Тестирование стратегий...")
        self.test_started.emit()

        worker = Worker(job, parent=self)
        worker.finished_signal.connect(
            lambda report, cb=on_ready: self._test_ready(report, cb)
        )
        worker.error_signal.connect(self._on_test_error)
        worker.finished.connect(worker.deleteLater)
        self._test_worker = worker
        worker.start()
        return worker

    def _test_ready(self, report, on_ready) -> None:
        """Общая точка выхода теста: чистит состояние и зовёт обработчик."""
        self._test_running = False
        self._test_finishing = True
        self._test_worker = None
        if on_ready is not None:
            on_ready(report)
        self._test_finishing = False

    def on_tester_progress(self, progress: object) -> None:
        if not isinstance(progress, TestProgress):
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(progress.percent)
        if progress.config:
            # Засекаем начало прогона стратегии — из этого считается колонка «Время».
            self._test_starts[strip_bat_suffix(progress.config).lower()] = time.monotonic()
        if progress.config and self._test_running:
            self._set_test_status(
                f"Тестируется {progress.current} из {progress.total}: {progress.config}",
                "accent",
            )

    def on_tester_result(self, result: object) -> None:
        if not isinstance(result, StrategyTestResult):
            return
        self._upsert_test_row(result)

    # ------------------------------------------------------------------
    #  Таблица результатов
    # ------------------------------------------------------------------
    def _reset_table(self) -> None:
        """Готовит таблицу: строка на каждую известную стратегию, статус «ожидание»."""
        self.table.setRowCount(0)
        self._test_rows = {}
        self._test_starts = {}
        self._test_seconds = {}
        for strategy in self._strategies:
            self._add_test_row(strategy.filename, strategy.name)
        self.apply_best_button.setEnabled(False)
        self.apply_best_button.setToolTip(
            "Станет доступна, когда тестер определит лучшую стратегию."
        )

    def _add_test_row(self, filename: str, name: str | None = None) -> int:
        row = self.table.rowCount()
        self.table.insertRow(row)
        title = name or strip_bat_suffix(filename)
        first = QTableWidgetItem(title)
        first.setData(Qt.ItemDataRole.UserRole, filename)
        first.setToolTip(f"Файл: {filename}")
        self.table.setItem(row, 0, first)
        self.table.setItem(row, 1, QTableWidgetItem("нет данных"))
        self.table.setItem(row, 2, QTableWidgetItem("0"))
        self.table.setItem(row, 3, QTableWidgetItem("0"))
        self.table.setItem(row, _TIME_COLUMN, QTableWidgetItem("—"))
        self._test_rows[filename] = row
        return row

    def _row_for(self, filename: str) -> int:
        row = self._test_rows.get(filename)
        if row is not None:
            return row
        # Тестер вернул имя, которого не было среди найденных .bat-файлов
        # (например, список обновился во время теста) — добавляем строку.
        return self._add_test_row(filename)

    def _take_duration(self, filename: str) -> float:
        """Сколько секунд тестировалась стратегия (0, если время неизвестно)."""
        key = strip_bat_suffix(filename).lower()
        started = self._test_starts.pop(key, None)
        if started is None:
            return self._test_seconds.get(key, 0.0)
        elapsed = max(0.0, time.monotonic() - started)
        self._test_seconds[key] = elapsed
        return elapsed

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        if seconds <= 0:
            return "—"
        if seconds < 10:
            return f"{seconds:.1f} с"
        return f"{int(round(seconds))} с"

    def _upsert_test_row(self, result: StrategyTestResult) -> None:
        row = self._row_for(result.filename)
        ok, fail = result.ok_count, result.fail_count
        verdict = self._verdict_text(ok, fail)
        color = (
            self.colors()["success"]
            if verdict == "OK"
            else self.colors()["error"]
            if verdict == "FAIL"
            else self.colors()["warning"]
        )

        name_item = self.table.item(row, 0)
        if name_item is not None:
            name_item.setText(result.display_name)
            name_item.setToolTip(result.tooltip())
        verdict_item = QTableWidgetItem(verdict)
        verdict_item.setForeground(QBrush(QColor(color)))
        self.table.setItem(row, 1, verdict_item)
        self.table.setItem(row, 2, QTableWidgetItem(str(ok)))
        self.table.setItem(row, 3, QTableWidgetItem(str(fail)))
        time_item = QTableWidgetItem(self._format_seconds(self._take_duration(result.filename)))
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, _TIME_COLUMN, time_item)
        self.highlight_best_row()

    @staticmethod
    def _verdict_text(ok: int, fail: int) -> str:
        """Краткий итог по стратегии для колонки «Результат»."""
        if not ok and not fail:
            return "нет данных"
        if ok and not fail:
            return "OK"
        if fail and not ok:
            return "FAIL"
        return "частично"

    def _row_counts(self, row: int) -> tuple[int, int]:
        ok_item = self.table.item(row, 2)
        fail_item = self.table.item(row, 3)
        try:
            ok = int(ok_item.text()) if ok_item is not None else 0
            fail = int(fail_item.text()) if fail_item is not None else 0
        except ValueError:
            return 0, 0
        return ok, fail

    def highlight_best_row(self) -> None:
        """Подсвечивает лучшую стратегию зелёным.

        Имя от тестера может быть как ``general (ALT11).bat``, так и
        ``general (ALT11)`` — сравнение идёт по имени файла без расширения.
        """
        best = strip_bat_suffix(self._best_filename() or "").lower()
        best_color = self.colors()["success"]
        best_bg = QColor(best_color)
        best_bg.setAlpha(38)
        accent_brush = QBrush(best_bg)
        clear_brush = QBrush(Qt.BrushStyle.NoBrush)
        for filename, row in self._test_rows.items():
            wanted = strip_bat_suffix(filename).lower()
            is_best = bool(best) and wanted == best
            verdict = self.table.item(row, 1)
            if verdict is not None:
                ok, fail = self._row_counts(row)
                verdict.setText("Лучшая" if is_best else self._verdict_text(ok, fail))
                verdict.setForeground(QBrush(QColor(best_color if is_best else self.colors()["text"])))
            brush = accent_brush if is_best else clear_brush
            for column in range(self.table.columnCount()):
                item = self.table.item(row, column)
                if item is not None:
                    item.setBackground(brush)

    def _best_filename(self) -> str | None:
        """Имя .bat-файла лучшей стратегии по данным тестера."""
        if self._test_report is not None and self._test_report.best_config:
            return self._test_report.best_config
        return self._test_best

    def _find_strategy(self, filename: str) -> Strategy | None:
        """Ищет .bat-стратегию по имени, которое вернул тестер."""
        if not filename:
            return None
        key = filename.strip().lower()
        stem = strip_bat_suffix(filename).lower()
        for strategy in self._strategies:
            if strategy.filename.lower() == key or strategy.name.lower() == key:
                return strategy
            if strategy.path.stem.lower() == stem:
                return strategy
        return None

    # ------------------------------------------------------------------
    #  Завершение теста
    # ------------------------------------------------------------------
    def _on_test_error(self, message: str) -> None:
        self._test_running = False
        self._test_finishing = True
        self._test_worker = None
        self._set_test_busy(False)
        self._set_test_status(f"Ошибка тестирования: {message}", "error")
        self._test_finishing = False
        self.failed.emit("Тестирование стратегий", message)
        self._notify_finished()

    def _on_test_finished(self, report: object) -> None:
        """Итог теста: подсветка лучшей, восстановление службы, сообщение."""
        self._test_finishing = True
        self._flush_test_console()
        self._set_test_busy(False)

        if isinstance(report, TestReport):
            self._test_report = report
            # Служба была удалена перед тестом. Если лучшая стратегия не
            # найдена, возвращаем прежнюю: оставлять пользователя без службы
            # после неудачного прогона нельзя. Если лучшая есть — она будет
            # применена кнопкой, и восстанавливать старую не нужно.
            if not report.best_config:
                self._test_restore_needed = self._test_previous_strategy is not None
            if report.best_config:
                self.apply_best_button.setEnabled(True)
                self.apply_best_button.setToolTip(
                    f"Установить службу со стратегией «{report.best_name}»."
                )
            self.highlight_best_row()
            self._report_test_outcome(report)
        else:
            self._test_restore_needed = self._test_previous_strategy is not None
            self._set_test_status("Тестер завершился с неожиданным результатом.", "error")

        self._test_finishing = False
        self._finish_restore_after_test()
        self._notify_finished()

    def _notify_finished(self) -> None:
        self.test_finished.emit()
        self.strategies_changed.emit()
        self.service_changed.emit()

    def _report_test_outcome(self, report: TestReport) -> None:
        if report.stopped:
            self._set_test_status("Тестирование остановлено пользователем.", "warning")
            return
        if report.problems:
            self._set_test_status("Тестирование не выполнено.", "error")
            for problem in report.problems:
                self.append_console(f"\n[GUI] {problem}")
            self.failed.emit("Тестирование стратегий", "\n\n".join(report.problems))
            return
        if report.best_config:
            self.progress.setValue(100)
            strategy = self._find_strategy(report.best_config)
            shown = report.best_display(strategy.name if strategy is not None else None)
            self._set_test_status(
                f"Тестирование завершено. Лучшая стратегия: {shown}. "
                "Нажмите «Применить лучшую», чтобы установить её.",
                "success",
            )
            self.window.set_status(f"Лучшая стратегия: {shown}", 10000)
            self.notify.emit("Тестирование стратегий", f"Лучшая стратегия: {shown}")
        else:
            self._set_test_status(
                "Тестирование завершено, но лучшую стратегию определить не удалось. "
                "Смотрите вывод тестера.",
                "warning",
            )

    def _finish_restore_after_test(self) -> None:
        """После остановки возвращает службу с исходной стратегией."""
        if not self._test_restore_needed or not self._test_previous_strategy:
            return
        self._test_restore_needed = False
        strategy = self._find_strategy(self._test_previous_strategy)
        name = strategy.name if strategy is not None else self._test_previous_strategy
        if strategy is None:
            self.window.set_status(
                f"Исходная стратегия «{name}» больше не найдена — служба не восстановлена.",
                10000,
            )
            return
        log.info("Восстанавливаю службу со стратегией %s", strategy.name)
        self.window.run_async(
            self.service_manager.install,
            strategy.path,
            on_success=lambda result: self._handle_restore_result(result, name),
            busy_message=f"Восстановление службы со стратегией «{name}»...",
        )

    def _handle_restore_result(self, result, name: str) -> None:
        ok, message = result
        if ok:
            log.info("Служба восстановлена: %s", message)
            self.window.set_status("Служба восстановлена", 6000)
        else:
            self.failed.emit("Восстановление службы", message)
        self.service_changed.emit()

    def on_apply_best_strategy(self) -> None:
        """Устанавливает службу с лучшей найденной стратегией."""
        filename = self._best_filename()
        if not filename:
            QMessageBox.information(
                self,
                "Тестирование стратегий",
                "Лучшая стратегия ещё не определена: сначала выполните тестирование.",
            )
            return
        strategy = self._find_strategy(filename)
        if strategy is None:
            QMessageBox.warning(
                self,
                "Тестирование стратегий",
                f"Файл стратегии не найден:\n{filename}\n\n"
                "Возможно, список стратегий изменился — обновите его и повторите тест.",
            )
            return
        self._apply_tested_strategy(strategy)

    def _apply_tested_strategy(self, strategy: Strategy) -> None:
        informative = (
            "Служба zapret будет создана заново с аргументами этой стратегии "
            "и запущена."
        )
        if not is_admin():
            informative += "\n\n" + ADMIN_HINT
        if not self.window.confirm(
            "Применение лучшей стратегии",
            f"Установить службу со стратегией «{strategy.name}»?",
            informative,
        ):
            return
        self.window.run_async(
            self.service_manager.install,
            strategy.path,
            on_success=lambda result: self._handle_apply_result(result),
            busy_message=f"Установка службы со стратегией «{strategy.name}»...",
        )

    def _handle_apply_result(self, result) -> None:
        ok, message = result
        if ok:
            log.info("Установка службы: %s", message)
            self.window.set_status("Лучшая стратегия применена", 6000)
            self.service_changed.emit()
        else:
            self.failed.emit("Установка службы", message)

    # ------------------------------------------------------------------
    #  Состояние кнопок
    # ------------------------------------------------------------------
    def _set_test_busy(self, busy: bool) -> None:
        available = self._tester_available()
        self.test_selected_button.setEnabled(available and not busy)
        self.test_all_button.setEnabled(available and not busy)
        self.test_stop_button.setEnabled(busy)
        has_best = busy is False and self._best_filename() is not None
        self.apply_best_button.setEnabled(has_best and not busy)

    def _set_test_status(self, text: str, color: str = "muted") -> None:
        self._status_color = color
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {self.colors()[color]};")

    def test_busy(self) -> bool:
        """Идёт ли тестирование (используется при закрытии окна)."""
        return (
            self._test_running
            or self._test_finishing
            or (self._test_worker is not None and self._test_worker.isRunning())
        )

    def abandon_test(self, wait: bool = True) -> None:
        """Аварийная остановка при выходе из приложения."""
        self._test_restore_needed = False
        try:
            self.tester.stop()
        except Exception:  # noqa: BLE001 — выход не должен падать
            log.exception("Не удалось остановить тестер при выходе")
        if wait and self._test_worker is not None and self._test_worker.isRunning():
            self._test_worker.wait(3000)
