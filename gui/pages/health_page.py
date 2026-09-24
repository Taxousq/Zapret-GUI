"""Страница «Проверка связи».

Проверка доступности сайтов из :data:`config.CHECK_SITES`: каждая строка —
отдельный сайт с точкой-индикатором, статусом и причиной.

Проверка выполняется в фоне (:class:`core.health_checker.HealthChecker`),
поэтому интерфейс не подвисает даже при таймаутах.

Результаты каждой проверки дополнительно уходят в историю пингов
(:class:`core.ping_history.PingHistory`) — её показывает график на дашборде
«Обзор». История только пополняется: перерисовкой графика занимается дашборд
и только по кнопке.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from config import CHECK_SITES
from core.health_checker import CheckResult, CheckStatus, HealthChecker
from core.ping_history import PingHistory
from gui.pages.base import Page, WindowApi
from gui.widgets import ModernCard, SiteCheckRow

log = logging.getLogger(__name__)


class HealthPage(Page):
    """Карточка «Проверка доступности»."""

    #: Ошибка операции: (заголовок, сообщение).
    failed = pyqtSignal(str, str)
    #: Результаты проверки ушли в историю пингов (график ждёт кнопки).
    history_updated = pyqtSignal()

    #: Ключ цвета темы для каждого статуса проверки: зелёный для OK (в том
    #: числе 401/403), красный для FAILED/ERROR, жёлтый для TIMEOUT.
    _STATUS_COLOR_KEYS = {
        CheckStatus.OK: "success",
        CheckStatus.FAILED: "error",
        CheckStatus.TIMEOUT: "warning",
        CheckStatus.ERROR: "error",
    }

    def __init__(
        self,
        window: WindowApi,
        health_checker: HealthChecker,
        ping_history: PingHistory | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(window, "Проверка связи", parent=parent)
        self.health_checker = health_checker
        #: История пингов для графика на дашборде. Объект общий с «Обзором»:
        #: страница только пополняет её, рисует график дашборд.
        self.ping_history = ping_history if ping_history is not None else PingHistory()
        self._checking = False
        self._summary_color = "muted"
        #: Последние результаты по именам сайтов — чтобы перекрасить строки
        #: при смене темы, не теряя конкретную причину из ``detail``.
        self._results: dict[str, CheckResult] = {}

        # Шапки-подсказки у карточки нет: статус и причина видны прямо в
        # строках сайтов.
        self.card = ModernCard("Проверка доступности")
        self._build()
        self.add_card(self.card)
        self.finish_layout()
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.site_rows: list[SiteCheckRow] = []
        for name, url in CHECK_SITES:
            row = SiteCheckRow(name, url)
            self._clear_time(row)
            self.site_rows.append(row)
            self.card.add_widget(row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.check_button = self.make_button("Проверить", "primary")
        self.check_button.clicked.connect(self.on_check_sites)
        buttons.addWidget(self.check_button)

        self.summary_label = QLabel("Проверка не запускалась")
        self.summary_label.setObjectName("Muted")
        buttons.addWidget(self.summary_label, 1)
        self.card.add_layout(buttons)

    @staticmethod
    def _clear_time(row: SiteCheckRow) -> None:
        """Убирает время отклика из строки, сохраняя место под него.

        Метка времени в :class:`SiteCheckRow` имеет фиксированную ширину,
        поэтому пустой текст оставляет правый край строки на месте —
        выравнивание и вид карточки не меняются.
        """
        row.time_label.setText("")

    # ------------------------------------------------------------------
    #  Проверка
    # ------------------------------------------------------------------
    def on_check_sites(self) -> None:
        if self._checking:
            return
        self._checking = True
        self.check_button.setEnabled(False)
        self.summary_label.setText("Проверка...")
        self._set_summary_color("muted")
        for row in self.site_rows:
            row.set_pending()
            self._clear_time(row)
            self._style_detail(row, "muted")

        self.window.run_async(
            self.health_checker.check_all,
            on_success=self._on_checked,
            on_error=self._on_error,
            busy_message="Проверка доступности сайтов...",
        )

    def _end_check(self) -> None:
        self._checking = False
        self.check_button.setEnabled(True)

    def start_check(self) -> None:
        """Запускает проверку доступности сайтов (быстрое действие дашборда).

        Обёртка над :meth:`on_check_sites`: повторный запуск во время уже
        идущей проверки страница игнорирует сама.
        """
        self.on_check_sites()

    def _on_error(self, message: str) -> None:
        self._end_check()
        for row in self.site_rows:
            row.detail_label.setText("ошибка проверки")
            self._style_detail(row, "error")
        self.summary_label.setText("Ошибка проверки")
        self._set_summary_color("error")
        self.failed.emit("Проверка доступности", message)

    def _on_checked(self, results: object) -> None:
        self._end_check()
        if not isinstance(results, list):
            self._on_error("Неожиданный результат проверки.")
            return

        checked = [r for r in results if isinstance(r, CheckResult)]
        by_name: dict[str, CheckResult] = {r.name: r for r in checked}
        self._results = by_name
        for row in self.site_rows:
            result = by_name.get(row.site_name)
            if result is not None:
                # В строке показываются и статус, и конкретная причина
                # («Доступен · HTTP 403», «Таймаут · Timeout 10s»), а не
                # общая фраза.
                row.set_result(result)
                # Время отклика в интерфейсе не показываем (см. _clear_time).
                self._clear_time(row)
                detail = self._detail_text(result)
                row.detail_label.setText(detail)
                row.detail_label.setToolTip(f"{result.url}\n{detail}")
                self._style_detail(row, self._detail_color_key(result))

        self._remember_history(checked)

        available = sum(1 for r in by_name.values() if r.ok)
        total = len(by_name)
        text = f"Доступно {available} из {total}"
        color_key = "success" if available == total and total else "warning"
        if available == 0 and total:
            color_key = "error"
        self.summary_label.setText(text)
        self._set_summary_color(color_key)
        self.window.set_status(f"Проверка доступности: {text}", 6000)
        log.info("Проверка доступности: %s", text)

    def _remember_history(self, results: list[CheckResult]) -> None:
        """Складывает результаты проверки в историю пингов (для графика).

        График на дашборде по сигналу не перерисовывается: он ждёт кнопки
        «Обновить график» (см. :meth:`OverviewPage.on_history_updated`).
        """
        if not results:
            return
        added = self.ping_history.add_results(results)
        self.history_updated.emit()
        log.debug("В историю пингов добавлено замеров: %d", len(added))

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    @classmethod
    def _detail_color_key(cls, result: CheckResult) -> str:
        """Цвет причины: зелёный для OK (в том числе 403), красный, жёлтый."""
        return cls._STATUS_COLOR_KEYS.get(result.status, "muted")

    @staticmethod
    def _detail_text(result: CheckResult) -> str:
        """Статус и причина одной строкой: ``Доступен · HTTP 403``."""
        label = result.status.label
        reason = (result.detail or "").strip()
        if not reason or reason == label:
            return label
        return f"{label} · {reason}"

    def _style_detail(self, row: SiteCheckRow, color_key: str) -> None:
        """Красит текст причины в строке сайта цветом активной темы."""
        color = self.colors().get(color_key, color_key)
        row.detail_label.setStyleSheet(f"color: {color};")

    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.card.refresh_theme()
        for row in self.site_rows:
            row.refresh_theme()
            # ``row.refresh_theme`` возвращает подпись к «muted» — возвращаем
            # цвет статуса последнего результата.
            result = self._results.get(row.site_name)
            self._style_detail(
                row, self._detail_color_key(result) if result else "muted"
            )
        self._set_summary_color(self._summary_color)

    def _set_summary_color(self, key: str) -> None:
        """Красит итог проверки цветом темы (``muted``, ``success`` и т. д.)."""
        self._summary_color = key
        self.summary_label.setStyleSheet(f"color: {self.colors().get(key, key)};")
