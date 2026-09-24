"""Страница «Обход (WARP)»: управление Cloudflare WARP через ``warp-cli``.

Раздел заменил «Proxy Hosts»: вместо подмены IP-адресов через системный файл
``hosts`` приложение управляет **уже установленным** Cloudflare WARP — тем
самым туннелем, который используется для обхода блокировок.

Что умеет страница:

* показать состояние WARP («Подключён» / «Отключён» / «Подключение...») и
  причину, которую сообщил клиент;
* подключить, отключить и переподключить туннель;
* переключить режим: «Трафик и DNS (WARP)», «Только DNS (DoH)», «Выключено»;
* управлять **исключениями Split Tunnel** (карточка «Исключения (Split
  Tunnel)»): загрузить готовый список российских адресов и доменов из
  WarpBypass, добавить текущие активные соединения, посмотреть список,
  добавить или удалить отдельные записи и очистить всё, что добавило
  приложение;
* исключить из WARP трафик **YouTube и Discord** (кнопки «Исключить YouTube»,
  «Исключить Discord», «Исключить YouTube + Discord»): их обходит запрет
  (Flowseal), и если тот же трафик завернёт в туннель ещё и WARP, два обхода
  начнут мешать друг другу. Кнопки «Убрать YouTube» / «Убрать Discord» —
  откат: удаляются только домены набора, чужие записи не трогаются;
* показать, где лежит ``warp-cli``, его версию и текущий режим.

Ключевая идея раздела: WARP работает в режиме **Exclude** — через туннель идёт
весь трафик, *кроме* перечисленных адресов. Российские сайты попадают в
исключения и открываются напрямую, а весь остальной трафик уходит через
Cloudflare.

Массовые операции (список РФ — сотни записей, а ``warp-cli`` принимает их
только по одной) идут в фоне с прогресс-баром и кнопкой «Остановить»: записи
добавляются пакетом, лимит за один заход задаётся в интерфейсе. Если версия
``warp-cli`` не умеет управлять исключениями (``Not yet implemented``),
карточка показывает подсказку, а её кнопки остаются неактивными.

Управление **только ручное**: приложение не подключает WARP при старте — это
осознанное решение (туннель меняет маршрут всего трафика, и включать его без
спроса нельзя). Все команды ``warp-cli`` выполняются в фоне
(``window.run_async``), поэтому интерфейс не подвисает, а ``CREATE_NO_WINDOW``
в :mod:`core.warp_manager` не даёт мелькать консольным окнам.

Состояние после операции читается **не один раз, а опросом**: ``warp-cli
connect`` отвечает сразу, а в ``Connected`` клиент переходит через 1–3 секунды
(``Connecting`` -> ``Connected``), поэтому одиночное чтение статуса показало бы
старое «Отключён». Сразу после успешного подключения/отключения страница
запускает :class:`QTimer` (:data:`STATUS_POLL_INTERVAL_MS` мс, не больше
:data:`STATUS_POLL_ATTEMPTS` попыток), показывает промежуточное «Подключение...»
/ «Отключение...» и прекращает опрос, как только клиент сообщит ожидаемое
состояние. Каждая попытка идёт в фоне, так что интерфейс остаётся отзывчивым;
если за отведённое время состояние не наступило, страница показывает то, что
ответил клиент, и предупреждение «проверьте окно Cloudflare WARP».

Если WARP не установлен, страница не падает: она показывает подсказку со
ссылкой на загрузку и блокирует кнопки. Список файлов и логика работы с
клиентом — в :class:`core.warp_manager.WarpManager`.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from config import (
    DISCORD_EXCLUDE_DOMAINS,
    WARP_EXCLUDE_LIMIT_DEFAULT,
    WARP_EXCLUDE_LIMIT_MAX,
    WARP_EXCLUDE_LIMIT_MIN,
    WARP_INSTALL_URL,
    WARP_MODES,
    YOUTUBE_EXCLUDE_DOMAINS,
)
from core.settings import set_warp_last_download, warp_last_download
from core.warp_manager import (
    HOST_UNSUPPORTED_HINT,
    KIND_HOST,
    KIND_IP,
    MODE_OFF,
    MODE_UNSUPPORTED_HINT,
    NOT_INSTALLED_HINT,
    PRESET_DISCORD,
    PRESET_YOUTUBE,
    PSUTIL_MISSING_HINT,
    STATE_CONNECTED,
    STATE_CONNECTING,
    STATE_DISCONNECTED,
    STATE_UNKNOWN,
    TUNNEL_UNSUPPORTED_HINT,
    ActiveConnections,
    TunnelAddResult,
    WarpManager,
    WarpStatus,
    dedup_key,
    guess_kind,
    mode_label,
    parse_warpbypass_list,
    preset_label,
)
from gui.pages.base import Page, WindowApi
from gui.theme import Theme
from gui.widgets import Card, ModernCard, StatusIndicator

log = logging.getLogger(__name__)

#: Подпись состояния, когда клиент не найден (короче подсказки в карточке).
NOT_FOUND_TEXT = "Cloudflare WARP не найден"

#: Что показывает страница, если WARP не установлен: подсказка со ссылкой.
INSTALL_HINT = (
    "Cloudflare WARP не найден. Установите его с "
    f'<a href="{WARP_INSTALL_URL}">cloudflare.com/warp</a>, затем вернитесь '
    "в этот раздел — статус обновится сам. Кнопки станут активными, когда "
    "клиент <b>warp-cli</b> появится в системе."
)

#: Пояснение к карточке состояния.
STATUS_HINT = (
    "Раздел не устанавливает WARP и не создаёт свой туннель: он управляет "
    "уже установленным Cloudflare WARP через консольный клиент warp-cli. "
    "Автоподключения нет — WARP включается только по кнопке."
)

#: Пояснение к выбору режима.
MODE_HINT = (
    "«Трафик и DNS» — через туннель идут и данные, и DNS-запросы. «Только "
    "DNS» — шифруется лишь DNS, трафик идёт напрямую. «Выключено» — WARP не "
    "вмешивается в соединения."
)

#: Подсказка после смены режима: туннель нужно переподнять.
MODE_APPLIED_HINT = (
    "Если WARP подключён, нажмите «Переподключить», чтобы режим применился "
    "к туннелю."
)

#: Предупреждение в информационной карточке.
MANAGED_HINT = (
    "WARP установлен отдельно. Этот раздел управляет им через warp-cli: "
    "файлы Cloudflare приложение не трогает."
)

# ---------------------------------------------------------------------------
#  Исключения (Split Tunnel)
# ---------------------------------------------------------------------------
#: Пояснение к карточке исключений: как это работает.
EXCLUDE_HINT = (
    "WARP работает в режиме Exclude: через туннель идёт весь трафик, кроме "
    "перечисленных адресов. Российские сайты из списка WarpBypass открываются "
    "напрямую, а остальное — через Cloudflare. Исключения можно добавлять и "
    "при отключённом WARP: они применятся при подключении."
)

#: Главное предупреждение карточки — что именно происходит с трафиком.
EXCLUDE_WARNING = (
    "WARP не трогает трафик к этим адресам — они идут напрямую, а весь "
    "остальной трафик уходит через туннель Cloudflare."
)

#: Подсказка над кнопками, когда клиент не умеет управлять исключениями.
EXCLUDE_UNSUPPORTED_HINT = (
    f"{TUNNEL_UNSUPPORTED_HINT} Добавлять исключения можно в приложении "
    "Cloudflare WARP или в дашборде Zero Trust."
)

#: Пояснение к лимиту записей.
EXCLUDE_LIMIT_HINT = (
    "Список РФ содержит сотни записей, а warp-cli принимает их только по "
    "одной, поэтому за один заход добавляются первые N записей."
)

#: Подсказка кнопки «Добавить текущие соединения».
ACTIVE_BUTTON_HINT = (
    "Собрать удалённые адреса активных соединений (порты 80 и 443) "
    "через psutil и добавить их в исключения"
)

# ---------------------------------------------------------------------------
#  Исключения (Split Tunnel): наборы доменов YouTube и Discord
# ---------------------------------------------------------------------------
#: Пояснение к кнопкам наборов — что именно изменится в маршруте трафика.
EXCLUDE_PRESET_HINT = (
    "WARP не будет перехватывать трафик YouTube и Discord — они пойдут через "
    "запрет."
)

#: Подпись строки кнопок добавления наборов.
PRESET_CAPTION = "Исключить из WARP:"

#: Подпись строки кнопок отката.
PRESET_ROLLBACK_CAPTION = "Убрать из исключений:"

#: Кнопки добавления наборов: (подпись, tooltip, наборы доменов).
PRESET_BUTTONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Исключить YouTube",
        (
            "Добавить домены YouTube в исключения WARP (warp-cli tunnel host "
            "add): WARP не будет их перехватывать, трафик обойдёт запрет"
        ),
        (PRESET_YOUTUBE,),
    ),
    (
        "Исключить Discord",
        (
            "Добавить домены Discord в исключения WARP (warp-cli tunnel host "
            "add): WARP не будет их перехватывать, трафик обойдёт запрет"
        ),
        (PRESET_DISCORD,),
    ),
    (
        "Исключить YouTube + Discord",
        "Добавить оба набора доменов сразу",
        (PRESET_YOUTUBE, PRESET_DISCORD),
    ),
)

#: Кнопки отката: (подпись, tooltip, наборы доменов).
PRESET_ROLLBACK_BUTTONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Убрать YouTube",
        (
            "Удалить домены YouTube из исключений WARP. Другие записи "
            "(WarpBypass, добавленные вручную) не трогаются"
        ),
        (PRESET_YOUTUBE,),
    ),
    (
        "Убрать Discord",
        (
            "Удалить домены Discord из исключений WARP. Другие записи "
            "(WarpBypass, добавленные вручную) не трогаются"
        ),
        (PRESET_DISCORD,),
    ),
)

#: Подсказка вместо кнопок, когда клиент не умеет управлять доменами.
EXCLUDE_HOST_UNSUPPORTED_HINT = (
    f"{HOST_UNSUPPORTED_HINT} Кнопки наборов доменов неактивны."
)

#: Заголовки таблицы исключений.
EXCLUDE_HEADERS: tuple[str, ...] = ("Тип", "Значение", "Источник")

#: Как показывается источник записи в таблице: запись самого WARP (локальные
#: сети, адреса Cloudflare) и записи, добавленные через CLI.
SOURCE_WARP = "WARP"
SOURCE_WARPBYPASS = "WarpBypass"
SOURCE_YOUTUBE = "YouTube"
SOURCE_DISCORD = "Discord"
SOURCE_MANUAL = "Вручную"

#: Набор доменов -> источник записи в таблице.
PRESET_SOURCES: dict[str, str] = {
    PRESET_YOUTUBE: SOURCE_YOUTUBE,
    PRESET_DISCORD: SOURCE_DISCORD,
}

#: Домены наборов в нижнем регистре — по ним определяется источник записи.
_YOUTUBE_EXCLUDE_SET = frozenset(domain.lower() for domain in YOUTUBE_EXCLUDE_DOMAINS)
_DISCORD_EXCLUDE_SET = frozenset(domain.lower() for domain in DISCORD_EXCLUDE_DOMAINS)

#: Пояснение к источнику записи в подсказке таблицы.
SOURCE_HINTS: dict[str, str] = {
    SOURCE_WARP: (
        "Запись самого WARP (локальные сети, адреса Cloudflare) — приложение "
        "её не удаляет."
    ),
    SOURCE_WARPBYPASS: (
        "Домены и адреса из списка WarpBypass: их WARP тоже не трогает — "
        "российские сайты открываются напрямую."
    ),
    SOURCE_YOUTUBE: (
        "Домен YouTube, исключённый кнопкой «Исключить YouTube»: его трафик "
        "идёт мимо WARP, его обходит запрет."
    ),
    SOURCE_DISCORD: (
        "Домен Discord, исключённый кнопкой «Исключить Discord»: его трафик "
        "идёт мимо WARP, его обходит запрет."
    ),
    SOURCE_MANUAL: (
        "Запись добавлена вручную (поле ввода, текущие соединения или другой "
        "источник) — её удаляет кнопка «Очистить исключения»."
    ),
}

#: Цвет подписи статуса, когда исключения ещё не читались.
EXCLUDE_IDLE_TEXT = "Исключения не читались."

# ---------------------------------------------------------------------------
#  Опрос состояния после операции
# ---------------------------------------------------------------------------
#: Как часто перечитывается состояние, пока WARP переходит в целевое (мс).
#: Чаще не нужно: каждая попытка — это запуск ``warp-cli status``.
STATUS_POLL_INTERVAL_MS = 500

#: Сколько попыток делает опрос: 10 × 500 мс = 5 секунд ожидания.
STATUS_POLL_ATTEMPTS = 10

#: Ожидаемое состояние -> подпись индикатора, пока оно не наступило.
PENDING_LABELS: dict[str, str] = {
    STATE_CONNECTED: "Подключение...",
    STATE_DISCONNECTED: "Отключение...",
}

#: Ожидаемое состояние -> пояснение под индикатором во время опроса.
PENDING_REASONS: dict[str, str] = {
    STATE_CONNECTED: (
        "WARP переходит в состояние «Подключён» — статус обновится сам."
    ),
    STATE_DISCONNECTED: (
        "WARP переходит в состояние «Отключён» — статус обновится сам."
    ),
}

#: Предупреждение, если за отведённое время целевое состояние не наступило.
POLL_TIMEOUT_WARNINGS: dict[str, str] = {
    STATE_CONNECTED: (
        "WARP всё ещё подключается — проверьте окно Cloudflare WARP"
    ),
    STATE_DISCONNECTED: (
        "WARP всё ещё отключается — проверьте окно Cloudflare WARP"
    ),
}

#: Слова, которыми клиент обозначает переходные состояния. Их наличие в
#: выводе ``status`` означает, что клиент работает, а не отвечает ошибкой.
_TRANSITION_MARKERS: tuple[str, ...] = (
    "connecting",
    "disconnecting",
    "reconnecting",
)



class WarpPage(Page):
    """Раздел «Обход (WARP)»: статус, подключение, режим, исключения."""

    #: Сообщение для шапки окна: (текст, уровень info/success/error).
    status_message = pyqtSignal(str, str)
    #: Ошибка операции: (заголовок, сообщение) — окно показывает диалог.
    failed = pyqtSignal(str, str)
    #: Прогресс массового добавления исключений: (готово, всего) — из фона.
    bulk_progress = pyqtSignal(int, int)
    #: Стадия массовой операции («Скачивание списка РФ...») — из фона.
    bulk_status = pyqtSignal(str)

    def __init__(
        self,
        window: WindowApi,
        manager: WarpManager | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            window,
            "Обход (WARP)",
            parent=parent,
            spacing=12,
            margins=(18, 14, 18, 14),
        )
        #: Логика работы с warp-cli (запуск команд — в фоне).
        self.manager = manager if manager is not None else WarpManager()
        #: Идёт фоновая операция: кнопки на это время блокируются.
        self._busy = False
        #: Последнее известное состояние WARP (нужно для перекраски темы).
        self._status = WarpStatus()
        #: Режим, показанный клиентом (ключ warp-cli).
        self._mode = ""
        #: Уровень цвета подписи статуса исключений.
        self._exclude_level = "muted"
        #: Идёт массовое добавление исключений (тогда работает «Остановить»).
        self._bulk_running = False
        #: Лимит записей для текущей массовой операции (из поля «лимит»).
        self._bulk_limit = WARP_EXCLUDE_LIMIT_DEFAULT
        #: Текущая массовая операция — загрузка списка РФ (нужно для даты).
        self._bulk_download = False
        #: Флаг остановки: его выставляет кнопка «Остановить», читает фон.
        self._stop_event = threading.Event()
        #: Записи, добавленные в этом сеансе кнопкой «Загрузить список РФ»
        #: (ключи :func:`core.warp_manager.dedup_key`) — по ним в таблице
        #: различаются источники WarpBypass и «Вручную».
        self._warpbypass_values: set[str] = set()
        #: Записи, добавленные в этом сеансе вручную или из активных соединений.
        self._manual_values: set[str] = set()
        #: Ожидаемое состояние после текущей операции (пусто — опроса нет).
        self._poll_expected = ""
        #: Сколько попыток опроса уже сделано.
        self._poll_attempts = 0
        #: Идёт фоновая попытка опроса (вторая одновременно не запускается).
        self._poll_running = False
        #: Работник текущей попытки: по нему отсеиваются ответы прошлых опросов.
        self._poll_worker = None
        #: Состояние, которого ждём от завершившейся команды (см. _run_command).
        self._pending_expectation = ""
        #: Счётчик подтверждённых опросом состояний. Растёт, когда опрос увидел
        #: целевое состояние; по нему отсеиваются ответы старых опросов настроек
        #: (см. _on_snapshot), которые иначе вернули бы старое «Отключён».
        self._status_serial = 0
        #: Значение счётчика на момент запуска текущего опроса настроек.
        self._snapshot_serial = 0

        #: Таймер опроса состояния: тикает, пока WARP не придёт в целевое
        #: состояние. Каждый тик делает один фоновый ``warp-cli status``.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(STATUS_POLL_INTERVAL_MS)
        self._poll_timer.setSingleShot(False)
        self._poll_timer.timeout.connect(self._poll_status)

        self._build()
        self.bulk_progress.connect(self._on_bulk_progress)
        self.bulk_status.connect(self._on_bulk_status)
        self._apply_availability()

    # ==================================================================
    #  Построение
    # ==================================================================
    def _build(self) -> None:
        self.status_card = ModernCard("Cloudflare WARP", STATUS_HINT)
        self._build_status()
        self.add_card(self.status_card)

        self.mode_card = Card("Режим работы", MODE_HINT)
        self._build_mode()
        self.add_card(self.mode_card)

        self.exclude_card = Card("Исключения (Split Tunnel)", EXCLUDE_HINT)
        self._build_exclusions()
        self.add_card(self.exclude_card)

        self.info_card = Card("Информация")
        self._build_info()
        self.add_card(self.info_card)

        self.finish_layout()

    def _build_status(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.indicator = StatusIndicator()
        self.indicator.set_text("Проверка...", "muted")
        row.addWidget(self.indicator)
        row.addStretch(1)
        self.status_card.add_layout(row)

        self.reason_label = QLabel("")
        self.reason_label.setObjectName("Muted")
        self.reason_label.setWordWrap(True)
        self.reason_label.setVisible(False)
        self.status_card.add_widget(self.reason_label)

        self.install_hint = QLabel(INSTALL_HINT)
        self.install_hint.setObjectName("Warning")
        self.install_hint.setWordWrap(True)
        self.install_hint.setOpenExternalLinks(True)
        self.install_hint.setVisible(False)
        self.status_card.add_widget(self.install_hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        self.connect_button = self.make_button(
            "Подключить",
            "primary",
            tooltip="Поднять туннель WARP: warp-cli connect",
        )
        self.connect_button.clicked.connect(self.on_connect)
        buttons.addWidget(self.connect_button)

        self.disconnect_button = self.make_button(
            "Отключить",
            "danger",
            tooltip="Опустить туннель WARP: warp-cli disconnect",
        )
        self.disconnect_button.clicked.connect(self.on_disconnect)
        buttons.addWidget(self.disconnect_button)

        self.reconnect_button = self.make_button(
            "Переподключить",
            tooltip="Отключить и подключить заново — нужно после смены режима",
        )
        self.reconnect_button.clicked.connect(self.on_reconnect)
        buttons.addWidget(self.reconnect_button)

        buttons.addStretch(1)

        self.refresh_button = self.make_button(
            "Обновить статус",
            tooltip="Перечитать состояние и режим WARP",
        )
        self.refresh_button.clicked.connect(self.on_refresh)
        buttons.addWidget(self.refresh_button)

        self.status_card.add_layout(buttons)

    def _build_mode(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        caption = QLabel("Режим:")
        caption.setObjectName("Muted")
        row.addWidget(caption)

        self.mode_combo = QComboBox()
        for key, label in WARP_MODES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.setToolTip(
            "Режим WARP: «Трафик и DNS» (warp), «Только DNS» (doh) или "
            "«Выключено» (off). Выбор применяется сразу."
        )
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        row.addWidget(self.mode_combo, 1)

        self.mode_card.add_layout(row)

        self.mode_notice = QLabel(MODE_APPLIED_HINT)
        self.mode_notice.setObjectName("Hint")
        self.mode_notice.setWordWrap(True)
        self.mode_notice.setVisible(False)
        self.mode_card.add_widget(self.mode_notice)

    def _build_exclusions(self) -> None:
        """Карточка «Исключения (Split Tunnel)»."""
        self.exclude_warning = QLabel(EXCLUDE_WARNING)
        self.exclude_warning.setObjectName("Warning")
        self.exclude_warning.setWordWrap(True)
        self.exclude_card.add_widget(self.exclude_warning)

        self.exclude_notice = QLabel(EXCLUDE_UNSUPPORTED_HINT)
        self.exclude_notice.setObjectName("Warning")
        self.exclude_notice.setWordWrap(True)
        self.exclude_notice.setVisible(False)
        self.exclude_card.add_widget(self.exclude_notice)

        self.host_notice = QLabel(EXCLUDE_HOST_UNSUPPORTED_HINT)
        self.host_notice.setObjectName("Warning")
        self.host_notice.setWordWrap(True)
        self.host_notice.setVisible(False)
        self.exclude_card.add_widget(self.host_notice)

        # Наборы доменов YouTube и Discord: их обходит запрет, и WARP не должен
        # перехватывать их трафик, иначе два обхода конфликтуют.
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        preset_caption = QLabel(PRESET_CAPTION)
        preset_caption.setObjectName("Muted")
        preset_row.addWidget(preset_caption)

        #: Кнопки добавления наборов; ключ — их подпись (для логов и отладки).
        self.preset_buttons: dict[str, QPushButton] = {}
        for text, tooltip, presets in PRESET_BUTTONS:
            button = self.make_button(text, tooltip=tooltip)
            button.clicked.connect(
                lambda _checked=False, group=presets: self.on_exclude_presets(group)
            )
            preset_row.addWidget(button)
            self.preset_buttons[text] = button
        preset_row.addStretch(1)
        self.exclude_card.add_layout(preset_row)

        self.preset_hint = QLabel(EXCLUDE_PRESET_HINT)
        self.preset_hint.setObjectName("Hint")
        self.preset_hint.setWordWrap(True)
        self.exclude_card.add_widget(self.preset_hint)

        # Откат: убираются только домены наборов, чужие записи не трогаются.
        rollback_row = QHBoxLayout()
        rollback_row.setSpacing(8)
        rollback_caption = QLabel(PRESET_ROLLBACK_CAPTION)
        rollback_caption.setObjectName("Muted")
        rollback_row.addWidget(rollback_caption)

        #: Кнопки отката наборов; ключ — их подпись.
        self.rollback_buttons: dict[str, QPushButton] = {}
        for text, tooltip, presets in PRESET_ROLLBACK_BUTTONS:
            button = self.make_button(text, "danger", tooltip=tooltip)
            button.clicked.connect(
                lambda _checked=False, group=presets: self.on_remove_presets(group)
            )
            rollback_row.addWidget(button)
            self.rollback_buttons[text] = button
        rollback_row.addStretch(1)
        self.exclude_card.add_layout(rollback_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.load_ru_button = self.make_button(
            "Загрузить список РФ",
            "primary",
            tooltip=(
                "Скачать ru_bypass_ip.txt и ru_bypass_domain.txt из "
                "BushHub/WarpBypass и добавить их в исключения WARP"
            ),
        )
        self.load_ru_button.clicked.connect(self.on_load_ru_list)
        buttons.addWidget(self.load_ru_button)

        self.active_button = self.make_button(
            "Добавить текущие соединения",
            tooltip=ACTIVE_BUTTON_HINT,
        )
        self.active_button.clicked.connect(self.on_add_active_connections)
        buttons.addWidget(self.active_button)
        buttons.addStretch(1)

        self.refresh_exclude_button = self.make_button(
            "Обновить список",
            tooltip="Перечитать исключения из warp-cli",
        )
        self.refresh_exclude_button.clicked.connect(self.on_refresh)
        buttons.addWidget(self.refresh_exclude_button)

        self.clear_button = self.make_button(
            "Очистить исключения",
            "danger",
            tooltip=(
                "Удалить все записи, добавленные приложением (помета "
                "«CLI exclude»). Записи самого WARP не трогаются"
            ),
        )
        self.clear_button.clicked.connect(self.on_clear_exclusions)
        buttons.addWidget(self.clear_button)
        self.exclude_card.add_layout(buttons)

        limit_row = QHBoxLayout()
        limit_row.setSpacing(8)
        limit_caption = QLabel("Лимит записей за один раз:")
        limit_caption.setObjectName("Muted")
        limit_row.addWidget(limit_caption)

        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(WARP_EXCLUDE_LIMIT_MIN, WARP_EXCLUDE_LIMIT_MAX)
        self.limit_spin.setSingleStep(50)
        self.limit_spin.setValue(WARP_EXCLUDE_LIMIT_DEFAULT)
        self.limit_spin.setToolTip(EXCLUDE_LIMIT_HINT)
        limit_row.addWidget(self.limit_spin)

        limit_hint = QLabel(EXCLUDE_LIMIT_HINT)
        limit_hint.setObjectName("Hint")
        limit_hint.setWordWrap(True)
        limit_row.addWidget(limit_hint, 1)
        self.exclude_card.add_layout(limit_row)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        self.manual_edit = QLineEdit()
        self.manual_edit.setPlaceholderText(
            "Добавить вручную: адрес, диапазон (10.0.0.0/8) или домен (*.ru)"
        )
        self.manual_edit.setToolTip(
            "Запись с маской уходит в warp-cli tunnel ip add-range, без маски — "
            "в tunnel ip add, домен — в tunnel host add."
        )
        self.manual_edit.returnPressed.connect(self.on_manual_add)
        manual_row.addWidget(self.manual_edit, 1)

        self.manual_add_button = self.make_button(
            "Добавить",
            tooltip="Добавить введённую запись в исключения",
        )
        self.manual_add_button.clicked.connect(self.on_manual_add)
        manual_row.addWidget(self.manual_add_button)

        self.manual_remove_button = self.make_button(
            "Удалить выбранное",
            "danger",
            tooltip="Удалить выделенную строку таблицы из исключений",
        )
        self.manual_remove_button.clicked.connect(self.on_remove_selected)
        manual_row.addWidget(self.manual_remove_button)
        self.exclude_card.add_layout(manual_row)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.exclude_progress = QProgressBar()
        self.exclude_progress.setTextVisible(False)
        self.exclude_progress.setFixedHeight(8)
        self.exclude_progress.setRange(0, 100)
        self.exclude_progress.setValue(0)
        self.exclude_progress.setVisible(False)
        progress_row.addWidget(self.exclude_progress, 1)

        self.stop_button = self.make_button(
            "Остановить",
            "danger",
            tooltip="Прервать массовое добавление (текущая запись будет дозаписана)",
        )
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.on_stop_bulk)
        progress_row.addWidget(self.stop_button)
        self.exclude_card.add_layout(progress_row)

        self.exclude_status = QLabel(EXCLUDE_IDLE_TEXT)
        self.exclude_status.setObjectName("Muted")
        self.exclude_status.setWordWrap(True)
        self.exclude_card.add_widget(self.exclude_status)

        self.ip_count_value = self.kv_row(self.exclude_card.body, "IP-адреса в исключениях")
        self.host_count_value = self.kv_row(self.exclude_card.body, "Домены в исключениях")
        self.last_download_value = self.kv_row(
            self.exclude_card.body, "Последняя загрузка списка РФ"
        )
        self.last_download_value.setText(warp_last_download() or "—")

        self.exclude_table = QTableWidget(0, len(EXCLUDE_HEADERS))
        self.exclude_table.setHorizontalHeaderLabels(list(EXCLUDE_HEADERS))
        self.exclude_table.verticalHeader().setVisible(False)
        self.exclude_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.exclude_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.exclude_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.exclude_table.setWordWrap(False)
        self.exclude_table.setAlternatingRowColors(True)
        self.exclude_table.setMinimumHeight(200)
        self.exclude_table.setToolTip(
            "Текущие исключения WARP. Колонка «Источник» показывает, откуда "
            "взялась запись: «WARP» — записи самого клиента (локальные сети и "
            "адреса Cloudflare), «WarpBypass» — список российских адресов, "
            "«YouTube» и «Discord» — домены наборов, «Вручную» — остальные "
            "записи. Удалить добавленное приложением можно кнопками очистки."
        )
        header = self.exclude_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setHighlightSections(False)
        self.exclude_card.add_widget(self.exclude_table)

    def _build_info(self) -> None:
        self.cli_value = self.kv_row(self.info_card.body, "warp-cli")
        self.version_value = self.kv_row(self.info_card.body, "Версия клиента")
        self.mode_value = self.kv_row(self.info_card.body, "Текущий режим")

        self.managed_label = QLabel(MANAGED_HINT)
        self.managed_label.setObjectName("Warning")
        self.managed_label.setWordWrap(True)
        self.info_card.add_widget(self.managed_label)

    # ==================================================================
    #  Обновление состояния
    # ==================================================================
    def refresh(self) -> None:
        """Перечитывает состояние, режим и версию WARP в фоне.

        Если клиент не найден, ничего не запускаем: страница переходит в
        режим «WARP не установлен» (кнопки блокируются).
        """
        if not self.manager.is_installed():
            self._show_not_installed()
            return
        if self._busy:
            return

        self._set_busy(True)
        #: Запоминаем, какое состояние уже подтверждено: если за время чтения
        #: настроек опрос увидит новое, старое значение не должно его перебить.
        self._snapshot_serial = self._status_serial
        worker = self.window.run_async(
            self._snapshot,
            on_success=self._on_snapshot,
            on_error=self._on_refresh_error,
        )
        if worker is None:  # вызов не из потока окна — запрос переадресован
            self._set_busy(False)

    def _snapshot(self) -> tuple:
        """Состояние, настройки, версия и исключения — одним фоновым вызовом.

        Поддержка исключений проверяется здесь же: ``tunnel_supported`` и
        ``tunnel_host_supported`` запускают ``warp-cli`` (только чтение —
        разбирается справка), и вызывать их из отрисовки нельзя. Результат
        кэшируется в менеджере, поэтому дальше это лишь чтение двух списков.
        """
        return (
            self.manager.status(),
            self.manager.settings(),
            self.manager.version(),
            self.manager.tunnel_supported(),
            self.manager.tunnel_host_supported(),
            self.manager.tunnel_entries(KIND_IP),
            self.manager.tunnel_entries(KIND_HOST),
        )

    def _on_snapshot(self, data: object) -> None:
        self._set_busy(False)
        if not (isinstance(data, tuple) and len(data) == 7):
            log.warning("Неожиданный ответ опроса WARP: %r", data)
            return

        (
            status,
            settings,
            version,
            supported,
            host_supported,
            ip_entries,
            host_entries,
        ) = data
        if self._snapshot_serial == self._status_serial:
            self._apply_status(status)
        else:
            # Пока читались настройки, состояние подтвердил опрос: его ответ
            # свежее, и старым значением индикатор перезаписывать нельзя.
            log.debug(
                "Состояние из опроса настроек устарело (подтверждено: %s) — пропускаю",
                self._status.state,
            )
        self._apply_settings(settings)
        self.version_value.setText(str(version) if version else "—")
        self.cli_value.setText(str(self.manager.warp_cli_path() or "не найден"))
        self._apply_exclusions(ip_entries, host_entries)
        # Подсказку «не поддерживается» и доступность кнопок выставляет
        # _apply_availability: она читает кэш проверки, заполненный опросом.
        if not supported:
            log.info(
                "Исключения Split Tunnel не поддержаны: %s",
                self.manager.tunnel_unsupported_reason() or "без ответа клиента",
            )
        elif not host_supported:
            # Управление исключениями есть, а доменов в них — нет: кнопки
            # наборов (YouTube/Discord) остаются неактивными.
            log.info("Домены в исключениях WARP не поддержаны: %s", HOST_UNSUPPORTED_HINT)
        self._apply_availability()

    def _on_refresh_error(self, message: str) -> None:
        self._set_busy(False)
        self._apply_availability()
        self.status_message.emit(message.splitlines()[0] if message else "Ошибка WARP", "error")

    def _apply_status(self, status: WarpStatus) -> None:
        """Показывает состояние WARP и причину, сообщённую клиентом."""
        self._status = status
        if self._poll_expected and status.state != self._poll_expected:
            # Идёт опрос после операции: показываем промежуточное состояние, а
            # не старое значение, которое клиент ещё не сменил.
            self._show_pending(self._poll_expected)
            return

        self.indicator.set_text(status.label, self._state_color(status.state))
        self.indicator.setToolTip(status.raw or NOT_INSTALLED_HINT)
        self.install_hint.setVisible(False)

        if status.reason:
            self.reason_label.setText(f"Причина: {status.reason}")
        elif status.state == STATE_CONNECTED:
            self.reason_label.setText("Трафик и DNS идут через туннель Cloudflare.")
        elif status.state == STATE_DISCONNECTED:
            self.reason_label.setText(
                "Туннель опущен. Нажмите «Подключить», чтобы поднять его."
            )
        elif status.state == STATE_CONNECTING:
            self.reason_label.setText("Устанавливается соединение с Cloudflare...")
        else:
            self.reason_label.setText(
                "Состояние определить не удалось — нажмите «Обновить статус»."
            )
        self.reason_label.setVisible(bool(self.reason_label.text()))

    def _apply_settings(self, settings: dict) -> None:
        """Синхронизирует комбобокс режима с настройками клиента."""
        self._mode = str(settings.get("mode", "") or "")
        index = self.mode_combo.findData(self._mode)
        self.mode_combo.blockSignals(True)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        self.mode_value.setText(mode_label(self._mode) or "—")
        self.mode_combo.setToolTip(
            settings.get("raw", "")[:1500]
            or "Режим WARP: «Трафик и DNS» (warp), «Только DNS» (doh), «Выключено» (off)"
        )

    def _apply_exclusions(self, ip_data: object, host_data: object) -> None:
        """Заполняет таблицу исключений и счётчики.

        :param ip_data: ответ :meth:`core.warp_manager.WarpManager.tunnel_entries`
            для адресов — ``(успех, записи, сообщение)``.
        :param host_data: то же для доменов.
        """
        rows: list[tuple[str, object]] = []
        problems: list[str] = []
        counts = {KIND_IP: 0, KIND_HOST: 0}

        if not self.manager.is_installed():
            self.exclude_table.setRowCount(0)
            self._set_exclude_status(
                "Исключения недоступны: клиент warp-cli не найден.", "muted"
            )
            return

        for kind, data in ((KIND_IP, ip_data), (KIND_HOST, host_data)):
            label = "IP-адреса" if kind == KIND_IP else "домены"
            if not (isinstance(data, tuple) and len(data) == 3):
                problems.append(f"Неожиданный ответ по исключениям ({label}).")
                continue
            ok, entries, message = data
            if not ok:
                problems.append(str(message) or f"Не удалось прочитать исключения ({label}).")
                continue
            entries = list(entries)
            counts[kind] = len(entries)
            rows.extend((kind, entry) for entry in entries)

        self.exclude_table.setRowCount(0)
        self.exclude_table.setRowCount(len(rows))
        ours = 0
        for index, (kind, entry) in enumerate(rows):
            value = getattr(entry, "value", str(entry))
            cli = bool(getattr(entry, "cli", False))
            ours += 1 if cli else 0

            kind_item = QTableWidgetItem("IP" if kind == KIND_IP else "домен")
            kind_item.setData(Qt.ItemDataRole.UserRole, kind)
            self.exclude_table.setItem(index, 0, kind_item)
            self.exclude_table.setItem(index, 1, QTableWidgetItem(str(value)))

            source = self._entry_source(kind, str(value), cli)
            source_item = QTableWidgetItem(source)
            source_item.setToolTip(
                SOURCE_HINTS.get(source, "Источник записи неизвестен.")
            )
            self.exclude_table.setItem(index, 2, source_item)

        self.ip_count_value.setText(str(counts[KIND_IP]))
        self.host_count_value.setText(str(counts[KIND_HOST]))

        if problems:
            self._set_exclude_status(" ".join(problems), "error")
            return
        if not rows:
            self._set_exclude_status(
                "Исключений нет: весь трафик идёт через туннель.", "warning"
            )
            return
        self._set_exclude_status(
            f"Исключений: {len(rows)} (IP: {counts[KIND_IP]}, домены: "
            f"{counts[KIND_HOST]}), из них добавлено приложением: {ours}.",
            "muted",
        )

    def _entry_source(self, kind: str, value: str, cli: bool) -> str:
        """Источник записи для колонки «Источник».

        Записи без пометы ``(CLI exclude)`` добавил сам WARP — это
        :data:`SOURCE_WARP`. Остальные раскладываются так:

        * домен из наборов — :data:`SOURCE_YOUTUBE` / :data:`SOURCE_DISCORD`
          (это видно по самому значению, поэтому источник не теряется между
          запусками приложения);
        * значение, добавленное в этом сеансе списком РФ, — :data:`SOURCE_WARPBYPASS`
          (``_warpbypass_values``), добавленное вручную — :data:`SOURCE_MANUAL`
          (``_manual_values``);
        * остаток: адреса и диапазоны считаются списком WarpBypass (его грузят
          пакетом, и именно он наполняет исключения по IP), а незнакомые домены —
          добавленными вручную.
        """
        if not cli:
            return SOURCE_WARP

        text = str(value or "").strip().lower().rstrip(".")
        if kind == KIND_HOST:
            if text in _YOUTUBE_EXCLUDE_SET:
                return PRESET_SOURCES[PRESET_YOUTUBE]
            if text in _DISCORD_EXCLUDE_SET:
                return PRESET_SOURCES[PRESET_DISCORD]

        key = dedup_key(kind, str(value))
        if key in self._manual_values:
            return SOURCE_MANUAL
        if key in self._warpbypass_values:
            return SOURCE_WARPBYPASS
        return SOURCE_WARPBYPASS if kind == KIND_IP else SOURCE_MANUAL

    def _set_exclude_status(self, text: str, level: str) -> None:
        """Показывает статус исключений в карточке и красит его по уровню."""
        self._exclude_level = level
        self.exclude_status.setText(text)
        self._paint(self.exclude_status, level)

    def _show_not_installed(self) -> None:
        """Режим «WARP не установлен»: подсказка вместо состояния."""
        # Клиент исчез (удалён, убран из PATH) — опрашивать больше нечего.
        self._cancel_status_poll()
        self._status = WarpStatus(state=STATE_UNKNOWN, reason=NOT_INSTALLED_HINT)
        self.indicator.set_text(NOT_FOUND_TEXT, "muted")
        self.indicator.setToolTip(NOT_INSTALLED_HINT)
        self.reason_label.setText("Клиент warp-cli не найден в PATH и по стандартному пути.")
        self.reason_label.setVisible(True)
        self.install_hint.setVisible(True)

        self.cli_value.setText("не найден")
        self.version_value.setText("—")
        self.mode_value.setText("—")

        self.exclude_notice.setVisible(False)
        self._apply_exclusions(None, None)
        self.ip_count_value.setText("—")
        self.host_count_value.setText("—")

        self._apply_availability()

    @staticmethod
    def _state_color(state: str) -> str:
        """Цвет индикатора состояния из активной темы."""
        if state == STATE_CONNECTED:
            return "success"
        if state == STATE_DISCONNECTED:
            return "error"
        if state == STATE_CONNECTING:
            return "warning"
        return "muted"

    # ==================================================================
    #  Опрос состояния после операции
    # ==================================================================
    def _start_status_poll(self, expected: str) -> None:
        """Запускает повторный опрос состояния после операции.

        ``warp-cli connect``/``disconnect`` отвечают сразу, а туннель переходит
        в целевое состояние через 1–3 секунды, поэтому одиночное чтение статуса
        показало бы старое значение. Опрос идёт каждые
        :data:`STATUS_POLL_INTERVAL_MS` мс (фоном, не блокируя окно) и
        прекращается, как только клиент сообщит ``expected`` — или когда
        попытки закончатся.

        :param expected: ожидаемое состояние (:data:`STATE_CONNECTED` после
            подключения, :data:`STATE_DISCONNECTED` после отключения).
        """
        self._cancel_status_poll()
        if not self.isVisible():
            # Раздел уже закрыт (пользователь ушёл в другой): опрашивать нечего —
            # при возвращении состояние прочитает refresh() из showEvent.
            return
        self._poll_expected = expected
        self._show_pending(expected)
        self._poll_timer.start()

    def _cancel_status_poll(self) -> None:
        """Останавливает опрос: новая операция, уход со страницы, конец опроса."""
        self._poll_timer.stop()
        self._poll_expected = ""
        self._poll_attempts = 0
        self._poll_running = False
        self._poll_worker = None

    def _show_pending(self, expected: str) -> None:
        """Показывает промежуточное состояние, пока WARP переходит в целевое."""
        self.indicator.set_text(PENDING_LABELS.get(expected, "Ожидание..."), "warning")
        self.indicator.setToolTip(
            "Дождитесь ответа WARP.\n" + (self._status.raw or "warp-cli не отвечал.")
        )
        self.install_hint.setVisible(False)
        self.reason_label.setText(
            PENDING_REASONS.get(expected, "Ожидание ответа WARP...")
        )
        self.reason_label.setVisible(True)

    def _poll_status(self) -> None:
        """Один тик таймера: запускает фоновое чтение состояния.

        Тик считается попыткой (их всего :data:`STATUS_POLL_ATTEMPTS`, то есть
        5 секунд) и тогда, когда предыдущий опрос ещё не вернулся: ``warp-cli``
        иногда отвечает дольше 500 мс, а второй вызов поверх первого только
        добавил бы нагрузки. Поэтому опрос гарантированно прекращается по
        таймеру, а не ждёт ответа бесконечно.
        """
        if not self._poll_expected:
            self._poll_timer.stop()
            return
        if self._poll_attempts >= STATUS_POLL_ATTEMPTS:
            self._finish_status_poll(
                POLL_TIMEOUT_WARNINGS.get(self._poll_expected, "")
            )
            return
        self._poll_attempts += 1
        if self._poll_running:
            return

        self._poll_running = True
        worker = self.window.run_async(self.manager.status)
        if worker is None:  # вызов не из потока окна — попытку не тратим
            self._poll_running = False
            return
        self._poll_worker = worker
        worker.finished_signal.connect(self._on_poll_result)
        worker.error_signal.connect(self._on_poll_error)

    def _on_poll_result(self, result: object) -> None:
        """Ответ очередного опроса: обновляет индикатор и решает, ждать ли ещё."""
        sender = self.sender()
        if sender is not None and sender is not self._poll_worker:
            # Ответ прошлого опроса: новая операция уже запустила свой.
            return
        self._poll_running = False
        self._poll_worker = None

        expected = self._poll_expected
        if not expected:  # опрос отменён (уход со страницы, новая операция)
            return

        if not isinstance(result, WarpStatus):
            log.warning("Неожиданный ответ опроса состояния WARP: %r", result)
            self._fail_status_poll("Не удалось прочитать состояние WARP")
            return

        status = result
        if (
            status.state == STATE_UNKNOWN
            and status.reason
            and not self._is_transition(status.raw)
        ):
            # Клиент не смог ответить (служба остановлена, нет прав): ждать
            # дальше бессмысленно — показываем причину и прекращаем опрос.
            self._cancel_status_poll()
            self._apply_status(status)
            self.status_message.emit(f"Состояние WARP: {status.reason}", "error")
            return

        self._apply_status(status)
        if status.state == expected:
            # Дождались: индикатор уже показывает конечное состояние. Режим и
            # исключения перечитываются заново — состояние подтверждено, и
            # старый опрос настроек его больше не перебьёт (см. _on_snapshot).
            self._status_serial += 1
            self._cancel_status_poll()
            self.refresh()
        # Если состояние ещё не наступило, опрос продолжит следующий тик
        # таймера, а по исчерпании попыток завершится с предупреждением.

    def _on_poll_error(self, message: str) -> None:
        """Фоновая попытка опроса завершилась ошибкой."""
        sender = self.sender()
        if sender is not None and sender is not self._poll_worker:
            return
        self._poll_running = False
        self._poll_worker = None
        if not self._poll_expected:
            return
        self._fail_status_poll(message or "Не удалось прочитать состояние WARP")

    def _finish_status_poll(self, warning: str = "") -> None:
        """Останавливает опрос и показывает то состояние, что ответил клиент.

        :param warning: предупреждение, если целевое состояние так и не
            наступило; пусто — опрос завершился успешно.
        """
        status = self._status
        self._cancel_status_poll()
        self._apply_status(status)
        if warning:
            reason = self.reason_label.text()
            self.reason_label.setText(f"{reason}\n{warning}" if reason else warning)
            self.reason_label.setVisible(True)
            self.status_message.emit(warning, "info")
            log.warning("WARP: %s (%s)", warning, status.raw or status.state)
        # Режим и исключения перечитываются в фоне: за время опроса клиент мог
        # успеть ответить иначе (например, всё-таки подключиться).
        self.refresh()

    def _fail_status_poll(self, message: str) -> None:
        """Останавливает опрос и показывает ошибку чтения состояния."""
        text = (message or "Не удалось прочитать состояние WARP").splitlines()[0]
        self._cancel_status_poll()
        self.indicator.set_text("Ошибка WARP", "error")
        self.indicator.setToolTip(message or text)
        self.reason_label.setText(text)
        self.reason_label.setVisible(True)
        self.status_message.emit(text, "error")
        log.warning("Опрос состояния WARP не удался: %s", text)

    @staticmethod
    def _is_transition(raw: str) -> bool:
        """Сообщает ли клиент переходное состояние (Connecting/Disconnecting)."""
        lowered = (raw or "").lower()
        return any(marker in lowered for marker in _TRANSITION_MARKERS)

    def _apply_availability(self) -> None:
        """Включает элементы только когда клиент найден и нет фоновой задачи.

        Селектор режимов дополнительно блокируется, если клиент не поддерживает
        смену режима (``unrecognized subcommand`` на команду ``mode``): кнопки
        подключения и отключения при этом остаются рабочими.

        Кнопки исключений блокируются ещё и тогда, когда клиент не умеет
        управлять Split Tunnel (``Not yet implemented``): пока проверка
        (``tunnel_supported``) не выполнена, они тоже неактивны — её делает
        фоновый опрос при открытии раздела.

        Кнопки наборов доменов (YouTube/Discord) блокируются дополнительно,
        если клиент не умеет ``tunnel host add``: пока проверка
        (``tunnel_host_supported``) не выполнена, они тоже неактивны.
        """
        installed = self.manager.is_installed()
        enabled = installed and not self._busy
        mode_supported = self.manager.mode_switch_supported()
        support = self.manager.tunnel_supported_cached()
        exclude_enabled = enabled and bool(support)
        host_support = self.manager.tunnel_host_supported_cached()
        preset_enabled = exclude_enabled and bool(host_support)

        for button in (
            self.connect_button,
            self.disconnect_button,
            self.reconnect_button,
            self.refresh_button,
        ):
            button.setEnabled(enabled)
        self.mode_combo.setEnabled(enabled and mode_supported)
        if not mode_supported:
            self.mode_combo.setToolTip(MODE_UNSUPPORTED_HINT)
        self.install_hint.setVisible(not installed)

        for button in (
            self.load_ru_button,
            self.active_button,
            self.refresh_exclude_button,
            self.clear_button,
            self.manual_add_button,
            self.manual_remove_button,
        ):
            button.setEnabled(exclude_enabled)
        # psutil — необязательная зависимость, и её наличие проверяется без
        # импорта (см. WarpManager.psutil_available). Кнопку намеренно не
        # отключаем: нажатие должно показать подсказку об установке пакета, а
        # не оставить пользователя перед «мёртвой» кнопкой.
        self.active_button.setToolTip(
            ACTIVE_BUTTON_HINT
            if self.manager.psutil_available()
            else PSUTIL_MISSING_HINT
        )
        self.limit_spin.setEnabled(exclude_enabled)
        self.manual_edit.setEnabled(exclude_enabled)
        self.stop_button.setEnabled(self._bulk_running)
        self.exclude_notice.setVisible(installed and support is False)
        # Наборы доменов: кнопки активны только там, где клиент умеет
        # ``tunnel host add``, иначе рядом показана подсказка.
        for button in (*self.preset_buttons.values(), *self.rollback_buttons.values()):
            button.setEnabled(preset_enabled)
        self.host_notice.setVisible(installed and support is not False and host_support is False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._apply_availability()

    # ==================================================================
    #  Действия
    # ==================================================================
    def on_refresh(self) -> None:
        """Кнопка «Обновить статус»."""
        self.refresh()

    def on_connect(self) -> None:
        """Кнопка «Подключить»."""
        self._run_command(
            self.manager.connect,
            "Подключение WARP...",
            expect=STATE_CONNECTED,
        )

    def on_disconnect(self) -> None:
        """Кнопка «Отключить»."""
        self._run_command(
            self.manager.disconnect,
            "Отключение WARP...",
            expect=STATE_DISCONNECTED,
        )

    def on_reconnect(self) -> None:
        """Кнопка «Переподключить»: применить режим к туннелю."""
        self._run_command(
            self._reconnect,
            "Переподключение WARP...",
            expect=STATE_CONNECTED,
        )

    def _reconnect(self) -> tuple[bool, str]:
        """Переподключение в фоне: отключить и подключить заново."""
        self.manager.disconnect()
        return self.manager.connect()

    def on_mode_changed(self, _index: int) -> None:
        """Смена режима в комбобоксе — сразу ``warp-cli mode``."""
        key = self.mode_combo.currentData()
        if not key or key == self._mode:
            return
        # Напоминание о переподключении показывается сразу: смена режима меняет
        # настройку, но уже поднятый туннель продолжает работать по-старому.
        self.mode_notice.setVisible(True)
        self._run_command(
            lambda: self.manager.set_mode(str(key)),
            f"Смена режима WARP: {mode_label(str(key))}...",
            # Режим «Выключено» клиент выполняет отключением туннеля: состояние
            # тоже нужно дочитать опросом, иначе индикатор остался бы «Подключён».
            expect=STATE_DISCONNECTED if str(key) == MODE_OFF else "",
        )

    def _run_command(self, fn, busy_message: str, *, expect: str = "") -> None:
        """Выполняет команду warp-cli в фоне и обновляет состояние.

        :param expect: состояние, в которое команда должна привести WARP
            (``connected``/``disconnected``). После успеха страница дочитывает
            его опросом: клиент переходит в него не мгновенно. Пусто — команда
            состояние не меняет (например, смена режима), опрос не нужен.
        """
        if self._busy:
            return
        # Новая операция отменяет прежний опрос: иначе «Отключить» во время
        # опроса подключения ждало бы состояния «Подключён».
        self._cancel_status_poll()
        self._pending_expectation = expect
        self._set_busy(True)
        self.status_message.emit(busy_message, "info")

        worker = self.window.run_async(fn, busy_message=busy_message)
        if worker is None:
            self._pending_expectation = ""
            self._set_busy(False)
            return
        worker.finished_signal.connect(self._on_command_done)
        worker.error_signal.connect(self._on_command_error)

    def _on_command_done(self, result: object) -> None:
        self._set_busy(False)
        expected = self._pending_expectation
        self._pending_expectation = ""
        ok, message = self._as_result(result)

        if ok:
            log.info("WARP: %s", message)
            self.status_message.emit(
                message.splitlines()[0] if message else "Готово", "success"
            )
        else:
            self.status_message.emit(message.splitlines()[0], "error")
            self.failed.emit("Cloudflare WARP", message)

        if ok and expected:
            # Команда принята, но клиент переходит в целевое состояние через
            # 1–3 секунды: статус дочитывается опросом (см. _start_status_poll),
            # который по завершении сам перечитает режим и исключения.
            self._start_status_poll(expected)
            return

        # Состояние и режим перечитываются в фоне: клиент мог ответить не так,
        # как ожидалось (например, «Уже подключено»).
        self.refresh()

    def _on_command_error(self, message: str) -> None:
        self._set_busy(False)
        self._pending_expectation = ""
        self._cancel_status_poll()
        self._apply_availability()
        text = message.splitlines()[0] if message else "Ошибка операции с WARP"
        self.status_message.emit(text, "error")
        self.failed.emit("Cloudflare WARP", message or text)

    @staticmethod
    def _as_result(result: object) -> tuple[bool, str]:
        """Приводит ответ команды к ``(успех, сообщение)``."""
        if isinstance(result, tuple) and len(result) == 2:
            return bool(result[0]), str(result[1] or "")
        return False, "Неожиданный ответ warp-cli."

    # ==================================================================
    #  Действия: исключения (Split Tunnel)
    # ==================================================================
    def _exclusions_ready(self) -> bool:
        """Можно ли сейчас менять исключения (клиент есть и умеет это)."""
        if self._busy:
            return False
        if not self.manager.is_installed():
            return False
        if not self.manager.tunnel_supported_cached():
            # Поддержка ещё не проверена или клиент её не заявляет: подсказка
            # уже показана карточкой, кнопки неактивны.
            return False
        return True

    def on_load_ru_list(self) -> None:
        """Кнопка «Загрузить список РФ»: скачать WarpBypass и добавить записи."""
        if not self._exclusions_ready():
            return
        limit = self.limit_spin.value()
        if not self.window.confirm(
            "Загрузка списка РФ",
            "Скачать список российских адресов и доменов WarpBypass "
            "(BushHub/WarpBypass) и добавить их в исключения WARP?",
            "Адреса из списка WARP не трогает — они открываются напрямую, "
            f"остальной трафик идёт через туннель. За один заход добавляется "
            f"не больше {limit} записей каждого вида: список длинный, а "
            "warp-cli принимает записи по одной.",
        ):
            return
        self._start_bulk(self._load_ru_list, "Загрузка списка РФ...", download=True)

    def _load_ru_list(self) -> tuple[bool, str]:
        """Фоновый этап кнопки «Загрузить список РФ».

        Скачивает оба файла списка и добавляет первые :attr:`_bulk_limit`
        записей каждого вида. Прогресс и стадии уходят в интерфейс сигналами:
        трогать виджеты из этого потока нельзя.
        """
        limit = self._bulk_limit
        lines: list[str] = []
        problems: list[str] = []

        for download, kind, label in (
            (self.manager.download_warpbypass_ip, KIND_IP, "IP-адреса"),
            (self.manager.download_warpbypass_domain, KIND_HOST, "домены"),
        ):
            if self._stop_event.is_set():
                lines.append(f"{label}: остановлено пользователем")
                break

            self.bulk_status.emit(f"Скачивание списка РФ: {label}...")
            ok, text, message = download()
            if not ok:
                problems.append(message)
                continue

            values = parse_warpbypass_list(text)
            selected = values[:limit]
            if len(values) > len(selected):
                lines.append(
                    f"{label}: в списке {len(values)}, взяты первые {len(selected)}"
                )

            self.bulk_status.emit(f"Добавление исключений: {label}...")
            result = self.manager.add_tunnel_entries(
                selected,
                kind=kind,
                on_progress=self._emit_progress,
                should_stop=self._stop_event.is_set,
            )
            # Источник записей списка РФ запоминается: по нему таблица отличает
            # WarpBypass от добавленного вручную (см. _entry_source).
            self._warpbypass_values.update(dedup_key(kind, value) for value in selected)
            if result.error:
                problems.append(f"{label}: {result.error}")
            else:
                lines.append(self._result_text(result, label))

        messages = [*lines, *problems]
        return not problems, "\n".join(messages) if messages else "Список загружен"

    def on_add_active_connections(self) -> None:
        """Кнопка «Добавить текущие соединения»: адреса из ``psutil``."""
        if not self._exclusions_ready():
            return
        self._start_bulk(
            self._add_active_connections, "Сбор активных соединений..."
        )

    def _add_active_connections(self) -> tuple[bool, str]:
        """Фоновый этап кнопки «Добавить текущие соединения»."""
        self.bulk_status.emit("Чтение активных соединений...")
        connections = self.manager.active_connections()
        if not connections.ok:
            return False, connections.warning
        if not connections.ips:
            return False, self._empty_connections_hint(connections)

        limit = self._bulk_limit
        values = list(connections.ips)
        selected = values[:limit]
        self.bulk_status.emit(f"Добавление исключений: найдено {len(values)} адресов...")
        result = self.manager.add_tunnel_entries(
            selected,
            kind=KIND_IP,
            on_progress=self._emit_progress,
            should_stop=self._stop_event.is_set,
        )
        if result.error:
            return False, result.error

        label = f"Текущие соединения (найдено адресов: {len(values)})"
        if len(selected) < len(values):
            label += f", взяты первые {len(selected)}"
        return True, self._result_text(result, label)

    @staticmethod
    def _empty_connections_hint(connections: ActiveConnections) -> str:
        """Объясняет, почему активных адресов не нашлось."""
        if connections.established == 0:
            return (
                "Активных (установленных) соединений не найдено — добавлять нечего."
            )
        if connections.web == 0:
            return (
                "Среди активных соединений нет соединений на порты 80 и 443 — "
                "добавлять нечего."
            )
        return (
            "Найдены только локальные адреса — в исключения добавлять нечего."
        )

    def on_manual_add(self) -> None:
        """Кнопка «Добавить»: ручное добавление записи в исключения."""
        if not self._exclusions_ready():
            return
        value = self.manual_edit.text().strip()
        if not value:
            self.status_message.emit("Введите адрес, диапазон или домен", "error")
            return
        self._run_command(
            lambda: self._add_manual(value),
            f"Добавление в исключения: {value}...",
        )

    def _add_manual(self, value: str) -> tuple[bool, str]:
        """Добавляет запись, введённую вручную, и помечает её источник."""
        ok, message = self.manager.tunnel_add(value)
        if ok:
            self._manual_values.add(dedup_key(guess_kind(value), value))
        return ok, message

    def on_remove_selected(self) -> None:
        """Кнопка «Удалить выбранное»: убрать запись из исключений."""
        if not self._exclusions_ready():
            return
        row = self.exclude_table.currentRow()
        if row < 0:
            self.status_message.emit("Выберите строку в таблице исключений", "error")
            return
        item = self.exclude_table.item(row, 1)
        value = item.text().strip() if item is not None else ""
        if not value:
            return
        self._run_command(
            lambda: self._remove_entry(value),
            f"Удаление из исключений: {value}...",
        )

    def _remove_entry(self, value: str) -> tuple[bool, str]:
        """Удаляет запись и снимает пометку источника: её больше нет в списке."""
        ok, message = self.manager.tunnel_remove(value)
        if ok:
            key = dedup_key(guess_kind(value), value)
            self._manual_values.discard(key)
            self._warpbypass_values.discard(key)
        return ok, message

    def on_clear_exclusions(self) -> None:
        """Кнопка «Очистить все исключения»: удалить добавленное приложением."""
        if not self._exclusions_ready():
            return
        if not self.window.confirm(
            "Очистка исключений",
            "Удалить все исключения, добавленные приложением?",
            "Записи, которые WARP добавил сам (локальные сети, адреса "
            "Cloudflare), не удаляются: клиент их не помечает, а их удаление "
            "нарушило бы маршрутизацию. После очистки весь трафик снова пойдёт "
            "через туннель, а российские сайты откроются напрямую.",
        ):
            return
        self._run_command(self._clear_exclusions, "Очистка исключений...")

    def _clear_exclusions(self) -> tuple[bool, str]:
        """Фоновый этап очистки: удаляет наши записи по IP и по доменам."""
        ok_ip, message_ip = self.manager.tunnel_ip_clear()
        ok_host, message_host = self.manager.tunnel_host_clear()
        # Записи удалены — пометки источников больше не нужны.
        self._warpbypass_values.clear()
        self._manual_values.clear()
        return (ok_ip and ok_host), f"{message_ip}\n{message_host}"

    # ------------------------------------------------------------------
    #  Действия: наборы доменов YouTube и Discord
    # ------------------------------------------------------------------
    def on_exclude_presets(self, presets: tuple[str, ...]) -> None:
        """Кнопки «Исключить YouTube» / «Discord» / «YouTube + Discord».

        Домены наборов добавляются в исключения WARP: тогда его туннель их не
        перехватывает и они остаются запрету. Уже добавленные домены не
        дублируются (см. :meth:`core.warp_manager.WarpManager.add_preset_exclusions`).
        """
        if not self._exclusions_ready():
            return
        label = self._presets_label(presets)
        self._start_bulk(
            lambda: self._add_presets(presets),
            f"Добавление исключений WARP: {label}...",
        )

    def _add_presets(self, presets: tuple[str, ...]) -> tuple[bool, str]:
        """Фоновый этап добавления наборов: домены по одному, с прогрессом."""
        lines: list[str] = []
        ok_all = True
        for preset in presets:
            self.bulk_status.emit(f"Добавление исключений: {preset_label(preset)}...")
            ok, message = self.manager.add_preset_exclusions(
                preset,
                on_progress=self._emit_progress,
                should_stop=self._stop_event.is_set,
            )
            ok_all = ok_all and ok
            lines.append(message)
            if self._stop_event.is_set():
                break
        return ok_all, "\n".join(lines) if lines else "Нечего добавлять"

    def on_remove_presets(self, presets: tuple[str, ...]) -> None:
        """Кнопки «Убрать YouTube» / «Убрать Discord»: откат наборов.

        Удаляются **только** домены набора: остальные исключения (WarpBypass,
        записи самого WARP, добавленное вручную) не трогаются.
        """
        if not self._exclusions_ready():
            return
        label = self._presets_label(presets)
        if not self.window.confirm(
            "Убрать исключения",
            f"Убрать домены {label} из исключений WARP?",
            "Эти домены снова пойдут через туннель Cloudflare и начнут "
            "конфликтовать с запретом. Другие записи (WarpBypass, добавленные "
            "вручную) не удаляются.",
        ):
            return
        self._start_bulk(
            lambda: self._remove_presets(presets),
            f"Удаление исключений WARP: {label}...",
        )

    def _remove_presets(self, presets: tuple[str, ...]) -> tuple[bool, str]:
        """Фоновый этап отката наборов: удаляются только домены набора."""
        lines: list[str] = []
        ok_all = True
        for preset in presets:
            self.bulk_status.emit(f"Удаление исключений: {preset_label(preset)}...")
            ok, message = self.manager.remove_preset_exclusions(
                preset,
                on_progress=self._emit_progress,
                should_stop=self._stop_event.is_set,
            )
            ok_all = ok_all and ok
            lines.append(message)
            if self._stop_event.is_set():
                break
        return ok_all, "\n".join(lines) if lines else "Нечего удалять"

    @staticmethod
    def _presets_label(presets: tuple[str, ...]) -> str:
        """Подпись набора для статуса и диалогов («YouTube + Discord»)."""
        return " + ".join(preset_label(preset) for preset in presets)

    def on_stop_bulk(self) -> None:
        """Кнопка «Остановить»: прерывает массовое добавление."""
        if not self._bulk_running:
            return
        self._stop_event.set()
        self.stop_button.setEnabled(False)
        self._set_exclude_status(
            "Останавливаю: текущая запись будет дозаписана.", "warning"
        )

    # ------------------------------------------------------------------
    #  Массовые операции: запуск и обработка результата
    # ------------------------------------------------------------------
    def _start_bulk(self, fn, busy_message: str, *, download: bool = False) -> None:
        """Запускает массовую операцию в фоне с прогрессом и остановкой."""
        if self._busy:
            return
        self._bulk_running = True
        self._bulk_download = download
        self._bulk_limit = self.limit_spin.value()
        self._stop_event.clear()

        self._set_busy(True)
        self.exclude_progress.setVisible(True)
        self.exclude_progress.setRange(0, 0)
        self.exclude_progress.setValue(0)
        self._set_exclude_status(busy_message, "muted")
        self.status_message.emit(busy_message, "info")

        worker = self.window.run_async(fn, busy_message=busy_message)
        if worker is None:
            self._finish_bulk()
            return
        worker.finished_signal.connect(self._on_bulk_done)
        worker.error_signal.connect(self._on_bulk_error)

    def _emit_progress(self, done: int, total: int) -> None:
        """Прогресс из фонового потока: сигнал доставляется в поток окна."""
        self.bulk_progress.emit(done, total)

    def _on_bulk_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.exclude_progress.setRange(0, 0)
            return
        self.exclude_progress.setRange(0, total)
        self.exclude_progress.setValue(done)
        self.exclude_status.setText(f"Добавление в исключения: {done} из {total}")

    def _on_bulk_status(self, text: str) -> None:
        self._set_exclude_status(text, "muted")

    def _on_bulk_done(self, result: object) -> None:
        downloaded = self._bulk_download
        self._finish_bulk()
        ok, message = self._as_result(result)
        first = message.splitlines()[0] if message else "Готово"

        if ok:
            if downloaded:
                stamp = set_warp_last_download(
                    datetime.now().strftime("%d.%m.%Y %H:%M")
                )
                self.last_download_value.setText(stamp or "—")
            log.info("Исключения WARP: %s", message)
            self._set_exclude_status(message or "Готово", "success")
            self.status_message.emit(first, "success")
        else:
            log.warning("Исключения WARP: %s", message)
            self._set_exclude_status(message or "Не удалось изменить исключения", "error")
            self.status_message.emit(first or "Ошибка исключений", "error")
            self.failed.emit("Исключения (Split Tunnel)", message or first)

        # Список исключений и счётчики перечитываются в фоне.
        self.refresh()

    def _on_bulk_error(self, message: str) -> None:
        self._finish_bulk()
        text = (message or "Ошибка операции с исключениями").splitlines()[0]
        self._set_exclude_status(message or text, "error")
        self.status_message.emit(text, "error")
        self.failed.emit("Исключения (Split Tunnel)", message or text)

    def _finish_bulk(self) -> None:
        """Возвращает карточку исключений в спокойное состояние."""
        self._bulk_running = False
        self._bulk_download = False
        self._stop_event.clear()
        self.exclude_progress.setVisible(False)
        self.exclude_progress.setRange(0, 100)
        self.exclude_progress.setValue(0)
        self._set_busy(False)

    @staticmethod
    def _result_text(result: TunnelAddResult, label: str) -> str:
        """Строка итога массового добавления с примерами ошибок."""
        text = result.summary(label)
        for value, message in result.first_errors:
            text += f"; {value} — {message}"
        extra = result.failed - len(result.first_errors)
        if extra > 0:
            text += f"; и ещё ошибок: {extra}"
        return text

    # ==================================================================
    #  События и тема
    # ==================================================================
    def showEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """При открытии раздела перечитывает состояние и режим WARP.

        Автоподключения при этом не происходит: раздел только показывает
        состояние — WARP включается кнопкой.
        """
        super().showEvent(event)
        self.refresh()

    def hideEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """При уходе со страницы опрос состояния прекращается.

        Иначе таймер продолжал бы запускать ``warp-cli status`` из другого
        раздела, а новый опрос после возвращения конфликтовал бы со старым.
        """
        super().hideEvent(event)
        self._cancel_status_poll()

    @staticmethod
    def _paint(label: QLabel, level: str) -> None:
        """Красит подпись цветом темы (``success`` / ``error`` / ``muted``)."""
        label.setStyleSheet(f"color: {Theme.color(level)};")

    def refresh_theme(self) -> None:
        """Перекрашивает виджеты под активную тему."""
        super().refresh_theme()
        for card in (
            self.status_card,
            self.mode_card,
            self.exclude_card,
            self.info_card,
        ):
            card.refresh_theme()
        self.indicator.refresh_theme()
        if not self.manager.is_installed():
            self.indicator.set_text(NOT_FOUND_TEXT, "muted")
        elif self._poll_expected and self._status.state != self._poll_expected:
            # Идёт опрос: подпись остаётся промежуточной, а не «старой».
            self._show_pending(self._poll_expected)
        else:
            self.indicator.set_text(self._status.label, self._state_color(self._status.state))
        self._paint(self.exclude_status, self._exclude_level)


__all__ = [
    "EXCLUDE_HINT",
    "EXCLUDE_PRESET_HINT",
    "EXCLUDE_WARNING",
    "INSTALL_HINT",
    "MANAGED_HINT",
    "PENDING_LABELS",
    "POLL_TIMEOUT_WARNINGS",
    "PRESET_BUTTONS",
    "PRESET_ROLLBACK_BUTTONS",
    "SOURCE_DISCORD",
    "SOURCE_MANUAL",
    "SOURCE_WARP",
    "SOURCE_WARPBYPASS",
    "SOURCE_YOUTUBE",
    "STATUS_POLL_ATTEMPTS",
    "STATUS_POLL_INTERVAL_MS",
    "WarpPage",
]
