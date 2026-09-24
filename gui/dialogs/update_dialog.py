"""Диалог обновления приложения.

Открывается, когда GitHub сообщил о релизе новее установленного: показывает
текущую и новую версии, список изменений (``body`` релиза), кнопки «Скачать и
установить», «Позже» и «Открыть на GitHub».

Скачивание установщика идёт в фоне (``WindowApi.run_async``), прогресс
приходит сигналом из потока. После загрузки диалог предупреждает, что
приложение будет закрыто: установщик запускается «отвязанным» процессом и
работает уже без GUI (см. :class:`core.app_updater.AppUpdater`). О фактическом
запуске диалог сообщает сигналом :attr:`UpdateDialog.installer_started` —
главное окно по нему завершает работу.

Portable-сборка автоматически не обновляется: кнопка установки в диалоге
отключена, а вместо неё показывается подсказка.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import APP_GITHUB_RELEASES_URL, APP_NAME
from core.app_updater import PORTABLE_TEXT, AppRelease, AppUpdater
from gui.icons import make_pixmap
from gui.pages.base import WindowApi
from gui.theme import Theme

log = logging.getLogger(__name__)

#: Заголовок диалога.
DIALOG_TITLE = "Обновление Zapret GUI"

#: Текст предупреждения после скачивания установщика.
CLOSE_WARNING = (
    "Приложение будет закрыто, установщик запустится автоматически "
    "и завершит обновление."
)


def display_version(version: str) -> str:
    """Версия без ведущей ``v`` — для показа пользователю."""
    return str(version or "").lstrip("vV").strip() or "—"


class UpdateDialog(QDialog):
    """Модальный диалог «доступна новая версия».

    :param window: интерфейс главного окна — им диалог запускает скачивание в
        фоне, чтобы окно не подвисало;
    :param updater: загрузчик обновлений приложения;
    :param release: сведения о релизе с GitHub;
    :param parent: родительское окно.
    """

    #: Прогресс скачивания, процент 0..100 (из фонового потока).
    progress_signal = pyqtSignal(float)
    #: Установщик запущен — приложение должно завершиться.
    installer_started = pyqtSignal()

    def __init__(
        self,
        window: WindowApi | None,
        updater: AppUpdater,
        release: AppRelease,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.updater = updater
        self.release = release

        self._busy = False
        #: Путь к уже скачанному установщику (повторное нажатие не скачивает заново).
        self._installer_path: Path | None = None

        self.setWindowTitle(f"{APP_NAME} — обновление")
        self.setModal(True)
        self.setMinimumWidth(540)

        self._build()
        self._set_busy(False)
        self.refresh_theme()

        self.progress_signal.connect(self._on_progress)

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
        self.icon_label.setPixmap(make_pixmap("download", 36, Theme.color("accent")))
        head.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        title = QLabel("Доступна новая версия")
        title.setObjectName("HeaderTitle")
        self.text_label = QLabel(
            f"Текущая версия: {display_version(self.updater.current_version)} → "
            f"новая: {display_version(self.release.version)}"
        )
        self.text_label.setObjectName("Value")
        self.text_label.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(self.text_label)
        head.addLayout(titles, 1)
        layout.addLayout(head)

        asset_text = self.release.asset_name or "установщик не найден"
        sizes = self.release.size_text
        hint = QLabel(
            f"Установщик: {asset_text}"
            + (f" ({sizes})" if sizes != "—" else "")
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        changes_caption = QLabel("Что нового:")
        changes_caption.setObjectName("Muted")
        layout.addWidget(changes_caption)

        self.changes = QPlainTextEdit()
        self.changes.setReadOnly(True)
        self.changes.setPlainText(
            self.release.notes.strip() or "Описание изменений не указано."
        )
        self.changes.setMinimumHeight(96)
        self.changes.setMaximumHeight(180)
        layout.addWidget(self.changes)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.install_button = QPushButton("Скачать и установить")
        self.install_button.setProperty("variant", "primary")
        self.install_button.setToolTip(
            "Скачать установщик и запустить его: приложение будет закрыто"
        )
        self.install_button.clicked.connect(self._on_install)

        self.later_button = QPushButton("Позже")
        self.later_button.setToolTip("Закрыть диалог, ничего не скачивая")
        self.later_button.clicked.connect(self.reject)

        self.github_button = QPushButton("Открыть на GitHub")
        self.github_button.setToolTip("Открыть страницу релиза в браузере")
        self.github_button.clicked.connect(self._open_github)

        buttons.addWidget(self.install_button)
        buttons.addWidget(self.later_button)
        buttons.addStretch(1)
        buttons.addWidget(self.github_button)
        layout.addLayout(buttons)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        if self.updater.portable:
            # Portable-сборку установщик не обновит: ставить нечего.
            self.install_button.setEnabled(False)
            self.install_button.setToolTip(PORTABLE_TEXT)
            self._set_status(PORTABLE_TEXT)

    # ------------------------------------------------------------------
    #  Действия
    # ------------------------------------------------------------------
    def _on_install(self) -> None:
        """Скачивает установщик (или сразу запускает уже скачанный)."""
        if self._busy:
            return
        if self.updater.portable:
            QMessageBox.information(self, DIALOG_TITLE, PORTABLE_TEXT)
            return

        if self._installer_path is not None and self._installer_path.is_file():
            self._launch_installer(self._installer_path)
            return

        if self.window is None:
            QMessageBox.warning(
                self,
                DIALOG_TITLE,
                "Скачивание недоступно: диалог открыт без главного окна.",
            )
            return
        if not self.release.asset_url:
            QMessageBox.warning(
                self,
                DIALOG_TITLE,
                "В релизе нет ссылки на установщик. Скачайте его со страницы "
                "релиза вручную.",
            )
            return

        self._set_busy(True, "Скачивание установщика...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        worker = self.window.run_async(
            self.updater.download_installer,
            self.release.asset_url,
            progress_cb=self.progress_signal.emit,
            busy_message="Скачивание обновления...",
        )
        if worker is None:
            # Диалог открыт не из потока окна — ждать результат нечего.
            self._set_busy(False)
            return
        worker.finished_signal.connect(self._on_downloaded)
        worker.error_signal.connect(self._on_error)

    def _open_github(self) -> None:
        """Открывает страницу релиза в браузере."""
        url = self.release.html_url or APP_GITHUB_RELEASES_URL
        QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------------
    #  Скачивание
    # ------------------------------------------------------------------
    def _on_progress(self, value: float) -> None:
        """Прогресс скачивания (значение приходит из фонового потока)."""
        if value <= 0:
            # Общий размер неизвестен — показываем «идёт».
            self.progress.setRange(0, 0)
            self._set_status("Скачивание установщика...")
            return
        self.progress.setRange(0, 100)
        # 100% показываем только после проверки файла — иначе полоса «врёт».
        self.progress.setValue(min(99, int(value)))
        self._set_status(f"Скачано {int(value)}%...")

    def _on_downloaded(self, result: object) -> None:
        """Установщик скачан (или загрузка завершилась ошибкой)."""
        self._set_busy(False)
        ok = False
        payload: object = ""
        if isinstance(result, tuple) and len(result) == 2:
            ok, payload = bool(result[0]), result[1]
        else:  # pragma: no cover — download_installer всегда отдаёт пару
            payload = f"Неожиданный ответ загрузчика: {result!r}"

        if not ok:
            self._on_error(str(payload))
            return

        path = Path(str(payload))
        self._installer_path = path
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._set_status("Установщик скачан.")

        answer = QMessageBox.question(
            self,
            DIALOG_TITLE,
            CLOSE_WARNING,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._set_status("Обновление отложено: установщик уже скачан, "
                             "кнопка «Скачать и установить» запустит его.")
            return
        self._launch_installer(path)

    def _on_error(self, message: str) -> None:
        """Ошибка загрузки: показываем и не закрываем приложение."""
        self._set_busy(False)
        self.progress.setVisible(False)
        text = message or "Не удалось скачать установщик."
        self._set_status(text)
        QMessageBox.critical(self, DIALOG_TITLE, text)

    def _launch_installer(self, path: Path) -> None:
        """Запускает установщик; при ошибке приложение остаётся открытым."""
        ok, message = self.updater.run_installer(path)
        if not ok:
            log.error("Не удалось запустить установщик: %s", message)
            self._set_status(message)
            QMessageBox.critical(self, DIALOG_TITLE, message)
            return

        log.info("Установщик обновления запущен из диалога")
        self._set_status("Установщик запущен. Приложение закрывается...")
        self.accept()
        self.installer_started.emit()

    # ------------------------------------------------------------------
    #  Состояние интерфейса
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, status: str = "") -> None:
        """Блокирует кнопки на время скачивания."""
        self._busy = busy
        for button in (self.install_button, self.later_button, self.github_button):
            button.setEnabled(not busy)
        if busy:
            self.progress.setVisible(True)
        elif status:
            self.progress.setVisible(False)
        if status:
            self._set_status(status)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def refresh_theme(self) -> None:
        """Перекрашивает иконку под активную тему (остальное — QSS)."""
        self.icon_label.setPixmap(make_pixmap("download", 36, Theme.color("accent")))

    def reject(self) -> None:  # noqa: D102 — Qt API
        """«Позже»: скачивание не прерываем, но окно закрыть даём."""
        if self._busy:
            return
        log.info("Обновление отложено пользователем")
        super().reject()


__all__ = ["CLOSE_WARNING", "DIALOG_TITLE", "UpdateDialog", "display_version"]
