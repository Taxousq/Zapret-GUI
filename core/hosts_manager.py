"""Системный файл ``hosts``: просмотр, применение пар «IP домен» и откат.

.. deprecated::
   Модуль **больше не используется интерфейсом**. Раздел «Proxy Hosts»
   заменён разделом «Обход (WARP)» (:mod:`core.warp_manager`): подмена
   адресов через ``hosts`` отправляла весь трафик на один чужой IP, который
   не работал как прокси. Код оставлен как есть — он рабочий и самодостаточный
   (свои константы, без импортов из :mod:`config`), и может пригодиться
   позже. Ничего не импортирует его: удалять или оживлять — по необходимости.

Файл перенаправляет домены на конкретные IP-адреса. Это единственный способ
сменить «точку выхода» без VPN: запрет (zapret) обходит DPI, но IP-адрес не
меняет, поэтому, например, TikTok по российскому IP отдаёт старую ленту.
Готовый список пар «IP домен» берётся из HolyZapret (``lists/hosts-list.txt``)
и скачивается по кнопке «Обновить список» (см. :func:`download_hosts_list`).

Почему всё так осторожно:

* файл системный и общий для всей машины — ошибка в нём ломает разрешение
  имён сразу всем приложениям;
* запись требует **прав администратора** (см. :func:`is_admin`), поэтому
  кнопки записи в интерфейсе заблокированы, если приложение запущено обычно;
* перед первой записью рядом создаётся копия ``hosts.bak``, из которой потом
  можно откатиться (:meth:`HostsManager.rollback`). Бэкап создаётся **один
  раз** — до первого изменения: иначе «Откатить» возвращал бы к предыдущему
  нажатию «Применить», а не к состоянию до правок;
* запись идёт через временный файл с последующим ``replace``: оборванная
  запись (нехватка прав, питание, антивирус) не оставит вместо ``hosts``
  пустой файл;
* приложение удаляет из файла **только свои** строки — помеченные
  комментарием :data:`OWN_MARKER`. Чужие записи (в том числе добавленные
  вручную) не трогаются никогда.

Формат ``hosts-list.txt``: одна строка — одна пара ``IP домен``, строки,
начинающиеся с ``#``, — комментарии. Секций по сервисам в файле нет
(заголовки вида ``#HolyZapret 1.3.6`` — это версии), поэтому фильтрация по
сервисам идёт по домену: см. :data:`SERVICE_GROUPS` и :func:`service_of`.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Константы
# ---------------------------------------------------------------------------
#: Системный файл hosts. Путь фиксирован: Windows не позволяет переносить его
#: без правки реестра (``DataBasePath``), а такой правки мы не делаем.
HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")

#: Суффикс файла резервной копии (``hosts`` -> ``hosts.bak``).
BACKUP_SUFFIX = ".bak"

#: Маркер «наших» строк. Он дописывается в конец каждой записи, которую
#: создаёт приложение, — по нему запись потом и удаляется. Короткий и
#: латинский: файл hosts читают системные утилиты, и «кракозябры» в нём
#: нежелательны.
OWN_MARKER = "Zapret GUI"

#: Комментарий-шапка блока, который пишет приложение.
DEFAULT_COMMENT = f"{OWN_MARKER} - Proxy Hosts"

#: Дополнительные (устаревшие) маркеры: записи, созданные прошлыми версиями,
#: тоже должны удаляться кнопкой «Очистить мои записи».
OWN_MARKERS: tuple[str, ...] = (OWN_MARKER, "ZapretGUI", "zapret-gui")

#: Строка-комментарий начинается с решётки.
COMMENT_PREFIX = "#"

#: Суффикс временного файла, из которого запись переименовывается в целевой.
TEMP_SUFFIX = ".tmp"

#: Формат даты бэкапа для интерфейса («23.09.2026 15:30»).
BACKUP_TIME_FORMAT = "%d.%m.%Y %H:%M"

#: Сколько знаков после запятой у таймаута скачивания списка (секунды).
DOWNLOAD_TIMEOUT = 15

#: Адрес списка пар «IP домен» из HolyZapret (raw-файл на GitHub).
HOSTS_LIST_URL = (
    "https://raw.githubusercontent.com/HolyLightRU/HolyZapret/main/lists/hosts-list.txt"
)

#: Запасные адреса: если основной недоступен, пробуем по очереди. Значения
#: проверены на доступность через GitHub API/raw; зеркала указаны на случай
#: переименования ветки или файла.
HOSTS_LIST_URLS: tuple[str, ...] = (
    HOSTS_LIST_URL,
    "https://raw.githubusercontent.com/HolyLightRU/HolyZapret/master/lists/hosts-list.txt",
)

#: User-Agent «как Chrome»: raw.githubusercontent отдаёт файл и без него, но
#: часть промежуточных прокси иначе отвечает заглушкой.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

#: Адреса, которые не перенаправляют, а **блокируют** домен. Показываются
#: отдельной группой «Блокировка»: применять их надо осознанно.
BLOCKING_ADDRESSES = frozenset({"0.0.0.0", "127.0.0.1", "::", "::1"})

#: Регулярное выражение IP-адреса (IPv4 и IPv6) — по нему строка признаётся
#: парой «IP домен». Домены без адреса пропускаются (см. :func:`parse_hosts_list`).
IP_RE = re.compile(
    r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]{2,45}(?::\d{1,5})?)$"
)

#: Проверка домена: буквы, цифры, дефис, точка; допускается ведущий wildcard
#: и завершающая точка (FQDN-форма).
DOMAIN_RE = re.compile(r"^(?:\*\.)?[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?\.?$")


# ---------------------------------------------------------------------------
#  Права администратора
# ---------------------------------------------------------------------------
def is_admin() -> bool:
    """Запущено ли приложение с правами администратора.

    Проверка через ``ctypes.windll.shell32.IsUserAnAdmin()``: она возвращает
    1, если процесс повышен. На не-Windows и при любой ошибке считаем, что
    прав нет: раздел «Proxy Hosts» без них бесполезен, а падать из-за
    проверки он не должен.
    """
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 — проверка не должна ронять приложение
        log.debug("Не удалось определить права администратора", exc_info=True)
        return False


#: Подсказка для интерфейса, когда прав администратора нет.
ADMIN_HINT = (
    "Раздел «Proxy Hosts» изменяет системный файл hosts, а для этого нужны "
    "права администратора.\n\n"
    "Закройте приложение и запустите его заново от имени администратора "
    "(правый клик по ярлыку → «Запуск от имени администратора»)."
)

#: Предупреждение про Windows Defender: он удаляет «подозрительные» записи.
DEFENDER_HINT = (
    f"Windows Defender может удалить записи из {HOSTS_PATH}, посчитав их "
    "подозрительными.\n\n"
    "Чтобы этого избежать: Параметры → Конфиденциальность и защита → "
    "Защита от вирусов и угроз → Управление настройками → Исключения → "
    f"Добавить исключение → Файл → {HOSTS_PATH}"
)

#: Предупреждение про Controlled Folder Access (запись падает с EBUSY).
CFA_HINT = (
    "Controlled Folder Access (контролируемый доступ к папкам) блокирует "
    "запись.\n\n"
    "Отключите его временно: Параметры → Конфиденциальность и защита → "
    "Защита от вирусов и угроз → Защита от программ-вымогателей → "
    "Контролируемый доступ к папкам."
)

#: Признаки блокировки записи антивирусом/защитой папок: по ним интерфейс
#: добавляет к ошибке :data:`CFA_HINT`.
BLOCKED_MARKERS = (
    "ebusy",
    "controlled folder",
    "access is denied",
    "отказано в доступе",
    "the process cannot access",
    "занят другим процессом",
)


# ---------------------------------------------------------------------------
#  Группы сервисов
# ---------------------------------------------------------------------------
#: Группы сервисов для чекбоксов: ключ -> (подпись, подстроки домена).
#: Порядок словаря — порядок чекбоксов в интерфейсе. Фильтрация идёт по
#: домену: секций в ``hosts-list.txt`` нет, а домены у сервисов узнаваемые.
SERVICE_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tiktok": (
        "TikTok",
        (
            "tiktok",
            "tiktokv",
            "tiktokcdn",
            "tiktokw",
            "ttwstatic",
            "ttcdn",
            "byteoversea",
            "ibytedtos",
            "ibyteimg",
            "oecstatic",
            "musical.ly",
            "muscdn",
            "tik-tokapi",
        ),
    ),
    "chatgpt": ("ChatGPT / OpenAI", ("chatgpt", "openai", "oaistatic", "oaiusercontent")),
    "spotify": ("Spotify", ("spotify",)),
    "deepl": ("DeepL", ("deepl",)),
    "claude": ("Claude / Anthropic", ("claude", "anthropic")),
    "gemini": ("Gemini / AI Studio", ("gemini", "aistudio", "generativelanguage", "makersuite")),
    "copilot": ("Copilot", ("copilot",)),
    "grok": ("Grok / xAI", ("grok", "x.ai")),
    "jetbrains": ("JetBrains", ("jetbrains",)),
    "discord": ("Discord", ("discord",)),
    "supercell": ("Supercell", ("supercell",)),
}

#: Группа для записей-блокировок (``0.0.0.0``). Ключ виртуальный: он не
#: хранится в :data:`SERVICE_GROUPS`, потому что определяется по адресу, а не
#: по домену.
BLOCKING_KEY = "blocking"
#: Подпись группы блокировок.
BLOCKING_LABEL = "Блокировка (0.0.0.0)"

#: Группа для всего, что не подошло ни под один сервис.
OTHER_KEY = "other"
#: Подпись группы «прочее».
OTHER_LABEL = "Прочее"


def service_of(entry: tuple[str, str]) -> str:
    """Ключ группы сервиса для пары ``(ip, domain)``.

    Порядок проверок важен: запись с адресом ``0.0.0.0`` — это блокировка,
    даже если домен узнаваем (``0.0.0.0 ads-api.tiktok.com`` не должен
    попасть в группу TikTok и «обход» случайно не включил бы блокировку).
    Не узнанный домен попадает в :data:`OTHER_KEY`.
    """
    try:
        address, domain = entry
    except (TypeError, ValueError):  # pragma: no cover — защита от чужого входа
        return OTHER_KEY

    if str(address).strip() in BLOCKING_ADDRESSES:
        return BLOCKING_KEY

    lowered = str(domain).strip().lower()
    for key, (_label, patterns) in SERVICE_GROUPS.items():
        if any(pattern in lowered for pattern in patterns):
            return key
    return OTHER_KEY


#: Порядок групп в интерфейсе: сервисы из :data:`SERVICE_GROUPS`, затем
#: «Блокировка» и «Прочее». Пары ``(ключ, подпись)`` — удобно для чекбоксов.
SERVICE_ORDER: tuple[tuple[str, str], ...] = (
    *((key, label) for key, (label, _patterns) in SERVICE_GROUPS.items()),
    (BLOCKING_KEY, BLOCKING_LABEL),
    (OTHER_KEY, OTHER_LABEL),
)

#: Сервисы, включённые в чекбоксах по умолчанию. Блокировка по умолчанию
#: **выключена**: она не «обходит», а запрещает доступ.
DEFAULT_SERVICES: tuple[str, ...] = ("tiktok", "chatgpt", "spotify", "deepl", "claude")


def service_label(key: str) -> str:
    """Подпись сервиса по его ключу (неизвестный ключ возвращается как есть)."""
    for candidate, label in SERVICE_ORDER:
        if candidate == key:
            return label
    return key


# ---------------------------------------------------------------------------
#  Разбор и фильтрация списка
# ---------------------------------------------------------------------------
def parse_hosts_list(text: str) -> list[tuple[str, str]]:
    """Разбирает текст ``hosts-list.txt`` в список пар ``(IP, домен)``.

    Игнорируются: пустые строки, комментарии (``#``), строки с числом
    полей не равным двум и строки, где первое поле не похоже на IP-адрес
    (домен без адреса пропускается — см. требования к формату файла).
    Дубликаты сохраняются: список показывается пользователю как есть, а
    повторы убирает :func:`format_hosts_lines` при записи.
    """
    entries: list[tuple[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue

        # Хвостовой комментарий после записи (``1.2.3.4 example.com # провайдер``).
        body = line.split(COMMENT_PREFIX, 1)[0].strip()
        if not body:
            continue

        parts = body.split()
        if len(parts) != 2:
            # Больше двух полей — неоднозначная строка, меньше — нет пары.
            continue

        address, domain = parts[0], parts[1]
        if not IP_RE.match(address):
            log.debug("Пропущена строка без IP-адреса: %r", line)
            continue
        if not DOMAIN_RE.match(domain):
            log.debug("Пропущена строка с некорректным доменом: %r", line)
            continue

        entries.append((address, domain))
    return entries


def filter_entries(
    entries,
    services: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """Оставляет только записи выбранных сервисов.

    ``services=None`` или пустой список означает «ничего не выбрано» и
    возвращает пустой список: применять весь файл целиком (включая блокировки)
    по умолчанию нельзя. Чтобы получить всё, передайте все ключи из
    :data:`SERVICE_ORDER`.
    """
    if not services:
        return []
    wanted = set(services)
    return [entry for entry in entries if service_of(entry) in wanted]


def deduplicate(entries) -> list[tuple[str, str]]:
    """Убирает повторы пар, сохраняя порядок первого появления."""
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for entry in entries:
        try:
            pair = (str(entry[0]).strip(), str(entry[1]).strip().lower())
        except (TypeError, IndexError, KeyError):  # pragma: no cover — чужой вход
            continue
        if pair[1] and pair not in seen:
            seen.add(pair)
            unique.append((pair[0], pair[1]))
    return unique


def format_hosts_lines(entries, comment: str = DEFAULT_COMMENT) -> list[str]:
    """Формирует строки блока для записи в ``hosts``.

    Каждая запись помечается хвостовым комментарием :data:`OWN_MARKER` —
    именно по нему приложение потом находит и удаляет свои строки
    (:meth:`HostsManager.remove_own_entries`), не трогая чужие. Пустой список
    записей даёт пустой блок (шапка без записей не пишется).
    """
    unique = deduplicate(entries)
    if not unique:
        return []

    lines = [f"# {comment}", f"# Удаляется кнопкой «Очистить мои записи» в Zapret GUI", ""]
    lines.extend(f"{address}\t{domain}\t# {OWN_MARKER}" for address, domain in unique)
    return lines


# ---------------------------------------------------------------------------
#  Вспомогательные функции путей
# ---------------------------------------------------------------------------
def _backup_path(path: Path) -> Path:
    """``hosts`` -> ``hosts.bak``."""
    return path.with_suffix(path.suffix + BACKUP_SUFFIX)


def _join_lines(lines: list[str]) -> str:
    """Собирает текст файла: по строке на запись, перевод строки — Windows.

    hosts на Windows читается с ``\\r\\n``; пустой список даёт пустой файл
    (без одинокой пустой строки).
    """
    if not lines:
        return ""
    return "".join(f"{line}\r\n" for line in lines)


def _is_own_line(line: str) -> bool:
    """Наша ли это строка (по маркеру в комментарии)."""
    lowered = line.lower()
    return any(marker.lower() in lowered for marker in OWN_MARKERS)


def _drop_own_block(lines: list[str]) -> list[str]:
    """Убирает строки, созданные приложением, сохраняя порядок остальных.

    Дополнительно схлопываются «осиротевшие» пустые строки в конце: после
    удаления блока файл не должен заканчиваться пачкой пустых строк.
    """
    kept = [line for line in lines if not _is_own_line(line)]
    while kept and not kept[-1].strip():
        kept.pop()
    return kept


# ---------------------------------------------------------------------------
#  Менеджер
# ---------------------------------------------------------------------------
class HostsManager:
    """Чтение, запись, бэкап и откат системного файла ``hosts``.

    Класс не знает про интерфейс: он возвращает ``(успех, сообщение)``, а
    сообщения пригодны для показа пользователю как есть.
    """

    def __init__(self, hosts_path: Path | None = None) -> None:
        """:param hosts_path: путь к файлу hosts (по умолчанию системный)."""
        self.hosts_path = Path(hosts_path) if hosts_path else HOSTS_PATH
        #: Предупреждение последнего чтения (кодировка, ошибка доступа).
        #: Интерфейс показывает его в шапке, не мешая работе.
        self.last_warning: str | None = None

    # ------------------------------------------------------------------
    #  Пути и состояние
    # ------------------------------------------------------------------
    @property
    def backup_path(self) -> Path:
        """Путь к резервной копии (``hosts.bak`` рядом с файлом)."""
        return _backup_path(self.hosts_path)

    def path_text(self) -> str:
        """Путь к файлу hosts строкой (для интерфейса и подсказок)."""
        return str(self.hosts_path)

    def exists(self) -> bool:
        """Есть ли файл hosts (обычно он есть; отсутствие — не ошибка)."""
        try:
            return self.hosts_path.is_file()
        except OSError:  # pragma: no cover — редкая ошибка доступа
            return False

    def has_backup(self) -> bool:
        """Есть ли резервная копия (от неё зависит кнопка «Откатить»)."""
        try:
            return self.backup_path.is_file()
        except OSError:  # pragma: no cover — редкая ошибка доступа
            return False

    def backup_age(self) -> str | None:
        """Время создания бэкапа в читаемом виде («23.09.2026 15:30»)."""
        try:
            if not self.backup_path.is_file():
                return None
            return datetime.fromtimestamp(self.backup_path.stat().st_mtime).strftime(
                BACKUP_TIME_FORMAT
            )
        except (OSError, OverflowError, ValueError) as exc:
            log.warning("Не удалось определить дату бэкапа %s: %s", self.backup_path, exc)
            return None

    # ------------------------------------------------------------------
    #  Чтение
    # ------------------------------------------------------------------
    def read_hosts(self) -> list[str]:
        """Читает файл hosts и возвращает строки без переводов строк.

        Отсутствующий файл — это пустой список (не ошибка): интерфейс должен
        показать пустой файл, а не падать. Кодировка: сначала UTF-8 (с BOM),
        затем cp1251 — hosts часто правят блокнотом в старой кодировке.
        """
        self.last_warning = None
        if not self.exists():
            return []

        try:
            data = self.hosts_path.read_bytes()
        except PermissionError:
            self.last_warning = (
                f"Нет прав на чтение {self.hosts_path}. "
                "Запустите приложение от имени администратора."
            )
            log.warning("Нет прав на чтение %s", self.hosts_path)
            return []
        except OSError as exc:
            self.last_warning = f"Не удалось прочитать hosts: {exc}"
            log.warning("Не удалось прочитать %s: %s", self.hosts_path, exc)
            return []

        return self._decode(data).splitlines()

    def _decode(self, data: bytes) -> str:
        """Декодирует содержимое hosts: UTF-8 (с BOM), иначе cp1251."""
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            if encoding != "utf-8-sig":
                self.last_warning = (
                    f"hosts прочитан в кодировке {encoding} — файл не в UTF-8. "
                    "После записи он станет UTF-8."
                )
                log.warning("hosts прочитан как %s", encoding)
            return text

        self.last_warning = "Не удалось определить кодировку hosts: часть символов заменена."
        log.warning("hosts: не подошли ни UTF-8, ни cp1251")
        return data.decode("utf-8", errors="replace")

    def read_entries(self) -> list[tuple[str, str]]:
        """Пары ``(IP, домен)`` из файла hosts (за вычетом комментариев)."""
        return parse_hosts_list("\n".join(self.read_hosts()))

    def count_own_entries(self) -> int:
        """Сколько строк в hosts создано приложением."""
        return sum(1 for line in self.read_hosts() if _is_own_line(line))

    def own_entries_present(self, expected: int = 1) -> bool:
        """Остались ли в hosts наши записи (``expected`` — сколько ждём).

        Нужна для проверки «Windows Defender удалил записи»: если после
        применения записей в файле нет — показываем предупреждение.
        """
        if expected <= 0:
            return False
        return self.count_own_entries() >= expected

    # ------------------------------------------------------------------
    #  Запись
    # ------------------------------------------------------------------
    def write_hosts(self, lines) -> tuple[bool, str]:
        """Записывает строки в hosts, создав бэкап перед первой правкой.

        Существующие чужие строки сохраняются: наши строки из файла
        вычищаются, блок приложения дописывается в конец. Атомарность —
        через временный файл и ``replace``.

        :return: ``(успех, сообщение)`` — сообщение пригодно для показа.
        """
        prepared = [str(line).rstrip("\r\n") for line in lines]

        try:
            self.hosts_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Не удалось создать папку %s: %s", self.hosts_path.parent, exc)
            return False, f"Не удалось подготовить папку для hosts:\n{exc}"

        if not is_admin():
            # Проверяем права заранее: так пользователь получает понятную
            # подсказку, а не «отказано в доступе» от системы.
            return False, ADMIN_HINT

        current = self.read_hosts()
        base = _drop_own_block(current)
        # Пустая строка-разделитель: блок приложения не должен «прилипать»
        # к последней чужой записи.
        merged = [*base, ""] if base else []
        merged.extend(prepared)

        try:
            # Бэкап — только если его ещё нет: «Откатить» должен возвращать к
            # состоянию до первой правки, а не к предыдущему «Применить».
            if self.exists() and not self.has_backup():
                shutil.copy2(self.hosts_path, self.backup_path)
                log.info("Создан бэкап hosts: %s", self.backup_path)

            self._atomic_write(merged)
        except PermissionError:
            log.warning("Нет прав на запись в %s", self.hosts_path)
            return False, (
                f"Нет прав на запись:\n{self.hosts_path}\n\n{ADMIN_HINT}"
            )
        except OSError as exc:
            log.error("Не удалось записать %s: %s", self.hosts_path, exc)
            return False, self._write_error_message(exc)

        written = len(prepared)
        log.info("hosts обновлён: записано строк %d", written)
        if not written and not base:
            return True, "Файл hosts очищен: записей нет."
        return True, f"Записано строк: {written}"

    def _atomic_write(self, lines: list[str]) -> None:
        """Пишет строки во временный файл рядом и переименовывает в hosts.

        Файл создаётся в той же папке: ``replace`` в пределах одного тома
        атомарен, а перенос из ``%TEMP%`` на другой том — уже копирование.
        Права и владелец при ``replace`` берутся у нового файла, поэтому
        права копируются с исходного hosts (если он есть).
        """
        handle, temp_name = tempfile.mkstemp(
            prefix=self.hosts_path.name + ".",
            suffix=TEMP_SUFFIX,
            dir=str(self.hosts_path.parent),
        )
        os.close(handle)
        temp = Path(temp_name)
        try:
            temp.write_bytes(_join_lines(lines).encode("utf-8"))
            if self.exists():
                try:
                    shutil.copymode(self.hosts_path, temp)
                except OSError as exc:  # не критично: запись важнее прав
                    log.debug("Не удалось скопировать права hosts: %s", exc)
            temp.replace(self.hosts_path)
        except BaseException:
            # Обязательно убираем временный файл: иначе в системной папке
            # остаётся мусор, который помешает следующей попытке.
            try:
                temp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — уборка не должна мешать ошибке
                pass
            raise

    @staticmethod
    def _write_error_message(exc: OSError) -> str:
        """Понятное сообщение об ошибке записи (с учётом блокировки защиты)."""
        text = str(exc)
        lowered = text.lower()
        if any(marker in lowered for marker in BLOCKED_MARKERS):
            return (
                f"Не удалось записать hosts:\n{text}\n\n{CFA_HINT}"
            )
        return (
            f"Не удалось записать hosts:\n{text}\n\n"
            "Проверьте, не блокирует ли запись антивирус "
            "(см. предупреждение про Windows Defender в разделе)."
        )

    # ------------------------------------------------------------------
    #  Бэкап и откат
    # ------------------------------------------------------------------
    def rollback(self) -> tuple[bool, str]:
        """Восстанавливает hosts из ``.bak`` (текущее содержимое теряется)."""
        if not self.has_backup():
            return False, (
                f"Резервная копия не найдена:\n{self.backup_path}\n\n"
                "Копия создаётся автоматически при первом применении записей."
            )
        if not is_admin():
            return False, ADMIN_HINT

        try:
            # Пишем через временный файл: прерывание отката не оставит
            # половину старого hosts.
            data = self.backup_path.read_bytes()
            self._atomic_write_bytes(data)
        except PermissionError:
            log.warning("Нет прав на восстановление %s", self.hosts_path)
            return False, f"Нет прав на запись:\n{self.hosts_path}\n\n{ADMIN_HINT}"
        except OSError as exc:
            log.error("Не удалось восстановить hosts из бэкапа: %s", exc)
            return False, f"Не удалось восстановить hosts из бэкапа:\n{exc}"

        log.info("hosts восстановлен из бэкапа %s", self.backup_path)
        age = self.backup_age()
        hint = f" (копия от {age})" if age else ""
        return True, f"Файл hosts восстановлен из бэкапа{hint}."

    def _atomic_write_bytes(self, data: bytes) -> None:
        """Атомарная запись готовых байт (используется откатом)."""
        handle, temp_name = tempfile.mkstemp(
            prefix=self.hosts_path.name + ".",
            suffix=TEMP_SUFFIX,
            dir=str(self.hosts_path.parent),
        )
        os.close(handle)
        temp = Path(temp_name)
        try:
            temp.write_bytes(data)
            if self.exists():
                try:
                    shutil.copymode(self.hosts_path, temp)
                except OSError as exc:  # не критично
                    log.debug("Не удалось скопировать права hosts: %s", exc)
            temp.replace(self.hosts_path)
        except BaseException:
            try:
                temp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — уборка не должна мешать ошибке
                pass
            raise

    def create_backup(self) -> tuple[bool, str]:
        """Перезаписывает ``.bak`` текущим содержимым hosts.

        Нужна, когда пользователь уверен в текущем состоянии файла и хочет
        сделать его новой точкой отката.
        """
        if not self.exists():
            return False, f"Файл hosts не найден:\n{self.hosts_path}"
        if not is_admin():
            return False, ADMIN_HINT

        try:
            shutil.copy2(self.hosts_path, self.backup_path)
        except PermissionError:
            return False, f"Нет прав на запись:\n{self.backup_path}\n\n{ADMIN_HINT}"
        except OSError as exc:
            log.error("Не удалось создать бэкап hosts: %s", exc)
            return False, f"Не удалось создать бэкап hosts:\n{exc}"

        log.info("Бэкап hosts обновлён: %s", self.backup_path)
        return True, "Резервная копия hosts обновлена."

    # ------------------------------------------------------------------
    #  Удаление своих записей
    # ------------------------------------------------------------------
    def remove_own_entries(self) -> tuple[bool, str]:
        """Удаляет из hosts только строки, созданные приложением.

        Чужие записи и комментарии сохраняются в исходном порядке. Если
        наших строк нет, файл не перезаписывается.
        """
        if not is_admin():
            return False, ADMIN_HINT
        if not self.exists():
            return False, f"Файл hosts не найден:\n{self.hosts_path}"

        current = self.read_hosts()
        removed = sum(1 for line in current if _is_own_line(line))
        if not removed:
            return True, "Записей Zapret GUI в hosts нет — файл не изменялся."

        kept = _drop_own_block(current)
        try:
            self._atomic_write(kept)
        except PermissionError:
            log.warning("Нет прав на запись в %s", self.hosts_path)
            return False, f"Нет прав на запись:\n{self.hosts_path}\n\n{ADMIN_HINT}"
        except OSError as exc:
            log.error("Не удалось очистить записи hosts: %s", exc)
            return False, self._write_error_message(exc)

        log.info("Из hosts удалено строк Zapret GUI: %d", removed)
        return True, f"Удалено строк: {removed}"


# ---------------------------------------------------------------------------
#  Скачивание списка и локальная копия
# ---------------------------------------------------------------------------
def cache_path() -> Path:
    """Путь к локальной копии списка: ``%APPDATA%\\ZapretGUI\\hosts-list.txt``.

    Копия нужна, когда скачать список не удалось (нет сети, GitHub
    недоступен): в этом случае страница берёт последний удачный вариант.
    """
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "ZapretGUI" / "hosts-list.txt"


def cache_age() -> str | None:
    """Дата локальной копии списка в читаемом виде (``None`` — копии нет)."""
    path = cache_path()
    try:
        if not path.is_file():
            return None
        return datetime.fromtimestamp(path.stat().st_mtime).strftime(BACKUP_TIME_FORMAT)
    except (OSError, OverflowError, ValueError) as exc:
        log.warning("Не удалось определить дату копии списка: %s", exc)
        return None


def read_cached_list() -> str:
    """Читает локальную копию списка (пустая строка — копии нет)."""
    path = cache_path()
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Не удалось прочитать копию списка %s: %s", path, exc)
        return ""


def download_hosts_list(
    url: str | None = None, timeout: int = DOWNLOAD_TIMEOUT
) -> tuple[bool, str, str]:
    """Скачивает ``hosts-list.txt`` и сохраняет локальную копию.

    Адреса пробуются по очереди (см. :data:`HOSTS_LIST_URLS`): первый —
    основной, остальные — запасные. Удачный текст кладётся в
    :func:`cache_path`, чтобы страница могла работать без сети.

    :param url: конкретный адрес; ``None`` — перебор :data:`HOSTS_LIST_URLS`.
    :param timeout: таймаут одного запроса, секунды.
    :return: ``(успех, текст, ошибка)`` — при успехе ошибка пустая, при
        неудаче текст пустой.
    """
    try:
        import requests
    except ImportError as exc:  # pragma: no cover — requests есть в requirements
        return False, "", f"Не найден модуль requests: {exc}"

    urls = (url,) if url else HOSTS_LIST_URLS
    errors: list[str] = []

    for candidate in urls:
        try:
            response = requests.get(
                candidate, timeout=timeout, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
            text = response.text
        except Exception as exc:  # noqa: BLE001 — сеть отвечает чем угодно
            log.warning("Не удалось скачать список %s: %s", candidate, exc)
            errors.append(f"{candidate}\n{exc}")
            continue

        if not text.strip():
            errors.append(f"{candidate}\nСервер вернул пустой файл.")
            continue

        _save_cache(text)
        log.info("Список hosts скачан: %s (%d символов)", candidate, len(text))
        return True, text, ""

    return False, "", "Не удалось скачать список.\n\n" + "\n\n".join(errors)


def _save_cache(text: str) -> None:
    """Сохраняет скачанный список в ``%APPDATA%`` (ошибка не критична)."""
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        log.warning("Не удалось сохранить копию списка %s: %s", path, exc)
