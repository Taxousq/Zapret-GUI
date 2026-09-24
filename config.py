"""Константы и настройки приложения Zapret GUI.

Здесь собраны все «магические» значения: путь к папке запрета, имя службы,
список сайтов для проверки, таймауты и цвета тёмной темы.

Тёмная тема реализована целиком через inline-стили, внешние .qss-файлы не
используются — проект самодостаточен. Сейчас стили собирает :class:`gui.theme.Theme`
(тёмная и светлая темы); словарь :data:`COLORS` ниже остаётся тёмной палитрой
по умолчанию, а ``GLOBAL_QSS`` — её устаревшей копией (оставлен для совместимости).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import tempfile
from pathlib import Path
from string import Template

# ---------------------------------------------------------------------------
#  Общие сведения о приложении
# ---------------------------------------------------------------------------
APP_NAME = "Zapret GUI"
APP_VERSION = "1.0.1"
APP_TITLE = "Zapret GUI — управление обходом блокировок"
#: Автор сборки — подписывается в статус-баре («by Taxo»).
APP_AUTHOR = "Taxo"
#: Копирайт для статус-бара. Символ © записан escape-последовательностью,
#: чтобы файл не зависел от кодировки редактора.
APP_COPYRIGHT = "\u00a9 2026 Taxo"

# ---------------------------------------------------------------------------
#  Автообновление приложения (GitHub Releases)
# ---------------------------------------------------------------------------
# Обновляется **сам GUI**: релизы публикуются в репозитории приложения, оттуда
# скачивается установщик Inno Setup и запускается. Это отдельный механизм от
# обновления запрета (см. GITHUB_REPO ниже): там релизы Flowseal и .zip-архив,
# здесь установщик ZapretGUI-Setup-*.exe.
#: Репозиторий приложения, из релизов которого обновляется GUI.
APP_GITHUB_REPO = "Taxousq/Zapret-GUI"
#: Последний опубликованный релиз (GitHub API). Не путать с GITHUB_API_LATEST.
APP_GITHUB_API_LATEST = f"https://api.github.com/repos/{APP_GITHUB_REPO}/releases/latest"
#: Страница релизов — открывается кнопкой «Открыть на GitHub».
APP_GITHUB_RELEASES_URL = f"https://github.com/{APP_GITHUB_REPO}/releases"
#: Задержка первой проверки обновлений после старта (мс). Проверка идёт в фоне,
#: но запускать её сразу не нужно: окно должно появиться мгновенно.
UPDATE_CHECK_DELAY_MS = 5000
#: Период фоновой проверки обновлений приложения (часы).
UPDATE_CHECK_INTERVAL_HOURS = 24
#: Таймаут запроса к GitHub API при проверке обновлений приложения (секунды).
APP_UPDATE_TIMEOUT = 15
#: Таймаут скачивания установщика (секунды): файл заметно больше ответа API.
APP_UPDATE_DOWNLOAD_TIMEOUT = 60
#: Минимальный размер установщика (байт). Файл меньше — считаем загрузку битой.
APP_UPDATE_MIN_SIZE = 1024 * 1024
#: Папка во временном каталоге Windows, куда скачивается установщик.
APP_UPDATE_DIR_NAME = "ZapretGUI-update"
#: Часть имени ассета релиза, по которой ищется установщик (``Setup`` + ``.exe``).
APP_UPDATE_ASSET_HINT = "setup"

# ===========================================================================
#  ПУТЬ К ПАПКЕ «ЗАПРЕТА» — ЗНАЧЕНИЕ ПО УМОЛЧАНИЮ
#
#  Путь к запрету теперь настраивается через GUI и хранится в QSettings
#  (``QSettings("ZapretGUI", "Paths")``, ключ ``zapret_path``). Это значение
#  используется только если настройки нет: при первом запуске приложение
#  ищет запрет само (:class:`core.zapret_locator.ZapretLocator`) и предлагает
#  скачать его с GitHub или указать папку вручную.
#
#  Менять строку ниже вручную не нужно — откройте раздел «Настройки» в GUI.
#  Значение остаётся здесь как запасной путь для запуска без сохранённых
#  настроек; по умолчанию предполагается, что папка zapret лежит рядом с
#  папкой приложения (на одном уровне с zapret-gui/ при запуске из исходников
#  или с exe в сборке).
#
#  ВАЖНО: путь считается от папки приложения, а не от ``__file__``. В
#  onefile-сборке PyInstaller модули распаковываются во временную папку
#  (``%TEMP%\\_MEIxxxxxx``), поэтому ``Path(__file__).parent.parent / "zapret"``
#  давал ``%TEMP%\\zapret`` — именно он попадал в ошибку «Тестер не найден».
# ===========================================================================


def _app_dir() -> Path:
    """Папка приложения: рядом с exe в сборке, иначе — корень проекта.

    Импорт :mod:`core.portable` ленивый: ``config`` не должен тянуть ядро при
    импорте (его же импортируют модули ядра).
    """
    from core import portable

    return portable.application_dir()


#: Имя папки запрета рядом с папкой приложения.
ZAPRET_DIR_NAME = "zapret"

ZAPRET_PATH = _app_dir().parent / ZAPRET_DIR_NAME


def _fallback_zapret_paths() -> tuple[Path, ...]:
    """Типичные места установки запрета. Временные папки отбрасываются."""
    from core import portable

    candidates: tuple[Path, ...] = (
        Path(r"D:\Zapret"),
        Path(r"C:\Zapret"),
        Path(r"D:\zapret"),
        Path(r"C:\zapret"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Zapret",
        Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Zapret",
        _app_dir() / ZAPRET_DIR_NAME,
        _app_dir().parent / ZAPRET_DIR_NAME,
    )
    kept = tuple(item for item in candidates if not portable.is_temp_path(item))
    if len(kept) != len(candidates):  # pragma: no cover — зависит от среды
        logging.getLogger(__name__).warning(
            "Временные папки исключены из автопоиска запрета"
        )
    return kept


# Порядок автопоиска запрета (см. core/zapret_locator.py). Если путь выше не
# существует, приложение попробует найти запрет сам в типичных местах.
# Путей с %TEMP% здесь быть не должно: запрет туда не устанавливается.
FALLBACK_ZAPRET_PATHS: tuple[Path, ...] = _fallback_zapret_paths()


def looks_like_zapret(path: Path) -> bool:
    """Похожа ли папка на установленный запрет (есть bin\\winws.exe).

    Временные папки запретом не считаются: в onefile-сборке PyInstaller такие
    пути (``%TEMP%\\zapret``) появляются сами собой, хотя запрета там нет.
    """
    from core import portable

    if portable.is_temp_path(path):
        return False
    try:
        return path.is_dir() and (path / "bin" / "winws.exe").is_file()
    except OSError:
        return False


def resolve_zapret_path() -> Path:
    """Возвращает существующий путь к запрету или исходное значение.

    Временные папки (``%TEMP%``) пропускаются: запрет там не устанавливается,
    а в onefile-сборке PyInstaller такие пути получаются из ``__file__``.
    """
    if looks_like_zapret(ZAPRET_PATH):
        return ZAPRET_PATH
    for candidate in FALLBACK_ZAPRET_PATHS:
        if looks_like_zapret(candidate):
            logging.getLogger(__name__).info(
                "Папка запрета найдена автоматически: %s", candidate
            )
            return candidate
    return ZAPRET_PATH


ZAPRET_PATH = resolve_zapret_path()

# Производные пути внутри папки запрета.
BIN_DIR = ZAPRET_PATH / "bin"
LISTS_DIR = ZAPRET_PATH / "lists"
UTILS_DIR = ZAPRET_PATH / "utils"
WINWS_EXE = BIN_DIR / "winws.exe"
SERVICE_BAT = ZAPRET_PATH / "service.bat"
GAME_FILTER_FLAG = UTILS_DIR / "game_filter.enabled"

# ---------------------------------------------------------------------------
#  Служба Windows
# ---------------------------------------------------------------------------
SERVICE_NAME = "zapret"
SERVICE_DISPLAY_NAME = "zapret"
SERVICE_DESCRIPTION = "Zapret DPI bypass software"
# В этот параметр реестра service.bat записывает имя выбранной стратегии.
STRATEGY_REGISTRY_VALUE = "zapret-discord-youtube"
SERVICE_REGISTRY_KEY = rf"HKLM\System\CurrentControlSet\Services\{SERVICE_NAME}"

#: Сколько ждать после удаления службы перед повторной установкой.
#: Без паузы Windows может отказаться создавать службу с тем же именем.
SERVICE_DELETE_DELAY_SEC = 2.0

#: Таймаут для команд sc/net/reg (секунды).
COMMAND_TIMEOUT = 15

#: Сколько ждать перехода службы в новое состояние (секунды).
STATE_WAIT_TIMEOUT = 12.0

#: Период автообновления статуса службы в шапке окна (мс).
STATUS_POLL_INTERVAL_MS = 5000

# ---------------------------------------------------------------------------
#  Размеры интерфейса
# ---------------------------------------------------------------------------
#: Ширина бокового меню (разделы приложения), px.
SIDEBAR_WIDTH = 220
#: Размер иконок пунктов бокового меню, px.
SIDEBAR_ICON_SIZE = 24
#: Высота шапки окна, px.
HEADER_HEIGHT = 56
#: Высота статус-бара, px. Полоса фиксированной высоты: она отделена от окон
#: линией сверху и не перекрывает сайдбар (место резервирует QMainWindow).
STATUS_BAR_HEIGHT = 26

# ---------------------------------------------------------------------------
#  Индексы страниц (QStackedWidget главного окна)
# ---------------------------------------------------------------------------
#: Порядок страниц в стеке и пунктов в боковом меню. Первой идёт «Обзор» —
#: это стартовый экран приложения. Все переходы выполняются по этим именам,
#: а не по «магическим» числам: при добавлении раздела достаточно поправить
#: порядок в :data:`gui.sidebar.SECTIONS` и константы ниже.
OVERVIEW_PAGE_INDEX = 0
SERVICE_PAGE_INDEX = 1
STRATEGIES_PAGE_INDEX = 2
#: «Списки» — правка list-general-user.txt и list-exclude.txt (папка lists).
LISTS_PAGE_INDEX = 3
#: «Обход (WARP)» — управление Cloudflare WARP через warp-cli.
WARP_PAGE_INDEX = 4
TESTING_PAGE_INDEX = 5
HEALTH_PAGE_INDEX = 6
UPDATE_PAGE_INDEX = 7
LOGS_PAGE_INDEX = 8
#: «Настройки» — папка запрета (последний пункт меню).
SETTINGS_PAGE_INDEX = 9
#: Сколько всего страниц (для проверки согласованности меню и стека).
PAGE_COUNT = 10
#: Минимальный размер окна: меньше — интерфейс перестаёт помещаться.
WINDOW_MIN_WIDTH = 900
WINDOW_MIN_HEIGHT = 600

# ---------------------------------------------------------------------------
#  Стратегии обхода (.bat-файлы в папке запрета)
# ---------------------------------------------------------------------------
#: Имена вида «general (ALT11).bat» -> стратегия «ALT11».
STRATEGY_RE = r"general\s*\(([^)]+)\)\.bat"

#: Служебные .bat-файлы, которые не являются стратегиями.
SKIP_STRATEGY_FILES = frozenset(
    {"service.bat", "uninstall.bat", "kill-windivert.bat", "uninstall_service.bat"}
)

#: Стратегии, которые показываются в списке первыми (рекомендованные).
PREFERRED_STRATEGIES = ("ALT11", "ALT12")

# ---------------------------------------------------------------------------
#  Проверка доступности сайтов
# ---------------------------------------------------------------------------
CHECK_SITES: tuple[tuple[str, str], ...] = (
    ("YouTube", "https://www.youtube.com/"),
    ("Discord", "https://discord.com/"),
    ("Instagram", "https://www.instagram.com/"),
    ("Telegram", "https://web.telegram.org/"),
    ("Google", "https://www.google.com/"),
)

#: Таймаут на один сайт (секунды).
CHECK_TIMEOUT = 5.0
#: Сколько сайтов проверять одновременно.
CHECK_WORKERS = 5
#: User-Agent «как Chrome» — иначе часть сайтов отдаёт заглушки.
CHECK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
#  Обновления с GitHub
# ---------------------------------------------------------------------------
GITHUB_REPO = "Flowseal/zapret-discord-youtube"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_ACCEPT = "application/vnd.github.v3+json"
#: Таймаут сетевых запросов к GitHub (секунды).
UPDATE_TIMEOUT = 30

#: Файлы, которые обновление не перезаписывает: это пользовательские списки.
UPDATE_PRESERVE_PATTERNS = ("-user.",)

# ---------------------------------------------------------------------------
#  Обход (WARP): управление Cloudflare WARP через warp-cli
# ---------------------------------------------------------------------------
# Раздел не реализует VPN сам и ничего не устанавливает: он управляет уже
# установленным Cloudflare WARP через его консольный клиент warp-cli
# (см. core/warp_manager.WarpManager).

#: Имя исполняемого файла клиента. Сначала ищется в PATH, затем по пути ниже.
WARP_CLI_NAME = "warp-cli.exe"

#: Куда Cloudflare WARP ставит warp-cli по умолчанию (Windows).
WARP_CLI_PATH = Path(r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe")

#: Таймаут одной команды warp-cli (секунды).
WARP_COMMAND_TIMEOUT = 15

#: Режимы работы WARP: ключ для ``warp-cli mode`` -> подпись в интерфейсе.
#: Значение ``off`` поддерживают не все версии клиента: если его нет, режим
#: «Выключено» выполняется отключением WARP (см. core.warp_manager.WarpManager).
WARP_MODES: tuple[tuple[str, str], ...] = (
    ("warp", "Трафик и DNS (WARP)"),
    ("doh", "Только DNS (DoH)"),
    ("off", "Выключено (Off)"),
)

#: Страница загрузки WARP: показывается, если клиент не найден, и открывается
#: кнопкой «Открыть сайт» на вкладке «Обновление» (см. core/warp_updater).
WARP_DOWNLOAD_URL = "https://cloudflare.com/warp"
#: Прежнее имя той же ссылки — оставлено для совместимости: на него ссылается
#: страница «Обход (WARP)». Значение одно и то же, менять нужно оба.
WARP_INSTALL_URL = WARP_DOWNLOAD_URL

#: Идентификатор WARP в winget — по нему проверяется обновление клиента.
#: WARP обновляется сам через Cloudflare, но winget знает о новых версиях
#: раньше, чем они доедут: команда ``winget upgrade Cloudflare.Warp`` просто
#: сообщает, есть ли что-то новее установленного.
WARP_WINGET_ID = "Cloudflare.Warp"
#: Таймаут проверки обновления через winget (секунды): winget не отвечает
#: мгновенно, но и ждать его минутами нельзя — GUI замирал бы.
WARP_WINGET_TIMEOUT = 60.0
#: Слова в выводе winget, означающие «доступна новая версия».
WARP_WINGET_UPDATE_MARKERS: tuple[str, ...] = (
    "доступно обновление",
    "обновление доступно",
    "an update is available",
    "update available",
    "upgrades available",
)
#: Слова в выводе winget, означающие «установлена последняя версия».
WARP_WINGET_LATEST_MARKERS: tuple[str, ...] = (
    "последняя версия",
    "нет доступных обновлений",
    "no applicable update found",
    "no newer package versions are available",
    "latest version is already installed",
)

# ---------------------------------------------------------------------------
#  Обход (WARP): исключения (Split Tunnel)
# ---------------------------------------------------------------------------
# WARP работает в режиме Exclude: через туннель идёт весь трафик, КРОМЕ
# перечисленных адресов. Чтобы российские сайты открывались напрямую (а весь
# остальной трафик шёл через Cloudflare), их адреса добавляются в исключения
# командами ``warp-cli tunnel ip add`` / ``tunnel host add``.
#
# Готовый список российских адресов берётся из проекта BushHub/WarpBypass.

#: Репозиторий и ветка со списком российских адресов.
WARP_BYPASS_REPO = "BushHub/WarpBypass"
WARP_BYPASS_BRANCH = "main"
#: Файлы списка: IP-диапазоны (CIDR) и домены.
WARP_BYPASS_IP_FILE = "ru_bypass_ip.txt"
WARP_BYPASS_DOMAIN_FILE = "ru_bypass_domain.txt"
#: Ссылки на «сырые» файлы списка (raw.githubusercontent.com отдаёт их текстом).
WARP_BYPASS_IP_URL = (
    f"https://raw.githubusercontent.com/{WARP_BYPASS_REPO}/"
    f"{WARP_BYPASS_BRANCH}/{WARP_BYPASS_IP_FILE}"
)
WARP_BYPASS_DOMAIN_URL = (
    f"https://raw.githubusercontent.com/{WARP_BYPASS_REPO}/"
    f"{WARP_BYPASS_BRANCH}/{WARP_BYPASS_DOMAIN_FILE}"
)
#: Таймаут скачивания одного файла списка (секунды).
WARP_BYPASS_TIMEOUT = 30.0

#: Сколько записей списка добавлять в исключения за один заход по умолчанию.
#: В списке сотни и тысячи строк, а warp-cli принимает их только по одной:
#: добавлять всё сразу без спроса нельзя, поэтому страница показывает лимит
#: и спрашивает подтверждение, если список длиннее.
WARP_EXCLUDE_LIMIT_DEFAULT = 500
#: Границы поля «лимит записей» в интерфейсе.
WARP_EXCLUDE_LIMIT_MIN = 10
WARP_EXCLUDE_LIMIT_MAX = 20000

#: Порты удалённых соединений, которые попадают в исключения кнопкой
#: «Добавить текущие соединения» (обычный веб-трафик).
WARP_ACTIVE_PORTS = (80, 443)

#: Сколько строк-ошибок массового добавления показывать в итоговом сообщении.
WARP_EXCLUDE_ERROR_LIMIT = 5

#: Готовые наборы доменов для исключений WARP: их трафик обходит **запрет**
#: (Flowseal), и если его перехватит ещё и WARP, оба обхода начнут мешать друг
#: другу — соединения будут рваться и переподключаться. Кнопки «Исключить
#: YouTube» / «Исключить Discord» добавляют домены из этих наборов в Split
#: Tunnel (``warp-cli tunnel host add``): трафик к ним идёт напрямую, мимо
#: Cloudflare, а остальной трафик остаётся через WARP.
YOUTUBE_EXCLUDE_DOMAINS: tuple[str, ...] = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "youtubei.googleapis.com",
    "ytimg.com",
    "googlevideo.com",
    "yt3.ggpht.com",
)

#: Домены Discord: голосовые каналы и медиа тоже рвутся, если трафик уходит
#: в туннель Cloudflare поверх работающего запрета.
DISCORD_EXCLUDE_DOMAINS: tuple[str, ...] = (
    "discord.com",
    "www.discord.com",
    "discordapp.com",
    "discord.gg",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "gateway.discord.gg",
)

# ---------------------------------------------------------------------------
#  Тестирование стратегий (utils\test zapret.ps1)
# ---------------------------------------------------------------------------
#: Встроенный в запрет тестер стратегий. Свой тестер не пишем: GUI только
#: запускает этот скрипт и разбирает его консольный вывод.
TEST_SCRIPT = UTILS_DIR / "test zapret.ps1"

#: Список целей, по которым тестер проверяет каждую стратегию.
TEST_TARGETS_FILE = UTILS_DIR / "targets.txt"


def tester_script(zapret_path: Path | None = None) -> Path:
    """Путь к встроенному тестеру запрета.

    :data:`TEST_SCRIPT` вычисляется при импорте и потому «замораживает» путь к
    запрету. GUI берёт путь из настроек, поэтому страница тестирования
    спрашивает путь здесь.

    :param zapret_path: папка запрета; ``None`` — значение по умолчанию.
    """
    root = Path(zapret_path) if zapret_path else ZAPRET_PATH
    return root / "utils" / "test zapret.ps1"


def tester_targets_file(zapret_path: Path | None = None) -> Path:
    """Путь к ``utils\\targets.txt`` (см. :func:`tester_script`)."""
    root = Path(zapret_path) if zapret_path else ZAPRET_PATH
    return root / "utils" / "targets.txt"


#: Сколько ждать строку вывода тестера, прежде чем считать его зависшим (секунды).
#: Тестер сам гоняет curl с таймаутом 4 с, поэтому паузы между строками бывают
#: заметными — значение намеренно большое.
TEST_OUTPUT_TIMEOUT = 240.0

#: Сколько ждать завершения процесса тестера при остановке (секунды).
TEST_STOP_TIMEOUT = 15.0

#: Номер пункта меню тестера «Standard tests (HTTP/ping)».
#: DPI-чекеры (пункт 2) намеренно не используются — это отдельный режим.
TEST_TYPE_STANDARD = "1"
#: Пункт меню «All configs».
TEST_MODE_ALL = "1"
#: Пункт меню «Selected configs».
TEST_MODE_SELECTED = "2"

#: Регулярные выражения для разбора консольного вывода тестера (WCAG-текст).
#: Строка аналитики в блоке ``=== ANALYTICS ===``: тестер печатает её через
#: ``$config.PadRight($maxConfigLen)``, поэтому у самой длинной стратегии
#: двоеточие идёт сразу за именем файла::
#:
#:     general                 : OK:  17, FAIL:   9, UNSUP:   0, BLOCKED:   0
#:     general (ALT11).bat     : OK:  17, FAIL:   9, UNSUP:   0, BLOCKED:   0
#:     general (ALT11).bat : OK:  17, FAIL:   9, UNSUP:   0, BLOCKED:   0
#:
#: Поэтому строка аналитики разбирается как «имя файла» + пары «метрика: число»
#: (см. parse_analytics и parse_bare_analytics в core/strategy_tester.py).
TEST_ANALYTICS_RE = (
    r"^(?P<config>.+?)\s*:\s*(?P<body>"
    r"(?:OK|HTTP OK|ERR|ERROR|FAIL|UNSUP|UNSUPPORTED|BLOCKED|LIKELY_BLOCKED|Ping OK|PingFail|Fail)"
    r"\s*:\s*\d+"
    r"(?:\s*,\s*)?)+$"
)
#: Отдельная пара «метрика: число» внутри строки аналитики.
TEST_METRIC_RE = r"(?P<key>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>\d+)"
#: Заголовок блока аналитики — после него строки разбираются как результаты.
TEST_ANALYTICS_HEADER_RE = r"===\s*ANALYTICS\s*==="
#: Строка прогресса: ``  [3/17] general (ALT11).bat``
TEST_CONFIG_HEADER_RE = r"^\s*\[(?P<index>\d+)\s*/\s*(?P<total>\d+)\]\s*(?P<config>.+?)\s*$"
#: Заголовок с общим числом конфигов: ``                 Total configs: 17``
TEST_TOTAL_RE = r"Total configs:\s*(?P<total>\d+)"
#: Итоговая строка: ``Best config: general (ALT11).bat``
TEST_BEST_RE = r"Best config:\s*(?P<config>.+?)\s*$"
#: Элемент списка конфигов: ``  [3] general (ALT11).bat``
TEST_CONFIG_ITEM_RE = r"^\s*\[(?P<index>\d+)\]\s*(?P<config>.+?\.bat)\s*$"
#: Приглашение выбрать конфиги (после него можно отправлять номер).
TEST_SELECT_PROMPT_RE = r"Enter numbers"
#: Признак того, что тестер дошёл до конца.
TEST_COMPLETED_RE = r"All tests finished"
#: Ошибки тестера, по которым GUI показывает понятное сообщение.
TEST_ADMIN_ERROR_RE = r"Run as Administrator"
TEST_SERVICE_ERROR_RE = r"service 'zapret' is installed"
TEST_CURL_ERROR_RE = r"curl\.exe not found"
TEST_NO_CONFIGS_RE = r"No general\*\.bat files found"
#: Тестер ругается на пробелы и кириллицу в пути.
TEST_PATH_FLAG_RE = r"Press any key to exit|Press any key to close"
#: ANSI-последовательности в выводе (цвета консоли) — вырезаются.
ANSI_RE = r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)"

#: Символы, из-за которых тестер может не найти файлы (пробелы/кириллица в пути).
RISKY_PATH_CHARS = (" ",)

# ---------------------------------------------------------------------------
#  Логи службы
# ---------------------------------------------------------------------------
LOGS_EVENT_LIMIT = 50
#: Период автообновления окна логов (мс).
LOGS_REFRESH_INTERVAL_MS = 5000

# ---------------------------------------------------------------------------
#  Автозапуск
# ---------------------------------------------------------------------------
STARTUP_DIR = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "Microsoft"
    / "Windows"
    / "Start Menu"
    / "Programs"
    / "Startup"
)
AUTOSTART_LNK_NAME = "Zapret GUI.lnk"
AUTOSTART_CMD_NAME = "Zapret GUI.cmd"

# ---------------------------------------------------------------------------
#  Цвета тёмной темы
# ---------------------------------------------------------------------------
COLORS = {
    "background": "#0d0d0d",
    "surface": "#1a1a1a",
    "surface_alt": "#141414",
    "border": "#2a2a2a",
    "accent": "#4d6bfe",
    "accent_hover": "#5f7bff",
    "accent_pressed": "#3f59e0",
    "text": "#e5e5e5",
    "muted": "#888888",
    "success": "#22c55e",
    "error": "#ef4444",
    "warning": "#f59e0b",
}

#: Цвета статуса службы (используются в индикаторе и в трее).
STATE_COLORS = {
    "RUNNING": COLORS["success"],
    "STOPPED": COLORS["error"],
    "NOT_INSTALLED": COLORS["muted"],
    "UNKNOWN": COLORS["warning"],
}

#: Цвета для иконки в трее: зелёный / красный / синий по статусу службы.
TRAY_STATE_COLORS = {
    "RUNNING": COLORS["success"],
    "STOPPED": COLORS["error"],
    "NOT_INSTALLED": COLORS["accent"],
    "UNKNOWN": COLORS["warning"],
}

#: Цвета результатов проверки сайтов.
CHECK_COLORS = {
    "OK": COLORS["success"],
    "FAILED": COLORS["error"],
    "TIMEOUT": COLORS["warning"],
    "ERROR": COLORS["error"],
    "PENDING": COLORS["muted"],
}

#: Глобальная таблица стилей. $подстановки заполняются из COLORS.
GLOBAL_QSS = Template(
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

    /* ---------- Карточки ---------- */
    QFrame#Card {
        background-color: $surface;
        border: 1px solid $border;
        border-radius: 12px;
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
    QLabel#HeaderTitle {
        font-size: 18px;
        font-weight: 600;
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
        background-color: #232323;
        color: $text;
        border: 1px solid $border;
        border-radius: 8px;
        padding: 7px 14px;
    }
    QPushButton:hover {
        background-color: #2c2c2c;
        border-color: #3a3a3a;
    }
    QPushButton:pressed {
        background-color: #1e1e1e;
    }
    QPushButton:disabled {
        background-color: #171717;
        color: #555555;
        border-color: #222222;
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
        background-color: #2c3560;
        border-color: #2c3560;
        color: #8b8b8b;
    }
    QPushButton[variant="danger"] {
        color: $error;
        border-color: #4a2222;
    }
    QPushButton[variant="danger"]:hover {
        background-color: #2a1717;
        border-color: $error;
    }

    /* ---------- Комбобокс ---------- */
    QComboBox {
        background-color: #232323;
        border: 1px solid $border;
        border-radius: 8px;
        padding: 7px 10px;
        min-height: 18px;
    }
    QComboBox:hover {
        border-color: #3a3a3a;
    }
    QComboBox:disabled {
        color: #555555;
        background-color: #171717;
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
        selection-background-color: $accent;
        selection-color: #ffffff;
        outline: none;
        padding: 4px;
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
        background-color: #232323;
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
        spacing: 8px;
    }
    QCheckBox::indicator {
        width: 15px;
        height: 15px;
        border: 1px solid $border;
        border-radius: 4px;
        background-color: #232323;
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
        background: #333333;
        border-radius: 5px;
        min-height: 30px;
    }
    QScrollBar::handle:vertical:hover {
        background: #444444;
    }
    QScrollBar::add-line, QScrollBar::sub-line,
    QScrollBar::add-page, QScrollBar::sub-page {
        background: none;
        height: 0;
        border: none;
    }
    QStatusBar {
        background-color: $surface;
        border-top: 1px solid $border;
        color: $muted;
    }
    """
).substitute(COLORS)


def log_file_path() -> Path:
    """Путь к файлу журнала приложения.

    Обычный режим — ``%LOCALAPPDATA%\\ZapretGUI\\zapret-gui.log``; в
    portable-режиме (маркер ``portable.flag`` рядом с exe) — ``logs`` рядом
    с приложением (см. :mod:`core.portable`).
    """
    try:
        from core import portable

        if portable.enabled:
            return portable.log_file_path()
    except Exception:  # noqa: BLE001 — журнал не должен мешать запуску
        pass
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "ZapretGUI" / "zapret-gui.log"


def setup_logging(level: int = logging.INFO) -> None:
    """Настраивает логирование в файл и в консоль.

    Ошибки нигде не «проглатываются» молча: всё, что перехвачено
    через try/except, попадает сюда.
    """
    root = logging.getLogger()
    if root.handlers:  # повторный вызов (например, из main.py) не дублирует хендлеры
        return
    root.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # нет прав на запись — не повод падать
        root.warning("Не удалось открыть файл журнала: %s", exc)
