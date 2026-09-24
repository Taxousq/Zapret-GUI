"""Общие helpers для страниц приложения.

Страница раздела — обычный ``QWidget``, который живёт внутри
``QStackedWidget`` главного окна. Здесь собрано то, что нужно всем страницам:
прокручиваемая область, строка «подпись — значение», кнопка с вариантом
оформления и интерфейс главного окна, через который страницы запускают
длительные операции в фоне.
"""

from __future__ import annotations

from typing import Protocol

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui.theme import Theme
from gui.widgets import Card, Worker


class WindowApi(Protocol):
    """То, что страницы вправе просить у главного окна.

    Страницы не создают ``Worker`` сами и не трогают статус-бар напрямую:
    окно владеет фоновыми задачами, треем и общим статусом приложения.
    """

    def run_async(
        self,
        fn,
        *args,
        on_success=None,
        on_error=None,
        busy_message: str | None = None,
        **kwargs,
    ) -> Worker | None: ...

    def report_error(self, title: str, message: str) -> None: ...

    def set_status(self, text: str, timeout_ms: int = 0, level: str = "info") -> None: ...

    def confirm(self, title: str, text: str, informative: str = "") -> bool: ...

    def show_logs(self) -> None: ...

    def refresh_all(self) -> None: ...

    def status_bar_progress(self, visible: bool, value: int | None = None) -> None: ...

    def check_app_updates(self) -> None:
        """Проверить обновления самого приложения (кнопка в «Настройках»)."""
        ...


class Page(QWidget):
    """Страница раздела: прокручиваемая область с карточкой.

    Наследники наполняют ``self.card`` и при необходимости переопределяют
    :meth:`refresh_theme`.
    """

    #: Заголовок страницы (для документации и отладки).
    title: str = ""

    def __init__(
        self,
        window: WindowApi,
        title: str = "",
        *,
        parent: QWidget | None = None,
        spacing: int = 14,
        margins: tuple[int, int, int, int] = (2, 2, 14, 2),
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.title = title or self.title

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setObjectName("PageScroll")

        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setContentsMargins(*margins)
        self.content_layout.setSpacing(spacing)

        scroll.setWidget(content)
        root.addWidget(scroll)

        self.scroll_area = scroll
        self.content = content

    # ------------------------------------------------------------------
    #  Помощники для наследников
    # ------------------------------------------------------------------
    def add_card(self, card: Card) -> Card:
        """Добавляет карточку в конец страницы (с растяжкой под ней)."""
        self.content_layout.addWidget(card)
        return card

    def finish_layout(self) -> None:
        """Добавляет растяжку: карточки прижимаются к верху страницы."""
        self.content_layout.addStretch(1)

    def kv_row(self, layout: QVBoxLayout, label_text: str, width: int = 150) -> QLabel:
        """Строка «подпись — значение». Возвращает QLabel значения."""
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(label_text)
        label.setObjectName("Muted")
        label.setMinimumWidth(width)
        value = QLabel("—")
        value.setObjectName("Value")
        value.setWordWrap(True)
        value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(label)
        row.addWidget(value, 1)
        layout.addLayout(row)
        return value

    @staticmethod
    def make_button(
        text: str,
        variant: str | None = None,
        *,
        tooltip: str = "",
        checkable: bool = False,
    ) -> QPushButton:
        """Кнопка страницы с (необязательным) вариантом оформления."""
        button = QPushButton(text)
        if variant:
            button.setProperty("variant", variant)
        if tooltip:
            button.setToolTip(tooltip)
        if checkable:
            button.setCheckable(True)
        return button

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def refresh_theme(self) -> None:
        """Перекрашивает элементы, которым QSS не помогает.

        Наследники переопределяют метод и вызывают ``super().refresh_theme()``.
        """
        return None

    def colors(self) -> dict[str, str]:
        """Цвета активной темы."""
        return Theme.colors()
