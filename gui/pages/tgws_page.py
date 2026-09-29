"""Страница «TG WS Proxy»: статус и запуск локального MTProto-прокси.

`TG WS Proxy <https://github.com/Flowseal/tg-ws-proxy>`_ (Flowseal — тот же
автор, что и у запрета) — отдельное приложение: оно поднимает локальный
MTProto-прокси для Telegram (по умолчанию ``127.0.0.1:1443``) и живёт в
системном трее. Раздел устроен по образцу «Обход (WARP)», но намеренно проще:
он не реализует прокси сам, ничего не устанавливает и не останавливает
приложение в обычной работе.

Что умеет страница:

* показать состояние процесса — «Работает», «Не запущен» или «Не найден»;
* запустить exe, если процесс не запущен (``Popen`` **без**
  ``CREATE_NO_WINDOW``: трей-приложение TG WS Proxy должно появиться);
* указать путь к exe вручную — автопоиск ищет строго ``TgWsProxy*.exe``, а
  скачанный файл может называться иначе; путь хранится в
  ``QSettings("ZapretGUI", "Paths")`` (ключ ``tgws_path``);
* открыть страницу релизов на GitHub.

Подключение Telegram идёт **через трей самого TG WS Proxy** («Открыть в
Telegram»), поэтому ссылки ``tg://proxy`` раздел не собирает: secret
генерируется динамически, и без чтения конфига прокси его не узнать — страница
лишь подсказывает, где нажать.

Проверяется только процесс (через ``psutil``), а не порт: прокси может слушать
другой порт, а имя процесса остаётся прежним. ``psutil`` — необязательная
зависимость: без него состояние процесса неизвестно, страница показывает
подсказку, но кнопка «Запустить» остаётся рабочей (запуск от него не зависит).

Обновление (проверка релизов, ``taskkill`` и замена exe) живёт на странице
«Обновление» — см. :class:`core.tgws_updater.TgwsUpdater` и карточку «TG WS
Proxy (Flowseal)».
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from config import TGWS_GITHUB_RELEASES_URL
from core.settings import set_tgws_path
from core.tgws_manager import (
    NOT_INSTALLED_HINT,
    PSUTIL_MISSING_HINT,
    RUNNING_HINT,
    TgwsManager,
)
from gui.pages.base import Page, WindowApi
from gui.theme import Theme, repolish
from gui.widgets import ModernCard, StatusIndicator

log = logging.getLogger(__name__)

#: Заголовок карточки раздела.
CARD_TITLE = "TG WS Proxy"

#: Пояснение к карточке: чем раздел управляет и чем — нет.
CARD_HINT = (
    "Локальный MTProto-прокси для Telegram (Flowseal/tg-ws-proxy). Раздел не "
    "устанавливает прокси и не меняет его настройки: он показывает состояние "
    "процесса и запускает приложение. Подключение Telegram — через трей TG WS "
    "Proxy («Открыть в Telegram»)."
)

#: Подпись состояния, когда exe не найден (короче подсказки в карточке).
NOT_FOUND_TEXT = "Не найден"
#: Подпись состояния, когда процесс не запущен.
STOPPED_TEXT = "Не запущен"
#: Подпись состояния, когда процесс работает.
RUNNING_TEXT = "Работает"
#: Подпись состояния, пока идёт первая проверка.
CHECKING_TEXT = "Проверка..."
#: Подпись состояния, когда нет ``psutil``: без него нельзя утверждать ни
#: «запущен», ни «не запущен» — состояние именно неизвестно.
UNKNOWN_TEXT = "Неизвестно"

#: Подсказка под индикатором, когда exe есть, но процесс не запущен.
STOPPED_HINT = (
    "Приложение не запущено. Нажмите «Запустить», чтобы поднять прокси."
)

#: Подсказка, когда exe не найден: ссылка на релизы и объяснение, почему
#: кнопка запуска неактивна.
INSTALL_HINT = (
    "Приложение не найдено — запускать нечего. Скачайте его со страницы "
    f'<a href="{TGWS_GITHUB_RELEASES_URL}">Flowseal/tg-ws-proxy</a> '
    "(раздел управляет уже скачанным exe, а не устанавливает его сам)."
)

#: Подсказка, если exe не найден и в интерфейсе показывается строка адреса.
NOT_FOUND_PATH = "не найден"


class TgwsPage(Page):
    """Раздел «TG WS Proxy»: статус процесса и запуск приложения."""

    #: Сообщение для шапки окна: (текст, уровень info/success/error).
    status_message = pyqtSignal(str, str)
    #: Ошибка операции: (заголовок, сообщение) — окно показывает диалог.
    failed = pyqtSignal(str, str)

    def __init__(
        self,
        window: WindowApi,
        manager: TgwsManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            window,
            CARD_TITLE,
            parent=parent,
            spacing=12,
            margins=(18, 14, 18, 14),
        )
        #: Логика работы с приложением (запуск и проверка — в фоне).
        self.manager = manager if manager is not None else TgwsManager()
        #: Идёт фоновая операция: кнопки на это время блокируются.
        self._busy = False
        #: Последний снимок состояния: (путь, версия, запущен, есть psutil).
        #: Хранится ради перекраски при смене темы — чтобы не запускать
        #: проверки из отрисовки.
        self._snapshot: tuple[Path | None, str, bool, bool] | None = None
        #: Уровень цвета подсказки под индикатором (для смены темы).
        self._reason_level = "muted"

        self._build()
        self._apply_availability()

    # ==================================================================
    #  Построение
    # ==================================================================
    def _build(self) -> None:
        self.status_card = ModernCard(CARD_TITLE, CARD_HINT)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.indicator = StatusIndicator()
        self.indicator.set_text(CHECKING_TEXT, "muted")
        row.addWidget(self.indicator)
        row.addStretch(1)
        self.status_card.add_layout(row)

        # Подсказка под индикатором — единственное место, где показывается
        # INSTALL_HINT. Отдельный label для неё был вторым показом того же
        # текста с той же жёлтой подсветкой, поэтому его убрали.
        self.reason_label = QLabel("")
        self.reason_label.setObjectName("Muted")
        self.reason_label.setWordWrap(True)
        self.reason_label.setVisible(False)
        # В INSTALL_HINT есть ссылка на релизы: без этого флага QLabel рисует
        # её как ссылку, но не открывает — раньше за это отвечал отдельный
        # label (``setOpenExternalLinks``).
        self.reason_label.setOpenExternalLinks(True)
        self.status_card.add_widget(self.reason_label)

        self.psutil_hint = QLabel(PSUTIL_MISSING_HINT)
        self.psutil_hint.setObjectName("Warning")
        self.psutil_hint.setWordWrap(True)
        self.psutil_hint.setVisible(False)
        self.status_card.add_widget(self.psutil_hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = self.make_button(
            "Запустить",
            "primary",
            tooltip="Запустить TgWsProxy_windows.exe: в трее появится значок прокси",
        )
        self.start_button.clicked.connect(self.on_start)
        buttons.addWidget(self.start_button)

        self.refresh_button = self.make_button(
            "Обновить статус",
            tooltip="Перечитать состояние процесса и версию приложения",
        )
        self.refresh_button.clicked.connect(self.on_refresh)
        buttons.addWidget(self.refresh_button)

        # Путь к exe: вариант оформления выбирает _apply_availability — когда
        # приложение не найдено, это самое полезное действие, и кнопка
        # становится главной (primary).
        self.set_path_button = self.make_button(
            "Указать путь к exe",
            tooltip="Выбрать TgWsProxy*.exe вручную",
        )
        self.set_path_button.clicked.connect(self.on_set_path)
        buttons.addWidget(self.set_path_button)

        self.releases_button = self.make_button(
            "Открыть на GitHub",
            tooltip=f"Открыть {TGWS_GITHUB_RELEASES_URL} в браузере",
        )
        self.releases_button.clicked.connect(self.on_open_releases)
        buttons.addWidget(self.releases_button)
        buttons.addStretch(1)
        self.status_card.add_layout(buttons)

        # Строки «подпись — значение»: путь к exe, версия и состояние процесса.
        self.path_value = self.kv_row(self.status_card.body, "Путь к приложению")
        self.version_value = self.kv_row(self.status_card.body, "Версия")
        self.process_value = self.kv_row(self.status_card.body, "Статус процесса")

        self.add_card(self.status_card)
        self.finish_layout()

    # ==================================================================
    #  Обновление состояния
    # ==================================================================
    def refresh(self) -> None:
        """Перечитывает состояние процесса, путь и версию в фоне.

        ``--version`` запускает сам exe, поэтому и путь, и версия, и проверка
        процесса собираются в фоновом потоке одной задачей: интерфейс не
        подвисает.
        """
        if self._busy:
            return

        # Кэш версии сбрасывается: exe могли заменить (обновление) — версию
        # нужно прочитать заново, а не показывать прошлую.
        self.manager.reset_version_cache()
        self._set_busy(True)
        worker = self.window.run_async(
            self._snapshot_state,
            on_success=self._on_snapshot,
            on_error=self._on_snapshot_error,
        )
        if worker is None:  # вызов не из потока окна — запрос переадресован
            self._set_busy(False)

    def _snapshot_state(self) -> tuple:
        """Фоновый снимок: ``(exe, версия, запущен, есть psutil)``.

        Путь запрашивается первым: если exe нет, версию и процесс проверять
        незачем — это сэкономит один запуск процесса. Версия не спрашивается,
        если сборка уже показала, что флага ``--version`` у неё нет: иначе
        каждое открытие раздела запускало бы exe и ждало таймаут.

        Последний элемент — «есть ли ``psutil``» — спрашивается у менеджера,
        а не подставляется как ``True``: без ``psutil``
        :meth:`TgwsManager.is_running` всегда возвращает ``False``, и страница
        без этого флага показывала бы «не запущен» вместо «неизвестно».
        """
        exe = self.manager.tgws_exe_path()
        if exe is None:
            return (None, "", False, self.manager.psutil_available())
        # Флаг читается у менеджера, а не подставляется как True (см. выше).
        psutil_ok = self.manager.psutil_available()
        version = self.manager.version() if self.manager.version_supported() else ""
        return (exe, version, self.manager.is_running(), psutil_ok)

    def _on_snapshot(self, data: object) -> None:
        self._set_busy(False)
        if not (isinstance(data, tuple) and len(data) == 4):
            log.warning("Неожиданный ответ проверки TG WS Proxy: %r", data)
            return

        exe, version, running, psutil_ok = data
        self._snapshot = (exe, str(version or ""), bool(running), bool(psutil_ok))
        self._apply_snapshot()
        self._apply_availability()

    def _on_snapshot_error(self, message: str) -> None:
        self._set_busy(False)
        self._apply_availability()
        text = (message or "Не удалось проверить TG WS Proxy").splitlines()[0]
        self._apply_reason(text, "error")
        self.status_message.emit(text, "error")

    def _apply_snapshot(self) -> None:
        """Показывает снимок: индикатор, подсказку и строки карточки."""
        if self._snapshot is None:
            return

        exe, version, running, psutil_ok = self._snapshot
        installed = exe is not None

        self.path_value.setText(str(exe) if installed else NOT_FOUND_PATH)
        self.path_value.setToolTip(
            str(exe)
            if installed
            else (
                "exe не найден: автопоиск ищет только TgWsProxy*.exe — "
                "укажите путь к файлу вручную"
            )
        )
        # Версия берётся у менеджера: снимок запускает ``--version``, и его
        # ответ уже лежит в кэше. Кэш остаётся заполненным и после закрытия
        # страницы — повторный запуск exe при возвращении не нужен.
        version = self.manager.known_version() or version
        self.version_value.setText(version or "неизвестно")

        # Без psutil проверка процесса не выполнялась: «запущен/не запущен» в
        # этом случае — догадка, поэтому в строке стоит «неизвестен».
        if not psutil_ok:
            self.process_value.setText("неизвестен (psutil не установлен)")
        else:
            self.process_value.setText("запущен" if running else "не запущен")

        # Подсказка «приложение не найдено» (INSTALL_HINT) показывается ниже —
        # в reason_label; отдельного label для неё больше нет.
        self.psutil_hint.setVisible(installed and not psutil_ok)

        if not installed:
            self.indicator.set_text(NOT_FOUND_TEXT, "muted")
            self.indicator.setToolTip(NOT_INSTALLED_HINT)
            self._apply_reason(INSTALL_HINT, "warning")
            return

        # Без psutil состояние процесса неизвестно, а не «не запущен»: в снимке
        # ``running`` в этом случае всегда False (см. TgwsManager.is_running),
        # и индикатор повторял бы ложное «Не запущен». «exe не найден» важнее и
        # разобран веткой выше — там psutil ни при чём.
        if not psutil_ok:
            self.indicator.set_text(UNKNOWN_TEXT, "muted")
            self.indicator.setToolTip(PSUTIL_MISSING_HINT)
            self._apply_reason(PSUTIL_MISSING_HINT, "muted")
            return

        if running:
            self.indicator.set_text(RUNNING_TEXT, "success")
            self.indicator.setToolTip(RUNNING_HINT)
            self._apply_reason(RUNNING_HINT, "muted")
            return

        self.indicator.set_text(STOPPED_TEXT, "muted")
        self.indicator.setToolTip(
            "Процесс TgWsProxy*.exe не найден. Нажмите «Запустить»."
        )
        self._apply_reason(STOPPED_HINT, "muted")

    def _apply_reason(self, text: str, level: str) -> None:
        """Показывает подсказку под индикатором и запоминает её уровень.

        Уровень хранится: подпись красится inline (QSS её цвет не перекрасит),
        и при смене темы её нужно покрасить тем же уровнем заново.
        """
        self._reason_level = level
        if text != self.reason_label.text():
            self.reason_label.setText(text)
        self.reason_label.setVisible(bool(text))
        self._paint(self.reason_label, level)

    def _apply_availability(self) -> None:
        """Включает кнопки по состоянию приложения и фоновой задачи.

        «Запустить» активна, только если exe найден, процесс не запущен и нет
        фоновой операции. «Обновить статус» остаётся активной всегда, когда
        проверять есть что, а «Открыть на GitHub» — всегда: скачать приложение
        можно и без него самого. «Указать путь к exe» активна **всегда** (кроме
        времени фоновой операции): путь можно переопределить и у найденного
        приложения, а когда exe не найден — это единственный способ его задать.
        """
        exe, _version, running, _psutil_ok = self._snapshot or (None, "", False, True)
        installed = exe is not None

        self.start_button.setEnabled(installed and not running and not self._busy)
        self.refresh_button.setEnabled(installed and not self._busy)
        self.releases_button.setEnabled(True)
        self.set_path_button.setEnabled(not self._busy)
        # Когда exe не найден, главное действие — указать путь к нему:
        # «Запустить» в этом состоянии неактивна, и primary переходит к этой
        # кнопке. У найденного приложения primary снимается, чтобы не спорить
        # с «Запустить».
        self._set_variant(self.set_path_button, None if installed else "primary")

        if not installed:
            self.start_button.setToolTip(
                "TG WS Proxy не найден: скачайте exe со страницы релизов или "
                "укажите путь к нему вручную"
            )
        elif running:
            self.start_button.setToolTip(
                "TG WS Proxy уже запущен — он работает в своём трее"
            )
        else:
            self.start_button.setToolTip(
                "Запустить TgWsProxy_windows.exe: в трее появится значок прокси"
            )

        # Кнопка «Запустить» остаётся рабочей и без psutil: запуск не требует
        # этого модуля, недоступна только проверка процесса (подсказка выше).
        if installed and not _psutil_ok:
            self.process_value.setToolTip(PSUTIL_MISSING_HINT)
        else:
            self.process_value.setToolTip("")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_availability()

    # ==================================================================
    #  Действия
    # ==================================================================
    def on_refresh(self) -> None:
        """Кнопка «Обновить статус»."""
        self.refresh()

    def on_start(self) -> None:
        """Кнопка «Запустить»: поднять прокси и перечитать состояние.

        Запуск идёт в фоне: ``Popen`` возвращается быстро, но страница после
        него ждёт регистрации процесса, и это ожидание не должно блокировать
        интерфейс.
        """
        if self._busy:
            return

        self._set_busy(True)
        self.status_message.emit("Запуск TG WS Proxy...", "info")
        self.window.run_async(
            self.manager.start,
            on_success=self._on_start_done,
            on_error=self._on_start_error,
            busy_message="Запуск TG WS Proxy...",
        )

    def _on_start_done(self, result: object) -> None:
        self._set_busy(False)
        ok, message = self._as_result(result)
        if ok:
            log.info("TG WS Proxy: %s", message)
            self.status_message.emit(message or "TG WS Proxy запущен", "success")
        else:
            self.status_message.emit(message.splitlines()[0], "error")
            self.failed.emit("TG WS Proxy", message)
        self.refresh()

    def _on_start_error(self, message: str) -> None:
        self._set_busy(False)
        text = (message or "Не удалось запустить TG WS Proxy").splitlines()[0]
        self.status_message.emit(text, "error")
        self.failed.emit("TG WS Proxy", message or text)
        self.refresh()

    @staticmethod
    def _as_result(result: object) -> tuple[bool, str]:
        """Приводит ответ команды к ``(успех, сообщение)``."""
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1] or "")
        return False, "Неожиданный ответ при запуске TG WS Proxy."

    def on_set_path(self) -> None:
        """Кнопка «Указать путь к exe»: ручной выбор приложения.

        Нужна потому, что автопоиск ищет строго ``TgWsProxy*.exe``: скачанный
        файл может называться иначе (``tg-ws-proxy.exe``, ``TgWsProxy (1).exe``)
        или лежать во вложенной папке. Путь сохраняется в
        ``QSettings("ZapretGUI", "Paths")`` (ключ ``tgws_path``) и при
        следующем запуске приложения подхватывается
        :class:`core.tgws_manager.TgwsManager` сам.

        Любой exe принимается только после подтверждения: запуск чужого файла
        — ответственность пользователя, но предупредить о нём нужно.
        """
        current = self.manager.tgws_exe_path()
        if current is not None:
            start_dir = str(current.parent)
        else:
            # Автопоиск ничего не нашёл: скорее всего, exe лежит в «Загрузках».
            start_dir = str(Path.home() / "Downloads")

        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            "Выберите TgWsProxy*.exe",
            start_dir,
            "Исполняемые файлы (*.exe);;Все файлы (*)",
        )
        if not chosen:
            return
        path = Path(chosen)

        if path.suffix.lower() != ".exe":
            QMessageBox.warning(
                self,
                CARD_TITLE,
                f"Это не исполняемый файл (.exe):\n{path}",
            )
            return

        if not self.manager.is_probably_tgws(path):
            ok = QMessageBox.question(
                self,
                CARD_TITLE,
                f"Имя файла не похоже на TG WS Proxy:\n{path.name}\n\n"
                "Использовать всё равно?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                return

        # Порядок важен: сначала сохраняем в настройки, потом сообщаем менеджеру,
        # и только затем перечитываем снимок — иначе ``refresh`` показал бы
        # старый путь.
        saved = set_tgws_path(path)
        self.manager.set_exe_path(path)
        self.window.set_status(f"Путь к TG WS Proxy сохранён: {saved}", 6000)
        self.refresh()

    def on_open_releases(self) -> None:
        """Кнопка «Открыть на GitHub»: страница релизов в браузере."""
        if QDesktopServices.openUrl(QUrl(TGWS_GITHUB_RELEASES_URL)):
            self.status_message.emit("Страница релизов открыта в браузере", "info")
            return
        self.failed.emit(
            "TG WS Proxy",
            f"Не удалось открыть браузер.\nСсылка: {TGWS_GITHUB_RELEASES_URL}",
        )

    # ==================================================================
    #  Оформление
    # ==================================================================
    @staticmethod
    def _paint(label: QLabel, level: str) -> None:
        """Красит подпись цветом темы (``success`` / ``warning`` / ``muted``)."""
        label.setStyleSheet(f"color: {Theme.color(level)};")

    @staticmethod
    def _set_variant(button: QPushButton, variant: str | None) -> None:
        """Меняет вариант оформления кнопки (QSS-селектор ``[variant=...]``).

        Qt не перекрашивает виджет при смене динамического свойства, поэтому
        стиль пересобирается через :func:`gui.theme.repolish`. Пустая строка
        (вариант снят) с селектором ``[variant="primary"]`` не совпадает —
        кнопка становится обычной. Если вариант не изменился, ничего не
        делаем: ``_apply_availability`` вызывается часто.
        """
        current = button.property("variant") or ""
        new = variant or ""
        if current == new:
            return
        button.setProperty("variant", new)
        repolish(button)

    def refresh_theme(self) -> None:
        """Перекрашивает виджеты под активную тему.

        Проверок здесь нет намеренно: они запускают процесс и сеть, а смена
        темы не должна ждать exe. Индикатор перекрашивается по последнему
        снимку, подписи — по сохранённым уровням.
        """
        super().refresh_theme()
        self.status_card.refresh_theme()
        self.indicator.refresh_theme()
        if self._snapshot is not None:
            self._apply_snapshot()
        else:
            self._paint(self.reason_label, self._reason_level)

    # ==================================================================
    #  События
    # ==================================================================
    def showEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """При открытии раздела перечитывает состояние приложения.

        Автозапуска при этом не происходит: раздел только показывает состояние
        и подсказывает, что нажать.
        """
        super().showEvent(event)
        self.refresh()


__all__ = [
    "CARD_HINT",
    "CARD_TITLE",
    "INSTALL_HINT",
    "NOT_FOUND_TEXT",
    "RUNNING_TEXT",
    "STOPPED_HINT",
    "STOPPED_TEXT",
    "TgwsPage",
]
