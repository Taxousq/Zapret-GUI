"""Боковое меню приложения.

Слева — список разделов. Каждый пункт — обычная ``QPushButton`` с
``checkable=True``: активный подсвечивается акцентным цветом, остальные
полупрозрачны. Иконки рисуются кодом (см. :mod:`gui.icons`), поэтому при
смене темы они перекрашиваются вместе с текстом.

Виджет ничего не знает о страницах: он только сообщает индекс выбранного
раздела сигналом :attr:`Sidebar.section_changed`.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import SIDEBAR_ICON_SIZE, SIDEBAR_WIDTH
from gui.icons import make_icon
from gui.theme import Theme, repolish

log = logging.getLogger(__name__)

#: Разделы приложения: подпись, имя иконки и подсказка.
#: Порядок здесь — порядок пунктов меню и страниц в стеке главного окна;
#: индексы разделов совпадают с константами ``*_PAGE_INDEX`` из config.py.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("Обзор", "grid", "Сводка по системе и быстрые действия"),
    ("Служба", "shield", "Служба Windows zapret: запуск, остановка, перезапуск"),
    ("Стратегии", "arrows", "Список стратегий обхода и установка выбранной"),
    ("Списки", "list", "Пользовательские списки запрета: домены и исключения"),
    ("Обход (WARP)", "globe", "Cloudflare WARP: статус, подключение, режим, исключения"),
    ("Тестирование", "flask", "Тестирование стратегий встроенным тестером"),
    ("Проверка связи", "magnifier", "Проверка доступности сайтов"),
    ("Обновление", "download", "Обновление запрета, интерфейса и Cloudflare WARP"),
    ("Логи", "document", "Журнал событий Windows о службе zapret"),
    ("Настройки", "gear", "Папка запрета: изменить, проверить, скачать заново"),
)

#: Индекс раздела «Обзор» — стартовый экран при запуске.
OVERVIEW_SECTION = 0
#: Индекс раздела «Служба».
SERVICE_SECTION = 1
#: Индекс раздела «Списки» (пользовательские списки запрета).
LISTS_SECTION = 3
#: Индекс раздела «Обход (WARP)» (управление Cloudflare WARP).
WARP_SECTION = 4
#: Индекс раздела «Логи» (по нему открывается страница из кнопки и трея).
LOGS_SECTION = 8
#: Индекс раздела «Настройки» (папка запрета).
SETTINGS_SECTION = 9


class Sidebar(QWidget):
    """Боковое меню с разделами приложения."""

    #: Выбран раздел с указанным индексом (см. :data:`SECTIONS`).
    section_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        # Меню тянется на всю высоту центральной части окна — до самого
        # статус-бара. Политика задана явно: при вертикальном Preferred
        # сайдбар в некоторых раскладках «не дотягивался» до низа, и полоска
        # меню выглядела оборванной.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        # Фон и правая граница приходят из QSS (см. refresh_theme). Без этого
        # атрибута QWidget-наследник не рисует фон из таблицы стилей, и
        # сайдбар сливается с областью страниц.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._buttons: list[QPushButton] = []
        self._current = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 10)
        layout.setSpacing(4)

        caption = QLabel("РАЗДЕЛЫ")
        caption.setObjectName("SidebarCaption")
        caption.setContentsMargins(18, 0, 0, 6)
        layout.addWidget(caption)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        for index, (title, icon_name, tooltip) in enumerate(SECTIONS):
            button = self._make_button(title, icon_name, tooltip, index)
            self._buttons.append(button)
            self._group.addButton(button, index)
            layout.addWidget(button)

        # Растяжка прижимает пункты меню к верху и позволяет фону сайдбара
        # закрывать всю высоту. Подписи версии здесь больше нет: она переехала
        # в статус-бар (слева), а её футер-виджет рисовал фон основного окна
        # поверх низа сайдбара — из-за этого меню выглядело обрезанным.
        layout.addStretch(1)

        self.set_section(OVERVIEW_SECTION)
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _make_button(self, title: str, icon_name: str, tooltip: str, index: int) -> QPushButton:
        button = QPushButton(title)
        button.setObjectName("NavButton")
        button.setCheckable(True)
        button.setAutoExclusive(False)  # эксклюзивностью управляет QButtonGroup
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setIconSize(QSize(SIDEBAR_ICON_SIZE, SIDEBAR_ICON_SIZE))
        button.setIcon(make_icon(icon_name, SIDEBAR_ICON_SIZE, Theme.color("muted")))
        button.setProperty("active", False)
        button.clicked.connect(lambda _checked=False, i=index: self._on_clicked(i))
        return button

    # ------------------------------------------------------------------
    #  Выбор раздела
    # ------------------------------------------------------------------
    def _on_clicked(self, index: int) -> None:
        self.set_section(index)
        self.section_changed.emit(index)

    def set_section(self, index: int) -> None:
        """Подсвечивает раздел, не испуская сигнал (для внешних вызовов)."""
        if not 0 <= index < len(self._buttons):
            return
        self._current = index
        for position, button in enumerate(self._buttons):
            active = position == index
            button.setChecked(active)
            if button.property("active") != active:
                button.setProperty("active", active)
                repolish(button)
        self._update_icons()

    def current_section(self) -> int:
        return self._current

    # ------------------------------------------------------------------
    #  Оформление
    # ------------------------------------------------------------------
    def _update_icons(self) -> None:
        """Активный пункт получает акцентную иконку, остальные — приглушённую."""
        accent = Theme.color("accent")
        muted = Theme.color("muted")
        for index, button in enumerate(self._buttons):
            color = accent if index == self._current else muted
            button.setIcon(make_icon(SECTIONS[index][1], SIDEBAR_ICON_SIZE, color))

    def refresh_theme(self) -> None:
        """Перекрашивает сайдбар под активную тему.

        Фон и правая граница задаются здесь, а не только общей таблицей
        стилей: граница отделяет меню от страниц и обязана перерисовываться
        вместе с темой (``MainWindow.apply_theme`` зовёт этот метод).
        """
        self.setStyleSheet(
            "QWidget#Sidebar {"
            f" background-color: {Theme.color('sidebar')};"
            f" border-right: 1px solid {Theme.color('border')};"
            " }"
        )
        self._update_icons()
