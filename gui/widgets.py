"""Кастомные виджеты и вспомогательный поток.

Здесь собраны переиспользуемые элементы интерфейса: карточка (обычная и
«современная», с тенью), индикатор статуса, строка проверки сайта — и класс
:class:`Worker`, через который длительные операции уходят из главного потока,
чтобы окно не подвисало.

Цвета виджеты берут из активной темы (:mod:`gui.theme`): при переключении
темы приложение перерисовывает их и зовёт :meth:`refresh_theme`.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from PyQt6.QtCore import QRectF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import CHECK_COLORS
from gui.theme import Theme

log = logging.getLogger(__name__)


def _resolve(color: str | None, fallback: str = "muted") -> str:
    """Превращает имя цвета темы в hex, а готовый цвет оставляет как есть.

    Так один и тот же код может передавать и ключ темы (``success``), и
    конкретный цвет из :mod:`config` (``#22c55e``).
    """
    if not color:
        return Theme.color(fallback)
    return Theme.colors().get(color, color)


# ---------------------------------------------------------------------------
#  Фоновые задачи
# ---------------------------------------------------------------------------
class Worker(QThread):
    """Выполняет функцию в отдельном потоке.

    Сигналы:
        ``finished_signal(object)`` — результат работы функции;
        ``error_signal(str)`` — текст ошибки (исключения не «проглатываются»,
        а попадают в интерфейс и в журнал).
    """

    finished_signal = pyqtSignal(object)
    error_signal = pyqtSignal(str)

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        parent: QWidget | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.result: Any = None

    def run(self) -> None:  # noqa: D102 — наследуемся от QThread
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 — ошибку показываем пользователю
            log.exception("Фоновая задача завершилась ошибкой")
            self.error_signal.emit(str(exc) or type(exc).__name__)
            return
        self.result = result
        self.finished_signal.emit(result)


# ---------------------------------------------------------------------------
#  Мелкие элементы
# ---------------------------------------------------------------------------
class Dot(QWidget):
    """Цветная точка-индикатор."""

    def __init__(
        self,
        color: str = "muted",
        size: int = 10,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._color = _resolve(color)
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_color(self, color: str) -> None:
        resolved = _resolve(color)
        if resolved != self._color:
            self._color = resolved
            self.update()

    def color(self) -> str:
        return self._color

    def paintEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(self._color)))
        side = min(self.width(), self.height()) - 1
        painter.drawEllipse(QRectF(0.5, 0.5, side, side))
        painter.end()


class StatusIndicator(QWidget):
    """Индикатор состояния службы: точка + подпись."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.dot = Dot("muted", 11)
        self.label = QLabel("Проверка...")
        self.label.setObjectName("Value")

        layout.addWidget(self.dot)
        layout.addWidget(self.label)

    def set_text(self, text: str, color: str) -> None:
        self.dot.set_color(color)
        self.label.setText(text)

    def set_service_state(self, state) -> None:
        """Принимает :class:`core.service_manager.ServiceState`."""
        self.set_text(state.label, state.color)

    def refresh_theme(self) -> None:
        """Перекрашивает подпись под активную тему (точка знает свой цвет)."""
        self.label.setStyleSheet(f"color: {Theme.color('text')};")


class SiteCheckRow(QWidget):
    """Строка проверки одного сайта: точка, имя, подробности и время отклика."""

    def __init__(self, name: str, url: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.site_name = name
        self.url = url

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(10)

        self.dot = Dot("muted", 10)

        self.name_label = QLabel(name)
        self.name_label.setMinimumWidth(84)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        self.detail_label = QLabel("не проверялся")
        self.detail_label.setObjectName("Muted")
        self.detail_label.setToolTip(url)

        self.time_label = QLabel("—")
        self.time_label.setObjectName("Muted")
        self.time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.time_label.setFixedWidth(64)

        layout.addWidget(self.dot)
        layout.addWidget(self.name_label)
        layout.addWidget(self.detail_label, 1)
        layout.addWidget(self.time_label)

    def set_pending(self) -> None:
        self.dot.set_color(CHECK_COLORS["PENDING"])
        self.detail_label.setText("проверяется...")
        self.time_label.setText("—")

    def set_result(self, result) -> None:
        """Принимает :class:`core.health_checker.CheckResult`."""
        self.dot.set_color(_resolve(CHECK_COLORS.get(result.status.name, "muted")))
        detail = result.detail or result.status.label
        self.detail_label.setText(detail)
        self.detail_label.setToolTip(f"{self.url}\n{detail}")
        # Время показываем и для неудач: по нему видно, истёк таймаут или нет.
        self.time_label.setText(result.time_text)

    def refresh_theme(self) -> None:
        """Перекрашивает строку под активную тему."""
        self.name_label.setStyleSheet(f"color: {Theme.color('text')};")
        for label in (self.detail_label, self.time_label):
            label.setStyleSheet(f"color: {Theme.color('muted')};")


class Card(QFrame):
    """Карточка-раздел с заголовком и содержимым."""

    def __init__(self, title: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.header_layout = header
        outer.addLayout(header)

        self.hint_label: QLabel | None = None
        if hint:
            self.hint_label = QLabel(hint)
            self.hint_label.setObjectName("CardHint")
            self.hint_label.setWordWrap(True)
            outer.addWidget(self.hint_label)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def add_widget(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)

    def add_header_widget(self, widget: QWidget) -> QWidget:
        self.header_layout.addWidget(widget)
        return widget

    def add_stretch(self) -> None:
        self.body.addStretch(1)

    def set_hint(self, text: str) -> None:
        if self.hint_label is None:
            self.hint_label = QLabel(text)
            self.hint_label.setObjectName("CardHint")
            self.hint_label.setWordWrap(True)
            self.body.insertWidget(0, self.hint_label)
        else:
            self.hint_label.setText(text)
        self.hint_label.setVisible(bool(text))

    def refresh_theme(self) -> None:
        """Карточка красится через QSS, но заголовок может иметь inline-цвет."""
        self.title_label.setStyleSheet("")


class ModernCard(Card):
    """Карточка с аккуратными отступами (16 px), скруглением 16 px и тенью.

    Отличается от :class:`Card` только оформлением: содержимое добавляется
    теми же методами, поэтому страницы не зависят от конкретного класса.
    Тень и её цвет берутся из активной темы (``card_shadow_blur`` и
    ``card_shadow_color``), а не задаются здесь числами.
    """

    def __init__(
        self,
        title: str,
        hint: str = "",
        parent: QWidget | None = None,
        shadow: bool = True,
    ) -> None:
        super().__init__(title, hint, parent)
        self.setObjectName("ModernCard")

        layout = self.layout()
        if layout is not None:
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(14)
        self.body.setSpacing(12)

        #: Включена ли тень (её переприменяет refresh_theme при смене темы).
        self._shadow_enabled = shadow
        if shadow:
            self._apply_shadow()

    def _apply_shadow(self) -> None:
        """Тень под карточкой: заметная, но не «грязная» (параметры из темы)."""
        effect = self.graphicsEffect()
        if not isinstance(effect, QGraphicsDropShadowEffect):
            effect = QGraphicsDropShadowEffect(self)
            self.setGraphicsEffect(effect)
        effect.setBlurRadius(float(Theme.color("card_shadow_blur", "20")))
        effect.setXOffset(0)
        effect.setYOffset(4)
        effect.setColor(_shadow_color())

    def set_accent(self, bg_color: str | None = None) -> None:
        """Красит фон карточки (плитки дашборда) или возвращает цвет темы.

        :param bg_color: цвет в формате Qt (``rgba(...)`` или ``#rrggbb``);
            ``None`` снимает inline-фон, и карточка красится QSS темы.
        """
        # Селектор собирается по фактическому objectName: плитки дашборда
        # переименованы (``StatusTile``), и жёсткий ``QFrame#ModernCard``
        # не совпадал с ними — фон состояния просто не рисовался.
        name = self.objectName() or "ModernCard"
        if bg_color:
            self.setStyleSheet(f"QFrame#{name} {{ background-color: {bg_color}; }}")
        else:
            self.setStyleSheet("")

    def refresh_theme(self) -> None:
        super().refresh_theme()
        if self._shadow_enabled:
            self._apply_shadow()


def _shadow_color() -> QColor:
    """Цвет тени карточек из активной темы (с запасным значением)."""
    raw = Theme.color("card_shadow_color", "rgba(0, 0, 0, 0.25)")
    color = QColor(raw)
    if color.isValid():
        return color
    # Запасной вариант: тема без ключа тени — прежнее поведение (чёрная тень).
    color = QColor(0, 0, 0)
    color.setAlpha(90 if Theme.is_dark() else 40)
    return color
