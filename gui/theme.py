"""Тёмная и светлая темы приложения.

Тема — это словарь цветов и таблица стилей (QSS), собранная из него. Виджеты
не хранят цвета у себя: они берут их у :class:`Theme` в момент отрисовки, а по
сигналу смены темы перечитывают. Так переключение темы не ломает состояние
интерфейса: меняются только цвета.

Выбранная тема сохраняется в ``QSettings("ZapretGUI", "Theme")`` (через
:func:`core.settings.theme_settings`, поэтому в portable-режиме — в
``config.ini`` рядом с exe) и восстанавливается при следующем запуске. Тема по
умолчанию — тёмная.
"""

from __future__ import annotations

import logging
from string import Template
from typing import Final

from PyQt6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

#: Имена тем.
DARK: Final[str] = "dark"
LIGHT: Final[str] = "light"

#: Радиус скругления карточек, px. Единственное место, где он задан:
#: страницы и виджеты берут значение отсюда (QSS карточек собирается ниже).
CARD_RADIUS: Final[int] = 16

#: Организация и приложение в реестре для QSettings. Настройки создаются через
#: :mod:`core.settings` (это нужно для portable-режима), а эти константы
#: оставлены для справки: они совпадают с SETTINGS_ORG/THEME_APP оттуда.
SETTINGS_ORG: Final[str] = "ZapretGUI"
SETTINGS_APP: Final[str] = "Theme"

#: Палитры тем. Ключи одинаковые у обеих тем — код страниц не знает,
#: какая тема активна, и просто подставляет цвета по имени.
PALETTES: dict[str, dict[str, str]] = {
    DARK: {
        # Тёмно-синяя тема (Tailwind gray-900 и окрестности): фон — почти
        # чёрный с холодным оттенком, карточки заметно светлее, сайдбар темнее
        # фона. Чисто чёрных (#0d0d0d) и серых (#1a1a1a) цветов здесь нет:
        # на них интерфейс выглядел «дёшево» и плитки сливались с фоном.
        "background": "#111827",
        "surface": "#1a2436",
        "surface_alt": "#151d2e",
        "sidebar": "#0f1729",
        "button": "#243044",
        "button_hover": "#2c3a52",
        "button_pressed": "#1f2a3d",
        "button_disabled": "#1b2432",
        "button_disabled_text": "#5b6b80",
        "button_disabled_border": "#253143",
        "border": "#2a3a4f",
        "border_strong": "#3a4a63",
        "accent": "#4d6bfe",
        "accent_hover": "#5f7bff",
        "accent_pressed": "#3f59e0",
        "accent_soft": "rgba(77, 107, 254, 0.18)",
        "accent_disabled": "#2c3560",
        "text": "#e5e7eb",
        "muted": "#94a3b8",
        "success": "#22c55e",
        "success_soft": "rgba(34, 197, 94, 0.10)",
        "error": "#ef4444",
        "error_soft": "rgba(239, 68, 68, 0.10)",
        "warning": "#f59e0b",
        "warning_soft": "rgba(245, 158, 11, 0.15)",
        # Отсутствие службы: приглушённый синевато-серый оттенок.
        "neutral_soft": "rgba(148, 163, 184, 0.08)",
        "danger_border": "#4c2630",
        "danger_hover": "#2c1a20",
        # График пинга на дашборде: линия пинга (на шаг светлее акцента —
        # на тёмном фоне так читается лучше), метки потерь и сетка осей.
        "chart_line": "#5f7bff",
        "chart_loss": "#ef4444",
        "chart_grid": "#2a3a4f",
        "scrollbar": "#334155",
        "scrollbar_hover": "#475569",
        "shadow": "#000000",
        # Статус-бар: фон не должен сливаться с основным фоном окна, поэтому
        # он светлее фона (как карточки) и отделён линией сверху.
        "statusbar_bg": "#1a2436",
        "statusbar_text": "#94a3b8",
        # Тень карточек: читается из палитры, а не задаётся в коде виджета.
        # На тёмно-синем фоне тень должна быть глубже, иначе карточки «висят».
        "card_shadow_color": "rgba(0, 0, 0, 0.4)",
        "card_shadow_blur": "24",
    },
    LIGHT: {
        # «Дорогая» светлая тема: белые карточки на светло-сером фоне,
        # мягкая серая граница и заметная, но не грубая тень.
        "background": "#f0f2f5",
        "surface": "#ffffff",
        "surface_alt": "#f7f8fa",
        "sidebar": "#ffffff",
        "button": "#ffffff",
        "button_hover": "#eef1f8",
        "button_pressed": "#e4e8f2",
        "button_disabled": "#f0f0f0",
        "button_disabled_text": "#a3a3a3",
        "button_disabled_border": "#e0e0e0",
        "border": "#d8dde3",
        "border_strong": "#c2c9d2",
        "accent": "#4d6bfe",
        "accent_hover": "#3f59e0",
        "accent_pressed": "#3550cc",
        "accent_soft": "rgba(77, 107, 254, 0.08)",
        "accent_disabled": "#b9c4f7",
        "text": "#1a1a1a",
        "muted": "#6b7280",
        "success": "#16a34a",
        "success_soft": "rgba(22, 163, 74, 0.08)",
        "error": "#dc2626",
        "error_soft": "rgba(220, 38, 38, 0.08)",
        "warning": "#d97706",
        "warning_soft": "rgba(217, 119, 6, 0.12)",
        "neutral_soft": "rgba(136, 136, 136, 0.08)",
        "danger_border": "#f0c2c2",
        "danger_hover": "#fdecec",
        # График пинга на дашборде: на светлом фоне линии темнее акцента.
        "chart_line": "#3f59e0",
        "chart_loss": "#dc2626",
        "chart_grid": "#d8dde3",
        "scrollbar": "#c4c4c4",
        "scrollbar_hover": "#a8a8a8",
        "shadow": "#000000",
        # Статус-бар: белая полоса на светло-сером фоне окна.
        "statusbar_bg": "#ffffff",
        "statusbar_text": "#6b7280",
        "card_shadow_color": "rgba(0, 0, 0, 0.12)",
        "card_shadow_blur": "20",
    },
}

#: Русские названия тем (для подсказок в интерфейсе).
THEME_LABELS: dict[str, str] = {DARK: "тёмная", LIGHT: "светлая"}

#: Таблица стилей. $подстановки заполняются цветами активной темы.
_QSS = Template(
    """
    QWidget {
        background-color: $background;
        color: $text;
        font-family: "Segoe UI", "Noto Sans", Arial, sans-serif;
        font-size: 13px;
    }
    QMainWindow, QDialog {
        background-color: $background;
    }
    QScrollArea, QScrollArea > QWidget > QWidget {
        background-color: $background;
        border: none;
    }
    QStackedWidget, QStackedWidget > QWidget {
        background-color: $background;
    }

    /* ---------- Шапка и сайдбар ---------- */
    QWidget#Header {
        background-color: $surface;
        border-bottom: 1px solid $border;
    }
    QWidget#Header QLabel {
        background: transparent;
    }
    QLabel#HeaderTitle {
        font-size: 16px;
        font-weight: 600;
        color: $text;
        background: transparent;
    }
    QLabel#HeaderSubtitle {
        color: $muted;
        font-size: 12px;
        background: transparent;
    }
    QWidget#Sidebar {
        background-color: $sidebar;
        border-right: 1px solid $border;
    }
    QWidget#Sidebar QLabel {
        background: transparent;
    }
    QLabel#SidebarCaption {
        color: $muted;
        font-size: 11px;
        font-weight: 600;
        background: transparent;
    }
    QPushButton#NavButton {
        background-color: transparent;
        border: none;
        border-left: 3px solid transparent;
        border-radius: 0;
        padding: 10px 12px 10px 15px;
        text-align: left;
        font-size: 13px;
        color: $text;
    }
    QPushButton#NavButton:hover {
        background-color: $accent_soft;
    }
    QPushButton#NavButton[active="true"] {
        background-color: $accent_soft;
        border-left: 3px solid $accent;
        color: $accent;
        font-weight: 600;
    }
    QPushButton#HeaderButton {
        background-color: transparent;
        border: 1px solid $border;
        border-radius: 8px;
        padding: 4px 10px;
        font-size: 14px;
        color: $text;
    }
    QPushButton#HeaderButton:hover {
        background-color: $button_hover;
        border-color: $border_strong;
    }
    QPushButton#HeaderButton:pressed {
        background-color: $button_pressed;
    }

    /* ---------- Карточки ---------- */
    QFrame#Card, QFrame#ModernCard {
        background-color: $surface;
        border: 1px solid $border;
        border-radius: ${card_radius}px;
    }
    QLabel#CardTitle {
        font-size: 14px;
        font-weight: 600;
        color: $text;
        background: transparent;
    }
    QLabel#CardHint, QLabel#Muted, QLabel#Hint {
        color: $muted;
        font-size: 12px;
        background: transparent;
    }
    QLabel#Value {
        color: $text;
        font-size: 13px;
        background: transparent;
    }
    QLabel#Warning {
        color: $warning;
        font-size: 12px;
        background: transparent;
    }
    QFrame#Separator {
        background-color: $border;
        max-height: 1px;
        border: none;
    }

    /* ---------- Кнопки ---------- */
    QPushButton {
        background-color: $button;
        color: $text;
        border: 1px solid $border;
        border-radius: 8px;
        padding: 7px 14px;
    }
    QPushButton:hover {
        background-color: $button_hover;
        border-color: $border_strong;
    }
    QPushButton:pressed {
        background-color: $button_pressed;
    }
    QPushButton:disabled {
        background-color: $button_disabled;
        color: $button_disabled_text;
        border-color: $button_disabled_border;
    }
    QPushButton[variant="primary"] {
        background-color: $accent;
        border-color: $accent;
        color: #ffffff;
        font-weight: 600;
    }
    QPushButton[variant="primary"]:hover {
        background-color: $accent_hover;
        border-color: $accent_hover;
    }
    QPushButton[variant="primary"]:pressed {
        background-color: $accent_pressed;
    }
    QPushButton[variant="primary"]:disabled {
        background-color: $accent_disabled;
        border-color: $accent_disabled;
        color: #f0f0f0;
    }
    QPushButton[variant="danger"] {
        color: $error;
        border-color: $danger_border;
    }
    QPushButton[variant="danger"]:hover {
        background-color: $danger_hover;
        border-color: $error;
    }

    /* ---------- Дашборд «Обзор» ---------- */
    /* Плитка статуса службы: фон задаётся inline (зелёный/красный/серый),    */
    /* поэтому подписи внутри обязаны быть прозрачными — иначе они перекрыли  */
    /* бы оттенок плитки.                                                     */
    /* Плитка состояния — обычная карточка: рамка и скругление как у
       остальных. Фон (зелёный/красный/серый оттенок) ставится inline
       (``ModernCard.set_accent``) и обязан перекрывать прозрачность ниже. */
    QFrame#StatusTile {
        border: 1px solid $border;
        border-radius: ${card_radius}px;
    }
    QFrame#StatusTile, QFrame#StatusTile QLabel {
        background: transparent;
    }
    QLabel#TileCaption {
        color: $muted;
        font-size: 12px;
        font-weight: 600;
        background: transparent;
    }
    QLabel#TileStatus {
        font-size: 22px;
        font-weight: 700;
        background: transparent;
    }
    QLabel#TileValue {
        font-size: 18px;
        font-weight: 600;
        background: transparent;
    }
    QLabel#TileIcon {
        background: transparent;
    }
    QPushButton#TileAction {
        padding: 8px 16px;
        font-size: 14px;
        border-radius: 10px;
    }

    /* ---------- Комбобокс ---------- */
    QComboBox {
        background-color: $button;
        border: 1px solid $border;
        border-radius: 8px;
        padding: 7px 10px;
        min-height: 18px;
        color: $text;
    }
    QComboBox:hover {
        border-color: $border_strong;
    }
    QComboBox:disabled {
        color: $button_disabled_text;
        background-color: $button_disabled;
    }
    QComboBox::drop-down {
        border: none;
        width: 24px;
    }
    QComboBox::down-arrow {
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid $muted;
        width: 0;
        height: 0;
        margin-right: 8px;
    }
    QComboBox QAbstractItemView {
        background-color: $surface;
        border: 1px solid $border;
        color: $text;
        selection-background-color: $accent;
        selection-color: #ffffff;
        outline: none;
        padding: 4px;
    }

    /* ---------- Таблица результатов ---------- */
    QTableWidget, QTableView {
        background-color: $surface;
        alternate-background-color: $surface_alt;
        border: 1px solid $border;
        border-radius: 8px;
        gridline-color: $border;
        color: $text;
        selection-background-color: $accent;
        selection-color: #ffffff;
        outline: none;
    }
    QTableWidget::item, QTableView::item {
        padding: 5px 8px;
    }
    QTableWidget::item:selected, QTableView::item:selected {
        background-color: $accent;
        color: #ffffff;
    }
    QHeaderView {
        background-color: $surface;
        border: none;
    }
    QHeaderView::section {
        background-color: $surface;
        color: $muted;
        border: none;
        border-bottom: 1px solid $border;
        border-right: 1px solid $border;
        padding: 6px 8px;
        font-weight: 600;
    }
    QTableCornerButton::section {
        background-color: $surface;
        border: none;
    }

    /* ---------- Прочее ---------- */
    QPlainTextEdit, QTextEdit {
        background-color: $surface_alt;
        border: 1px solid $border;
        border-radius: 8px;
        color: $text;
        font-family: "Cascadia Mono", "Consolas", monospace;
        font-size: 12px;
        selection-background-color: $accent;
    }
    QProgressBar {
        background-color: $button;
        border: 1px solid $border;
        border-radius: 6px;
        height: 8px;
        text-align: center;
        color: transparent;
    }
    QProgressBar::chunk {
        background-color: $accent;
        border-radius: 5px;
    }
    QCheckBox {
        background: transparent;
        color: $text;
        spacing: 8px;
    }
    QCheckBox::indicator {
        width: 15px;
        height: 15px;
        border: 1px solid $border;
        border-radius: 4px;
        background-color: $button;
    }
    QCheckBox::indicator:checked {
        background-color: $accent;
        border-color: $accent;
    }
    QToolTip {
        background-color: $surface;
        color: $text;
        border: 1px solid $border;
        padding: 4px;
    }
    QMenu {
        background-color: $surface;
        border: 1px solid $border;
        color: $text;
        padding: 4px;
    }
    QMenu::item {
        padding: 6px 22px 6px 12px;
        border-radius: 6px;
    }
    QMenu::item:selected {
        background-color: $accent;
        color: #ffffff;
    }
    QMenu::separator {
        height: 1px;
        background-color: $border;
        margin: 4px 6px;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 10px;
        margin: 0;
    }
    QScrollBar::handle:vertical {
        background: $scrollbar;
        border-radius: 5px;
        min-height: 30px;
    }
    QScrollBar::handle:vertical:hover {
        background: $scrollbar_hover;
    }
    QScrollBar:horizontal {
        background: transparent;
        height: 10px;
        margin: 0;
    }
    QScrollBar::handle:horizontal {
        background: $scrollbar;
        border-radius: 5px;
        min-width: 30px;
    }
    QScrollBar::handle:horizontal:hover {
        background: $scrollbar_hover;
    }
    QScrollBar::add-line, QScrollBar::sub-line,
    QScrollBar::add-page, QScrollBar::sub-page {
        background: none;
        height: 0;
        width: 0;
        border: none;
    }
    QStatusBar {
        background-color: $statusbar_bg;
        border-top: 1px solid $border;
        color: $statusbar_text;
    }
    QStatusBar::item {
        border: none;
    }
    QStatusBar QLabel {
        background: transparent;
        color: $statusbar_text;
    }
    /* Подписи статус-бара: слева «Zapret GUI v… by …», справа версия запрета. */
    QLabel#StatusBarLabel {
        color: $statusbar_text;
        font-size: 11px;
        background: transparent;
        padding: 0 8px;
    }
    /* Статус последней операции переехал в шапку — рядом со службой. */
    QLabel#HeaderStatus {
        color: $muted;
        font-size: 12px;
        background: transparent;
    }
    """
)


class Theme:
    """Активная тема приложения.

    Класс хранит только выбор темы: цвета — статические словари в
    :data:`PALETTES`. Метод :meth:`apply` собирает QSS и навешивает его на
    приложение или на отдельный виджет.
    """

    #: Имя активной темы (``dark`` или ``light``).
    current: str = DARK

    # ------------------------------------------------------------------
    #  Выбор темы
    # ------------------------------------------------------------------
    @classmethod
    def colors(cls) -> dict[str, str]:
        """Цвета активной темы."""
        return PALETTES.get(cls.current, PALETTES[DARK])

    @classmethod
    def color(cls, key: str, default: str = "") -> str:
        """Один цвет активной темы (без исключения на неизвестном ключе)."""
        return cls.colors().get(key, default or PALETTES[DARK].get(key, "#888888"))

    @classmethod
    def is_dark(cls) -> bool:
        return cls.current == DARK

    @classmethod
    def label(cls) -> str:
        return THEME_LABELS.get(cls.current, THEME_LABELS[DARK])

    @classmethod
    def toggle(cls) -> str:
        """Переключает тему и возвращает её новое имя."""
        cls.current = LIGHT if cls.is_dark() else DARK
        return cls.current

    @classmethod
    def stylesheet(cls) -> str:
        """QSS для активной темы."""
        # $card_radius — не цвет, а размер: радиус карточек (16 px).
        return _QSS.substitute({**cls.colors(), "card_radius": CARD_RADIUS})

    @classmethod
    def apply(cls, app_or_widget: QApplication | QWidget | None) -> str:
        """Применяет тему к приложению (или виджету) и возвращает её имя."""
        if app_or_widget is not None:
            app_or_widget.setStyleSheet(cls.stylesheet())
        return cls.current

    # ------------------------------------------------------------------
    #  Сохранение выбора
    # ------------------------------------------------------------------
    @classmethod
    def load(cls) -> str:
        """Читает тему из QSettings; тёмная — если выбора ещё нет.

        Настройки берутся через :func:`core.settings.theme_settings`: в
        portable-режиме они лежат в ``config.ini`` рядом с exe, а не в реестре.
        """
        from core.settings import theme_settings

        try:
            settings = theme_settings()
            value = settings.value("theme", DARK)
        except Exception:  # noqa: BLE001 — настройки не должны ронять запуск
            log.debug("Не удалось прочитать тему из настроек", exc_info=True)
            value = DARK
        name = str(value).strip().lower() if value is not None else DARK
        cls.current = name if name in PALETTES else DARK
        return cls.current

    @classmethod
    def save(cls) -> None:
        """Запоминает выбранную тему между запусками."""
        from core.settings import theme_settings

        try:
            settings = theme_settings()
            settings.setValue("theme", cls.current)
            settings.sync()
        except Exception:  # noqa: BLE001 — сохранение настроек не критично
            log.debug("Не удалось сохранить тему в настройках", exc_info=True)

    @classmethod
    def apply_and_save(cls, app_or_widget: QApplication | QWidget | None) -> str:
        """Применяет текущую тему и сохраняет её."""
        name = cls.apply(app_or_widget)
        cls.save()
        return name


def repolish(widget: QWidget) -> None:
    """Пересчитывает стиль виджета после смены динамического свойства.

    Qt не перекрашивает виджет при изменении свойства (``active`` у пункта
    меню), поэтому стиль приходится сбрасывать и навешивать заново.
    """
    style = widget.style()
    if style is None:  # pragma: no cover — у созданного виджета стиль есть
        return
    style.unpolish(widget)
    style.polish(widget)
    widget.update()
