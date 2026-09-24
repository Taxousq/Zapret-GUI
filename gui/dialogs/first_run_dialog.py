"""Мастер первого запуска: где взять папку запрета.

Если запрет не найден автопоиском (:class:`core.zapret_locator.ZapretLocator`)
и путь не сохранён в ``QSettings("ZapretGUI", "Paths")``, главное окно
показывает модальный :class:`FirstRunDialog`. В нём три пути:

* **Скачать с GitHub** — последний релиз Flowseal распаковывается в выбранную
  папку (прогресс-бар показывает скачивание);
* **Указать папку вручную** — ``QFileDialog.getExistingDirectory`` и проверка
  :meth:`ZapretLocator.is_valid`;
* **Отмена** — приложение запустится в ограниченном режиме: страницы
  «Стратегии» и «Тестирование» покажут подсказку, а статус-бар — «Запрет не
  найден».

Служба Windows здесь **не** устанавливается: после скачивания пользователь сам
запускает ``service.bat`` от имени администратора и выбирает
``Install Service``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import APP_NAME
from core.settings import set_zapret_path
from core.zapret_locator import DOWNLOAD_FAILED_HINT, DOWNLOAD_OK_HINT, ZapretLocator
from gui.icons import make_pixmap
from gui.pages.base import WindowApi
from gui.theme import Theme

log = logging.getLogger(__name__)

#: Заголовок диалога и пояснение для пользователя.
DIALOG_TITLE = "Первый запуск"
DIALOG_TEXT = "Папка с запретом не найдена. Выберите, что делать:"

#: Текст ошибки для папки, не похожей на запрет.
INVALID_DIR_TEXT = (
    "Эта папка не похожа на запрет.\n"
    "Убедитесь, что там есть service.bat или bin\\winws.exe."
)


class FirstRunDialog(QDialog):
    """Модальный мастер первого запуска.

    :param window: интерфейс главного окна — им диалог запускает скачивание в
        фоне (:meth:`WindowApi.run_async`), чтобы окно не подвисало;
    :param locator: искатель/загрузчик запрета (по умолчанию свой);
    :param parent: родительское окно.

    Результат: :attr:`result_path` — путь к папке запрета, если пользователь
    скачал запрет или указал папку. ``None`` — пользователь отменил.
    """

    def __init__(
        self,
        window: WindowApi | None = None,
        locator: ZapretLocator | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.locator = locator or ZapretLocator()

        #: Выбранная папка запрета (``None`` — пользователь отменил).
        self.result_path: Path | None = None
        self._busy = False
        self._mode = ""

        self.setWindowTitle(f"{APP_NAME} — {DIALOG_TITLE}")
        self.setModal(True)
        self.setMinimumWidth(520)

        self._build()
        self._set_busy(False)
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
        self.icon_label.setPixmap(make_pixmap("download", 36, Theme.color("accent")))
        head.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(4)
        self.title_label = QLabel(DIALOG_TITLE)
        self.title_label.setObjectName("HeaderTitle")
        self.text_label = QLabel(DIALOG_TEXT)
        self.text_label.setObjectName("Value")
        self.text_label.setWordWrap(True)
        titles.addWidget(self.title_label)
        titles.addWidget(self.text_label)
        head.addLayout(titles, 1)
        layout.addLayout(head)

        hint = QLabel(
            "Запрет не входит в поставку GUI: его можно скачать с GitHub "
            "(последний релиз Flowseal) или указать уже установленную папку."
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.download_button = QPushButton("Скачать с GitHub")
        self.download_button.setProperty("variant", "primary")
        self.download_button.setToolTip(
            "Скачать последний релиз запрета и распаковать его в выбранную папку"
        )
        self.download_button.clicked.connect(self._on_download)

        self.manual_button = QPushButton("Указать папку вручную")
        self.manual_button.setToolTip("Выбрать папку с уже установленным запретом")
        self.manual_button.clicked.connect(self._on_manual)

        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setToolTip(
            "Продолжить без запрета: страницы «Стратегии» и «Тестирование» "
            "будут недоступны"
        )
        self.cancel_button.clicked.connect(self.reject)

        buttons.addWidget(self.download_button)
        buttons.addWidget(self.manual_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
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

    # ------------------------------------------------------------------
    #  Режимы
    # ------------------------------------------------------------------
    def _on_download(self) -> None:
        """Скачивает запрет с GitHub в выбранную пользователем папку."""
        if self._busy:
            return
        if self.window is None:
            QMessageBox.warning(
                self,
                DIALOG_TITLE,
                "Скачивание недоступно: диалог открыт без главного окна.",
            )
            return

        chosen = QFileDialog.getExistingDirectory(
            self,
            "Куда скачать запрет",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not chosen:
            return
        target = Path(chosen)
        if self._confirm_overwrite(target) is False:
            return

        self._mode = "download"
        self._set_busy(True, "Получение сведений о последнем релизе...")
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        worker = self.window.run_async(
            self.locator.download_from_github,
            target,
            on_progress=self._on_progress,
            on_status=self._on_status_ready,
            busy_message="Скачивание запрета с GitHub...",
        )
        if worker is None:
            # Диалог мог быть открыт не из потока окна — тогда ждать нечего.
            self._set_busy(False)
            return
        worker.finished_signal.connect(self._on_download_ready)
        worker.error_signal.connect(self._on_download_error)

    def _on_manual(self) -> None:
        """Просит указать папку с уже установленным запретом."""
        if self._busy:
            return
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Папка с запретом",
            str(Path.home()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not chosen:
            return
        path = Path(chosen)
        # Временная папка не запрещается (пользователь мог выбрать её
        # осознанно), но о подозрительном месте его предупреждают.
        if not self.locator.is_valid(path, allow_temp=True):
            QMessageBox.warning(self, DIALOG_TITLE, f"{INVALID_DIR_TEXT}\n\n{path}")
            return
        warning = self.locator.temp_warning(path)
        if warning:
            ok = QMessageBox.question(
                self,
                DIALOG_TITLE,
                f"{warning}\n\nПродолжить с этой папкой?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return

        self._accept_path(path)

    def _accept_path(self, path: Path) -> None:
        """Сохраняет путь в настройках и закрывает мастер."""
        saved = set_zapret_path(path)
        self.result_path = Path(saved) if saved else path
        log.info("Папка запрета выбрана в мастере первого запуска: %s", self.result_path)
        self.accept()

    def _confirm_overwrite(self, target: Path) -> bool | None:
        """Предупреждает, если в выбранной папке уже что-то есть.

        ``False`` — пользователь отказался, ``True``/``None`` — можно писать.
        """
        try:
            items = [item.name for item in target.iterdir()]
        except OSError as exc:
            QMessageBox.warning(self, DIALOG_TITLE, f"Не удалось открыть папку:\n{exc}")
            return False
        if not items:
            return True
        ok = QMessageBox.question(
            self,
            DIALOG_TITLE,
            f"В папке уже есть файлы ({len(items)} шт.).\n"
            "Файлы запрета будут распакованы поверх — существующие файлы с "
            "такими же именами будут заменены.\n\nПродолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ok == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    #  Скачивание
    # ------------------------------------------------------------------
    def _on_progress(self, downloaded: int, total: int) -> None:
        """Прогресс скачивания (вызывается из фонового потока).

        Qt доставит этот вызов в главный поток автоматически: ``self`` —
        QObject, а сигналы воркера соединены с методами диалога.
        """
        if total > 0:
            self.progress.setRange(0, 100)
            percent = int(downloaded * 100 / total)
            # 100% показываем только после распаковки — иначе полоса «врёт».
            self.progress.setValue(min(99, percent))
            self._set_status(
                f"Скачано {downloaded / 1024 / 1024:.1f} из "
                f"{total / 1024 / 1024:.1f} МБ..."
            )
        else:
            self.progress.setRange(0, 0)
            self._set_status(f"Скачано {downloaded / 1024 / 1024:.1f} МБ...")

    def _on_status_ready(self, text: str) -> None:
        """Стадия операции из фонового потока."""
        if text:
            self._set_status(text)

    def _on_download_ready(self, result: object) -> None:
        """Скачивание завершилось (успех или ошибка от ``ZapretLocator``)."""
        ok = False
        message = ""
        if isinstance(result, tuple) and len(result) == 2:
            ok, message = bool(result[0]), str(result[1])
        else:  # pragma: no cover — download_from_github всегда отдаёт пару
            message = f"Неожиданный ответ загрузчика: {result!r}"

        self._set_busy(False)
        if not ok:
            QMessageBox.critical(self, DIALOG_TITLE, message or DOWNLOAD_FAILED_HINT)
            return

        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        target = self._downloaded_dir(message)
        if target is not None and self.locator.is_valid(target, allow_temp=True):
            saved = set_zapret_path(target)
            self.result_path = Path(saved) if saved else target
        QMessageBox.information(
            self,
            DIALOG_TITLE,
            message or f"Запрет скачан.\n\n{DOWNLOAD_OK_HINT}",
        )
        self.accept()

    def _on_download_error(self, message: str) -> None:
        """Ошибка в фоновом потоке скачивания (например, нет сети)."""
        self._set_busy(False)
        text = message or DOWNLOAD_FAILED_HINT
        if DOWNLOAD_FAILED_HINT not in text:
            text = f"{text}\n\n{DOWNLOAD_FAILED_HINT}"
        QMessageBox.critical(self, DIALOG_TITLE, text)

    @staticmethod
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

    # ------------------------------------------------------------------
    #  Состояние интерфейса
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, status: str = "") -> None:
        """Блокирует кнопки на время скачивания."""
        self._busy = busy
        for button in (self.download_button, self.manual_button, self.cancel_button):
            button.setEnabled(not busy)
        if status:
            self._set_status(status)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    def refresh_theme(self) -> None:
        """Перекрашивает иконку под активную тему (остальное — QSS)."""
        self.icon_label.setPixmap(make_pixmap("download", 36, Theme.color("accent")))

    def reject(self) -> None:  # noqa: D102 — Qt API
        """Отмена: скачивание не прерываем, но закрывать окно не даём."""
        if self._busy:
            return
        log.info("Мастер первого запуска отменён — работаем без запрета")
        super().reject()
