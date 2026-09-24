"""Стартовая страница «Обзор» (дашборд).

Первый экран приложения: сводка по системе четырьмя плитками.

* **Служба** — большая плитка на всю ширину: крупная иконка, состояние
  («Работает» / «Остановлена» / «Не установлена»), подсказка и кнопки
  управления. Фон плитки меняется по состоянию: зелёный оттенок при работе,
  красный при остановке, серый, если служба не установлена.
* **Текущая стратегия** — название стратегии, с которой установлена служба,
  и кнопка «Сменить» (переход в раздел «Стратегии»).
* **Быстрые действия** — тестирование всех стратегий, проверка связи и
  обновление запрета. Каждое действие сначала показывает нужную страницу,
  а затем просит главное окно запустить операцию.
* **ПИНГ** — карточка во всю ширину внизу дашборда: график пинга (сплошная
  линия с точками) и потерь (красные метки на нуле) по истории проверок
  связи. Точек ровно столько, сколько было замеров: один раунд проверки даёт
  по точке на каждый сайт, поэтому линия появляется сразу после первого
  нажатия «Проверить связь». Ось Y зафиксирована на 0–500 мс — шкала не
  «прыгает» от замера к замеру. История живёт в
  :class:`core.ping_history.PingHistory`, а график перерисовывается **только
  по кнопке** «Обновить график»: проверка идёт в фоне, и график, меняющийся
  сам по себе, только отвлекал бы.

Навигацию страница выполняет сигналами (:attr:`OverviewPage.navigate_to` и
:attr:`OverviewPage.quick_action`), а не прямым переключением стека: страницы
не знают друг о друге. Состояние приходит от главного окна
(:meth:`OverviewPage.set_service_state`, ``set_current_strategy``), поэтому
своих фоновых задач у страницы нет — она только показывает данные.
"""

from __future__ import annotations

import logging
from datetime import datetime

from PyQt6.QtCore import QDateTime, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import (
    HEALTH_PAGE_INDEX,
    STRATEGIES_PAGE_INDEX,
    TESTING_PAGE_INDEX,
    UPDATE_PAGE_INDEX,
)
from core.ping_history import PingHistory
from core.service_manager import ServiceState
from gui.icons import make_pixmap
from gui.pages.base import Page, WindowApi
from gui.theme import Theme
from gui.widgets import ModernCard

# График пинга требует отдельного пакета PyQt6-Charts. Его отсутствие не
# должно ронять приложение: карточка «ПИНГ» просто покажет подсказку.
try:
    from PyQt6.QtCharts import (
        QChart,
        QChartView,
        QDateTimeAxis,
        QLineSeries,
        QValueAxis,
    )

    CHARTS_AVAILABLE = True
    CHARTS_ERROR = ""
except ImportError as exc:  # линия графика без PyQt6-Charts — не беда
    QChart = QChartView = QDateTimeAxis = QLineSeries = QValueAxis = None  # type: ignore[assignment,misc]
    CHARTS_AVAILABLE = False
    CHARTS_ERROR = str(exc)

# Круглые метки потерь рисует QScatterSeries. Пакет тот же, но серия может
# отсутствовать в урезанной сборке — тогда потери покажет линия с точками.
try:
    from PyQt6.QtCharts import QScatterSeries
except ImportError:  # pragma: no cover — зависит от сборки PyQt6-Charts
    QScatterSeries = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

#: Размер крупной иконки щита на плитке службы, px.
SHIELD_ICON_SIZE = 48

#: Отступ от краёв плиток дашборда, px. Его даёт сама плитка, поэтому у
#: layout кнопок «Быстрых действий» отступы нулевые (иначе было бы 24 px).
TILE_MARGIN = 12

#: Шаг между кнопками «Быстрых действий», px.
QUICK_TILE_SPACING = 8

#: Минимальная высота области графика пинга, px.
CHART_MIN_HEIGHT = 260

#: Сколько последних точек рисует график (история хранится целиком).
CHART_POINT_LIMIT = 1000

#: Толщина линий графика, px.
CHART_LINE_WIDTH = 2

#: Размер маркера точки пинга, px. Точки видны всегда: даже один раунд
#: проверки — это несколько точек, по которым уже можно судить о линии.
CHART_PING_MARKER_SIZE = 6

#: Размер маркера точки потери, px.
CHART_LOSS_MARKER_SIZE = 8

#: Сколько подписей показывать на осях графика.
CHART_TICK_COUNT = 6

#: Верхняя граница оси Y, мс. Шкала фиксированная: пинг выше просто
#: обрезается по верху, зато графики разных раундов сравнимы между собой.
CHART_Y_MAX = 500.0

#: Полуширина окна оси X, если точек мало, секунды.
CHART_MIN_SPAN_SEC = 30

#: Подпись вместо графика, когда проверок ещё не было.
CHART_EMPTY_TEXT = "Нет данных. Нажмите «Проверить связь» на странице «Проверка связи»."

#: Подпись вместо графика, когда не установлен PyQt6-Charts.
CHART_MISSING_TEXT = (
    "График недоступен: установите PyQt6-Charts (pip install PyQt6-Charts)."
)

#: Подсказка под состоянием службы: что значит текущее состояние.
_STATE_HINTS: dict[ServiceState, str] = {
    ServiceState.RUNNING: "Обход блокировок активен. Служба запущена автоматически.",
    ServiceState.STOPPED: "Служба установлена, но не запущена — обход не работает.",
    ServiceState.NOT_INSTALLED: "Служба не установлена. Выберите стратегию и установите её.",
    ServiceState.UNKNOWN: "Состояние службы проверяется...",
}

#: Ключи темы с фоном плитки состояния (серый — общий для обеих тем).
_STATE_BG_KEYS: dict[ServiceState, str] = {
    ServiceState.RUNNING: "success_soft",
    ServiceState.STOPPED: "error_soft",
    ServiceState.NOT_INSTALLED: "neutral_soft",
    ServiceState.UNKNOWN: "neutral_soft",
}

#: Фон плитки, если в палитре темы нет мягкого оттенка.
_STATE_BG_FALLBACKS: dict[ServiceState, str] = {
    ServiceState.RUNNING: "rgba(34, 197, 94, 0.10)",
    ServiceState.STOPPED: "rgba(239, 68, 68, 0.10)",
    ServiceState.NOT_INSTALLED: "rgba(136, 136, 136, 0.08)",
    ServiceState.UNKNOWN: "rgba(136, 136, 136, 0.08)",
}


def _line_pen(color: str) -> QPen:
    """Перо линии графика: сплошная линия толщиной CHART_LINE_WIDTH."""
    pen = QPen(QColor(color))
    pen.setWidth(CHART_LINE_WIDTH)
    pen.setStyle(Qt.PenStyle.SolidLine)
    return pen


def _to_msecs(moment: datetime) -> float:
    """Метка времени в миллисекундах — в таком виде её понимает QDateTimeAxis."""
    return float(QDateTime(moment).toMSecsSinceEpoch())


class OverviewPage(Page):
    """Дашборд: состояние службы, текущая стратегия и быстрые действия."""

    #: Переход на страницу с указанным индексом (см. config.*_PAGE_INDEX).
    navigate_to = pyqtSignal(int)
    #: Быстрое действие: ``test_all``, ``check_health`` или ``update``.
    quick_action = pyqtSignal(str)
    #: Операция со службой: ``start``, ``stop`` или ``restart``.
    service_action = pyqtSignal(str)

    def __init__(self, window: WindowApi, parent: QWidget | None = None) -> None:
        # Нижний отступ — под тень нижних плиток: иначе прокрутка её срезает.
        super().__init__(window, "Обзор", parent=parent, margins=(4, 2, 16, 16))

        self._state: ServiceState = ServiceState.UNKNOWN
        self._strategy_name: str | None = None
        self._busy = False
        #: История пингов (общий объект со страницей «Проверка связи»).
        self.ping_history: PingHistory | None = None
        #: Точки, уже показанные на графике: по ним график перекрашивается
        #: при смене темы, не заглядывая в историю (иначе смена темы
        #: «подтянула» бы новые данные в обход кнопки «Обновить график»).
        #: Пинг хранится сегментами — линия рвётся на потерях.
        self._drawn_segments: list[list[tuple[datetime, float]]] = []
        #: Показанные точки потерь: ``(время, 0.0)``.
        self._drawn_losses: list[tuple[datetime, float]] = []
        #: Серии графика: пинг (по одной на непрерывный участок).
        self._ping_series: list = []
        #: Серия красных меток потерь (одна на весь график).
        self._loss_series: list = []

        self._build()
        self.finish_layout()
        self.refresh_theme()

    # ==================================================================
    #  Построение
    # ==================================================================
    def _build(self) -> None:
        self.content_layout.setSpacing(12)

        self.status_card = ModernCard("")
        self.strategy_card = ModernCard("")
        self.quick_card = ModernCard("")

        for card in (self.status_card, self.strategy_card, self.quick_card):
            self._make_compact(card)

        self._build_status_tile(self.status_card)
        self._build_strategy_tile(self.strategy_card)
        self._build_quick_tile(self.quick_card)

        self.add_card(self.status_card)

        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addWidget(self.strategy_card, 1)
        bottom.addWidget(self.quick_card, 1)
        self.content_layout.addLayout(bottom)

        # График пинга — отдельная карточка во всю ширину в самом низу.
        self._build_chart_tile()
        self.add_card(self.chart_card)

    @staticmethod
    def _make_compact(card: ModernCard) -> None:
        """Делает плитку компактной: без пустой шапки, отступы 12 px, шаг 8 px.

        Заголовок плитки — обычная подпись внутри содержимого (``TileCaption``),
        поэтому служебная шапка карточки скрывается: иначе она добавляла бы
        сверху пустую строку и лишний зазор.
        """
        card.title_label.setVisible(False)
        outer = card.layout()
        if outer is not None:
            outer.setContentsMargins(TILE_MARGIN, TILE_MARGIN, TILE_MARGIN, TILE_MARGIN)
            outer.setSpacing(0)
        card.body.setSpacing(8)

    # ------------------------------------------------------------------
    #  Плитка 1: служба
    # ------------------------------------------------------------------
    def _build_status_tile(self, card: ModernCard) -> None:
        card.setObjectName("StatusTile")
        body = card.body
        body.setSpacing(8)

        self._add_centered(body, self._caption("СЛУЖБА"))

        self.status_icon = QLabel()
        self.status_icon.setObjectName("TileIcon")
        self.status_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._add_centered(body, self.status_icon)

        self.status_label = QLabel(ServiceState.UNKNOWN.label)
        self.status_label.setObjectName("TileStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._add_centered(body, self.status_label)

        self.status_hint = QLabel(_STATE_HINTS[ServiceState.UNKNOWN])
        self.status_hint.setObjectName("Muted")
        self.status_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_hint.setWordWrap(True)
        body.addWidget(self.status_hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        self.start_button = self._tile_button(
            "Запустить", "primary", "Запустить службу zapret"
        )
        self.start_button.clicked.connect(lambda: self._service("start"))
        self.stop_button = self._tile_button("Остановить", None, "Остановить службу zapret")
        self.stop_button.clicked.connect(lambda: self._service("stop"))
        self.restart_button = self._tile_button(
            "Перезапустить", None, "Перезапустить службу zapret"
        )
        self.restart_button.clicked.connect(lambda: self._service("restart"))

        for button in (self.start_button, self.stop_button, self.restart_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        body.addLayout(buttons)

        self._apply_state()

    # ------------------------------------------------------------------
    #  Плитка 2: текущая стратегия
    # ------------------------------------------------------------------
    def _build_strategy_tile(self, card: ModernCard) -> None:
        body = card.body

        self._add_centered(body, self._caption("ТЕКУЩАЯ СТРАТЕГИЯ"))

        self.strategy_label = QLabel("—")
        self.strategy_label.setObjectName("TileValue")
        self.strategy_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.strategy_label.setWordWrap(True)
        body.addWidget(self.strategy_label)

        self.change_strategy_button = self._tile_button(
            "Сменить", None, "Открыть раздел «Стратегии»"
        )
        self.change_strategy_button.clicked.connect(self._change_strategy)
        body.addWidget(self.change_strategy_button, 0, Qt.AlignmentFlag.AlignHCenter)

        # Растяжка в конце (а не между элементами): содержимое прижато к верху,
        # а сама плитка выравнивается по высоте с соседней.
        body.addStretch(1)

    # ------------------------------------------------------------------
    #  Плитка 3: быстрые действия
    # ------------------------------------------------------------------
    def _build_quick_tile(self, card: ModernCard) -> None:
        body = card.body

        self._add_centered(body, self._caption("БЫСТРЫЕ ДЕЙСТВИЯ"))

        self.test_all_button = self._tile_button(
            "Тестировать все",
            "primary",
            "Перебрать все стратегии своим тестером (3-5 минут)",
        )
        self.test_all_button.clicked.connect(lambda: self._quick("test_all"))

        self.health_button = self._tile_button(
            "Проверить связь", None, "Проверить доступность сайтов из списка проверки"
        )
        self.health_button.clicked.connect(lambda: self._quick("check_health"))

        self.update_button = self._tile_button(
            "Обновить", None, "Проверить обновления запрета на GitHub"
        )
        self.update_button.clicked.connect(lambda: self._quick("update"))

        # Кнопки идут столбцом и растягиваются на всю ширину плитки: так они
        # занимают одинаковую долю ширины и не «висят» по центру. Отступ в
        # 12 px по краям (TILE_MARGIN) даёт сама плитка, поэтому у layout он
        # нулевой — иначе кнопки отошли бы от краёв на 24 px.
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(QUICK_TILE_SPACING)
        for button in (
            self.test_all_button,
            self.health_button,
            self.update_button,
        ):
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            buttons.addWidget(button)
        body.addLayout(buttons)

        body.addStretch(1)

        self.test_all_button.setEnabled(self._zapret_available())

    # ------------------------------------------------------------------
    #  Вспомогательное
    # ------------------------------------------------------------------
    def _zapret_available(self) -> bool:
        """Найдена ли папка запрета: без неё тестирование недоступно.

        Путь берётся у главного окна: он хранит его в настройках и меняет при
        смене папки запрета.
        """
        checker = getattr(self.window, "zapret_available", None)
        if callable(checker):
            return bool(checker())
        return getattr(self.window, "zapret_path", None) is not None

    # ------------------------------------------------------------------
    #  Плитка 4: график пинга
    # ------------------------------------------------------------------
    def _build_chart_tile(self) -> None:
        """Карточка «ПИНГ» во всю ширину: график пинга и потерь.

        Данные в неё приходят из :class:`core.ping_history.PingHistory`
        (общий объект со страницей «Проверка связи»), но рисуются только по
        нажатию кнопки «Обновить график» — см. :meth:`refresh_chart`.
        """
        self.chart_card = ModernCard("ПИНГ")
        self.chart = None
        self.chart_view = None
        self.axis_x = None
        self.axis_y = None

        self.refresh_chart_button = Page.make_button(
            "Обновить график",
            None,
            tooltip="Перерисовать график по накопленной истории проверок связи",
        )
        self.refresh_chart_button.clicked.connect(self.refresh_chart)
        self.chart_card.add_header_widget(self.refresh_chart_button)

        # Подпись вместо графика: пока проверок не было (или не установлен
        # PyQt6-Charts) области графика показывать нечего.
        self.chart_placeholder = QLabel("")
        self.chart_placeholder.setObjectName("Muted")
        self.chart_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chart_placeholder.setWordWrap(True)
        self.chart_placeholder.setMinimumHeight(CHART_MIN_HEIGHT)
        self.chart_card.body.addWidget(self.chart_placeholder)

        if CHARTS_AVAILABLE:
            self._build_chart()
            self._set_placeholder(CHART_EMPTY_TEXT)
        else:
            log.warning(
                "PyQt6-Charts не установлен — график пинга недоступен: %s", CHARTS_ERROR
            )
            self._set_placeholder(CHART_MISSING_TEXT)

        self.update_refresh_button()

    def _build_chart(self) -> None:
        """Создаёт QChart с осями времени и миллисекунд (один раз за сеанс)."""
        chart = QChart()
        # Без анимации: график перерисовывается по кнопке, и «доезжающие»
        # линии только мешали бы читать значения.
        chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        chart.setBackgroundRoundness(0)

        legend = chart.legend()
        if legend is not None:
            legend.setVisible(True)
            legend.setAlignment(Qt.AlignmentFlag.AlignBottom)

        self.axis_x = QDateTimeAxis()
        self.axis_x.setFormat("hh:mm:ss")
        self.axis_x.setTickCount(CHART_TICK_COUNT)
        self.axis_x.setTitleText("Время")
        chart.addAxis(self.axis_x, Qt.AlignmentFlag.AlignBottom)

        self.axis_y = QValueAxis()
        self.axis_y.setLabelFormat("%d")
        self.axis_y.setTickCount(CHART_TICK_COUNT)
        self.axis_y.setTitleText("мс")
        # Шкала фиксированная: 0, 100, 200, 300, 400, 500.
        self.axis_y.setRange(0.0, CHART_Y_MAX)
        chart.addAxis(self.axis_y, Qt.AlignmentFlag.AlignLeft)

        self.chart = chart
        self.chart_view = QChartView(chart)
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Фон рисует сама карточка: у QChartView он должен быть прозрачным,
        # иначе внутри плитки появился бы прямоугольник цвета страницы.
        self.chart_view.setStyleSheet("background: transparent; border: none;")
        self.chart_view.setMinimumHeight(CHART_MIN_HEIGHT)
        self.chart_view.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.chart_card.body.addWidget(self.chart_view)

    # ------------------------------------------------------------------
    #  Данные графика
    # ------------------------------------------------------------------
    def set_ping_history(self, history: PingHistory | None) -> None:
        """Принимает историю пингов (её ведёт страница «Проверка связи»)."""
        self.ping_history = history
        self.update_refresh_button()

    def on_history_updated(self) -> None:
        """Проверка связи пополнила историю — график ждёт кнопки.

        Сам график здесь намеренно не перерисовывается: проверка идёт в фоне,
        и меняющийся без спроса график только отвлекал бы.
        """
        was_empty = not self._drawn_segments and not self._drawn_losses
        self.update_refresh_button()
        if CHARTS_AVAILABLE and was_empty and self._has_data():
            self.chart_card.set_hint(
                "Данные проверки готовы — нажмите «Обновить график»."
            )

    def update_refresh_button(self) -> None:
        """Кнопка «Обновить график» активна, только если есть что рисовать."""
        self.refresh_chart_button.setEnabled(CHARTS_AVAILABLE and self._has_data())

    def _has_data(self) -> bool:
        return self.ping_history is not None and len(self.ping_history) > 0

    def _history_segments(self) -> list[list[tuple[datetime, float]]]:
        """Участки пинга из истории (история при этом не режется)."""
        if self.ping_history is None:
            return []
        return self.ping_history.chart_segments(CHART_POINT_LIMIT)

    def _history_losses(self) -> list[tuple[datetime, float]]:
        """Точки потерь из истории: ``(время, 0.0)``."""
        if self.ping_history is None:
            return []
        return self.ping_history.loss_points(CHART_POINT_LIMIT)

    def refresh_chart(self) -> None:
        """Перерисовывает график по накопленной истории (только по кнопке)."""
        if not CHARTS_AVAILABLE or self.chart is None:
            return

        segments = self._history_segments()
        losses = self._history_losses()
        if not segments and not losses:
            self._drawn_segments = []
            self._drawn_losses = []
            self._clear_series()
            self._set_placeholder(CHART_EMPTY_TEXT)
            self.chart_card.set_hint("")
            self.update_refresh_button()
            return

        self._draw(segments, losses)
        self._show_chart()
        self.update_refresh_button()
        shown = sum(len(segment) for segment in segments) + len(losses)
        stamps = [stamp for segment in segments for stamp, _ in segment]
        stamps += [stamp for stamp, _ in losses]
        self.chart_card.set_hint(
            f"Точек: {shown} · последняя проверка {max(stamps).strftime('%H:%M:%S')}"
        )
        self.window.set_status(f"График пинга обновлён (точек: {shown})", 4000)

    # ------------------------------------------------------------------
    #  Отрисовка графика
    # ------------------------------------------------------------------
    def _draw(
        self,
        segments: list[list[tuple[datetime, float]]],
        losses: list[tuple[datetime, float]],
    ) -> None:
        """Строит линии пинга и красные метки потерь.

        Пинг — сплошная линия с точками, по одной серии на непрерывный
        участок: ``QLineSeries`` разрывов не умеет, а на потерях линия обязана
        рваться. Потери — красные метки на уровне нуля.
        """
        if self.chart is None:
            return
        self._drawn_segments = [list(segment) for segment in segments]
        self._drawn_losses = list(losses)
        self._clear_series()

        ping_pen = _line_pen(Theme.color("chart_line", Theme.color("accent")))
        loss_color = Theme.color("chart_loss", Theme.color("error"))

        for index, segment in enumerate(segments):
            self._ping_series.append(
                self._add_ping_series(ping_pen, segment, hide_legend=bool(index))
            )
        if not segments:
            # Все сайты не ответили: линии пинга нет, но элемент легенды
            # «Пинг» остаётся на месте — пустая серия ничего не рисует.
            self._ping_series.append(self._add_ping_series(ping_pen, []))

        self._loss_series.append(self._add_loss_series(loss_color, losses))

        self._update_axes(segments, losses)

    def _add_ping_series(
        self,
        pen: QPen,
        points: list[tuple[datetime, float]],
        *,
        hide_legend: bool = False,
    ):
        """Добавляет линию пинга и привязывает её к осям."""
        series = QLineSeries()
        series.setName("Пинг")
        series.setPen(pen)
        # Точки видны всегда: даже сегмент из одной точки должен быть заметен.
        series.setPointsVisible(True)
        series.setMarkerSize(CHART_PING_MARKER_SIZE)
        for moment, ping in points:
            series.append(_to_msecs(moment), max(0.0, float(ping)))
        self.chart.addSeries(series)
        series.attachAxis(self.axis_x)
        series.attachAxis(self.axis_y)
        if hide_legend:
            self._hide_legend_marker(series)
        return series

    def _add_loss_series(self, color: str, points: list[tuple[datetime, float]]):
        """Добавляет красные метки потерь на уровне Y=0.

        Предпочитается :class:`QScatterSeries` (круглые маркеры); если её нет
        в сборке PyQt6-Charts, потери рисует обычная линия с точками.
        """
        if QScatterSeries is not None:
            series = QScatterSeries()
            series.setMarkerSize(CHART_LOSS_MARKER_SIZE)
            series.setColor(QColor(color))
            series.setBorderColor(QColor(color))
        else:  # pragma: no cover — зависит от сборки PyQt6-Charts
            series = QLineSeries()
            series.setPen(_line_pen(color))
            series.setPointsVisible(True)
            series.setMarkerSize(CHART_LOSS_MARKER_SIZE)
        series.setName("Потери")
        for moment, value in points:
            series.append(_to_msecs(moment), float(value))
        self.chart.addSeries(series)
        series.attachAxis(self.axis_x)
        series.attachAxis(self.axis_y)
        return series

    def _hide_legend_marker(self, series) -> None:
        """Прячет лишний элемент легенды (у разорванной линии их много)."""
        legend = self.chart.legend() if self.chart is not None else None
        if legend is None:
            return
        try:
            for marker in legend.markers(series):
                marker.setVisible(False)
        except (AttributeError, TypeError):  # pragma: no cover — версия Qt
            log.debug("Не удалось скрыть элемент легенды", exc_info=True)

    def _clear_series(self) -> None:
        """Убирает линии графика (QChart владеет ими и удаляет их сам)."""
        if self.chart is not None:
            self.chart.removeAllSeries()
        # Ссылки на удалённые серии держать нельзя: обращение к ним уронило бы
        # приложение, поэтому просто забываем их.
        self._ping_series = []
        self._loss_series = []

    def _update_axes(
        self,
        segments: list[list[tuple[datetime, float]]],
        losses: list[tuple[datetime, float]],
    ) -> None:
        """Настраивает оси: время — по точкам, миллисекунды — всегда 0–500."""
        if self.axis_x is None or self.axis_y is None:
            return

        stamps = [stamp for segment in segments for stamp, _ in segment]
        stamps += [stamp for stamp, _ in losses]
        if stamps:
            coords = [_to_msecs(stamp) for stamp in stamps]
            first, last = min(coords), max(coords)
            span = last - first
            half = CHART_MIN_SPAN_SEC * 1000.0
            if len(stamps) < 3 or span < half * 2:
                # Один раунд проверки укладывается в доли секунды: с реальным
                # разбросом оси подписи «hh:mm:ss» слиплись бы в одну. Поэтому
                # окно расширяется до CHART_MIN_SPAN_SEC в обе стороны.
                center = (first + last) / 2.0
                first, last = center - half, center + half
                span = last - first
            padding = max(1000.0, span * 0.02)
            self.axis_x.setRange(
                QDateTime.fromMSecsSinceEpoch(int(first - padding)),
                QDateTime.fromMSecsSinceEpoch(int(last + padding)),
            )

        # Шкала Y фиксированная: пинг выше 500 мс обрезается по верху.
        self.axis_y.setRange(0.0, CHART_Y_MAX)

    # ------------------------------------------------------------------
    #  Состояния карточки графика
    # ------------------------------------------------------------------
    def _set_placeholder(self, text: str) -> None:
        """Показывает подпись вместо графика (нет данных / нет PyQt6-Charts)."""
        self.chart_placeholder.setText(text)
        self.chart_placeholder.setVisible(True)
        if self.chart_view is not None:
            self.chart_view.setVisible(False)

    def _show_chart(self) -> None:
        """Показывает область графика, прячет подпись."""
        self.chart_placeholder.setVisible(False)
        if self.chart_view is not None:
            self.chart_view.setVisible(True)

    # ------------------------------------------------------------------
    #  Мелкие помощники
    # ------------------------------------------------------------------
    @staticmethod
    def _add_centered(layout: QVBoxLayout, widget: QWidget) -> None:
        """Добавляет виджет по центру плитки (ширина — по его размеру)."""
        layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)

    @staticmethod
    def _caption(text: str) -> QLabel:
        """Заголовок плитки: сверху по центру, приглушённый (QSS TileCaption)."""
        label = QLabel(text)
        label.setObjectName("TileCaption")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label

    @staticmethod
    def _tile_button(text: str, variant: str | None, tooltip: str) -> QPushButton:
        button = Page.make_button(text, variant, tooltip=tooltip)
        button.setObjectName("TileAction")
        return button

    # ==================================================================
    #  Действия
    # ==================================================================
    def _service(self, action: str) -> None:
        """Просит главное окно выполнить операцию со службой."""
        if self._busy:
            return
        self.service_action.emit(action)

    def _change_strategy(self) -> None:
        """Открывает раздел «Стратегии» (выбор и установку делает он)."""
        self.navigate_to.emit(STRATEGIES_PAGE_INDEX)

    def _quick(self, action: str) -> None:
        """Быстрое действие: проверяет окружение и отдаёт работу окну."""
        if action == "test_all" and not self._zapret_available():
            QMessageBox.information(
                self,
                "Тестирование стратегий",
                "Запрет не найден, тестирование недоступно.\n\n"
                "Укажите папку запрета в разделе «Настройки».",
            )
            return
        self.quick_action.emit(action)

    def set_busy(self, busy: bool) -> None:
        """Блокирует кнопки плитки службы, пока операция выполняется."""
        self._busy = busy
        self.update_buttons()

    def update_buttons(self) -> None:
        """Включает только те кнопки, которые имеют смысл для состояния."""
        if self._busy:
            for button in (self.start_button, self.stop_button, self.restart_button):
                button.setEnabled(False)
            return

        running = self._state.is_running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        # Для неустановленной службы «Перезапустить» означает «установить»:
        # окно подставит стратегию, и кнопка остаётся активной.
        self.restart_button.setEnabled(True)

    # ==================================================================
    #  Данные из главного окна
    # ==================================================================
    def set_service_state(self, state: ServiceState) -> None:
        """Применяет состояние службы, полученное главным окном."""
        self._state = state
        self._apply_state()

    def set_current_strategy(self, name: str | None) -> None:
        """Показывает стратегию, с которой установлена служба (``None`` — нет)."""
        self._strategy_name = name
        self._apply_strategy()

    def reset(self) -> None:
        """Возвращает плитки к исходному состоянию (данных ещё нет)."""
        self._strategy_name = None
        self._apply_strategy()

    def _apply_state(self) -> None:
        """Перекрашивает плитку службы под текущее состояние."""
        state = self._state
        colors = Theme.colors()
        background = colors.get(
            _STATE_BG_KEYS.get(state, "neutral_soft"),
            _STATE_BG_FALLBACKS.get(state, "rgba(136, 136, 136, 0.08)"),
        )
        self.status_card.set_accent(background)
        self.status_label.setText(state.label)
        self.status_label.setStyleSheet(f"color: {state.color};")
        self.status_hint.setText(_STATE_HINTS.get(state, _STATE_HINTS[ServiceState.UNKNOWN]))
        self.update_buttons()

    def _apply_strategy(self) -> None:
        """Показывает название стратегии (в полной ширине плитки)."""
        name = (self._strategy_name or "").strip()
        if name:
            self.strategy_label.setText(name)
            self.strategy_label.setToolTip(name)
            self.strategy_label.setStyleSheet(f"color: {Theme.color('text')};")
        else:
            self.strategy_label.setText("Стратегия не выбрана")
            self.strategy_label.setToolTip(
                "Служба не установлена или стратегия не определена."
            )
            self.strategy_label.setStyleSheet(f"color: {Theme.color('muted')};")
        self.change_strategy_button.setEnabled(True)

    def refresh_tiles(self) -> None:
        """Перерисовывает плитки после внешних изменений (тест, обновление)."""
        self._apply_state()
        self._apply_strategy()

    # ==================================================================
    #  Тема
    # ==================================================================
    def refresh_theme(self) -> None:
        """Перекрашивает плитки под активную тему (смена темы в окне)."""
        super().refresh_theme()
        self.status_icon.setPixmap(
            make_pixmap("shield", SHIELD_ICON_SIZE, Theme.color("accent"))
        )
        for card in (
            self.status_card,
            self.strategy_card,
            self.quick_card,
            self.chart_card,
        ):
            card.refresh_theme()
        self._apply_chart_theme()
        self.refresh_tiles()
        log.debug("Плитки обзора перекрашены (тема «%s»)", Theme.label())

    def _apply_chart_theme(self) -> None:
        """Перекрашивает график под активную тему.

        Из истории данные здесь не читаются: перерисовываются только те точки,
        что уже показаны (``_drawn_segments`` / ``_drawn_losses``), иначе смена
        темы приносила бы на график новые проверки в обход кнопки «Обновить
        график».
        """
        self.chart_placeholder.setStyleSheet(f"color: {Theme.color('muted')};")
        if self.chart is None:
            return

        text = QColor(Theme.color("text"))
        grid = QColor(Theme.color("chart_grid", Theme.color("border")))
        self.chart.setBackgroundBrush(QBrush(QColor(Theme.color("surface"))))
        self.chart.setBackgroundPen(QPen(Qt.PenStyle.NoPen))
        self.chart.setPlotAreaBackgroundVisible(False)

        for axis in (self.axis_x, self.axis_y):
            if axis is None:
                continue
            axis.setGridLineColor(grid)
            axis.setLinePenColor(grid)
            axis.setLabelsColor(text)
            axis.setTitleBrush(QBrush(text))

        legend = self.chart.legend()
        if legend is not None:
            legend.setLabelColor(text)

        if self._drawn_segments or self._drawn_losses:
            self._draw(self._drawn_segments, self._drawn_losses)
