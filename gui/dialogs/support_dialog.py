"""Мини-диалог «Поддержать разработку»: адреса BTC и TON с копированием.

Открывается кнопкой «Поддержать» на главной странице (см.
:class:`gui.pages.overview_page.OverviewPage`). Диалог ничего не делает сам:
показывает два крипто-адреса и кладёт выбранный в буфер обмена. Закрытие
ничего не сохраняет — окно информационное, а не форма.

Адреса — реальные реквизиты автора. Менять их при правке кода нельзя:
если они устареют, обновите константы централизованно (см. :data:`DONATIONS`).
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import APP_NAME
from gui.icons import make_pixmap
from gui.theme import Theme

log = logging.getLogger(__name__)

#: Заголовок диалога.
DIALOG_TITLE = "Поддержать разработку"

#: Вводный текст диалога.
DIALOG_TEXT = (
    "Спасибо, что пользуетесь приложением! "
    "Если оно оказалось полезным — можно поддержать автора:"
)

#: Список адресов для донатов: (валюта, адрес, примечание).
#: ВНИМАНИЕ: это реальные реквизиты — не менять без согласования с автором.
DONATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "BTC",
        "bc1qar53dz4tzkct3dnd5gmtjtnqtqg2ej6jf0qxfr",
        "Bitcoin (BTC)",
    ),
    (
        "TON",
        "UQAM3A0rlTaSAHFDmO0WFUX04Ft-Zlz0xLdK0hoBSpDpDGF3",
        "Toncoin (TON)",
    ),
)

#: Текст подтверждения после копирования.
COPIED_TEXT = "Адрес скопирован в буфер обмена."

#: Размер иконки-сердца в шапке диалога, px.
ICON_SIZE = 36


class SupportDialog(QDialog):
    """Модальный диалог с адресами BTC и TON для поддержки разработки."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — поддержать разработку")
        self.setModal(True)
        self.setMinimumWidth(520)

        # Ссылки заводятся до _build(): события стиля приходят и во время
        # построения, а refresh_theme обязан быть к ним готов.
        self.icon_label: QLabel | None = None
        self.status_label: QLabel | None = None

        self._build()
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        self.icon_label = QLabel()
        self.icon_label.setPixmap(make_pixmap("heart", ICON_SIZE, Theme.color("accent")))
        head.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel(DIALOG_TITLE)
        title.setObjectName("HeaderTitle")
        text = QLabel(DIALOG_TEXT)
        text.setObjectName("Value")
        text.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(text)
        head.addLayout(titles, 1)
        layout.addLayout(head)

        for currency, address, note in DONATIONS:
            layout.addWidget(self._make_address_block(currency, address, note))

        # Статус пустой и невидимый: подтверждение копирования показывается
        # здесь, а не в модальном QMessageBox — иначе диалог пришлось бы
        # закрывать после каждого копирования.
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.close_button = QPushButton("Закрыть")
        self.close_button.setToolTip("Закрыть окно поддержки")
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def _make_address_block(self, currency: str, address: str, note: str) -> QWidget:
        """Один блок: валюта + адрес + кнопка «Копировать»."""
        block = QWidget()
        row = QHBoxLayout(block)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(8)

        caption = QLabel(f"{currency}:")
        caption.setObjectName("Muted")
        caption.setFixedWidth(48)
        row.addWidget(caption)

        # Адрес можно выделить мышью: даже если кнопка копирования почему-то
        # недоступна, адрес реально унести через Ctrl+C.
        address_label = QLabel(address)
        address_label.setObjectName("Value")
        address_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        address_label.setToolTip(note)
        address_label.setWordWrap(True)
        row.addWidget(address_label, 1)

        copy_button = QPushButton("Копировать")
        copy_button.setToolTip(f"Скопировать адрес {note} в буфер обмена")
        copy_button.clicked.connect(
            lambda _checked=False, value=address: self._copy(value)
        )
        row.addWidget(copy_button)

        return block

    # ------------------------------------------------------------------
    #  Действия
    # ------------------------------------------------------------------
    def _copy(self, value: str) -> None:
        """Кладёт адрес в буфер обмена и показывает подтверждение."""
        QApplication.clipboard().setText(value)
        log.info("Адрес для поддержки скопирован: %s…", value[:12])
        self._set_status(COPIED_TEXT)

    def _set_status(self, text: str) -> None:
        if self.status_label is None:  # pragma: no cover — статус есть после _build
            return
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 — Qt API
        """Смена темы при открытом диалоге перекрашивает иконку и статус.

        Главное окно перекрашивает страницы, но об открытых диалогах не знает
        (их ведёт модальный цикл). Поэтому диалог слушает собственные события
        стиля: тема применяется ко всему приложению, и QDialog получает
        ``StyleChange``.
        """
        super().changeEvent(event)
        if event.type() in (
            QEvent.Type.StyleChange,
            QEvent.Type.PaletteChange,
        ):
            self.refresh_theme()

    def refresh_theme(self) -> None:
        """Перекрашивает иконку и статус под активную тему (остальное — QSS)."""
        if self.icon_label is not None:
            self.icon_label.setPixmap(
                make_pixmap("heart", ICON_SIZE, Theme.color("accent"))
            )
        if self.status_label is not None:
            self.status_label.setStyleSheet(f"color: {Theme.color('muted')};")


__all__ = ["COPIED_TEXT", "DIALOG_TEXT", "DIALOG_TITLE", "DONATIONS", "SupportDialog"]
