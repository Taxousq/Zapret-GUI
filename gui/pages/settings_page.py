"""Страница «Настройки»: папка запрета и место хранения настроек.

Раздел показывает путь к папке запрета, позволяет сменить его, проверить
валидность и переустановить запрет (скачать заново с GitHub). Путь хранится в
``QSettings("ZapretGUI", "Paths")`` (ключ ``zapret_path``) — см.
:mod:`core.settings`; смена пути сообщается главному окну сигналом
:attr:`SettingsPage.path_changed`, а оно пересобирает ядро и перечитывает
список стратегий.

Карточка «Обновления» отсюда **убрана**: и версия GUI, и версия запрета, и
Cloudflare WARP живут теперь в разделе «Обновление» (см.
:class:`gui.pages.update_page.UpdatePage`). Здесь осталось только то, что
относится к настройкам: путь к запрету и место хранения.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QWidget,
)

from core import portable
from core.settings import settings_file
from core.zapret_locator import DOWNLOAD_FAILED_HINT, DOWNLOAD_OK_HINT, ZapretLocator
from gui.pages.base import Page, WindowApi
from gui.theme import Theme
from gui.widgets import Card, ModernCard

log = logging.getLogger(__name__)

#: Текст ошибки для папки, не похожей на запрет.
INVALID_DIR_TEXT = (
    "Эта папка не похожа на запрет.\n"
    "Убедитесь, что там есть service.bat или bin\\winws.exe."
)


class SettingsPage(Page):
    """Карточки «Папка запрета» и «Где хранятся настройки»."""

    #: Путь к запрету изменён — главное окно пересобирает ядро и стратегии.
    path_changed = pyqtSignal(object)
    #: Ошибка операции: (заголовок, сообщение).
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        window: WindowApi,
        locator: ZapretLocator | None = None,
        zapret_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(window, "Настройки", parent=parent)
        self.locator = locator or ZapretLocator()
        self.zapret_path: Path | None = Path(zapret_path) if zapret_path else None
        self._downloading = False

        self.card = ModernCard(
            "Папка запрета",
            "Путь к папке с запретом хранится в настройках приложения "
            "и используется всеми разделами GUI.",
        )
        self._build()
        self.add_card(self.card)

        self.storage_card = Card("Где хранятся настройки")
        self._build_storage()
        self.add_card(self.storage_card)

        self.finish_layout()
        self.refresh_state()
        self.refresh_theme()

    # ------------------------------------------------------------------
    #  Построение
    # ------------------------------------------------------------------
    def _build(self) -> None:
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        caption = QLabel("Текущий путь:")
        caption.setObjectName("Muted")
        self.path_label = QLabel("—")
        self.path_label.setObjectName("Value")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        path_row.addWidget(caption)
        path_row.addWidget(self.path_label, 1)
        self.card.add_layout(path_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.card.add_widget(self.status_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.change_button = self.make_button(
            "Изменить папку",
            "primary",
            tooltip="Выбрать другую папку с запретом",
        )
        self.change_button.clicked.connect(self.on_change_folder)
        self.check_button = self.make_button(
            "Проверить папку",
            tooltip="Проверить, что в папке есть service.bat или bin\\winws.exe",
        )
        self.check_button.clicked.connect(self.on_check_folder)
        self.redownload_button = self.make_button(
            "Скачать заново с GitHub",
            tooltip="Скачать последний релиз запрета и распаковать его в выбранную папку",
        )
        self.redownload_button.clicked.connect(self.on_redownload)
        buttons.addWidget(self.change_button)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.redownload_button)
        buttons.addStretch(1)
        self.card.add_layout(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        self.card.add_widget(self.progress)

        self.download_status = QLabel("")
        self.download_status.setObjectName("Muted")
        self.download_status.setWordWrap(True)
        self.download_status.setVisible(False)
        self.card.add_widget(self.download_status)

        note = QLabel(
            "Служба Windows здесь не устанавливается: после скачивания запустите "
            "service.bat от имени администратора и выберите «Install Service»."
        )
        note.setObjectName("Hint")
        note.setWordWrap(True)
        self.card.add_widget(note)

    def _build_storage(self) -> None:
        mode = QLabel(
            "Portable-режим включён: настройки и журнал лежат рядом с exe."
            if portable.enabled
            else "Обычный режим: настройки хранятся в реестре Windows "
            "(%APPDATA%\\ZapretGUI)."
        )
        mode.setObjectName("Hint")
        mode.setWordWrap(True)
        self.storage_card.add_widget(mode)

        where = QLabel(settings_file() or "—")
        where.setObjectName("Muted")
        where.setWordWrap(True)
        self.storage_card.add_widget(where)

    # ------------------------------------------------------------------
    #  Состояние
    # ------------------------------------------------------------------
    def set_zapret_path(self, zapret_path: Path | None) -> None:
        """Запоминает путь, выбранный главным окном."""
        self.zapret_path = Path(zapret_path) if zapret_path else None
        self.refresh_state()

    def refresh_state(self) -> None:
        """Обновляет путь и статус проверки.

        Временная папка для текущего пути допускается: его выбрал человек
        (окно передаёт сюда путь из настроек). О подозрительном месте
        сообщается предупреждением, а не отказом.
        """
        if self.zapret_path is None or not self.locator.is_valid(
            self.zapret_path, allow_temp=True
        ):
            # В ограниченном режиме окно подставляет путь-заглушку, которого
            # не существует: показывать его пользователю бессмысленно.
            if self.zapret_path is not None:
                log.debug("Путь к запрету невалиден: %s", self.zapret_path)
            self.path_label.setText("не задан")
            self._set_status(
                "Запрет не найден. Укажите папку с запретом или скачайте её заново.",
                "warning",
            )
            return

        self.path_label.setText(str(self.zapret_path))
        warning = self.locator.temp_warning(self.zapret_path)
        if warning:
            self._set_status(warning, "warning")
            return
        self._set_status(
            f"Папка запрета найдена. {self.locator.describe(self.zapret_path)}.",
            "success",
        )

    def _set_status(self, text: str, color: str = "muted") -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {Theme.color(color)};")

    # ------------------------------------------------------------------
    #  Действия
    # ------------------------------------------------------------------
    def on_change_folder(self) -> None:
        """Выбор другой папки запрета."""
        if self.zapret_path is not None and self.locator.is_valid(
            self.zapret_path, allow_temp=True
        ):
            start = str(self.zapret_path)
        else:
            start = str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Папка с запретом",
            start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not chosen:
            return
        path = Path(chosen)
        if not self.locator.is_valid(path, allow_temp=True):
            QMessageBox.warning(self, "Настройки", f"{INVALID_DIR_TEXT}\n\n{path}")
            return
        warning = self.locator.temp_warning(path)
        if warning and not self.window.confirm(
            "Настройки",
            "Продолжить с этой папкой?",
            warning,
        ):
            return
        self._apply_path(path, "Папка запрета изменена")

    def on_check_folder(self) -> None:
        """Проверка валидности текущего пути."""
        if self.zapret_path is None:
            QMessageBox.information(
                self,
                "Проверка папки",
                "Путь к запрету не задан.\n\nНажмите «Изменить папку» или "
                "«Скачать заново с GitHub».",
            )
            return

        self.refresh_state()
        if self.locator.is_valid(self.zapret_path, allow_temp=True):
            warning = self.locator.temp_warning(self.zapret_path)
            if warning:
                QMessageBox.warning(
                    self,
                    "Проверка папки",
                    f"{warning}\n\n{self.locator.describe(self.zapret_path)}.",
                )
                return
            QMessageBox.information(
                self,
                "Проверка папки",
                f"Папка запрета в порядке:\n{self.zapret_path}\n\n"
                f"{self.locator.describe(self.zapret_path)}.",
            )
        else:
            QMessageBox.warning(
                self,
                "Проверка папки",
                f"{INVALID_DIR_TEXT}\n\n{self.zapret_path}",
            )

    def on_redownload(self) -> None:
        """Скачивает запрет заново в выбранную папку."""
        if self._downloading:
            return

        if self.zapret_path is not None and self.locator.is_valid(
            self.zapret_path, allow_temp=True
        ):
            start = str(self.zapret_path.parent)
        else:
            start = str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Куда скачать запрет",
            start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not chosen:
            return
        target = Path(chosen)

        try:
            if any(target.iterdir()) and not self.locator.is_valid(
                target, allow_temp=True
            ):
                ok = QMessageBox.question(
                    self,
                    "Скачивание запрета",
                    f"В папке уже есть файлы:\n{target}\n\n"
                    "Файлы запрета будут распакованы поверх: существующие файлы "
                    "с такими же именами будут заменены.\n\nПродолжить?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ok != QMessageBox.StandardButton.Yes:
                    return
        except OSError as exc:
            self.failed.emit("Скачивание запрета", f"Не удалось открыть папку:\n{exc}")
            return

        self._set_downloading(True, "Получение сведений о последнем релизе...")
        worker = self.window.run_async(
            self.locator.download_from_github,
            target,
            on_progress=self._on_progress,
            on_status=self._on_download_status,
            busy_message="Скачивание запрета с GitHub...",
        )
        if worker is None:
            self._set_downloading(False)
            return
        worker.finished_signal.connect(self._on_download_finished)
        worker.error_signal.connect(self._on_download_error)

    def _apply_path(self, path: Path, status: str) -> None:
        """Сообщает окну о новом пути (сохранение — на стороне окна)."""
        self.zapret_path = path
        self.refresh_state()
        self.window.set_status(f"{status}: {path}", 6000)
        self.path_changed.emit(path)

    # ------------------------------------------------------------------
    #  Скачивание
    # ------------------------------------------------------------------
    def _on_progress(self, downloaded: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(99, int(downloaded * 100 / total)))
            self._set_download_status(
                f"Скачано {downloaded / 1024 / 1024:.1f} из "
                f"{total / 1024 / 1024:.1f} МБ..."
            )
        else:
            self.progress.setRange(0, 0)
            self._set_download_status(f"Скачано {downloaded / 1024 / 1024:.1f} МБ...")

    def _on_download_status(self, text: str) -> None:
        if text:
            self._set_download_status(text)

    def _on_download_finished(self, result: object) -> None:
        self._set_downloading(False)
        ok = False
        message = ""
        if isinstance(result, tuple) and len(result) == 2:
            ok, message = bool(result[0]), str(result[1])
        else:  # pragma: no cover — download_from_github всегда отдаёт пару
            message = f"Неожиданный ответ загрузчика: {result!r}"

        if not ok:
            QMessageBox.critical(self, "Скачивание запрета", message or DOWNLOAD_FAILED_HINT)
            return

        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        target = _downloaded_dir(message)
        if target is not None and self.locator.is_valid(target, allow_temp=True):
            self._apply_path(target, "Запрет скачан")
        QMessageBox.information(
            self,
            "Скачивание запрета",
            message or f"Запрет скачан.\n\n{DOWNLOAD_OK_HINT}",
        )

    def _on_download_error(self, message: str) -> None:
        self._set_downloading(False)
        text = message or DOWNLOAD_FAILED_HINT
        if DOWNLOAD_FAILED_HINT not in text:
            text = f"{text}\n\n{DOWNLOAD_FAILED_HINT}"
        QMessageBox.critical(self, "Скачивание запрета", text)

    def _set_downloading(self, busy: bool, status: str = "") -> None:
        self._downloading = busy
        for button in (self.change_button, self.check_button, self.redownload_button):
            button.setEnabled(not busy)
        self.progress.setVisible(busy or self.progress.value() > 0)
        if busy:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
        if status:
            self._set_download_status(status)

    def _set_download_status(self, text: str) -> None:
        self.download_status.setText(text)
        self.download_status.setVisible(bool(text))

    # ------------------------------------------------------------------
    #  Тема
    # ------------------------------------------------------------------
    def refresh_theme(self) -> None:
        super().refresh_theme()
        self.card.refresh_theme()
        self.storage_card.refresh_theme()
        self.refresh_state()


def _downloaded_dir(message: str) -> Path | None:
    """Достаёт путь из сообщения «Запрет скачан в <папка>.»."""
    prefix = "Запрет скачан в "
    for line in message.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            candidate = line[len(prefix) :].rstrip(".").strip()
            if candidate:
                return Path(candidate)
    return None
