"""Страница «Обновление» — три источника обновлений в одном разделе.

Раздел отвечает на один вопрос: «что у меня устарело и как это обновить».
Источников три, и обновляются они по-разному, поэтому здесь три карточки:

* **Запрет (Flowseal)** — папка запрета. Проверяется последний релиз
  ``Flowseal/zapret-discord-youtube``, скачивается ``.zip`` и распаковывается
  в папку запрета с заменой файлов (пользовательские списки не трогаются).
  Логика — в :class:`core.updater.Updater`; если служба была запущена, она
  останавливается на время обновления и запускается заново.
* **Интерфейс (Zapret GUI)** — само приложение. Проверяется последний релиз
  ``config.APP_GITHUB_REPO``, скачивается установщик Inno Setup и запускается
  отдельным процессом: приложение закрывается, установщик ставит новую версию.
  Логика — в :class:`core.app_updater.AppUpdater`, диалог — в
  :class:`gui.dialogs.UpdateDialog` (его открывает главное окно).
* **Cloudflare WARP** — внешний клиент. Он **обновляется сам** через
  Cloudflare; GUI лишь показывает версию и по кнопке спрашивает у ``winget``,
  нет ли версии новее (:class:`core.warp_updater.WarpUpdater`). Управление
  самим WARP осталось в разделе «Обход (WARP)».

Кнопка «Проверить всё» запускает все три проверки сразу, у каждой карточки
свой прогресс-бар. Проверки идут в фоне (``WindowApi.run_async``), поэтому
окно не подвисает, а прогресс приходит сигналами из рабочих потоков.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QWidget,
)

from config import (
    APP_GITHUB_REPO,
    APP_GITHUB_RELEASES_URL,
    APP_VERSION,
    GITHUB_REPO,
    WARP_DOWNLOAD_URL,
)
from core import portable
from core.app_updater import PORTABLE_TEXT, AppRelease, AppUpdater
from core.service_manager import ServiceManager
from core.settings import check_updates_on_startup, set_check_updates_on_startup
from core.updater import UpdateCheck, UpdateInfo, Updater
from core.warp_updater import (
    NO_WINGET_TEXT,
    WarpUpdater,
    WarpUpdaterError,
)
from gui.pages.base import Page, WindowApi
from gui.theme import Theme
from gui.widgets import ModernCard

log = logging.getLogger(__name__)

#: Заголовки карточек — по ним же собирается сводка «Проверить всё».
ZAPRET_CARD_TITLE = "Запрет (Flowseal)"
APP_CARD_TITLE = "Интерфейс (Zapret GUI)"
WARP_CARD_TITLE = "Cloudflare WARP"

#: Подпись строки версии, когда версию определить не удалось.
UNKNOWN_VERSION = "неизвестно"

#: Цветовые уровни статусов раздела. Логика одна на все три карточки:
#:
#: * ``success`` (зелёный) — версия актуальна, обновление не нужно;
#: * ``warning`` (жёлтый) — доступно обновление (или подсказка, требующая
#:   действия пользователя);
#: * ``error`` (красный) — проверка не удалась (нет сети, ошибка API);
#: * ``muted`` (серый) — проверка не выполнялась, статус неизвестен.
LEVEL_SUCCESS = "success"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"
LEVEL_MUTED = "muted"

#: Приоритет уровней в сводке «Проверить всё»: важнейший побеждает.
LEVEL_PRIORITY = {LEVEL_ERROR: 3, LEVEL_WARNING: 2, LEVEL_MUTED: 1, LEVEL_SUCCESS: 0}

#: Начало информационной строки «обновление не нужно». Главное окно передаёт её
#: в том же параметре, что и текст ошибки проверки, поэтому различаем их по
#: смыслу: это успех, а не сбой.
LATEST_VERSION_PREFIX = "Установлена последняя версия"


class UpdatePage(Page):
    """Три карточки обновлений: запрет, GUI и Cloudflare WARP."""

    #: Проверена установленная версия запрета (для статус-бара главного окна).
    version_checked = pyqtSignal(str)
    #: Обновление запрета применено — нужно перечитать стратегии и службу.
    update_applied = pyqtSignal()
    #: Ошибка операции: (заголовок, сообщение).
    failed = pyqtSignal(str, str)
    #: Уведомление в трее: (заголовок, текст).
    notify = pyqtSignal(str, str)
    #: Прогресс скачивания запрета: (скачано байт, всего байт) — из потока.
    progress_signal = pyqtSignal(int, int)
    #: Текстовая стадия обновления запрета — из потока.
    status_signal = pyqtSignal(str)
    #: Прогресс скачивания установщика GUI, процент 0..100 — из потока.
    app_progress_signal = pyqtSignal(float)
    #: Тестовый прогресс в шапке окна: виден/не виден.
    progress_visible = pyqtSignal(bool)

    #: Карточка GUI просит главное окно проверить последний релиз приложения.
    app_check_requested = pyqtSignal()
    #: Нажата кнопка «Обновить» у карточки GUI (окно откроет диалог).
    app_update_requested = pyqtSignal()
    #: Результат проверки приложения разошёлся с данными окна — баннер в шапке.
    app_release_changed = pyqtSignal(object)
    #: Изменён чекбокс «Проверять обновления при запуске».
    check_on_startup_changed = pyqtSignal(bool)

    def __init__(
        self,
        window: WindowApi,
        updater: Updater,
        service_manager: ServiceManager,
        app_updater: AppUpdater | None = None,
        warp_updater: WarpUpdater | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(window, "Обновление", parent=parent)

        self.updater = updater
        self.service_manager = service_manager
        #: Обновление самого GUI (релизы config.APP_GITHUB_REPO). Главное окно
        #: передаёт свой экземпляр, чтобы не проверять GitHub дважды.
        self.app_updater = app_updater if app_updater is not None else AppUpdater()
        #: Проверка обновления Cloudflare WARP через winget.
        self.warp_updater = warp_updater if warp_updater is not None else WarpUpdater()

        #: Сведения о релизе запрета, готовые к установке (``None`` — нет).
        self._latest_info: UpdateInfo | None = None
        #: Сведения о релизе GUI (``None`` — проверка не нашла обновления).
        self._app_release: AppRelease | None = None
        #: Можно ли проверять WARP через winget (кнопка «Открыть сайт» иначе).
        self._warp_winget = False
        #: Идёт ли «Проверить всё» и сколько результатов ещё ждём.
        self._batch_running = False
        self._batch_expected = 0
        #: Строки сводки «Проверить всё» (по одной на карточку).
        self._batch_parts: list[str] = []
        #: Уровни карточек в текущей сводке (для цвета подписи).
        self._batch_levels: list[str] = []

        #: Текущий цветовой уровень каждой подписи: хранится, чтобы
        #: перекрасить её при смене темы (см. :meth:`refresh_theme`).
        self._message_level = LEVEL_MUTED
        self._app_message_level = LEVEL_MUTED
        self._warp_message_level = LEVEL_MUTED
        self._summary_level = LEVEL_MUTED

        self._build()
        self.progress_signal.connect(self._on_update_progress)
        self.status_signal.connect(self._on_update_status)
        self.app_progress_signal.connect(self._on_app_progress)
        self.refresh_theme()

    # ==================================================================
    #  Построение
    # ==================================================================
    def _build(self) -> None:
        """Собирает шапку раздела и три карточки."""
        self._build_toolbar()
        self._build_zapret_card()
        self._build_app_card()
        self._build_warp_card()
        self.finish_layout()

    def _build_toolbar(self) -> None:
        """Верхняя панель: «Проверить всё» и общая подпись."""
        bar = QHBoxLayout()
        bar.setSpacing(10)

        self.check_all_button = self.make_button(
            "Проверить всё",
            "primary",
            tooltip="Проверить обновления запрета, интерфейса и Cloudflare WARP",
        )
        self.check_all_button.clicked.connect(self.on_check_all)
        bar.addWidget(self.check_all_button)

        self.summary_label = QLabel(
            "Проверка обновлений всех трёх источников: запрета, интерфейса и WARP."
        )
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)
        bar.addWidget(self.summary_label, 1)

        self.content_layout.addLayout(bar)

    # ------------------------------------------------------------------
    #  Карточка 1: запрет
    # ------------------------------------------------------------------
    def _build_zapret_card(self) -> None:
        self.zapret_card = ModernCard(
            ZAPRET_CARD_TITLE,
            f"Обновление с GitHub: {GITHUB_REPO}. Файлы распаковываются в папку "
            "запрета; пользовательские списки (*-user.*) не перезаписываются.",
        )
        self.zapret_card.hint_label.setTextFormat(Qt.TextFormat.RichText)
        self.zapret_card.hint_label.setOpenExternalLinks(True)

        self.local_version_value = self.kv_row(
            self.zapret_card.body, "Установленная версия:"
        )
        self.latest_version_value = self.kv_row(
            self.zapret_card.body, "Доступная версия:"
        )

        self.progress = self._make_progress()
        self.zapret_card.add_widget(self.progress)

        self.message_label = QLabel("Обновление не проверялось.")
        self.message_label.setObjectName("Muted")
        self.message_label.setWordWrap(True)
        self.zapret_card.add_widget(self.message_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.check_button = self.make_button(
            "Проверить обновление",
            tooltip="Спросить у GitHub последний релиз запрета",
        )
        self.check_button.clicked.connect(self.on_check_updates)
        self.install_button = self.make_button(
            "Обновить",
            "primary",
            tooltip="Скачать релиз запрета и распаковать его в папку запрета",
        )
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self.on_install_update)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.install_button)
        buttons.addStretch(1)
        self.zapret_card.add_layout(buttons)

        self.add_card(self.zapret_card)

    # ------------------------------------------------------------------
    #  Карточка 2: интерфейс
    # ------------------------------------------------------------------
    def _build_app_card(self) -> None:
        self.app_card = ModernCard(
            APP_CARD_TITLE,
            "Обновление самого приложения установщиком из релизов "
            f'<a href="{APP_GITHUB_RELEASES_URL}">{APP_GITHUB_REPO}</a>: '
            "перед установкой приложение будет закрыто.",
        )
        self.app_card.hint_label.setTextFormat(Qt.TextFormat.RichText)
        self.app_card.hint_label.setOpenExternalLinks(True)

        self.app_local_value = self.kv_row(self.app_card.body, "Текущая версия:")
        self.app_local_value.setText(APP_VERSION)
        self.app_latest_value = self.kv_row(self.app_card.body, "Доступная версия:")

        self.app_progress = self._make_progress()
        self.app_card.add_widget(self.app_progress)

        self.app_message_label = QLabel("Обновление не проверялось.")
        self.app_message_label.setObjectName("Muted")
        self.app_message_label.setWordWrap(True)
        self.app_card.add_widget(self.app_message_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.app_check_button = self.make_button(
            "Проверить обновление",
            tooltip="Спросить у GitHub последний релиз Zapret GUI",
        )
        self.app_check_button.clicked.connect(self.on_check_app_update)
        self.app_update_button = self.make_button(
            "Обновить",
            "primary",
            tooltip="Скачать установщик и обновить приложение (GUI закроется)",
        )
        self.app_update_button.setEnabled(False)
        self.app_update_button.clicked.connect(self.app_update_requested.emit)
        buttons.addWidget(self.app_check_button)
        buttons.addWidget(self.app_update_button)
        buttons.addStretch(1)
        self.app_card.add_layout(buttons)

        self.app_note = QLabel("")
        self.app_note.setObjectName("Hint")
        self.app_note.setWordWrap(True)
        self.app_card.add_widget(self.app_note)

        # Чекбокс переехал сюда из раздела «Настройки» вместе с карточкой
        # «Обновления»: настройка относится к обновлениям, а не к путям.
        self.check_on_startup = QCheckBox("Проверять обновления при запуске")
        self.check_on_startup.setToolTip(
            "Проверка идёт в фоне через 5 секунд после старта и раз в сутки; "
            "ошибки сети при этом не показываются"
        )
        self.check_on_startup.setChecked(check_updates_on_startup())
        self.check_on_startup.toggled.connect(self._on_check_on_startup_toggled)
        self.app_card.add_widget(self.check_on_startup)

        if portable.enabled:
            # Portable-сборку установщик не обновит: файлы лежат рядом с exe.
            self.app_update_button.setEnabled(False)
            self.app_update_button.setToolTip(PORTABLE_TEXT)
            self.app_note.setText(PORTABLE_TEXT)
            self.app_note.setStyleSheet(f"color: {Theme.color(LEVEL_WARNING)};")

        self.add_card(self.app_card)

    def _on_check_on_startup_toggled(self, enabled: bool) -> None:
        """Сохраняет чекбокс и сообщает окну (оно включает/выключает таймер)."""
        saved = set_check_updates_on_startup(enabled)
        log.info("Проверять обновления при запуске: %s", "да" if saved else "нет")
        self.check_on_startup_changed.emit(saved)

    # ------------------------------------------------------------------
    #  Карточка 3: Cloudflare WARP
    # ------------------------------------------------------------------
    def _build_warp_card(self) -> None:
        self.warp_card = ModernCard(
            WARP_CARD_TITLE,
            "Клиент Cloudflare WARP обновляется автоматически через Cloudflare. "
            "Проверка лишь сообщает, есть ли версия новее установленной.",
        )

        self.warp_version_value = self.kv_row(
            self.warp_card.body, "Установленная версия:"
        )
        self.warp_version_value.setText("проверяется...")

        self.warp_progress = self._make_progress()
        self.warp_card.add_widget(self.warp_progress)

        self.warp_message_label = QLabel("Обновляется автоматически через Cloudflare.")
        self.warp_message_label.setObjectName("Muted")
        self.warp_message_label.setWordWrap(True)
        self.warp_card.add_widget(self.warp_message_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.warp_check_button = self.make_button(
            "Проверить обновление",
            tooltip=f"Спросить у winget, есть ли версия WARP новее установленной",
        )
        self.warp_check_button.clicked.connect(self.on_check_warp)
        self.warp_site_button = self.make_button(
            "Открыть сайт",
            tooltip=f"Открыть {WARP_DOWNLOAD_URL} в браузере",
        )
        self.warp_site_button.clicked.connect(self.on_open_warp_site)
        buttons.addWidget(self.warp_check_button)
        buttons.addWidget(self.warp_site_button)
        buttons.addStretch(1)
        self.warp_card.add_layout(buttons)

        self.add_card(self.warp_card)

    @staticmethod
    def _make_progress() -> QProgressBar:
        """Прогресс-бар карточки (у каждой карточки свой)."""
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setVisible(False)
        return bar

    # ==================================================================
    #  Версии
    # ==================================================================
    def refresh_local_version(self) -> str:
        """Перечитывает версию запрета и WARP; возвращает версию запрета."""
        version = self.updater.get_local_version()
        self.local_version_value.setText(version)
        if version == UNKNOWN_VERSION:
            self.local_version_value.setStyleSheet(
                f"color: {Theme.color(LEVEL_WARNING)};"
            )
        else:
            self.local_version_value.setStyleSheet("")
        self.version_checked.emit(version)
        self.refresh_warp_version()
        return version

    def refresh_warp_version(self) -> str:
        """Показывает версию установленного WARP (или «не найден»)."""
        version = self.warp_updater.current_version()
        if version:
            self.warp_version_value.setText(version)
            self.warp_version_value.setStyleSheet("")
        else:
            self.warp_version_value.setText("не найден")
            self.warp_version_value.setStyleSheet(
                f"color: {Theme.color(LEVEL_WARNING)};"
            )
        self.refresh_warp_buttons()
        return version or ""

    def refresh_warp_buttons(self) -> None:
        """Настраивает кнопки карточки WARP под то, что доступно в системе.

        Без ``winget`` проверять обновления нечем, поэтому кнопка «Проверить
        обновление» подменяется кнопкой «Открыть сайт», а если WARP вообще не
        установлен — обе кнопки неактивны (ставить его здесь нечем).
        """
        self._warp_winget = self.warp_updater.is_winget_available()
        installed = bool(self.warp_updater.current_version())

        if not installed:
            self.warp_check_button.setEnabled(False)
            self.warp_site_button.setEnabled(False)
            self.warp_message_label.setText("Cloudflare WARP не найден.")
            # Статус неизвестен (проверять нечего) — серый, без «ошибки».
            self._set_warp_message_color(LEVEL_MUTED)
            return None

        self.warp_site_button.setEnabled(True)
        if self._warp_winget:
            self.warp_check_button.setEnabled(True)
            self.warp_check_button.setToolTip(
                "Спросить у winget, есть ли версия WARP новее установленной"
            )
            return None

        self.warp_check_button.setEnabled(False)
        self.warp_check_button.setToolTip(NO_WINGET_TEXT)
        return None

    # ==================================================================
    #  Состояние обновлений (для главного окна)
    # ==================================================================
    @property
    def app_release(self) -> AppRelease | None:
        """Сведения о найденном релизе GUI (``None`` — обновления нет)."""
        return self._app_release

    def zapret_update_available(self) -> bool:
        """Найден ли релиз запрета новее установленного."""
        return self._latest_info is not None

    # ==================================================================
    #  Карточка «Запрет»: проверка
    # ==================================================================
    def on_check_updates(self, batch: bool = False) -> tuple[bool, bool]:
        """Проверяет последний релиз запрета на GitHub.

        :param batch: вызов из «Проверить всё» — результат карточки попадёт в
            общую сводку;
        :returns: ``(нужно ли ждать фоновую задачу, учтён ли результат в сводке)``.
        """
        self.check_button.setEnabled(False)
        self.message_label.setText("Проверка обновлений на GitHub...")
        self._set_message_color(LEVEL_MUTED)
        worker = self.window.run_async(
            self.updater.check,
            on_success=self._on_checked,
            on_error=self._on_error,
            busy_message="Проверка обновлений...",
        )
        # Worker создаётся только в потоке окна: ``None`` означает, что запрос
        # переадресован туда, а результата здесь ждать нечего.
        return (worker is not None), batch

    def start_check(self) -> None:
        """Проверяет обновления запрета (быстрое действие дашборда).

        Обёртка над :meth:`on_check_updates` без установки обновления:
        скачивание и замена файлов остаются отдельным осознанным действием.
        """
        self.on_check_updates()

    def _on_error(self, message: str) -> None:
        self.check_button.setEnabled(True)
        self.message_label.setText(message)
        self._set_message_color(LEVEL_ERROR)
        self._finish_check(ZAPRET_CARD_TITLE, message, LEVEL_ERROR)
        self.failed.emit("Обновление", message)

    def _on_checked(self, check: object) -> None:
        self.check_button.setEnabled(True)
        if not isinstance(check, UpdateCheck):
            self._on_error("Неожиданный ответ при проверке обновлений.")
            return

        self.local_version_value.setText(check.local_version)
        self.latest_version_value.setText(
            check.latest.version if check.latest else "—"
        )
        self.version_checked.emit(check.local_version)

        self._latest_info = check.latest if check.has_update else None
        self.install_button.setEnabled(self._latest_info is not None)

        # Цвет по смыслу результата, а не «обновиться есть чему — значит
        # тревога»: актуальная версия — это хорошо (зелёный), доступное
        # обновление — жёлтый, сбой проверки — красный.
        if check.errors:
            color_key = LEVEL_ERROR
        elif check.has_update:
            color_key = LEVEL_WARNING
        else:
            color_key = LEVEL_SUCCESS
        self.message_label.setText(check.message)
        self._set_message_color(color_key)
        self.window.set_status(check.message.splitlines()[0], 8000)

        self._finish_check(ZAPRET_CARD_TITLE, check.message, color_key)
        if check.has_update:
            self.notify.emit("Доступно обновление", check.message[:180])
        # Главное окно поднимает баннер в шапке, если обновиться есть чему:
        # релиз запрета и версия GUI учитываются вместе.
        self.app_release_changed.emit(self._app_release)

    # ==================================================================
    #  Карточка «Интерфейс»: проверка
    # ==================================================================
    def on_check_app_update(self, batch: bool = False) -> tuple[bool, bool]:
        """Просит главное окно проверить релизы приложения.

        Проверку выполняет окно (``app_check_requested``): его задача —
        баннер в шапке, и результат проверки должен быть один на всех.

        :param batch: вызов из «Проверить всё»;
        :returns: ``(ждём ли фон, учитывать ли результат в сводке)``.
        """
        self.app_check_button.setEnabled(False)
        self.app_message_label.setText("Проверка обновлений на GitHub...")
        self._set_app_message_color(LEVEL_MUTED)
        self.app_check_requested.emit()
        return True, batch

    def on_app_check_result(
        self, release: object, has_update: bool, message: str = ""
    ) -> None:
        """Показывает результат проверки релизов приложения (зовёт окно).

        :param release: :class:`core.app_updater.AppRelease` или ``None``;
        :param has_update: есть ли версия новее установленной;
        :param message: текст ошибки проверки; информационная строка
            «Установлена последняя версия…» тоже приходит этим параметром, но
            ошибкой не считается (пусто — проверка прошла, обновления нет).
        """
        self.app_check_button.setEnabled(True)

        current = self.app_updater.current_version
        self.app_local_value.setText(current)
        self.app_local_value.setStyleSheet("")

        # Главное окно передаёт в ``message`` и текст ошибки, и информационную
        # строку «Установлена последняя версия…» (в репозитории нет релизов —
        # это не сбой). Информационную строку красим зелёным, ошибку — красным.
        informational = message.startswith(LATEST_VERSION_PREFIX)
        if message and not informational:
            self.app_latest_value.setText("—")
            self._app_release = None
            self.app_update_button.setEnabled(False)
            self.app_message_label.setText(message)
            self._set_app_message_color(LEVEL_ERROR)
            self._finish_check(APP_CARD_TITLE, message, LEVEL_ERROR)
            return

        if informational or not isinstance(release, AppRelease) or not has_update:
            shown = release.version if isinstance(release, AppRelease) else "—"
            self.app_latest_value.setText(shown if release is not None else "—")
            self._app_release = None
            self.app_update_button.setEnabled(False)
            text = message or f"Установлена последняя версия: {current}."
            self.app_message_label.setText(text)
            # Версия актуальна — это успех, а не ошибка: зелёный.
            self._set_app_message_color(LEVEL_SUCCESS)
            self._finish_check(APP_CARD_TITLE, text, LEVEL_SUCCESS)
            return

        self._app_release = release
        self.app_latest_value.setText(release.version)
        self.app_update_button.setEnabled(not portable.enabled)
        notes = " ".join(release.notes.split())
        text = f"Доступна новая версия: {release.version}."
        if notes:
            text += f"\nЧто нового: {notes[:400]}"
        self.app_message_label.setText(text)
        # Доступное обновление — предупреждение: жёлтый.
        self._set_app_message_color(LEVEL_WARNING)
        self.window.set_status(f"Доступна версия {release.version}", 8000)
        self._finish_check(
            APP_CARD_TITLE,
            f"Доступна новая версия: {release.version}",
            LEVEL_WARNING,
        )
        self.app_release_changed.emit(release)

    def on_app_progress(self, value: float) -> None:
        """Прогресс скачивания установщика (зовёт окно, см. диалог обновления)."""
        self._on_app_progress(value)

    def _on_app_progress(self, value: float) -> None:
        self.app_progress.setVisible(True)
        if value <= 0:
            # Общий размер неизвестен — показываем «идёт».
            self.app_progress.setRange(0, 0)
            return
        self.app_progress.setRange(0, 100)
        self.app_progress.setValue(min(99, int(value)))
        self.app_message_label.setText(f"Скачивание установщика: {int(value)}%...")
        self._set_app_message_color(LEVEL_MUTED)

    # ==================================================================
    #  Карточка «Cloudflare WARP»: проверка
    # ==================================================================
    def on_check_warp(self, batch: bool = False) -> tuple[bool, bool]:
        """Проверяет через winget, есть ли версия WARP новее установленной.

        :param batch: вызов из «Проверить всё»;
        :returns: ``(ждём ли фон, учитывать ли результат в сводке)``. Если
            WARP не установлен или winget недоступен, фоновой задачи нет —
            карточка просто сообщает об этом.
        """
        self.refresh_warp_version()
        installed = bool(self.warp_updater.current_version())
        if not installed or not self._warp_winget:
            # Ставить WARP и проверять его без winget нечем: сообщаем и всё.
            text = (
                "Cloudflare WARP не найден — установите его со страницы загрузки."
                if not installed
                else NO_WINGET_TEXT
            )
            self.warp_message_label.setText(text)
            # Не найден / нет winget — подсказка, требующая действия: жёлтый.
            self._set_warp_message_color(LEVEL_WARNING)
            self._finish_check(WARP_CARD_TITLE, text, LEVEL_WARNING)
            return False, batch

        self.warp_check_button.setEnabled(False)
        self.warp_progress.setRange(0, 0)
        self.warp_progress.setVisible(True)
        self.warp_message_label.setText("Проверка через winget...")
        self._set_warp_message_color(LEVEL_MUTED)

        worker = self.window.run_async(
            self.warp_updater.check_winget,
            on_success=self._on_warp_checked,
            on_error=self._on_warp_check_error,
            busy_message="Проверка обновления WARP...",
        )
        return (worker is not None), batch

    def _on_warp_checked(self, result: object) -> None:
        self.refresh_warp_buttons()
        self.warp_progress.setVisible(False)
        self.warp_progress.setRange(0, 100)
        self.warp_progress.setValue(0)

        available = False
        message = ""
        if isinstance(result, tuple) and len(result) == 2:
            available, message = bool(result[0]), str(result[1])
        else:  # pragma: no cover — check_winget всегда отдаёт пару
            message = f"Неожиданный ответ winget: {result!r}"

        self.warp_message_label.setText(message)
        # Есть версия новее — жёлтый; актуальная версия — зелёный.
        level = LEVEL_WARNING if available else LEVEL_SUCCESS
        self._set_warp_message_color(level)
        self._finish_check(WARP_CARD_TITLE, message, level)
        if available:
            self.notify.emit("Cloudflare WARP", message[:180])

    def _on_warp_check_error(self, message: str) -> None:
        self.refresh_warp_buttons()
        self.warp_progress.setVisible(False)
        self.warp_progress.setRange(0, 100)
        self.warp_progress.setValue(0)
        text = message or NO_WINGET_TEXT
        # Сообщения самого winget и текст об отсутствии winget — не ошибка
        # пользователя, показываем подсказкой; всё остальное — ошибкой.
        level = LEVEL_WARNING if not self._warp_winget else LEVEL_ERROR
        self.warp_message_label.setText(text)
        self._set_warp_message_color(level)
        self._finish_check(WARP_CARD_TITLE, text, level)
        if self._warp_winget:
            self.failed.emit("Обновление WARP", text)

    def on_open_warp_site(self) -> None:
        """Открывает страницу загрузки WARP в браузере."""
        if self.warp_updater.open_download_page():
            self.window.set_status("Страница загрузки WARP открыта в браузере", 6000)
            return
        self.failed.emit(
            "Cloudflare WARP",
            f"Не удалось открыть браузер.\nСсылка: {WARP_DOWNLOAD_URL}",
        )

    # ==================================================================
    #  «Проверить всё»
    # ==================================================================
    def on_check_all(self) -> None:
        """Запускает проверку всех трёх источников сразу.

        Сводка собирается по мере готовности карточек: результаты приходят
        из фоновых потоков в разное время, и ждать их все, чтобы показать
        первый, незачем. Счётчик ожидаемых результатов увеличивается **до**
        запуска проверки: фоновая задача успевает завершиться раньше, чем
        метод вернёт управление.
        """
        if self._batch_running:
            return

        self._batch_running = True
        self._batch_expected = 0
        self._batch_parts = []
        self._batch_levels = []
        self.check_all_button.setEnabled(False)
        self._set_summary_color(LEVEL_MUTED)
        self.summary_label.setText("Проверка обновлений: запрет, интерфейс, WARP...")
        self.window.set_status("Проверка обновлений...")

        checks = (
            (ZAPRET_CARD_TITLE, self.on_check_updates),
            (APP_CARD_TITLE, self.on_check_app_update),
            (WARP_CARD_TITLE, self.on_check_warp),
        )
        for _title, check in checks:
            # Счётчик растёт заранее: результат может прийти из потока ещё до
            # того, как мы запустим следующую проверку.
            self._batch_expected += 1
            _async, counted = check(True)
            if not counted:
                self._batch_expected -= 1

        if self._batch_expected <= 0:
            # Ни одна проверка не запустилась: сводку собирать не из чего.
            self._finish_batch()

    def _finish_check(
        self, title: str, message: str, level: str = LEVEL_MUTED
    ) -> None:
        """Запоминает результат проверки карточки и собирает сводку.

        :param level: цветовой уровень результата — по нему красится итоговая
            сводка «Проверить всё» (см. :data:`LEVEL_PRIORITY`).

        Результат попадает в сводку, только если запущено «Проверить всё»:
        одиночные проверки карточек отчитываются в своих подписях.
        """
        if not self._batch_running:
            return
        first_line = (message or "").splitlines()[0].strip() if message else ""
        self._batch_parts.append(f"{title}: {first_line or 'результат неизвестен'}")
        self._batch_levels.append(level)
        self._batch_expected -= 1
        if self._batch_expected <= 0:
            self._finish_batch()

    def _finish_batch(self) -> None:
        """Показывает сводку «Проверить всё»."""
        if not self._batch_running:
            return
        self._batch_running = False
        self._batch_expected = 0
        self.check_all_button.setEnabled(True)

        parts = self._batch_parts
        levels = self._batch_levels
        if not parts:
            self.summary_label.setText(
                "Проверить не удалось: источники обновлений недоступны."
            )
            self._set_summary_color(LEVEL_ERROR)
            self.window.set_status("Проверка обновлений не удалась", 8000, level="error")
            return

        self.summary_label.setText(" | ".join(parts))
        # Цвет сводки — по худшему результату: ошибка > обновление > unknown.
        worst = max(levels, key=lambda key: LEVEL_PRIORITY.get(key, 0), default=None)
        self._set_summary_color(worst or LEVEL_MUTED)
        self.window.set_status("Проверка обновлений завершена", 8000)

    # ==================================================================
    #  Установка обновления запрета
    # ==================================================================
    def on_install_update(self) -> None:
        info = self._latest_info
        if info is None:
            return
        running = self._latest_running()
        informative = (
            "Файлы запрета будут заменены файлами из последнего релиза.\n"
            "Пользовательские списки (*-user.*) не перезаписываются."
        )
        if running:
            informative += (
                "\n\nСлужба будет остановлена на время обновления и запущена заново."
            )
        if not self.window.confirm(
            "Установка обновления",
            f"Обновить запрет до версии {info.version}?",
            informative,
        ):
            return

        self.install_button.setEnabled(False)
        self.check_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress_visible.emit(True)
        self.message_label.setText("Подготовка...")
        self._set_message_color(LEVEL_MUTED)

        self.window.run_async(
            self._apply_update,
            info,
            on_success=self._on_installed,
            on_error=self._on_install_error,
            busy_message="Установка обновления...",
        )

    def _apply_update(self, info: UpdateInfo) -> tuple[bool, str]:
        """Выполняется в фоновом потоке: остановка службы, обновление, запуск."""
        was_running = self.service_manager.get_status().state.is_running
        if was_running:
            self.status_signal.emit("Остановка службы...")
            stopped, stop_message = self.service_manager.stop()
            if not stopped:
                return (
                    False,
                    f"Не удалось остановить службу перед обновлением.\n{stop_message}",
                )

        ok, message = self.updater.download_and_install(
            info,
            on_progress=lambda done, total: self.progress_signal.emit(done, total),
            on_status=lambda text: self.status_signal.emit(text),
        )

        if was_running:
            self.status_signal.emit("Запуск службы...")
            started, start_message = self.service_manager.start()
            if started:
                message += "\nСлужба запущена заново."
            else:
                message += f"\nСлужба не запустилась: {start_message}"

        return ok, message

    def _on_update_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, int(done * 100 / total))))
            self.message_label.setText(
                f"Скачивание: {done / 1024 / 1024:.1f} из {total / 1024 / 1024:.1f} МБ"
            )
        else:
            self.progress.setRange(0, 0)
            self.message_label.setText(f"Скачивание: {done / 1024 / 1024:.1f} МБ")
        self._set_message_color(LEVEL_MUTED)

    def _on_update_status(self, text: str) -> None:
        self.message_label.setText(text)
        self._set_message_color(LEVEL_MUTED)
        self.window.set_status(text, 6000)

    def _finish_update_ui(self) -> None:
        self.progress.setVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress_visible.emit(False)
        self.check_button.setEnabled(True)
        self.install_button.setEnabled(self._latest_info is not None)

    def _on_install_error(self, message: str) -> None:
        self._finish_update_ui()
        self.message_label.setText(message)
        self._set_message_color(LEVEL_ERROR)
        self.failed.emit("Обновление", message)

    def _on_installed(self, result) -> None:
        self._finish_update_ui()
        ok, message = result
        self.message_label.setText(message)
        self._set_message_color(LEVEL_SUCCESS if ok else LEVEL_ERROR)
        if ok:
            log.info("Обновление запрета применено: %s", message)
            self.window.set_status("Обновление применено", 8000)
            self.notify.emit("Обновление", message[:180])
            # Появились новые .bat-стратегии — перечитываем список и версию.
            self.latest_version_value.setText("—")
            self._latest_info = None
            self.install_button.setEnabled(False)
            self.refresh_local_version()
            self.update_applied.emit()
        else:
            self.failed.emit("Обновление", message)

    # ==================================================================
    #  Признак «служба запущена»
    # ==================================================================
    #: Признак «служба запущена»: обновляет главное окно при опросе статуса.
    is_running: bool = False

    def _latest_running(self) -> bool:
        """Была ли служба запущена (для предупреждения в диалоге)."""
        return self.is_running

    def set_service_running(self, running: bool) -> None:
        self.is_running = running

    # ==================================================================
    #  Оформление
    # ==================================================================
    # Цветовые уровни статусов описаны в :data:`LEVEL_SUCCESS` и соседях:
    # зелёный — актуально, жёлтый — есть обновление, красный — ошибка,
    # серый — статус неизвестен.
    @staticmethod
    def _paint(label: QLabel, key: str) -> None:
        """Красит подпись цветом активной темы (без хардкода оттенков)."""
        label.setStyleSheet(f"color: {Theme.color(key)};")

    def _set_message_color(self, key: str) -> None:
        """Цвет статуса карточки «Запрет» (запоминается для смены темы)."""
        self._message_level = key
        self._paint(self.message_label, key)

    def _set_app_message_color(self, key: str) -> None:
        """Цвет статуса карточки «Интерфейс» (запоминается для смены темы)."""
        self._app_message_level = key
        self._paint(self.app_message_label, key)

    def _set_warp_message_color(self, key: str) -> None:
        """Цвет статуса карточки WARP (запоминается для смены темы)."""
        self._warp_message_level = key
        self._paint(self.warp_message_label, key)

    def _set_summary_color(self, key: str) -> None:
        """Цвет сводки «Проверить всё» (запоминается для смены темы)."""
        self._summary_level = key
        self._paint(self.summary_label, key)

    def refresh_theme(self) -> None:
        """Перекрашивает подписи карточек под активную тему.

        Цвета статусов заданы встроенными стилями (QSS их не перекрашивает),
        поэтому уровни хранятся в ``self._*_level`` и применяются заново.
        """
        super().refresh_theme()
        for card in (self.zapret_card, self.app_card, self.warp_card):
            card.refresh_theme()
        self.refresh_local_version()
        self._set_message_color(self._message_level)
        self._set_app_message_color(self._app_message_level)
        self._set_warp_message_color(self._warp_message_level)
        self._set_summary_color(self._summary_level)
        if portable.enabled:
            self.app_note.setStyleSheet(f"color: {Theme.color(LEVEL_WARNING)};")


__all__ = [
    "APP_CARD_TITLE",
    "UNKNOWN_VERSION",
    "UpdatePage",
    "WARP_CARD_TITLE",
    "ZAPRET_CARD_TITLE",
]
