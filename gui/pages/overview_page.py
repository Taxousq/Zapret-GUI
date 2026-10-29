"""Стартовая страница «Обзор» (дашборд).

Первый экран приложения: сводка по системе четырьмя плитками.

* **Служба** — большая плитка на всю ширину: крупная иконка, состояние
  («Работает» / «Остановлена» / «Не установлена») и кнопки управления.
  Главная (primary) кнопка меняется по состоянию службы, чтобы иерархия
  действий оставалась очевидной: при работающей службе это «Остановить»,
  при остановленной — «Запустить», у неустановленной — «Перезапустить»
  (в этом случае окно подставляет установку). Фон плитки меняется по
  состоянию: зелёный оттенок при работе, красный при остановке, серый,
  если служба не установлена.
* **Текущая стратегия** — название стратегии, с которой установлена служба,
  и кнопка «Сменить» (переход в раздел «Стратегии»).
* **Быстрые действия** — тестирование всех стратегий, проверка связи и
  обновление запрета. Каждое действие сначала показывает нужную страницу,
  а затем просит главное окно запустить операцию.
* **ПИНГ** — карточка во всю ширину внизу дашборда: график пинга (сплошная
  линия с точками) и потерь (красные метки на нуле) по истории проверок
  связи. Над графиком — сводка: последний, средний, минимальный и максимальный
  пинг плюс число потерь. Один раунд проверки даёт по точке на каждый сайт,
  поэтому точек на графике столько, сколько было замеров, а не раундов.
  Ось X переключается сама: пока замеров мало (один раунд), она подписана
  названиями сайтов, дальше — временем проверок. Ось Y подстраивается под
  данные с «круглым» шагом, уровень среднего показан пунктиром. Пока данных
  нет, вместо графика показывается компактная заглушка (текст + кнопка
  «Проверить связь»), а не пустая область в 260 px. История живёт в
  :class:`core.ping_history.PingHistory`, а график перерисовывается **только
  по кнопке** «Обновить график»: проверка идёт в фоне, и график, меняющийся
  сам по себе, только отвлекал бы.
* **Поддержать** — самая нижняя узкая карточка: короткий текст и кнопка,
  открывающая мини-диалог с адресами BTC и TON
  (:class:`gui.dialogs.support_dialog.SupportDialog`). Реквизитов страница не
  хранит — они лежат в самом диалоге, чтобы не расползались по коду.

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
from gui.dialogs.support_dialog import SupportDialog
from gui.icons import make_pixmap
from gui.pages.base import Page, WindowApi
from gui.theme import Theme, repolish
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

# Подписи оси X именами сайтов рисует QCategoryAxis — она тоже может
# отсутствовать в урезанной сборке, тогда ось остаётся числовой.
try:
    from PyQt6.QtCharts import QCategoryAxis
except ImportError:  # pragma: no cover — зависит от сборки PyQt6-Charts
    QCategoryAxis = None  # type: ignore[assignment,misc]

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

#: Минимальная высота заглушки вместо графика, px. Заглушка — это строка
#: текста и кнопка «Проверить связь»: держать под неё высоту графика (260 px)
#: незачем, карточка «ПИНГ» раздувалась бы на пустом месте.
CHART_PLACEHOLDER_MIN_HEIGHT = 80

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

#: Верхняя граница по умолчанию для оси Y, мс. Реальная граница считается по
#: данным (см. :func:`_nice_y_max`), но ниже этого значения не опускается:
#: шкала 0–40 мс при пинге 20 выглядела бы «пустой».
CHART_Y_MIN_MAX = 100.0

#: Полуширина окна оси X в режиме времени, если точек мало, секунды.
CHART_MIN_SPAN_SEC = 30

#: Порог переключения оси X в режим «номер замера», секунды. Пока вся история
#: укладывается в этот интервал, точки идут по номерам: на оси времени один
#: раунд проверки — это доли секунды, и он сливается в вертикальную черту.
CHART_TIME_MODE_MIN_SPAN_SEC = 120.0

#: Столько точек и меньше — всегда режим «номер замера»: на оси времени их
#: не разглядеть, даже если между ними прошло несколько минут.
CHART_INDEX_MODE_MAX_POINTS = 8

#: Столько точек и меньше подписываются на оси X именами сайтов (если имена
#: известны): «YouTube, Discord, …» понятнее, чем «0, 1, 2, …».
CHART_SITE_LABEL_MAX_POINTS = 6

#: Подпись вместо графика, когда проверок ещё не было. Короткая: действие
#: предлагает кнопка рядом, а не текст.
CHART_EMPTY_TEXT = "Проверок ещё не было."

#: Подпись вместо графика, когда не установлен PyQt6-Charts.
CHART_MISSING_TEXT = (
    "График недоступен: установите PyQt6-Charts (pip install PyQt6-Charts)."
)

#: Текст карточки «Поддержать».
SUPPORT_TEXT = "Нравится приложение? Поддержите разработку."

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


def _nice_y_scale(value: float) -> tuple[float, float]:
    """Верхняя граница оси Y и шаг делений под данные.

    Фиксированные 0–500 оставляли линию пинга 200 мс на середине пустой
    области, а «сырое» деление давало подписи вида 0, 60, 120… Шаг берётся из
    ряда 10/25/50/100/200 мс так, чтобы делений было не больше шести, а
    граница была ему кратна.
    """
    target = max(CHART_Y_MIN_MAX, float(value) * 1.2)
    for step in (10.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0, 1000.0):
        if target <= step * 5:
            return float(int((target + step - 1) // step) * step), step
    step = 1000.0
    return float(int((target + step - 1) // step) * step), step


def _short_site(site: str) -> str:
    """Короткая подпись сайта для оси X (длинные имена обрезаются)."""
    text = (site or "").strip()
    if len(text) <= 10:
        return text
    return text[:9] + "…"


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
        #: Хранятся все замеры подряд: ``None`` вместо пинга — потеря.
        self._drawn_points: list[tuple[datetime, float | None, str]] = []
        #: Ключ текущей оси X: ``(номера замеров?, подписи сайтов)``.
        #: ``None`` — ось ещё не пересоздавалась.
        self._x_axis_key: tuple[bool, tuple[str, ...]] | None = None
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

        # Карточка «Поддержать» — в самом низу страницы, под «ПИНГ».
        self._build_support_tile()
        self.add_card(self.support_card)

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

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        # Вариант оформления здесь не задаётся: главную кнопку выбирает
        # update_buttons() по состоянию службы (см. _set_variant).
        self.start_button = self._tile_button(
            "Запустить", None, "Запустить службу zapret"
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
            None,
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

        # Сводка над графиком: последний, средний, минимальный и максимальный
        # пинг плюс потери. Раньше числа приходилось «вычитывать» по линии, а
        # один раунд проверки давал вертикальную палку без единой подписи.
        self.chart_stats_label = QLabel("")
        self.chart_stats_label.setObjectName("Muted")
        self.chart_stats_label.setTextFormat(Qt.TextFormat.RichText)
        self.chart_stats_label.setWordWrap(True)
        self.chart_stats_label.setVisible(False)
        self.chart_card.body.addWidget(self.chart_stats_label)

        # Заглушка вместо графика: пока проверок не было (или не установлен
        # PyQt6-Charts) области графика показывать нечего. Это не одиночная
        # подпись, а контейнер «текст + кнопка»: пока данных нет, запустить
        # проверку связи можно прямо отсюда, не уходя на другую страницу.
        self.chart_placeholder_container = QWidget()
        placeholder_layout = QVBoxLayout(self.chart_placeholder_container)
        placeholder_layout.setContentsMargins(0, 0, 0, 0)
        placeholder_layout.setSpacing(8)

        self.chart_placeholder_label = QLabel("")
        self.chart_placeholder_label.setObjectName("Muted")
        self.chart_placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.chart_placeholder_label.setWordWrap(True)
        placeholder_layout.addWidget(self.chart_placeholder_label)

        # Кнопка проверки связи: то же действие, что и на плитке «Быстрые
        # действия» — отдельного сигнала для неё не заводим.
        self.chart_placeholder_button = Page.make_button(
            "Проверить связь",
            None,
            tooltip="Проверить доступность сайтов из списка проверки",
        )
        self.chart_placeholder_button.clicked.connect(
            lambda: self._quick("check_health")
        )
        placeholder_layout.addWidget(
            self.chart_placeholder_button, 0, Qt.AlignmentFlag.AlignHCenter
        )

        self.chart_placeholder_container.setMinimumHeight(CHART_PLACEHOLDER_MIN_HEIGHT)
        self.chart_card.body.addWidget(self.chart_placeholder_container)

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
        # Стартовый диапазон: дальше его подгоняет _update_axes под данные.
        self.axis_y.setRange(0.0, CHART_Y_MIN_MAX)
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
    #  Плитка 5: поддержать разработку
    # ------------------------------------------------------------------
    def _build_support_tile(self) -> None:
        """Карточка «Поддержать»: короткий текст и кнопка с адресами для донатов.

        Реквизиты здесь не хранятся: их показывает
        :class:`gui.dialogs.support_dialog.SupportDialog`. Карточка узкая —
        одна строка текста и кнопка справа, чтобы не отбирать высоту у графика
        пинга над ней.
        """
        self.support_card = ModernCard("")
        self._make_compact(self.support_card)

        row = QHBoxLayout()
        row.setSpacing(12)

        text = QLabel(SUPPORT_TEXT)
        text.setObjectName("Muted")
        text.setWordWrap(True)
        row.addWidget(text, 1)

        # Вариант оформления намеренно не задан: главная (primary) кнопка на
        # дашборде одна — это действие службы, и вторая «главная» кнопка
        # спорила бы с ней за внимание.
        self.support_button = Page.make_button(
            "Поддержать",
            None,
            tooltip="Показать адреса BTC и TON для поддержки разработки",
        )
        self.support_button.clicked.connect(self.on_support)
        row.addWidget(self.support_button, 0, Qt.AlignmentFlag.AlignRight)

        self.support_card.add_layout(row)

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
        was_empty = not self._drawn_points
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

    def _history_points(self) -> list[tuple[datetime, float | None, str]]:
        """Все замеры по порядку: ``(время, пинг, сайт)``; ``None`` — потеря.

        Один список вместо раздельных «сегментов» и «потерь»: графику нужен
        общий порядок точек — он же становится осью X, пока замеров мало.
        """
        if self.ping_history is None:
            return []
        return list(self.ping_history.chart_points(CHART_POINT_LIMIT))

    def refresh_chart(self) -> None:
        """Перерисовывает график по накопленной истории (только по кнопке)."""
        if not CHARTS_AVAILABLE or self.chart is None:
            return

        points = self._history_points()
        if not points:
            self._drawn_points = []
            self._clear_series()
            self._set_placeholder(CHART_EMPTY_TEXT)
            self.chart_card.set_hint("")
            self.update_refresh_button()
            return

        self._draw(points)
        self._show_chart()
        self.update_refresh_button()
        self._set_stats(points)
        lost = sum(1 for _, ping, _site in points if ping is None)
        last = points[-1][0]
        self.chart_card.set_hint(
            f"Замеров: {len(points)} · потерь: {lost}"
            f" · последняя проверка {last.strftime('%H:%M:%S')}"
        )
        self.window.set_status(f"График пинга обновлён (замеров: {len(points)})", 4000)

    def _set_stats(self, points: list[tuple[datetime, float | None, str]]) -> None:
        """Пишет сводку над графиком: последний, средний, мин, макс и потери.

        Числа над графиком отвечают на вопрос «что тут нарисовано» без
        разглядывания линии: один раунд проверки — это несколько точек за доли
        секунды, и по самой линии значения не прочитать.
        """
        values = [ping for _, ping, _site in points if ping is not None]
        total = len(points)
        lost = total - len(values)
        parts: list[str] = []
        if values:
            average = sum(values) / len(values)
            parts.append(f"Последний <b>{values[-1]:.0f}</b> мс")
            parts.append(f"средний <b>{average:.0f}</b> мс")
            parts.append(f"мин <b>{min(values):.0f}</b> мс")
            parts.append(f"макс <b>{max(values):.0f}</b> мс")
        else:
            parts.append("Все замеры — потери")
        share = f" ({lost * 100 // total}%)" if total else ""
        parts.append(f"потери <b>{lost}</b> из {total}{share}")
        self.chart_stats_label.setText(" · ".join(parts))
        self.chart_stats_label.setVisible(True)

    # ------------------------------------------------------------------
    #  Отрисовка графика
    # ------------------------------------------------------------------
    def _draw(self, points: list[tuple[datetime, float | None, str]]) -> None:
        """Строит линию пинга, метки потерь и линию среднего.

        Пинг — сплошная линия с точками, по одной серии на непрерывный
        участок: ``QLineSeries`` разрывов не умеет, а на потерях линия обязана
        рваться. Потери — красные метки на уровне нуля. Средний уровень —
        пунктир: по нему видно, «выше или ниже обычного» идёт последний замер.
        """
        if self.chart is None:
            return
        self._drawn_points = list(points)
        self._clear_series()

        by_index = self._use_index_axis(points)
        labels = self._axis_labels(points) if by_index and QCategoryAxis else []
        self._rebuild_axis_x(by_index, labels)
        xs = self._x_values(points, by_index, bool(labels))

        ping_pen = _line_pen(Theme.color("chart_line", Theme.color("accent")))
        loss_color = Theme.color("chart_loss", Theme.color("error"))

        segments: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        losses: list[tuple[float, float]] = []
        for (_stamp, ping, _site), x in zip(points, xs):
            if ping is None:
                losses.append((x, 0.0))
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append((x, max(0.0, float(ping))))
        if current:
            segments.append(current)

        for index, segment in enumerate(segments):
            self._ping_series.append(
                self._add_ping_series(ping_pen, segment, hide_legend=bool(index))
            )
        if not segments:
            # Все сайты не ответили: линии пинга нет, но элемент легенды
            # «Пинг» остаётся на месте — пустая серия ничего не рисует.
            self._ping_series.append(self._add_ping_series(ping_pen, []))

        loss_series = self._add_loss_series(loss_color, losses)
        self._loss_series.append(loss_series)
        if not losses:
            # Серии потерь на графике нет — легенда не должна её обещать.
            self._hide_legend_marker(loss_series)

        self._add_average_line(segments, xs)
        self._update_axes(points, xs, by_index)

    def _add_ping_series(
        self,
        pen: QPen,
        points: list[tuple[float, float]],
        *,
        hide_legend: bool = False,
    ):
        """Добавляет линию пинга (пары «x, мс») и привязывает её к осям."""
        series = QLineSeries()
        series.setName("Пинг")
        series.setPen(pen)
        # Точки видны всегда: даже сегмент из одной точки должен быть заметен.
        series.setPointsVisible(True)
        series.setMarkerSize(CHART_PING_MARKER_SIZE)
        for x, ping in points:
            series.append(x, float(ping))
        self.chart.addSeries(series)
        series.attachAxis(self.axis_x)
        series.attachAxis(self.axis_y)
        if hide_legend:
            self._hide_legend_marker(series)
        return series

    def _add_average_line(
        self,
        segments: list[list[tuple[float, float]]],
        xs: list[float],
    ) -> None:
        """Рисует пунктир среднего пинга на всю ширину графика."""
        values = [value for segment in segments for _, value in segment]
        if len(values) < 2 or not xs:
            return
        average = sum(values) / len(values)
        pen = QPen(QColor(Theme.color("muted")))
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        series = QLineSeries()
        series.setName("Среднее")
        series.setPen(pen)
        series.append(min(xs), average)
        series.append(max(xs), average)
        self.chart.addSeries(series)
        series.attachAxis(self.axis_x)
        series.attachAxis(self.axis_y)
        self._ping_series.append(series)
        # Значение среднего уже есть в сводке над графиком — в легенде оно
        # только добавляло бы шум.
        self._hide_legend_marker(series)

    def _add_loss_series(self, color: str, points: list[tuple[float, float]]):
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
        for x, value in points:
            series.append(x, float(value))
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

    def _use_index_axis(self, points: list[tuple[datetime, float | None, str]]) -> bool:
        """Показывать ось X по номеру замера, а не по времени.

        Один раунд проверки — это несколько точек за доли секунды: на оси
        времени они сливаются в вертикальную черту, и график «не читается».
        Пока история короткая, точки идут по номерам — видно каждый замер.
        """
        if len(points) <= CHART_INDEX_MODE_MAX_POINTS:
            return True
        span = (points[-1][0] - points[0][0]).total_seconds()
        return span < CHART_TIME_MODE_MIN_SPAN_SEC

    def _axis_labels(self, points: list[tuple[datetime, float | None, str]]) -> list[str]:
        """Подписи оси X именами сайтов — если замеров немного и имена есть."""
        if not points or len(points) > CHART_SITE_LABEL_MAX_POINTS:
            return []
        labels = [_short_site(site) for _stamp, _ping, site in points]
        if not all(labels):
            return []
        # Одинаковые имена (несколько замеров одного сайта) подписывать
        # бессмысленно — вернёмся к номерам.
        return labels if len(set(labels)) == len(labels) else []

    def _x_values(
        self,
        points: list[tuple[datetime, float | None, str]],
        by_index: bool,
        with_labels: bool,
    ) -> list[float]:
        """X-координаты точек под выбранный режим оси.

        В категорийном режиме (подписи сайтов) точка стоит в центре своей
        категории — ``i + 0.5``.
        """
        if not by_index:
            return [_to_msecs(stamp) for stamp, _ping, _site in points]
        if with_labels:
            return [index + 0.5 for index in range(len(points))]
        return [float(index) for index in range(len(points))]

    def _rebuild_axis_x(self, by_index: bool, labels: list[str] | None = None) -> None:
        """Пересоздаёт ось X: категории сайтов, номера замеров или время.

        Тип оси в QChart не переключается на месте, поэтому ось заменяется
        целиком. Диапазон выставляет :meth:`_update_axes` после добавления
        серий — здесь задаётся только «скелет».
        """
        if self.chart is None:
            return
        key = (by_index, tuple(labels or ()))
        if self.axis_x is not None and self._x_axis_key == key:
            return  # режим тот же — ось остаётся, диапазон обновит _update_axes

        old = self.axis_x
        if old is not None:
            # Порядок важен: если убрать ось сразу, QChart оставляет на сцене
            # её элементы — подписи предыдущего режима висят поверх новой оси.
            # Скрытие перед removeAxis убирает их чисто.
            old.setVisible(False)
            # removeAxis только отсоединяет ось: сам объект остаётся ребёнком
            # QChart, поэтому его ещё и отвязываем от родителя.
            self.chart.removeAxis(old)
            self.axis_x = None
            old.setParent(None)
            old.deleteLater()

        if labels:
            axis = QCategoryAxis()
            axis.setLabelsPosition(
                QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionCenter
            )
            for index, label in enumerate(labels):
                axis.append(label, float(index + 1))
            axis.setRange(0.0, float(len(labels)))
            axis.setTitleText("Сайты проверки")
        elif by_index:
            axis = QValueAxis()
            axis.setLabelFormat("%d")
            axis.setTickCount(CHART_TICK_COUNT)
            axis.setTitleText("Замеры (по порядку)")
            axis.setRange(0.0, 1.0)
        else:
            axis = QDateTimeAxis()
            axis.setFormat("hh:mm:ss")
            axis.setTickCount(CHART_TICK_COUNT)
            axis.setTitleText("Время проверки")

        self._style_axis(axis)
        self.chart.addAxis(axis, Qt.AlignmentFlag.AlignBottom)
        self.axis_x = axis
        self._x_axis_key = key

    def _style_axis(self, axis) -> None:
        """Красит ось под активную тему (та же палитра, что в _apply_chart_theme)."""
        if axis is None:
            return
        text = QColor(Theme.color("text"))
        grid = QColor(Theme.color("chart_grid", Theme.color("border")))
        axis.setGridLineColor(grid)
        axis.setLinePenColor(grid)
        axis.setLabelsColor(text)
        axis.setTitleBrush(QBrush(text))

    def _update_axes(
        self,
        points: list[tuple[datetime, float | None, str]],
        xs: list[float],
        by_index: bool,
    ) -> None:
        """Диапазоны осей: X — по точкам, Y — под данные с «круглым» шагом."""
        if self.axis_x is None or self.axis_y is None or not xs:
            return

        labels_mode = bool(self._x_axis_key and self._x_axis_key[1])
        if labels_mode:
            # Категорийная ось: диапазон уже задан по числу категорий.
            pass
        elif by_index:
            self.axis_x.setRange(-0.4, max(1.0, len(xs) - 1 + 0.4))
            # Подписи по одной на замер, но не больше CHART_TICK_COUNT.
            self.axis_x.setTickCount(max(2, min(CHART_TICK_COUNT, len(xs))))
        else:
            first, last = min(xs), max(xs)
            span = last - first
            half = CHART_MIN_SPAN_SEC * 1000.0
            if span < half * 2:
                # Две-три точки во времени: окно расширяется, иначе подписи
                # «hh:mm:ss» слиплись бы в одну.
                center = (first + last) / 2.0
                first, last = center - half, center + half
                span = last - first
            padding = max(3000.0, span * 0.04)
            self.axis_x.setRange(
                QDateTime.fromMSecsSinceEpoch(int(first - padding)),
                QDateTime.fromMSecsSinceEpoch(int(last + padding)),
            )

        values = [ping for _, ping, _site in points if ping is not None]
        y_max, y_step = _nice_y_scale(max(values) if values else 0.0)
        self.axis_y.setRange(0.0, y_max)
        self.axis_y.setTickCount(max(2, int(round(y_max / y_step)) + 1))

    # ------------------------------------------------------------------
    #  Состояния карточки графика
    # ------------------------------------------------------------------
    def _set_placeholder(self, text: str) -> None:
        """Показывает заглушку вместо графика (нет данных / нет PyQt6-Charts).

        Кнопка «Проверить связь» показывается только тогда, когда проверка
        действительно может помочь: без PyQt6-Charts графика не будет, и
        предлагать действие незачем.
        """
        self.chart_placeholder_label.setText(text)
        self.chart_placeholder_button.setVisible(CHARTS_AVAILABLE)
        self.chart_placeholder_container.setVisible(True)
        # Сводка относится к нарисованным данным — без графика её быть не должно.
        self.chart_stats_label.setVisible(False)
        if self.chart_view is not None:
            self.chart_view.setVisible(False)

    def _show_chart(self) -> None:
        """Показывает область графика, прячет заглушку вместе с кнопкой."""
        self.chart_placeholder_container.setVisible(False)
        self.chart_stats_label.setVisible(True)
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

    def on_support(self) -> None:
        """Открывает мини-окно с адресами для поддержки (BTC и TON).

        Диалог модальный и живёт до закрытия: нить работы окна он не трогает
        (копирование адреса — мгновенная операция), поэтому результат ему не
        нужен, а ``exec`` просто ждёт кнопку «Закрыть».
        """
        dialog = SupportDialog(parent=self)
        dialog.exec()

    def set_busy(self, busy: bool) -> None:
        """Блокирует кнопки плитки службы, пока операция выполняется."""
        self._busy = busy
        self.update_buttons()

    @staticmethod
    def _set_variant(button: QPushButton, variant: str | None) -> None:
        """Меняет вариант оформления кнопки (QSS-селектор ``[variant=...]``).

        Qt не перекрашивает виджет при изменении динамического свойства,
        поэтому стиль пересобирается через :func:`gui.theme.repolish`. Пустая
        строка (``variant`` снят) с селектором ``[variant="primary"]`` не
        совпадает — кнопка становится обычной. Если вариант не изменился,
        ничего не делаем: ``update_buttons`` вызывается часто.
        """
        current = button.property("variant") or ""
        new = variant or ""
        if current == new:
            return
        button.setProperty("variant", new)
        repolish(button)

    def update_buttons(self) -> None:
        """Включает нужные кнопки и выбирает главную под текущее состояние.

        Главная (primary) кнопка — наиболее вероятное следующее действие:
        у работающей службы это «Остановить», у остановленной — «Запустить»,
        у неустановленной — «Перезапустить» (окно подставит установку). Пока
        состояние неизвестно или идёт операция, главной кнопки нет: подсказать
        действие наугад хуже, чем не подсказывать вовсе.
        """
        if self._busy:
            for button in (self.start_button, self.stop_button, self.restart_button):
                button.setEnabled(False)
                self._set_variant(button, None)
            return

        running = self._state.is_running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        # Для неустановленной службы «Перезапустить» означает «установить»:
        # окно подставит стратегию, и кнопка остаётся активной.
        self.restart_button.setEnabled(True)

        if running:
            variants = (None, "primary", None)
        elif self._state is ServiceState.STOPPED:
            variants = ("primary", None, None)
        elif self._state is ServiceState.NOT_INSTALLED:
            variants = (None, None, "primary")
        else:  # ServiceState.UNKNOWN — состояние ещё не определено
            variants = (None, None, None)

        for button, variant in zip(
            (self.start_button, self.stop_button, self.restart_button), variants
        ):
            self._set_variant(button, variant)

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
            self.support_card,
        ):
            card.refresh_theme()
        self._apply_chart_theme()
        self.refresh_tiles()
        log.debug("Плитки обзора перекрашены (тема «%s»)", Theme.label())

    def _apply_chart_theme(self) -> None:
        """Перекрашивает график под активную тему.

        Из истории данные здесь не читаются: перерисовываются только те точки,
        что уже показаны (``_drawn_points``), иначе смена
        темы приносила бы на график новые проверки в обход кнопки «Обновить
        график».
        """
        self.chart_placeholder_label.setStyleSheet(f"color: {Theme.color('muted')};")
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

        if self._drawn_points:
            self._draw(self._drawn_points)
