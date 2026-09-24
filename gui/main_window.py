"""Главное окно Zapret GUI.

Окно состоит из трёх частей:

* **шапка** — название приложения, статус последней операции, индикатор
  состояния службы, кнопка переключения темы и кнопка сворачивания в трей;
* **боковое меню** слева (десять разделов) и **страницы** справа
  (``QStackedWidget``): «Обзор», «Служба», «Стратегии», «Списки»,
  «Обход (WARP)», «Тестирование», «Проверка связи», «Обновление», «Логи»,
  «Настройки»;
* **статус-бар** — подпись сборки с копирайтом слева и версия запрета справа.

When GitHub сообщает о новой версии **самого приложения**, в шапке появляется
баннер «Доступна версия X → Обновить»: проверка идёт через 5 секунд после
старта и раз в сутки (:class:`core.app_updater.AppUpdater`), а по клику
открывается :class:`gui.dialogs.UpdateDialog`. Обновляется только установленная
версия — portable-сборку установщик не тронет.

Там же живёт проверка обновления запрета (релизы Flowseal): её результат тоже
поднимает баннер — «Запрет и интерфейс: доступны обновления». По клику окно
открывает раздел «Обновление», где собраны все три источника (запрет, GUI и
Cloudflare WARP), см. :class:`gui.pages.UpdatePage`.

Статус-бар создаётся через :meth:`QMainWindow.setStatusBar`: Qt сам резервирует
полосу снизу, поэтому сайдбар и страницы не заезжают под неё. Статус последней
операции («Готово», «Тестирование…», «Ошибка: …») показывается в шапке —
:meth:`Header.set_status`.

Путь к папке запрета окно читает из ``QSettings("ZapretGUI", "Paths")``; если
настройки нет — ищет запрет сам (:class:`core.zapret_locator.ZapretLocator`) и
при неудаче показывает мастер первого запуска (:class:`gui.dialogs.FirstRunDialog`).
Отказ от мастера не мешает запуску: приложение работает в ограниченном режиме,
а разделы «Стратегии» и «Тестирование» показывают подсказку про «Настройки».

Вся работающая логика осталась там же, где была: управление службой —
в :mod:`core.service_manager`, тестирование — в :mod:`core.strategy_tester`,
проверка сайтов — в :mod:`core.health_checker`, обновление — в
:mod:`core.updater`. Окно владеет фоновыми задачами (:class:`gui.widgets.Worker`),
треем и общим статусом, а страницы показывают данные и просят окно выполнить
операцию.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from config import (
    APP_AUTHOR,
    APP_COPYRIGHT,
    APP_NAME,
    APP_TITLE,
    APP_VERSION,
    HEADER_HEIGHT,
    HEALTH_PAGE_INDEX,
    LISTS_PAGE_INDEX,
    LOGS_PAGE_INDEX,
    OVERVIEW_PAGE_INDEX,
    PAGE_COUNT,
    SERVICE_PAGE_INDEX,
    SETTINGS_PAGE_INDEX,
    STATUS_BAR_HEIGHT,
    STATUS_POLL_INTERVAL_MS,
    STRATEGIES_PAGE_INDEX,
    TESTING_PAGE_INDEX,
    UPDATE_CHECK_DELAY_MS,
    UPDATE_CHECK_INTERVAL_HOURS,
    UPDATE_PAGE_INDEX,
    WARP_PAGE_INDEX,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
)
from core import portable
from core import settings as app_settings
from core.app_updater import AppRelease, AppUpdater
from core.health_checker import HealthChecker
from core.ping_history import PingHistory
from core.service_manager import (
    ADMIN_HINT,
    AutostartManager,
    ServiceManager,
    ServiceState,
    is_admin,
)
from core.strategy_parser import StrategyParser
from core.strategy_tester import StrategyTester
from core.updater import Updater
from core.warp_manager import WarpManager
from core.warp_updater import WarpUpdater
from core.zapret_locator import ZapretLocator
from gui.dialogs import FirstRunDialog, UpdateDialog
from gui.icons import window_icon
from gui.pages import (
    HealthPage,
    ListsPage,
    LogsPage,
    OverviewPage,
    ServicePage,
    SettingsPage,
    StrategiesPage,
    TestingPage,
    UpdatePage,
    WarpPage,
)
from gui.sidebar import LOGS_SECTION, OVERVIEW_SECTION, SECTIONS, Sidebar
from gui.theme import Theme
from gui.tray_icon import TrayIcon
from gui.widgets import StatusIndicator, Worker

log = logging.getLogger(__name__)

#: Индекс раздела «Проверка связи» в боковом меню и в стеке страниц
#: (см. :data:`gui.sidebar.SECTIONS`). Остальные индексы — константы
#: ``*_PAGE_INDEX`` из :mod:`config`: порядок пунктов меню и страниц
#: должен совпадать, иначе переходы будут открывать не ту страницу.
HEALTH_SECTION = HEALTH_PAGE_INDEX

#: Ширина подписи статуса операции в шапке, px. Ограничена намеренно: длинное
#: сообщение обрезается по краю (``Header._elide_status``), а не растягивает
#: шапку и не выдавливает индикатор службы с кнопками. Значение подобрано так,
#: чтобы шапка целиком помещалась в минимальную ширину окна
#: (``config.WINDOW_MIN_WIDTH``) даже с видимым индикатором прогресса.
STATUS_TEXT_WIDTH = 170

#: Высота строки баннера обновления в шапке, px. Шапка задана фиксированной
#: высотой (см. :data:`config.HEADER_HEIGHT`), поэтому при показе баннера она
#: становится выше ровно на эту строку — иначе текст баннера обрезался бы.
UPDATE_BANNER_HEIGHT = 26

#: Уровни статуса операции: обычный, успех и ошибка (от них зависит цвет).
STATUS_INFO = "info"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"


class Header(QWidget):
    """Шапка окна: название, статус операции, служба, тема, сворачивание в трей.

    Дополнительно в шапке живёт баннер обновления приложения: строка
    «Доступна версия X → Обновить» со ссылкой. Баннер скрыт, пока обновлений
    нет (:meth:`set_update_available` / :meth:`clear_update`).
    """

    theme_toggled = pyqtSignal()
    tray_requested = pyqtSignal()
    #: Нажата ссылка «Обновить» в баннере — окно проверяет обновления заново.
    update_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Header")
        self.setFixedHeight(HEADER_HEIGHT)

        #: Полный текст статуса операции и его уровень (см. :meth:`set_status`).
        #: Заданы до сборки раскладки: resizeEvent может прийти раньше, чем
        #: появится подпись, и обрезка текста не должна падать.
        self._status_text = "Готово"
        self._status_level = STATUS_INFO
        #: Версия, о которой сообщает баннер (пустая — баннера нет).
        self._update_version = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 6, 18, 6)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(10)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_label = QLabel("Zapret GUI")
        self.title_label.setObjectName("HeaderTitle")
        self.subtitle_label = QLabel(
            "Управление службой zapret (обход блокировок Flowseal)"
        )
        self.subtitle_label.setObjectName("HeaderSubtitle")
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label)
        top.addLayout(titles)
        top.addStretch(1)

        # Статус последней операции раньше жил в статус-баре; теперь он здесь,
        # рядом с индикатором службы. Ширина фиксирована, длинный текст
        # обрезается многоточием — шапка не «разъезжается».
        self.status_label = QLabel("Готово")
        self.status_label.setObjectName("HeaderStatus")
        self.status_label.setFixedWidth(STATUS_TEXT_WIDTH)
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.status_label.setToolTip("Статус последней операции")
        top.addWidget(self.status_label)

        # Мини-прогресс длительной операции (тестирование, обновление): раньше
        # он был в статус-баре, где перекрывал подписи. Ширина небольшая, чтобы
        # шапка помещалась в минимальную ширину окна.
        self.status_progress = QProgressBar()
        self.status_progress.setTextVisible(False)
        self.status_progress.setFixedSize(72, 6)
        self.status_progress.setRange(0, 0)
        self.status_progress.setVisible(False)
        top.addWidget(self.status_progress)

        self.indicator = StatusIndicator()
        top.addWidget(self.indicator)

        self.theme_button = QPushButton()
        self.theme_button.setObjectName("HeaderButton")
        self.theme_button.setFixedWidth(44)
        self.theme_button.clicked.connect(self.theme_toggled.emit)
        top.addWidget(self.theme_button)

        self.tray_button = QPushButton("В трей")
        self.tray_button.setObjectName("HeaderButton")
        self.tray_button.setToolTip("Свернуть приложение в системный трей")
        self.tray_button.clicked.connect(self.tray_requested.emit)
        top.addWidget(self.tray_button)

        layout.addLayout(top)

        # Баннер обновления: своя строка под верхним рядом. Скрыт по умолчанию —
        # окно выглядит как обычно, пока GitHub не сообщит о новой версии.
        # Ссылка «Открыть» открывает раздел «Обновление», где собраны все три
        # источника: запрет, сам GUI и Cloudflare WARP.
        self.update_banner = QWidget()
        self.update_banner.setObjectName("UpdateBanner")
        self.update_banner.setFixedHeight(UPDATE_BANNER_HEIGHT)
        banner_layout = QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(10, 2, 6, 2)
        banner_layout.setSpacing(6)

        self.update_banner_label = QLabel()
        self.update_banner_label.setTextFormat(Qt.TextFormat.RichText)
        self.update_banner_label.setOpenExternalLinks(False)
        self.update_banner_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        self.update_banner_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_banner_label.linkActivated.connect(self._on_update_link)
        banner_layout.addWidget(self.update_banner_label, 1)

        self.update_banner_button = QPushButton("Открыть")
        self.update_banner_button.setObjectName("HeaderButton")
        self.update_banner_button.setToolTip("Открыть раздел «Обновление»")
        self.update_banner_button.clicked.connect(self._on_update_link)
        banner_layout.addWidget(self.update_banner_button)
        self.update_banner.setVisible(False)
        layout.addWidget(self.update_banner)

        self.admin_warning = QLabel(
            "⚠ Приложение запущено без прав администратора — "
            "управление службой и обновление будут недоступны. "
            "Перезапустите от имени администратора."
        )
        self.admin_warning.setObjectName("Warning")
        self.admin_warning.setWordWrap(True)
        self.admin_warning.setVisible(not is_admin())
        layout.addWidget(self.admin_warning)

    # ------------------------------------------------------------------
    #  Статус последней операции
    # ------------------------------------------------------------------
    def set_status(self, text: str, level: str = STATUS_INFO) -> None:
        """Показывает статус операции в шапке.

        :param text: сообщение; берётся только первая строка.
        :param level: ``info``, ``success`` или ``error`` — цвет подписи.
        """
        first_line = (text or "").splitlines()[0].strip() if text else ""
        self._status_text = first_line or "Готово"
        self._status_level = level
        self.status_label.setToolTip(self._status_text)
        self.refresh_status_color()
        self._elide_status()

    def refresh_status_color(self) -> None:
        """Красит подпись статуса под активную тему и уровень сообщения."""
        key = {
            STATUS_SUCCESS: "success",
            STATUS_ERROR: "error",
        }.get(self._status_level, "muted")
        self.status_label.setStyleSheet(f"color: {Theme.color(key)};")

    def _elide_status(self) -> None:
        """Обрезает длинный статус по ширине подписи, чтобы не ломать шапку."""
        label = getattr(self, "status_label", None)
        if label is None:  # resizeEvent до создания подписи (см. __init__)
            return
        metrics = label.fontMetrics()
        budget = max(40, label.width() - 6)
        label.setText(
            metrics.elidedText(self._status_text, Qt.TextElideMode.ElideRight, budget)
        )

    def set_progress(self, visible: bool, value: int | None = None) -> None:
        """Маленький индикатор прогресса в шапке.

        :param value: ``None`` — «идёт, сколько осталось неизвестно»;
            иначе процент 0..100.
        """
        if visible:
            if value is None:
                self.status_progress.setRange(0, 0)
            else:
                self.status_progress.setRange(0, 100)
                self.status_progress.setValue(max(0, min(100, value)))
        self.status_progress.setVisible(visible)

    # ------------------------------------------------------------------
    #  Баннер обновления приложения
    # ------------------------------------------------------------------
    def set_update_available(self, version: str, zapret: bool = False) -> None:
        """Показывает баннер «Обновить» в шапке.

        :param version: версия GUI из релиза GitHub (``""`` — обновления GUI
            нет); префикс ``v`` срезается;
        :param zapret: есть ли обновление запрета. Если да, баннер сообщает
            об обоих обновлениях, а не только о версии интерфейса.
        """
        shown = str(version or "").strip().lstrip("vV")
        if not shown and not zapret:
            self.clear_update()
            return

        self._update_version = shown
        accent = Theme.color("accent")
        if shown and zapret:
            text = f"Доступны обновления: интерфейс {shown} и запрет"
        elif shown:
            text = f"Доступна версия {shown}"
        else:
            text = "Доступно обновление запрета"
        self.update_banner_label.setText(
            f"{text} → "
            f'<a href="update" style="color: {accent}; text-decoration: none;">'
            "Открыть</a>"
        )
        self.update_banner.setToolTip(
            "Есть обновления. Нажмите «Открыть» — раздел «Обновление» "
            "покажет версии и предложит установку."
        )
        self.refresh_banner_theme()
        self.update_banner.setVisible(True)
        self._fit_height()

    def clear_update(self) -> None:
        """Прячет баннер обновления (обновлений нет или версия уже последняя)."""
        self._update_version = ""
        self.update_banner.setVisible(False)
        self._fit_height()

    def update_version(self) -> str:
        """Версия, о которой сейчас сообщает баннер (пустая — баннера нет)."""
        return self._update_version

    def _on_update_link(self, _url: str = "") -> None:
        """Клик по ссылке «Открыть» в баннере — переход в раздел «Обновление»."""
        self.update_requested.emit()

    def refresh_banner_theme(self) -> None:
        """Красит баннер под активную тему (QSS его не знает)."""
        self.update_banner.setStyleSheet(
            "QWidget#UpdateBanner {"
            f" background-color: {Theme.color('surface_alt')};"
            f" border: 1px solid {Theme.color('accent')};"
            " border-radius: 8px;"
            " }"
        )

    def _fit_height(self) -> None:
        """Подгоняет высоту шапки под видимые строки.

        Шапка фиксированной высоты, поэтому строку баннера нужно «оплатить»
        дополнительными пикселями: иначе он обрезался бы снизу.
        """
        banner = getattr(self, "update_banner", None)
        extra = UPDATE_BANNER_HEIGHT if banner is not None and self._update_version else 0
        self.setFixedHeight(HEADER_HEIGHT + extra)

    def resizeEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """Пересчитывает обрезку статуса при изменении ширины окна."""
        super().resizeEvent(event)
        self._elide_status()

    def refresh_theme(self) -> None:
        """Обновляет подсказку и символ кнопки темы под активную тему."""
        self.theme_button.setText("\u263D" if Theme.is_dark() else "\u2600")
        self.theme_button.setToolTip(
            f"Переключить тему (сейчас {Theme.label()})"
        )
        self.refresh_status_color()
        self.refresh_banner_theme()
        # Шрифт подписи задан в QSS темы: после её применения обрезку нужно
        # пересчитать, иначе многоточие встанет не на своё место.
        self._elide_status()


class MainWindow(QMainWindow):
    """Главное окно приложения."""

    #: Прогресс скачивания обновления: (скачано байт, всего байт).
    update_progress = pyqtSignal(int, int)
    #: Текстовая стадия обновления.
    update_status = pyqtSignal(str)
    #: Запрос фоновой задачи из чужого потока: (fn, args, ok, err, kwargs).
    _async_request = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(window_icon())
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self.resize(1060, 720)

        # --- настройки и путь к запрету -----------------------------------
        self._settings = app_settings.window_settings()
        self.locator = ZapretLocator()
        #: Папка запрета, выбранная пользователем. ``None`` — запрет не найден,
        #: приложение работает в ограниченном режиме (страницы показывают
        #: подсказку «Укажите папку в Настройках»).
        self.zapret_path = None
        #: Путь выбран человеком (настройки/мастер/«Настройки»), а не автопоиском:
        #: для такого пути временная папка допускается — с предупреждением.
        self._zapret_path_explicit = False
        self._resolve_zapret_path()

        # --- ядро ---------------------------------------------------------
        # Путь к запрету передаётся в ядро явно: настройки читаются до
        # создания страниц, поэтому «замороженные» константы config.py
        # (ZAPRET_PATH) здесь не используются.
        self.strategy_parser = StrategyParser(self.zapret_path)
        self.service_manager = ServiceManager(zapret_path=self.zapret_path)
        self.health_checker = HealthChecker()
        # История пингов общая: её пополняет страница «Проверка связи», а
        # показывает график на дашборде «Обзор».
        self.ping_history = PingHistory()
        self.updater = Updater(target_dir=self.zapret_path)
        # Автообновление самого приложения — отдельный механизм: релизы
        # публикуются в репозитории GUI, скачивается установщик Inno Setup
        # (см. core/app_updater.py). Не путать с self.updater (запрет Flowseal).
        self.app_updater = AppUpdater()
        # Проверка обновления Cloudflare WARP (winget). Версию и проверку
        # показывает карточка на вкладке «Обновление»; управление WARP
        # осталось в разделе «Обход (WARP)» и работает через WarpManager.
        self.warp_manager = WarpManager()
        self.warp_updater = WarpUpdater()
        self.autostart = AutostartManager()
        # Тестер стратегий — QObject с сигналами: тест идёт в отдельном потоке
        # (его запускает страница тестирования через run_async), а сигналы Qt
        # доставляет в главный поток. Страница подключается к ним сама.
        self.tester = StrategyTester(zapret_path=self.zapret_path)

        # --- состояние ----------------------------------------------------
        self._workers: set[Worker] = set()
        self._status_busy = False
        self._force_quit = False
        #: Идёт проверка обновлений приложения (защита от повторного запуска).
        self._update_check_busy = False
        self._tray_hint_shown = False
        self._last_state = ServiceState.UNKNOWN
        self._refreshing_strategies = False

        self._build_ui()
        self._setup_tester()
        self._connect_pages()
        self._async_request.connect(self._dispatch_async_request)
        #: Трей может быть недоступен (урезанная оболочка Windows) — тогда
        #: закрытие окна завершает приложение, иначе оно стало бы недостижимым.
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if not self._tray_available:
            log.warning("Системный трей недоступен — окно будет закрываться полностью")
        self._build_tray()
        self.header.refresh_theme()

        self._restore_geometry()
        # Мастер первого запуска показывается после построения интерфейса: он
        # модальный, а по его завершении окно обновляется (refresh_all).
        self._prompt_first_run_if_needed()
        self.refresh_all()
        self._start_timers()

    # ==================================================================
    #  Путь к папке запрета
    # ==================================================================
    def _resolve_zapret_path(self) -> None:
        """Определяет путь к запрету до создания ядра и страниц.

        Порядок: сохранённый в ``QSettings("ZapretGUI", "Paths")`` путь →
        автопоиск :class:`core.zapret_locator.ZapretLocator`. Невалидный
        сохранённый путь (папку удалили или она пустая) игнорируется — как
        будто настройки нет.

        Путь из настроек — явный выбор пользователя, поэтому временная папка
        для него допускается (``allow_temp=True``): иначе выбранная вручную
        папка в ``%TEMP%`` не сохранялась бы между запусками. Автопоиск же
        временные папки не рассматривает вовсе.

        Если запрет не найден, вместо ``None`` подставляется заведомо
        несуществующий путь-заглушка: ядро (``core/*``) при ``None`` берёт
        значение по умолчанию из ``config.py``, а в ограниченном режиме это
        неверно — страницы должны показать «Запрет не найден». Дальше окно
        показывает мастер первого запуска
        (:meth:`_prompt_first_run_if_needed`).
        """
        saved = app_settings.zapret_path()
        if saved:
            candidate = Path(saved)
            if self.locator.is_valid(candidate, allow_temp=True):
                self.zapret_path = candidate
                self._zapret_path_explicit = True
                warning = self.locator.temp_warning(candidate)
                if warning:
                    log.warning("Путь к запрету из настроек во временной папке: %s", candidate)
                log.info("Путь к запрету взят из настроек: %s", candidate)
                return
            log.warning("Сохранённый путь к запрету не подходит: %s", saved)

        found = self.locator.find()
        if found is not None:
            self.zapret_path = found
            app_settings.set_zapret_path(found)
            log.info("Путь к запрету найден автоматически: %s", found)
        else:
            self.zapret_path = self._missing_zapret_path()
            log.warning("Запрет не найден — работаем в ограниченном режиме")

    @staticmethod
    def _missing_zapret_path() -> Path:
        """Путь-заглушка для ограниченного режима (папки не существует).

        Считается от папки приложения, а не от ``__file__``: в onefile-сборке
        PyInstaller ``__file__`` лежит во временной папке распаковки, и
        заглушка оказывалась в ``%TEMP%`` — её путь потом показывался
        пользователю в сообщении «Тестер не найден».
        """
        return portable.application_dir() / "zapret-not-found"

    def zapret_available(self) -> bool:
        """Найдена ли папка запрета (иначе — ограниченный режим).

        Для пути, выбранного человеком, временная папка считается допустимой:
        о подозрительном месте пользователя предупреждают, но не запрещают.
        """
        return self.locator.is_valid(
            self.zapret_path, allow_temp=self._zapret_path_explicit
        )

    def _prompt_first_run_if_needed(self) -> None:
        """Показывает мастер первого запуска, если запрет не найден."""
        if self.zapret_available():
            return

        log.info("Показываю мастер первого запуска")
        dialog = FirstRunDialog(self, self.locator, parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted or dialog.result_path is not None
        if accepted and dialog.result_path is not None:
            self.apply_zapret_path(dialog.result_path)
        elif accepted:
            # Пользователь указал папку, но путь не сохранился (например,
            # настройки недоступны) — продолжаем без запрета.
            log.warning("Мастер завершён без пути к запрету")
        else:
            self.set_status("Запрет не найден — укажите папку в «Настройках»", 8000)

    def apply_zapret_path(self, zapret_path) -> None:
        """Применяет новый путь к запрету (из мастера или раздела «Настройки»).

        Путь сохраняется в настройках, ядро пересоздаётся с новым путём, а
        страницы получают его и перечитывают список стратегий.
        """
        if zapret_path is None:
            return
        path = Path(zapret_path)
        saved = app_settings.set_zapret_path(path)
        self.zapret_path = Path(saved) if saved else path
        # Путь выбран человеком (мастер первого запуска или «Настройки»):
        # временную папку не отвергаем, но предупреждаем о ней.
        self._zapret_path_explicit = True
        warning = self.locator.temp_warning(self.zapret_path)
        if warning:
            log.warning("Путь к запрету во временной папке: %s", self.zapret_path)
            self.set_status(
                "Внимание: папка запрета во временной папке — Windows может её удалить",
                8000,
            )
        log.info("Путь к запрету изменён: %s", self.zapret_path)

        self.strategy_parser = StrategyParser(self.zapret_path)
        self.service_manager = ServiceManager(zapret_path=self.zapret_path)
        self.updater = Updater(target_dir=self.zapret_path)

        self.strategies_page.strategy_parser = self.strategy_parser
        self.service_page.service_manager = self.service_manager
        self.testing_page.service_manager = self.service_manager
        # Страница сама переводит тестер на новую папку (и пересоздаёт его
        # парсер стратегий с менеджером службы).
        self.testing_page.set_zapret_path(self.zapret_path)
        self.lists_page.service_manager = self.service_manager
        self.lists_page.set_zapret_path(self.zapret_path)
        self.settings_page.set_zapret_path(self.zapret_path)

        self.refresh_all()

    # ==================================================================
    #  Построение интерфейса
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.header = Header()
        self.header.theme_toggled.connect(self.on_toggle_theme)
        self.header.tray_requested.connect(self.hide_to_tray)
        # Ссылка «Открыть» в баннере шапки: показываем раздел «Обновление»,
        # где собраны все три источника, и проверяем релизы GUI заново —
        # данные могли устареть, пока приложение работало.
        self.header.update_requested.connect(self._on_header_update_requested)
        self.header_indicator = self.header.indicator
        #: Подпись статуса последней операции в шапке (см. Header.set_status).
        self.header_status = self.header.status_label
        root.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # Меню и страницы разделяет правая граница сайдбара (QSS в
        # gui/sidebar.py): отдельный виджет-разделитель не нужен, а отступ
        # между ними — нулевой, иначе линия «отклеилась» бы от меню.
        # Сайдбар растягивается по вертикали на всю высоту центральной части:
        # снизу его подпирает статус-бар, который резервирует QMainWindow
        # (setStatusBar), а не виджет внутри этого layout, — иначе меню
        # уходило бы под полосу статуса.
        self.sidebar = Sidebar()
        body.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("Pages")
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        # Страницы создаются до подключения сигналов: каждая из них знает
        # только своё ядро, общие операции выполняет окно. Порядок страниц
        # совпадает с порядком пунктов меню: индекс 0 — стартовый «Обзор».
        self.overview_page = OverviewPage(self)
        self.service_page = ServicePage(self, self.service_manager, self.autostart)
        self.strategies_page = StrategiesPage(self, self.service_manager, self.strategy_parser)
        self.lists_page = ListsPage(self, self.service_manager, self.zapret_path)
        # «Обход (WARP)» управляет Cloudflare WARP через warp-cli и от запрета
        # не зависит: раздел доступен даже в ограниченном режиме.
        self.warp_page = WarpPage(self)
        self.testing_page = TestingPage(
            self, self.service_manager, self.tester, self.zapret_path
        )
        self.health_page = HealthPage(self, self.health_checker, self.ping_history)
        # «Обновление» — одна вкладка на три источника: запрет (Flowseal),
        # сам GUI (установщик из релизов) и Cloudflare WARP (winget).
        self.update_page = UpdatePage(
            self,
            self.updater,
            self.service_manager,
            app_updater=self.app_updater,
            warp_updater=self.warp_updater,
        )
        self.logs_page = LogsPage(self)
        self.settings_page = SettingsPage(self, self.locator, self.zapret_path)

        # График пинга на дашборде читает ту же историю, что пополняет
        # страница «Проверка связи».
        self.overview_page.set_ping_history(self.ping_history)

        self.pages = (
            self.overview_page,
            self.service_page,
            self.strategies_page,
            self.lists_page,
            self.warp_page,
            self.testing_page,
            self.health_page,
            self.update_page,
            self.logs_page,
            self.settings_page,
        )
        self._check_page_indices()
        for page in self.pages:
            self.stack.addWidget(page)

        self.sidebar.section_changed.connect(self.show_section)
        # Стартовый экран — «Обзор» (индекс 0).
        self.sidebar.set_section(OVERVIEW_SECTION)
        self.stack.setCurrentIndex(OVERVIEW_SECTION)

        self._build_status_bar()

    def _check_page_indices(self) -> None:
        """Проверяет, что меню и стек страниц согласованы по числу разделов.

        Индексы страниц — общие константы (:mod:`config`), и «тихий»
        рассинхрон меню и стека открывал бы не ту страницу. Дешевле один раз
        предупредить в журнале, чем ловить это по интерфейсу.
        """
        expected = (
            OVERVIEW_PAGE_INDEX,
            SERVICE_PAGE_INDEX,
            STRATEGIES_PAGE_INDEX,
            LISTS_PAGE_INDEX,
            WARP_PAGE_INDEX,
            TESTING_PAGE_INDEX,
            HEALTH_PAGE_INDEX,
            UPDATE_PAGE_INDEX,
            LOGS_PAGE_INDEX,
            SETTINGS_PAGE_INDEX,
        )
        if expected != tuple(range(PAGE_COUNT)):
            log.warning("Индексы страниц в config.py идут не по порядку: %s", expected)
        if len(SECTIONS) != PAGE_COUNT or len(self.pages) != PAGE_COUNT:
            log.warning(
                "Число разделов меню (%d), страниц (%d) и PAGE_COUNT (%d) не совпадает — "
                "проверьте gui/sidebar.py и gui/main_window.py",
                len(SECTIONS),
                len(self.pages),
                PAGE_COUNT,
            )

    def _build_status_bar(self) -> None:
        """Статус-бар: подпись сборки слева, версия запрета справа.

        Полоса создаётся через :meth:`QMainWindow.setStatusBar` — Qt сам
        резервирует место снизу, поэтому сайдбар и страницы не заезжают под
        неё. Статус последней операции здесь не показывается (он в шапке),
        мини-прогресс — тоже (он в шапке): в полосе высотой 26 px подписи
        иначе наезжали бы друг на друга.
        """
        bar = QStatusBar()
        # «Уголок» для изменения размера окна рисуется поверх правой подписи.
        bar.setSizeGripEnabled(False)
        self.setStatusBar(bar)

        self.build_label = QLabel(
            f"Zapret GUI v{APP_VERSION} by {APP_AUTHOR} · {APP_COPYRIGHT}"
        )
        self.build_label.setObjectName("StatusBarLabel")
        self.build_label.setToolTip(f"{APP_TITLE}\n{APP_COPYRIGHT}")
        bar.addWidget(self.build_label)

        self.version_label = QLabel("zapret: не найден")
        self.version_label.setObjectName("StatusBarLabel")
        self.version_label.setToolTip("Версия установленного запрета")
        bar.addPermanentWidget(self.version_label)

        # Высота фиксирована: полоса не «дышит» при смене подписей.
        bar.setFixedHeight(STATUS_BAR_HEIGHT)

    def _connect_pages(self) -> None:
        """Связывает сигналы страниц с окном (логика core не меняется)."""
        self.overview_page.navigate_to.connect(self.show_section)
        self.overview_page.quick_action.connect(self._on_quick_action)
        self.overview_page.service_action.connect(self._on_overview_service_action)

        self.service_page.service_changed.connect(self._after_service_action)
        self.service_page.logs_requested.connect(self.show_logs)
        self.service_page.failed.connect(self.report_error)

        self.strategies_page.strategies_changed.connect(self.refresh_strategies)
        self.strategies_page.service_changed.connect(self._after_service_action)
        self.strategies_page.failed.connect(self.report_error)

        self.lists_page.status_message.connect(self._on_lists_status)
        self.lists_page.service_changed.connect(self._after_service_action)
        self.lists_page.failed.connect(self.report_error)

        # «Обход (WARP)» управляет внешним клиентом Cloudflare, а не службой
        # запрета, поэтому service_changed здесь не нужен.
        self.warp_page.status_message.connect(self._on_lists_status)
        self.warp_page.failed.connect(self.report_error)

        self.testing_page.test_started.connect(self._on_test_started)
        self.testing_page.test_finished.connect(self._on_test_finished)
        self.testing_page.strategies_changed.connect(self.refresh_strategies)
        self.testing_page.service_changed.connect(self._after_service_action)
        self.testing_page.failed.connect(self.report_error)
        self.testing_page.notify.connect(self._notify)

        self.health_page.failed.connect(self.report_error)
        self.health_page.history_updated.connect(self._on_ping_history_updated)

        self.update_page.failed.connect(self.report_error)
        self.update_page.notify.connect(self._notify)
        self.update_page.version_checked.connect(self._on_version_checked)
        self.update_page.update_applied.connect(self._on_update_applied)
        self.update_page.progress_visible.connect(self.status_bar_progress)
        # Карточка «Интерфейс (Zapret GUI)» просит окно проверить релизы: окно
        # владеет баннером в шапке, поэтому и проверка, и её результат — его.
        self.update_page.app_check_requested.connect(self.manual_app_check)
        self.update_page.app_update_requested.connect(self._on_app_update_requested)
        self.update_page.app_release_changed.connect(self._on_page_app_release)
        self.update_page.check_on_startup_changed.connect(
            self._apply_update_check_enabled
        )

        self.logs_page.failed.connect(self.report_error)

        self.settings_page.path_changed.connect(self.apply_zapret_path)
        self.settings_page.failed.connect(self.report_error)

    def _on_lists_status(self, text: str, level: str) -> None:
        """Показывает статус раздела «Списки» в шапке окна.

        Страница не трогает шапку сама: она испускает ``status_message``, а
        окно решает, как и на сколько показать сообщение.
        """
        self.set_status(text, 6000, level=level)

    # ==================================================================
    #  API для страниц
    # ==================================================================
    @property
    def colors(self) -> dict[str, str]:
        """Цвета активной темы (страницы берут их отсюда)."""
        return Theme.colors()

    def run_async(
        self,
        fn,
        *args,
        on_success=None,
        on_error=None,
        busy_message: str | None = None,
        **kwargs,
    ) -> Worker | None:
        """Запускает функцию в отдельном потоке.

        Возвращает :class:`Worker`. Если метод вызван не из потока окна,
        запрос переадресуется в него, а возвращается ``None``: синхронно
        отдать поток, созданный в другом потоке, нельзя.
        """
        if busy_message:
            self.set_status(busy_message)

        if threading.current_thread() is not threading.main_thread():
            request = (fn, args, on_success, on_error, kwargs)
            self._async_request.emit(request)
            return None
        return self._run_async(fn, *args, on_success=on_success, on_error=on_error, **kwargs)

    @pyqtSlot(object)
    def _dispatch_async_request(self, request: object) -> None:
        """Создаёт Worker по запросу из чужого потока (уже в потоке окна)."""
        fn, args, on_success, on_error, kwargs = request  # type: ignore[misc]
        self._run_async(fn, *args, on_success=on_success, on_error=on_error, **kwargs)

    def set_status(self, text: str, timeout_ms: int = 0, level: str = STATUS_INFO) -> None:
        """Показывает статус последней операции в **шапке** окна.

        Раньше сообщение уходило в статус-бар и перекрывало подписи; теперь
        его показывает :attr:`header_status`.

        :param timeout_ms: если больше нуля — через это время вернуть «Готово».
        :param level: ``info``, ``success`` или ``error`` — цвет подписи.
        """
        self.header.set_status(text, level)
        if timeout_ms:
            QTimer.singleShot(timeout_ms, self._reset_status)

    def _reset_status(self) -> None:
        self.header.set_status("Готово")

    def status_bar_progress(self, visible: bool, value: int | None = None) -> None:
        """Маленький индикатор прогресса (теперь в шапке, не в статус-баре).

        Имя метода оставлено прежним: на него подписаны страницы
        (``progress_visible``) и протокол окна в :mod:`gui.pages.base`.
        """
        self.header.set_progress(visible, value)

    def confirm(self, title: str, text: str, informative: str = "") -> bool:
        """Диалог подтверждения операции."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(title)
        box.setText(text)
        if informative:
            box.setInformativeText(informative)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def show_logs(self) -> None:
        """Открывает раздел «Логи»."""
        self.show_section(LOGS_SECTION)

    def show_section(self, index: int) -> None:
        """Показывает страницу раздела и подсвечивает пункт меню."""
        if not 0 <= index < self.stack.count():
            return
        self.stack.setCurrentIndex(index)
        self.sidebar.set_section(index)
        page = self.stack.currentWidget()
        self._animate_page(page)

    # ==================================================================
    #  Дашборд «Обзор»
    # ==================================================================
    def _on_quick_action(self, action: str) -> None:
        """Быстрое действие с дашборда: показать страницу и запустить операцию."""
        if action == "test_all":
            self.show_section(TESTING_PAGE_INDEX)
            self.testing_page.start_test_all()
        elif action == "check_health":
            self.show_section(HEALTH_PAGE_INDEX)
            self.health_page.start_check()
        elif action == "update":
            self.show_section(UPDATE_PAGE_INDEX)
            self.update_page.start_check()
        else:
            log.warning("Неизвестное быстрое действие: %s", action)

    def _on_ping_history_updated(self) -> None:
        """Проверка связи пополнила историю пингов.

        График на дашборде при этом **не** перерисовывается: он обновляется
        только по кнопке «Обновить график». Проверка идёт в фоне, и график,
        меняющийся сам по себе, только отвлекал бы.
        """
        self.overview_page.on_history_updated()

    def _on_overview_service_action(self, action: str) -> None:
        """Кнопки плитки службы делают то же, что кнопки страницы «Служба».

        Для неустановленной службы «Перезапустить» означает «установить»:
        перезапускать нечего, поэтому подставляется текущая стратегия.
        """
        if action == "start":
            self.overview_page.set_busy(True)
            self.service_page.on_start_service()
        elif action == "stop":
            self.overview_page.set_busy(True)
            self.service_page.on_stop_service()
        elif action == "restart":
            self.overview_page.set_busy(True)
            if self._last_state.is_installed:
                self.service_page.on_restart_service()
            else:
                self._install_current_strategy()
        else:
            log.warning("Неизвестная операция со службой: %s", action)
            return
        # Кнопки плитки разблокирует ближайший опрос состояния (5 с), а пока
        # операция идёт, от повторного нажатия её защищает сама страница
        # «Служба»: она игнорирует команду, пока занята.
        QTimer.singleShot(1000, self._unbusy_overview)

    def _unbusy_overview(self) -> None:
        """Разблокирует кнопки плитки службы после короткой паузы."""
        self.overview_page.set_busy(False)

    def _install_current_strategy(self) -> None:
        """Ставит службу со стратегией, выбранной на странице «Стратегии»."""
        strategy = self.strategies_page.current_strategy()
        if strategy is None:
            self.strategies_page.on_apply_strategy()
            return
        self.service_page.run_service_task(
            lambda: self.service_manager.install(strategy.path),
            f"Установка службы со стратегией «{strategy.name}»...",
            success_prefix="Служба установлена",
        )

    # ==================================================================
    #  Тема
    # ==================================================================
    def on_toggle_theme(self) -> None:
        """Переключает тёмную и светлую темы.

        Прогресс и таблица тестирования не сбрасываются: страницы только
        перекрашиваются (``refresh_theme``).
        """
        Theme.toggle()
        self.apply_theme()
        Theme.save()
        log.info("Тема переключена на «%s»", Theme.label())
        self.set_status(f"Тема: {Theme.label()}", 4000)

    def apply_theme(self) -> None:
        """Применяет активную тему ко всему приложению и перекрашивает страницы."""
        Theme.apply(QApplication.instance())
        # Шапка и статус-бар перекрашиваются вместе со всеми: их цвета заданы
        # в QSS темы, а inline-цвет статуса в шапке обновляет refresh_theme.
        self.header.refresh_theme()
        self.statusBar().update()
        self.sidebar.refresh_theme()
        for page in self.pages:
            page.refresh_theme()
        self.setWindowIcon(window_icon())
        if self.tray is not None:
            # Иконка трея всегда светлая: трей — системная область, и при смене
            # темы окна перерисовывать её не нужно.
            self.tray.set_service_state(self._last_state)

    def _animate_page(self, page: QWidget | None) -> None:
        """Плавное появление страницы (200 мс) без влияния на её состояние."""
        if page is None:
            return
        from PyQt6.QtCore import QEasingCurve, QPropertyAnimation
        from PyQt6.QtWidgets import QGraphicsOpacityEffect

        effect = QGraphicsOpacityEffect(page)
        page.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(200)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.InOutQuad)
        animation.finished.connect(lambda: page.setGraphicsEffect(None))
        animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self._page_animation = animation

    # ==================================================================
    #  Обновление состояния
    # ==================================================================
    def _start_timers(self) -> None:
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(STATUS_POLL_INTERVAL_MS)
        self.status_timer.timeout.connect(self.poll_status)
        self.status_timer.start()

        # Автообновление приложения. Первая проверка — с задержкой: окно должно
        # появляться мгновенно, а сеть подождёт. Дальше — раз в сутки.
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(UPDATE_CHECK_INTERVAL_HOURS * 3600 * 1000)
        self.update_timer.timeout.connect(self._check_updates_silent)
        if app_settings.check_updates_on_startup():
            self.update_timer.start()
            QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._check_updates_silent)
        else:
            log.info("Проверка обновлений при запуске отключена в настройках")

    def refresh_all(self) -> None:
        """Полное обновление: статус, стратегии, версии."""
        self.poll_status()
        self.refresh_strategies()
        self.update_page.refresh_local_version()

    def poll_status(self) -> None:
        """Опрашивает службу в фоне (раз в 5 секунд)."""
        if self._status_busy:
            return
        self._status_busy = True
        self._run_async(
            self.service_manager.get_status,
            on_success=self._on_status_received,
            on_error=self._on_status_error,
        )

    def _on_status_error(self, message: str) -> None:
        self._status_busy = False
        log.warning("Не удалось получить статус службы: %s", message)

    def _on_status_received(self, status) -> None:
        self._status_busy = False
        state: ServiceState = status.state
        self._last_state = state

        self.header_indicator.set_text(state.label, state.color)
        self.service_page.set_service_state(state)
        self.strategies_page.set_service_state(state)
        self.testing_page.set_service_state(state)
        self.update_page.set_service_running(state.is_running)
        self.overview_page.set_service_state(state)

        if self.tray is not None:
            self.tray.set_service_state(state)

        if not self.zapret_available():
            # Ограниченный режим: без папки запрета управлять нечем.
            self.set_status("Запрет не найден", 6000)
        elif status.message:
            self.set_status(status.message.splitlines()[0], 6000)

    def refresh_strategies(self) -> None:
        """Перечитывает список .bat-стратегий и раздаёт его страницам.

        Метод сам подписан на сигнал ``strategies_changed`` (его испускает
        страница после перечитывания списка), поэтому защищён от повторного
        входа: иначе перечитывание вызывало бы само себя бесконечно.
        """
        if self._refreshing_strategies:
            return
        self._refreshing_strategies = True
        try:
            self.strategies_page.on_reload_strategies()
            strategies = self.strategies_page.strategies()
            self.testing_page.set_strategies(strategies)

            installed = self.service_manager.get_current_strategy()
            self.service_page.set_current_strategy(installed)
            self.overview_page.set_current_strategy(installed)
        finally:
            self._refreshing_strategies = False

    def _after_service_action(self) -> None:
        """После операции со службой обновляет её состояние и список стратегий."""
        QTimer.singleShot(400, self.poll_status)
        QTimer.singleShot(600, self.refresh_strategies)

    def _on_version_checked(self, version: str) -> None:
        """Обновляет справа в статус-баре версию запрета.

        Пустая версия и «неизвестно» (запрет не найден или версия не
        записана) показываются как «zapret: не найден» — пустой подписи нет.
        """
        shown = version.strip() if version and version.strip() else ""
        if not shown or shown.lower() == "неизвестно":
            shown = "не найден"
            self.version_label.setToolTip("Версия запрета не определена")
        else:
            self.version_label.setToolTip(f"Версия установленного запрета: {shown}")
        self.version_label.setText(f"zapret: {shown}")

    def _on_update_applied(self) -> None:
        self.refresh_all()

    def _notify(self, title: str, message: str) -> None:
        if self.tray is not None and self.tray.isVisible():
            self.tray.notify(title, message[:180])

    # ==================================================================
    #  Автообновление приложения (GitHub Releases)
    # ==================================================================
    # Обновляется сам GUI, а не папка запрета: проверка идёт через
    # core.app_updater.AppUpdater, скачивается установщик Inno Setup, он же и
    # запускается. Проверка при старте — тихая (ошибки только в журнал),
    # по кнопке — с диалогом.
    def _apply_update_check_enabled(self, enabled: bool) -> None:
        """Включает или выключает автоматические проверки обновлений."""
        if enabled:
            self.update_timer.start()
            self._check_updates_silent()
        else:
            self.update_timer.stop()
            log.info("Автоматическая проверка обновлений выключена")

    def _check_updates_silent(self) -> None:
        """Тихая проверка обновлений (при старте и раз в сутки).

        Ошибки сети пользователю не показываются: проверка фоновая, и
        «Не удалось проверить обновления» при каждом запуске без интернета
        только раздражало бы. Всё пишется в журнал.
        """
        if self._update_check_busy or self._force_quit:
            return
        self._update_check_busy = True
        self._run_async(
            self.app_updater.check_latest,
            on_success=self._on_silent_check,
            on_error=self._on_silent_check_error,
        )

    def _on_silent_check(self, release: object) -> None:
        """Результат тихой проверки: баннер в шапке и уведомление в трее.

        Обновление запрета в тихой проверке не участвует: его версия лежит на
        диске, а не в сети, и к моменту первых проверок карточка «Запрет» уже
        знает результат (см. ``_update_header_banner``).
        """
        self._update_check_busy = False
        if not isinstance(release, AppRelease) or not self.app_updater.has_update(release):
            self._update_header_banner(None)
            return

        log.info("Доступно обновление приложения: %s", release.version)
        self._update_header_banner(release)
        self._notify(
            "Доступно обновление",
            f"{APP_NAME} {release.version}: откройте раздел «Обновление».",
        )

    def _on_silent_check_error(self, message: str) -> None:
        """Ошибка тихой проверки — только журнал, без диалогов."""
        self._update_check_busy = False
        log.info("Проверка обновлений не удалась: %s", message)

    def check_app_updates(self) -> None:
        """Проверка обновлений приложения по кнопке (см. :meth:`WindowApi`)."""
        self.manual_app_check()

    def manual_app_check(self) -> None:
        """Проверка релизов приложения по кнопке — с показом ошибок.

        Запускается двумя путями: ссылкой «Открыть» в баннере шапки и кнопкой
        «Проверить обновление» в карточке «Интерфейс (Zapret GUI)». В обоих
        случаях проверка одна, а её результат уходит и в баннер, и в карточку.
        """
        if self._update_check_busy:
            return
        self._update_check_busy = True
        self.set_status("Проверка обновлений...")
        self._run_async(
            self.app_updater.check_latest,
            on_success=self._on_manual_check,
            on_error=self._on_manual_check_error,
        )

    def _on_manual_check(self, release: object) -> None:
        self._update_check_busy = False
        found = release if isinstance(release, AppRelease) else None
        if found is None or not self.app_updater.has_update(found):
            # Версия та же (или релиза нет) — баннер убираем, обновлять нечего.
            self._update_header_banner(None)
            self.set_status("Обновлений нет", 6000)
            self.update_page.on_app_check_result(
                found,
                False,
                "" if found is not None else f"Установлена последняя версия: {APP_VERSION}.",
            )
            QMessageBox.information(
                self,
                "Обновления",
                f"Установлена последняя версия: {APP_VERSION}.",
            )
            return

        self._update_header_banner(found)
        self.update_page.on_app_check_result(found, True)
        self.open_update_dialog(found)

    def _on_manual_check_error(self, message: str) -> None:
        self._update_check_busy = False
        text = message or "Не удалось проверить обновления."
        # Ошибку показываем и в карточке, и диалогом: пользователь нажал кнопку
        # и ждёт ответа, тихо глотать ошибку нельзя.
        self.update_page.on_app_check_result(None, False, text)
        self.report_error("Обновления", text)

    def _on_app_update_requested(self) -> None:
        """Кнопка «Обновить» в карточке GUI: открыть диалог обновления."""
        release = self.update_page.app_release
        if release is None:
            # Релиз ещё не проверялся (или карточка устарела) — проверяем заново.
            self.manual_app_check()
            return
        self.open_update_dialog(release)

    def _on_header_update_requested(self) -> None:
        """Клик по баннеру в шапке: открыть раздел «Обновление».

        Раздел показывает версии и кнопки для всех трёх источников сразу,
        поэтому ничего не скачивается автоматически: пользователь сам решает,
        что обновлять. Релизы GUI проверяются заново — данные могли устареть.
        """
        self.show_section(UPDATE_PAGE_INDEX)
        self.manual_app_check()

    def _on_page_app_release(self, release: object) -> None:
        """Карточка GUI или запрета сообщила о релизе — правим баннер в шапке."""
        self._update_header_banner(release if isinstance(release, AppRelease) else None)

    def _update_header_banner(self, release: AppRelease | None) -> None:
        """Показывает или прячет баннер обновлений в шапке.

        Баннер поднимается, если обновиться можно чему-то из двух: самому GUI
        (версия из релиза новее установленной) или запрету (карточка «Запрет»
        нашла релиз Flowseal новее установленного).
        """
        version = release.version if release is not None else ""
        zapret = self.update_page.zapret_update_available()
        if not version and not zapret:
            self.header.clear_update()
            return

        self.header.set_update_available(version, zapret)
        if version:
            self.set_status(f"Доступна версия {version}", 8000)

    def open_update_dialog(self, release: AppRelease) -> None:
        """Открывает диалог обновления для найденного релиза."""
        log.info("Открываю диалог обновления: %s", release.version)
        dialog = UpdateDialog(self, self.app_updater, release, parent=self)
        dialog.installer_started.connect(self._on_installer_started)
        dialog.exec()
        # Диалог создаётся заново на каждую проверку — старый отпускаем сразу.
        dialog.deleteLater()

    def _on_installer_started(self) -> None:
        """Установщик запущен: приложение обязано закрыться.

        Иначе установщик не сможет заменить ``ZapretGUI.exe``. Небольшая
        задержка нужна, чтобы модальный диалог успел закрыться до выхода.
        """
        log.info("Установщик обновления запущен — завершаю приложение")
        QTimer.singleShot(200, self.quit_app)

    # ==================================================================
    #  Фоновые задачи
    # ==================================================================
    def _run_async(
        self,
        fn,
        *args,
        on_success=None,
        on_error=None,
        **kwargs,
    ) -> Worker:
        """Создаёт Worker, подключает обработчики и запускает поток."""
        worker = Worker(fn, *args, parent=self, **kwargs)
        self._workers.add(worker)

        if on_success is not None:
            worker.finished_signal.connect(on_success)
        worker.error_signal.connect(
            on_error if on_error is not None else self._default_error_handler
        )
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        worker.start()
        return worker

    def _release_worker(self, worker: Worker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def _default_error_handler(self, message: str) -> None:
        self.report_error("Ошибка", message)

    def report_error(self, title: str, message: str) -> None:
        """Показывает ошибку: статус в шапке, диалог и уведомление в трее."""
        log.error("%s: %s", title, message)
        self.set_status(
            message.splitlines()[0] if message else title, 8000, level=STATUS_ERROR
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(message or "Неизвестная ошибка.")
        if "администратор" in message:
            box.setInformativeText(ADMIN_HINT)
        box.exec()
        if self.tray is not None and self.tray.isVisible():
            self.tray.notify(title, message[:180], warning=True)

    # ==================================================================
    #  Трей
    # ==================================================================
    def _build_tray(self) -> None:
        if not self._tray_available:
            self.tray = None  # type: ignore[assignment]
            return
        self.tray = TrayIcon(self)
        self.tray.show_window_requested.connect(self.show_window)
        self.tray.quit_requested.connect(self.quit_app)
        self.tray.start_requested.connect(self.service_page.on_start_service)
        self.tray.stop_requested.connect(self.service_page.on_stop_service)
        self.tray.restart_requested.connect(self.service_page.on_restart_service)
        self.tray.check_requested.connect(self._tray_check_sites)
        self.tray.logs_requested.connect(self._tray_show_logs)
        self.tray.show()

    def _tray_check_sites(self) -> None:
        self.show_section(HEALTH_SECTION)  # «Проверка связи»
        self.health_page.on_check_sites()

    def _tray_show_logs(self) -> None:
        self.show_window()
        self.show_logs()

    # ==================================================================
    #  Тестирование стратегий
    # ==================================================================
    def _setup_tester(self) -> None:
        """Проверяет, что тестирование вообще возможно.

        Свой тестер стратегий подключает свои сигналы сам: страница
        тестирования слушает ``tester_output``, ``tester_progress`` и
        ``tester_finished`` напрямую (см. :class:`gui.pages.TestingPage`).
        """
        if not self.zapret_available():
            log.warning("Запрет не найден — тестирование стратегий недоступно")

    def _on_test_started(self) -> None:
        self.status_bar_progress(True)

    def _on_test_finished(self) -> None:
        self.status_bar_progress(False)
        # Тестер переустанавливал службу: плитки дашборда могли устареть.
        self.overview_page.refresh_tiles()
        QTimer.singleShot(600, self.refresh_strategies)

    def test_busy(self) -> bool:
        """Идёт ли тестирование (используется при закрытии окна)."""
        return self.testing_page.test_busy()

    # ==================================================================
    #  Окно и выход
    # ==================================================================
    def show_window(self) -> None:
        """Показывает окно из трея."""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self) -> None:
        """Сворачивает приложение в трей (кнопка в шапке)."""
        if self.tray is None:
            return
        self._save_geometry()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(
                "Zapret GUI свёрнут в трей",
                "Приложение продолжает работать. Двойной клик по иконке — показать окно, "
                "пункт «Выход» в меню — закрыть полностью.",
            )
        log.info("Окно свёрнуто в трей")

    def _restore_geometry(self) -> None:
        geometry = self._settings.value("geometry")
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except (TypeError, ValueError):
                log.debug("Не удалось восстановить размеры окна", exc_info=True)

    def closeEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """Закрытие окна сворачивает приложение в трей, а не завершает его."""
        if self.test_busy() and not self._force_quit:
            self._warn_before_close_test(event)
            return

        if self._force_quit or self.tray is None:
            self._save_geometry()
            event.accept()
            return

        self._save_geometry()
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(
                "Zapret GUI свёрнут в трей",
                "Приложение продолжает работать. Двойной клик по иконке — показать окно, "
                "пункт «Выход» в меню — закрыть полностью.",
            )
        log.info("Окно свёрнуто в трей")

    def _warn_before_close_test(self, event) -> None:
        """Предупреждает о незавершённом тесте и предлагает его остановить.

        Окно не закрывается, пока тест не остановлен: иначе снятый на середине
        тестер оставит систему без службы, а winws.exe — висеть в процессах.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Тестирование стратегий")
        box.setText("Сейчас идёт тестирование стратегий.")
        box.setInformativeText(
            "Если остановить тестер, служба zapret будет восстановлена со стратегией, "
            "которая была активна до теста."
        )
        stop_button = box.addButton("Остановить тест", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Продолжить тест", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stop_button)
        box.exec()

        event.ignore()
        if box.clickedButton() is stop_button:
            self.testing_page.on_stop_test()

    def _save_geometry(self) -> None:
        try:
            self._settings.setValue("geometry", self.saveGeometry())
        except Exception:  # noqa: BLE001 — сохранение настроек не критично
            log.debug("Не удалось сохранить размеры окна", exc_info=True)

    def quit_app(self) -> None:
        """Полное завершение приложения."""
        self._force_quit = True
        log.info("Выход из приложения")
        if self.status_timer.isActive():
            self.status_timer.stop()
        if self.update_timer.isActive():
            self.update_timer.stop()

        # Незавершённый тестер снимается до ожидания потоков: если выйти сразу,
        # служба останется с последней протестированной стратегией, а поток
        # тестера — «живым» (Qt ругается «QThread: Destroyed while thread is
        # still running»). Ждём его отдельно и дольше остальных задач: возврат
        # исходной стратегии — это переустановка службы, то есть секунды.
        if self.test_busy():
            log.info("Останавливаю тестер перед выходом")
            self.testing_page.abandon_test(wait=True)

        # Ждём завершения фоновых задач, чтобы не убить поток на середине.
        for worker in list(self._workers):
            if worker.isRunning():
                worker.wait(2000)

        if self.tray is not None:
            self.tray.hide()
        self.close()
        QApplication.quit()
