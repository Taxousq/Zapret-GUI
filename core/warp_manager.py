"""Управление Cloudflare WARP через его консольный клиент ``warp-cli``.

Раздел «Обход (WARP)» ничего не устанавливает и не реализует VPN сам: он
управляет **уже установленным** Cloudflare WARP, вызывая ``warp-cli``. WARP
поднимает туннель (в режиме ``Warp`` через него идут и трафик, и DNS), а
приложение лишь показывает состояние и переключает его::

    warp-cli --accept-tos status      -> Connected / Disconnected + причина
    warp-cli --accept-tos connect     -> подключить
    warp-cli --accept-tos disconnect  -> отключить
    warp-cli --accept-tos mode X      -> режим warp / doh / off
    warp-cli --accept-tos settings    -> текущие настройки (в том числе Mode)

Несколько важных деталей:

* **Команда смены режима называется ``mode``, а не ``set-mode``**: в клиенте
  2026.7.1376.0 прежняя ``set-mode`` убрана (``error: unrecognized subcommand
  'set-mode'``). Варианты команды перебираются
  (:data:`MODE_COMMAND_CANDIDATES`), а если клиент не знает ни одного — смена
  режима считается неподдерживаемой и раздел блокирует селектор режимов вместо
  падения;
* **``--accept-tos`` добавляется всегда**: без него клиент при первом запуске
  останавливается на запросе принятия условий и команда «зависает» до таймаута;
* **``CREATE_NO_WINDOW``** (см. :mod:`core.win_utils`) обязателен: без него на
  каждую команду мигает чёрное консольное окно;
* вывод клиента разбирается регулярными выражениями
  (:func:`parse_status`, :func:`parse_mode`), а не «сырым» сравнением строк:
  формат ответа отличается между версиями WARP.

Отдельная группа методов управляет **исключениями Split Tunnel** (карточка
«Исключения (Split Tunnel)» на странице)::

    warp-cli --accept-tos tunnel ip list             -> список исключений по IP
    warp-cli --accept-tos tunnel ip add <ADDRESS>    -> одиночный адрес
    warp-cli --accept-tos tunnel ip add-range <CIDR> -> диапазон
    warp-cli --accept-tos tunnel ip remove[-range] X -> удалить
    warp-cli --accept-tos tunnel host add <DOMAIN>   -> домен (можно ``*.ru``)

Отдельно есть **готовые наборы доменов** (:data:`PRESET_DOMAINS`): YouTube и
Discord обходит запрет (Flowseal), и если их трафик перехватит ещё и WARP, два
обхода начинают конфликтовать. Метод :meth:`WarpManager.add_preset_exclusions`
добавляет домены набора в исключения, а
:meth:`WarpManager.remove_preset_exclusions` убирает **только** домены набора —
чужие записи не трогаются.

WARP в режиме Exclude не трогает трафик к этим адресам — они идут напрямую,
а весь остальной трафик уходит через Cloudflare. Несколько
деталей, выясненных на клиенте 2026.7.1376.0:

* ``add`` принимает **только адрес без маски**, а ``add-range`` — **только
  диапазон** с маской: ``add 198.51.100.0/24`` и ``add-range 198.51.100.1``
  отвечают ``invalid IP address syntax``. Поэтому запись с ``/`` уходит в
  ``add-range``, а без него — в ``add``;
* записи, добавленные через CLI, помечены в выводе ``list`` как
  ``(CLI exclude)``: по этой пометке видно, что добавили мы, а что WARP
  (локальные сети и адреса Cloudflare). Удаляются только «свои» записи;
* в старых версиях клиента команда может ответить ``Not yet implemented`` —
  тогда :meth:`WarpManager.tunnel_supported` возвращает ``False``, а страница
  блокирует кнопки и показывает подсказку;
* поддержка исключений определяется **по справке** клиента
  (``warp-cli tunnel --help``), а не пробным добавлением адреса: проверка
  ничего не меняет в конфигурации WARP (см. :func:`parse_help_commands`);
* ``psutil`` нужен только кнопке «Добавить текущие соединения» и
  импортируется **лениво** внутри метода: без него приложение запускается и
  работает, а страница показывает подсказку об установке пакета
  (:data:`PSUTIL_MISSING_HINT`). В ``requirements.txt`` он есть, но установку
  выполняет пользователь;
* клиент принимает записи **по одной**, поэтому массовое добавление идёт
  циклом (:meth:`WarpManager.add_tunnel_entries`) с прогрессом и остановкой.

Все методы не бросают исключений наружу: ошибки возвращаются текстом, чтобы
страница могла показать их в статусе и не упасть (WARP может быть не
установлен, служба может быть остановлена, прав может не хватать).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Callable, Iterable, Sequence

import requests

from config import (
    CHECK_USER_AGENT,
    DISCORD_EXCLUDE_DOMAINS,
    WARP_ACTIVE_PORTS,
    WARP_BYPASS_DOMAIN_URL,
    WARP_BYPASS_IP_URL,
    WARP_BYPASS_TIMEOUT,
    WARP_CLI_NAME,
    WARP_CLI_PATH,
    WARP_COMMAND_TIMEOUT,
    WARP_EXCLUDE_ERROR_LIMIT,
    WARP_MODES,
    YOUTUBE_EXCLUDE_DOMAINS,
)
from core.win_utils import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

#: Состояния WARP, с которыми работает интерфейс.
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_CONNECTING = "connecting"
STATE_UNKNOWN = "unknown"

#: Состояние -> подпись в интерфейсе.
STATE_LABELS: dict[str, str] = {
    STATE_CONNECTED: "Подключён",
    STATE_DISCONNECTED: "Отключён",
    STATE_CONNECTING: "Подключение...",
    STATE_UNKNOWN: "Неизвестно",
}

#: Что показать, если клиент не найден.
NOT_INSTALLED_HINT = (
    "Cloudflare WARP не найден. Установите его с https://cloudflare.com/warp — "
    "раздел управляет уже установленным клиентом через warp-cli."
)

#: Допустимые режимы (ключи команды ``warp-cli mode``).
VALID_MODES: frozenset[str] = frozenset(key for key, _label in WARP_MODES)

#: Режим «выключено»: в новых версиях клиента значения ``off`` у команды
#: ``mode`` уже нет (см. ``warp-cli mode --help``), и его заменяет отключение.
MODE_OFF = "off"

#: Варианты команды смены режима по версиям клиента, в порядке проверки.
#: Актуальная версия (2026.7.1376.0) понимает ``mode``; остальные оставлены
#: запасными — они пробуются, только если клиент не знает предыдущей.
MODE_COMMAND_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("mode",),
    ("settings", "mode"),
    ("tunnel", "mode"),
)

#: Признаки того, что клиент не знает саму подкоманду: так отвечает версия,
#: в которой команду переименовали (``unrecognized subcommand 'set-mode'``).
_UNSUPPORTED_COMMAND_MARKERS: tuple[str, ...] = (
    "unrecognized subcommand",
    "unknown subcommand",
    "invalid subcommand",
    "unrecognized command",
    "no such subcommand",
    "not a recognized subcommand",
)

#: Признаки того, что клиент не принимает переданное значение режима
#: (``error: invalid value 'off' for '<MODE>'``).
_UNSUPPORTED_VALUE_MARKERS: tuple[str, ...] = (
    "invalid value",
    "possible values",
    "unknown mode",
)

#: Что показать, если клиент не умеет менять режим.
MODE_UNSUPPORTED_HINT = (
    "Смена режима не поддерживается этой версией warp-cli. "
    "Используйте приложение Cloudflare WARP для смены режима."
)

#: Чем заменяется режим ``off``, которого нет у команды ``mode``.
MODE_OFF_EQUIVALENT_MESSAGE = (
    "Режим «Выключено»: эта версия warp-cli не поддерживает значение off — "
    "WARP отключён."
)

#: ``Status update: Connected`` — основной формат ответа ``warp-cli status``.
_STATUS_RE = re.compile(r"Status update:\s*(?P<state>[A-Za-z]+)")
#: Запасной вариант: в старых версиях клиента строка идёт без префикса.
_STATUS_KEYWORD_RE = re.compile(
    r"\b(?P<state>connected|disconnected|connecting|reconnecting)\b", re.IGNORECASE
)
#: ``Reason: Manual Disconnection`` — причина текущего состояния.
_REASON_RE = re.compile(r"^\s*Reason:\s*(?P<reason>.+?)\s*$", re.MULTILINE)
#: ``(user set)\tMode: Warp`` — текущий режим в выводе ``settings``.
_MODE_RE = re.compile(r"\bMode:\s*(?P<mode>[A-Za-z]+)")
#: ``warp-cli 2026.7.1376.0`` — версия клиента.
_VERSION_RE = re.compile(r"(?P<version>\d+(?:\.\d+){2,})")

#: Имена состояний клиента -> состояния интерфейса.
_STATE_BY_NAME: dict[str, str] = {
    "connected": STATE_CONNECTED,
    "disconnected": STATE_DISCONNECTED,
    "connecting": STATE_CONNECTING,
    "reconnecting": STATE_CONNECTING,
}

#: Подписи режимов: ключ ``warp-cli`` -> подпись для интерфейса.
MODE_LABELS: dict[str, str] = {key: label for key, label in WARP_MODES}


# ---------------------------------------------------------------------------
#  Исключения Split Tunnel: константы
# ---------------------------------------------------------------------------
#: Тип записи в исключениях: адрес/диапазон или домен.
KIND_IP = "ip"
KIND_HOST = "host"
#: Тип записи -> подпись для интерфейса.
KIND_LABELS: dict[str, str] = {KIND_IP: "IP", KIND_HOST: "домен"}
#: Тип записи -> подкоманда warp-cli (``tunnel ip ...`` / ``tunnel host ...``).
_TUNNEL_SUBCOMMANDS: dict[str, str] = {KIND_IP: "ip", KIND_HOST: "host"}

#: Подкоманда, наличие которой в справке ``warp-cli tunnel --help`` означает,
#: что клиент умеет управлять исключениями Split Tunnel. Поддержка выясняется
#: только по справке: пробное ``tunnel ip add`` меняло бы конфигурацию WARP,
#: а справка — команда исключительно для чтения.
TUNNEL_HELP_COMMAND = "ip"
#: Подкоманды диапазонов в справке ``warp-cli tunnel ip --help``: без них
#: записи вида ``10.0.0.0/8`` добавить нельзя (см. tunnel_range_supported).
TUNNEL_HELP_RANGE_COMMANDS: tuple[str, ...] = ("add-range", "remove-range")
#: Подкоманды в справке ``warp-cli tunnel host --help``: без них домены в
#: исключения не добавить (см. tunnel_host_supported).
TUNNEL_HELP_HOST_COMMANDS: tuple[str, ...] = ("add", "remove")
#: Заголовок раздела справки warp-cli со списком подкоманд (вывод clap).
HELP_COMMANDS_HEADER = "commands"

#: Помета, которой клиент отмечает записи, добавленные через CLI. Только такие
#: записи приложение считает «своими» и удаляет при очистке.
CLI_ENTRY_MARKER = "cli exclude"

#: Признаки того, что управление исключениями в этой версии не реализовано.
_UNSUPPORTED_TUNNEL_MARKERS: tuple[str, ...] = (
    *_UNSUPPORTED_COMMAND_MARKERS,
    "not yet implemented",
    "not implemented",
    "unimplemented",
    "not supported",
)

#: Что показать, если клиент не умеет управлять исключениями.
TUNNEL_UNSUPPORTED_HINT = (
    "Ваша версия warp-cli не поддерживает управление исключениями через CLI. "
    "Обновите клиент или используйте дашборд Zero Trust."
)

#: Что показать, если клиент не умеет управлять **доменами** в исключениях
#: (нет ``tunnel host add``).
HOST_UNSUPPORTED_HINT = (
    "Эта версия warp-cli не поддерживает домены в исключениях (нет команды "
    "tunnel host add). Добавьте домены в приложении Cloudflare WARP или "
    "обновите клиент."
)

# ---------------------------------------------------------------------------
#  Исключения Split Tunnel: готовые наборы доменов (YouTube, Discord)
# ---------------------------------------------------------------------------
# YouTube и Discord обходит запрет (Flowseal). Если тот же трафик завернёт в
# себя ещё и WARP (режим Exclude), два обхода начнут мешать друг другу:
# соединения будут рваться и переподключаться. Домены этих сервисов
# добавляются в исключения WARP — тогда его туннель их не трогает.

#: Ключ набора доменов: YouTube.
PRESET_YOUTUBE = "youtube"
#: Ключ набора доменов: Discord.
PRESET_DISCORD = "discord"
#: Ключ набора -> подпись для интерфейса и сообщений.
PRESET_LABELS: dict[str, str] = {
    PRESET_YOUTUBE: "YouTube",
    PRESET_DISCORD: "Discord",
}
#: Ключ набора -> домены (значения из :mod:`config`).
PRESET_DOMAINS: dict[str, tuple[str, ...]] = {
    PRESET_YOUTUBE: YOUTUBE_EXCLUDE_DOMAINS,
    PRESET_DISCORD: DISCORD_EXCLUDE_DOMAINS,
}


def preset_domains(preset: str) -> tuple[str, ...]:
    """Домены набора исключений; пустой кортеж — набор неизвестен.

    :param preset: :data:`PRESET_YOUTUBE` или :data:`PRESET_DISCORD`
        (регистр и пробелы вокруг не важны).
    """
    return PRESET_DOMAINS.get((preset or "").strip().lower(), ())


def preset_label(preset: str) -> str:
    """Подпись набора для интерфейса («YouTube» / «Discord»)."""
    key = (preset or "").strip().lower()
    return PRESET_LABELS.get(key, key)

#: Что показать, если нет psutil — без него не собрать активные соединения.
PSUTIL_MISSING_HINT = (
    "Модуль psutil не установлен, собрать активные соединения нельзя. "
    "Установите его командой: pip install psutil"
)

#: Что показать, если Windows не дала прочитать таблицу соединений.
PSUTIL_DENIED_HINT = (
    "Windows не разрешила прочитать список соединений (нужны права "
    "администратора). Запустите приложение от имени администратора."
)

#: ``  example.com (CLI exclude)`` — помета в конце строки вывода ``list``.
_ENTRY_MARKER_RE = re.compile(r"\s*\((?P<marker>[^)]*)\)\s*$")


# ---------------------------------------------------------------------------
#  Исключения Split Tunnel: разбор и подготовка данных
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TunnelEntry:
    """Одна запись в исключениях Split Tunnel."""

    #: Значение: адрес, диапазон (``10.0.0.0/8``) или домен (``*.ru``).
    value: str
    #: Запись добавлена через CLI (``(CLI exclude)`` в выводе ``list``), то
    #: есть её добавили мы. Записи самого WARP очистка не удаляет.
    cli: bool = False


@dataclass(frozen=True)
class ActiveConnections:
    """Активные удалённые адреса, собранные через ``psutil``."""

    #: Уникальные публичные IPv4-адреса удалённой стороны (порты 80 и 443).
    ips: tuple[str, ...] = ()
    #: Сколько соединений просмотрела система (для сообщения «из N»).
    total: int = 0
    #: Сколько соединений было в состоянии ``ESTABLISHED``.
    established: int = 0
    #: Сколько установленных соединений шло на порты 80/443.
    web: int = 0
    #: Предупреждение, если собрать адреса не удалось (пусто — всё хорошо).
    warning: str = ""
    #: Найден ли модуль ``psutil``. ``False`` — модуль не установлен, и это
    #: единственный случай, когда :meth:`WarpManager.active_remote_ips`
    #: возвращает ``None`` (сигнал «нужна установка пакета»).
    psutil_available: bool = True

    @property
    def ok(self) -> bool:
        """Удалось ли прочитать таблицу соединений."""
        return not self.warning


@dataclass(frozen=True)
class TunnelAddResult:
    """Итог массового добавления исключений."""

    #: Сколько записей было предложено к добавлению.
    total: int = 0
    #: Добавлено.
    added: int = 0
    #: Уже было в исключениях (пропущено).
    skipped: int = 0
    #: Не добавлено из-за неподдерживаемых диапазонов.
    unsupported: int = 0
    #: Сколько записей клиент добавить не смог.
    failed: int = 0
    #: Первые ошибки: ``(значение, сообщение)`` — чтобы показать примеры.
    first_errors: tuple[tuple[str, str], ...] = ()
    #: Операция прервана пользователем.
    stopped: bool = False
    #: Общая ошибка (список исключений недоступен и т.п.); пусто — её нет.
    error: str = ""

    @property
    def ok(self) -> bool:
        """Не было ли общей ошибки (ошибки отдельных записей — не в счёт)."""
        return not self.error

    @property
    def changed(self) -> bool:
        """Добавили ли хоть что-нибудь."""
        return self.added > 0

    def summary(self, label: str = "") -> str:
        """Короткая строка итога для статуса: «добавлено: 500, уже было: 12»."""
        parts = [f"добавлено: {self.added}"]
        if self.skipped:
            parts.append(f"уже было: {self.skipped}")
        if self.unsupported:
            parts.append(f"пропущено диапазонов: {self.unsupported}")
        if self.failed:
            parts.append(f"ошибок: {self.failed}")
        if self.stopped:
            parts.append("остановлено")
        text = ", ".join(parts)
        return f"{label}: {text}" if label else text

    def details(self) -> str:
        """Итог с примерами ошибок — для диалога и журнала."""
        text = self.summary()
        for value, message in self.first_errors:
            text += f"\n• {value} — {message}"
        if self.failed > len(self.first_errors):
            text += f"\n…и ещё ошибок: {self.failed - len(self.first_errors)}"
        return text


def parse_tunnel_list(output: str) -> list[TunnelEntry]:
    """Разбирает вывод ``warp-cli tunnel ip list`` / ``tunnel host list``.

    Формат ответа::

        Excluded routes:
          10.0.0.0/8
          192.0.2.1/32 (CLI exclude)

    Заголовок (строка с двоеточием в конце) пропускается, помета
    ``(CLI exclude)`` превращается в признак :attr:`TunnelEntry.cli`.
    """
    entries: list[TunnelEntry] = []
    for line in (output or "").splitlines():
        text = line.strip()
        if not text or text.endswith(":"):
            continue
        cli = False
        match = _ENTRY_MARKER_RE.search(text)
        if match is not None:
            cli = CLI_ENTRY_MARKER in match.group("marker").strip().lower()
            text = text[: match.start()].strip()
        if text:
            entries.append(TunnelEntry(text, cli))
    return entries


def parse_warpbypass_list(text: str) -> list[str]:
    """Разбирает файл списка WarpBypass (``ru_bypass_ip.txt`` и подобные).

    Строки с ``#`` — комментарии, пустые строки пропускаются, повторы
    (без учёта регистра) убираются: список может содержать их после ручной
    правки. Порядок строк сохраняется.
    """
    values: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip().lstrip("\ufeff")
        parts = line.split()
        if not parts:
            continue
        value = parts[0]
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values


def dedup_key(kind: str, value: str) -> str:
    """Ключ для сравнения записей: ``192.0.2.1`` и ``192.0.2.1/32`` — одно.

    Клиент показывает одиночный адрес как ``/32`` (и ``/128`` для IPv6),
    поэтому такие записи сравниваются по самому адресу. Домены сравниваются
    без учёта регистра и завершающей точки.
    """
    text = (value or "").strip()
    if kind == KIND_HOST:
        return text.lower().rstrip(".")
    address, _, prefix = text.partition("/")
    if prefix in ("32", "128"):
        return address
    return text


def guess_kind(value: str) -> str:
    """IP это или домен: ручное добавление обходится одной строкой ввода."""
    text = (value or "").strip()
    try:
        ipaddress.ip_network(text, strict=False)
    except ValueError:
        return KIND_HOST
    return KIND_IP


def is_public_ipv4(address: str) -> bool:
    """Публичный ли это IPv4-адрес (локальные и служебные не подходят).

    Отбрасываются петля, частные сети, link-local, multicast, ``0.0.0.0`` и
    зарезервированные диапазоны, а также любые IPv6-адреса: ``tunnel ip add``
    принимает и их, но в исключения для российских сайтов они не нужны.
    """
    text = (address or "").strip().split("%")[0]
    if not text:
        return False
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _address_parts(remote: object) -> tuple[str, int]:
    """Достаёт ``(ip, port)`` из адреса соединения ``psutil``.

    ``psutil`` отдаёт именованные кортежи (``addr(ip=..., port=...)``), но на
    всякий случай поддерживаются и обычные кортежи.
    """
    address = getattr(remote, "ip", None)
    port = getattr(remote, "port", None)
    if address is None and isinstance(remote, (tuple, list)) and remote:
        address = remote[0]
    if port is None and isinstance(remote, (tuple, list)) and len(remote) > 1:
        port = remote[1]
    try:
        number = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0
    return str(address or ""), number


def collect_remote_ips(
    connections: Iterable[object] | None,
    ports: Sequence[int] = WARP_ACTIVE_PORTS,
) -> list[str]:
    """Уникальные публичные IPv4-адреса удалённой стороны соединений.

    Берутся только установленные соединения (:attr:`status` ==
    ``ESTABLISHED``) на порты :data:`config.WARP_ACTIVE_PORTS` — это обычный
    веб-трафик. Результат отсортирован по адресу, чтобы список исключений не
    «прыгал» между запусками.
    """
    found: dict[str, None] = {}
    for connection in connections or ():
        if str(getattr(connection, "status", "") or "").upper() != "ESTABLISHED":
            continue
        address, port = _address_parts(getattr(connection, "raddr", None))
        if ports and port not in ports:
            continue
        if not is_public_ipv4(address):
            continue
        found.setdefault(address, None)
    return sorted(found, key=ipaddress.ip_address)


def connection_stats(
    connections: Iterable[object] | None,
    ports: Sequence[int] = WARP_ACTIVE_PORTS,
) -> tuple[int, int]:
    """Считает ``(установленных соединений, из них на порты 80/443)``.

    Нужно, чтобы страница отличала «соединений нет» от «есть только локальные
    адреса» и объясняла, почему добавлять нечего.
    """
    established = 0
    web = 0
    for connection in connections or ():
        if str(getattr(connection, "status", "") or "").upper() != "ESTABLISHED":
            continue
        established += 1
        _, port = _address_parts(getattr(connection, "raddr", None))
        if port in ports:
            web += 1
    return established, web


def is_unsupported_tunnel(output: str) -> bool:
    """Не поддерживает ли клиент управление исключениями.

    Так отвечает версия, в которой команда только объявлена: ``Not yet
    implemented``, либо клиент, который подкоманды не знает вовсе.
    """
    return _mentions_any(output, _UNSUPPORTED_TUNNEL_MARKERS)


def parse_help_commands(output: str) -> set[str]:
    """Имена подкоманд из блока ``Commands:`` в справке ``warp-cli``.

    Справка клиента — это вывод clap:

    .. code-block:: text

        Commands:
          host            Configure split tunnel hosts
          ip              Configure split tunnel IPs

    Разбирается только этот блок: в описаниях и примерах те же слова
    встречаются как обычный текст, и поиск подстроки дал бы ложное
    «поддержано». Так поддержка исключений выясняется **без изменения**
    конфигурации WARP — в отличие от пробного ``tunnel ip add``.

    :param output: вывод ``warp-cli <команда> --help``.
    :return: имена подкоманд; пустое множество — блока ``Commands:`` нет.
    """
    commands: set[str] = set()
    in_commands = False
    for line in (output or "").splitlines():
        text = line.strip()
        if not text:
            in_commands = False
            continue
        if text.endswith(":"):
            # Заголовок раздела: «Commands:» включает разбор, «Options:» и
            # любой другой — выключает.
            in_commands = text[:-1].strip().lower() == HELP_COMMANDS_HEADER
            continue
        if in_commands:
            commands.add(text.split()[0])
    return commands


# ---------------------------------------------------------------------------
#  Разбор вывода warp-cli
# ---------------------------------------------------------------------------
def parse_status(output: str) -> tuple[str, str]:
    """Разбирает вывод ``warp-cli status``: ``(состояние, причина)``.

    :param output: stdout/stderr команды ``status``.
    :return: состояние (:data:`STATE_CONNECTED`, :data:`STATE_DISCONNECTED`,
        :data:`STATE_CONNECTING` или :data:`STATE_UNKNOWN`) и причину —
        пустая строка, если клиент её не сообщил.
    """
    text = output or ""
    match = _STATUS_RE.search(text) or _STATUS_KEYWORD_RE.search(text)
    state = STATE_UNKNOWN
    if match is not None:
        state = _STATE_BY_NAME.get(match.group("state").strip().lower(), STATE_UNKNOWN)

    reason_match = _REASON_RE.search(text)
    reason = reason_match.group("reason").strip() if reason_match else ""
    return state, reason


def parse_mode(output: str) -> str:
    """Разбирает вывод ``warp-cli settings`` и возвращает режим в нижнем регистре.

    Пустая строка означает, что строку ``Mode:`` найти не удалось.
    """
    match = _MODE_RE.search(output or "")
    return match.group("mode").strip().lower() if match else ""


def mode_label(mode: str) -> str:
    """Подпись режима для интерфейса (``warp`` -> «Трафик и DNS (WARP)»)."""
    key = (mode or "").strip().lower()
    if not key:
        return ""
    return MODE_LABELS.get(key, key)


def _first_line(text: str) -> str:
    """Первая непустая строка вывода — для короткого сообщения в статусе."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _mentions(text: str, phrase: str) -> bool:
    """Есть ли фраза в выводе (регистр не важен)."""
    return phrase in (text or "").lower()


def _mentions_any(text: str, markers: tuple[str, ...]) -> bool:
    """Есть ли в выводе хотя бы одна из фраз (регистр не важен)."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in markers)


def is_unsupported_command(output: str) -> bool:
    """Не знает ли клиент саму команду смены режима.

    Так отвечает ``warp-cli``, когда версия клиента команду переименовала:
    ``error: unrecognized subcommand 'set-mode'``.
    """
    return _mentions_any(output, _UNSUPPORTED_COMMAND_MARKERS)


def is_unsupported_value(output: str) -> bool:
    """Не принимает ли клиент значение режима (например ``off``)."""
    return _mentions_any(output, _UNSUPPORTED_VALUE_MARKERS)


def _short(text: str, limit: int = 220) -> str:
    """Сжимает длинное сообщение об ошибке до одной короткой строки.

    Текст исключений ``requests`` занимает несколько строк и сотни символов
    (адрес, повторы, вложенная причина) — в подписи интерфейса он нечитаем.
    """
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class WarpStatus:
    """Состояние WARP: состояние, причина и «сырой» вывод клиента."""

    state: str = STATE_UNKNOWN
    #: Причина, которую сообщил клиент (``Reason: ...``); может быть пустой.
    reason: str = ""
    #: Полный вывод команды — показывается в подсказке.
    raw: str = ""

    @property
    def is_connected(self) -> bool:
        """Поднят ли туннель."""
        return self.state == STATE_CONNECTED

    @property
    def label(self) -> str:
        """Подпись состояния для интерфейса."""
        return STATE_LABELS.get(self.state, STATE_LABELS[STATE_UNKNOWN])


class WarpManager:
    """Обёртка над ``warp-cli``: статус, подключение, режим, исключения.

    :param cli_path: явный путь к ``warp-cli.exe``; ``None`` — искать самим
        (PATH, затем :data:`config.WARP_CLI_PATH`).
    """

    def __init__(self, cli_path: Path | str | None = None) -> None:
        #: Путь к клиенту: задан явно или найден при первом обращении.
        self._cli_path: Path | None = Path(cli_path) if cli_path else None
        #: Версия клиента кэшируется: она не меняется во время работы окна.
        self._version: str = ""
        #: Вариант команды смены режима, который клиент уже принял.
        self._mode_command: tuple[str, ...] | None = None
        #: Клиент не знает ни одной команды смены режима (см. set_mode).
        self._mode_unsupported: bool = False
        #: Поддерживает ли клиент управление исключениями; ``None`` — не
        #: проверяли (проверка запускает warp-cli, см. tunnel_supported).
        self._tunnel_support: bool | None = None
        #: Принимает ли клиент диапазоны адресов (``tunnel ip add-range``).
        self._range_support: bool | None = None
        #: Умеет ли клиент управлять доменами (``tunnel host add``); ``None`` —
        #: ещё не проверяли (проверка запускает warp-cli, см.
        #: tunnel_host_supported).
        self._host_support: bool | None = None
        #: Ответ клиента, из-за которого исключения признаны неподдерживаемыми.
        self._tunnel_error: str = ""
        #: Установлен ли ``psutil``; ``None`` — ещё не проверяли (см.
        #: psutil_available). Импорт необязательной зависимости не делается.
        self._psutil_available: bool | None = None

    # ------------------------------------------------------------------
    #  Поиск клиента
    # ------------------------------------------------------------------
    def warp_cli_path(self) -> Path | None:
        """Путь к ``warp-cli.exe`` или ``None``, если клиент не установлен.

        Путь ищется при каждом вызове, если раньше найти его не удалось:
        пользователь может поставить WARP, не закрывая приложение.
        """
        if self._cli_path is not None and self._is_file(self._cli_path):
            return self._cli_path
        self._cli_path = self._find_cli()
        return self._cli_path

    def _find_cli(self) -> Path | None:
        """Ищет клиент в ``PATH``, затем по стандартному пути установки."""
        for name in (WARP_CLI_NAME, "warp-cli"):
            found = shutil.which(name)
            if found:
                return Path(found)
        if self._is_file(WARP_CLI_PATH):
            return WARP_CLI_PATH
        return None

    @staticmethod
    def _is_file(path: Path) -> bool:
        try:
            return path.is_file()
        except OSError:  # pragma: no cover — недоступный путь (нет прав)
            return False

    def is_installed(self) -> bool:
        """Найден ли ``warp-cli`` (то есть установлен ли Cloudflare WARP)."""
        return self.warp_cli_path() is not None

    # ------------------------------------------------------------------
    #  Запуск команд
    # ------------------------------------------------------------------
    def _run(
        self,
        *args: str,
        timeout: float = WARP_COMMAND_TIMEOUT,
        accept_tos: bool = True,
    ) -> tuple[bool, str]:
        """Запускает ``warp-cli`` и возвращает ``(успех, вывод)``.

        ``--accept-tos`` добавляется по умолчанию: без него клиент ждёт
        подтверждения условий использования и команда завершается таймаутом.

        stderr подмешивается к stdout: клиент пишет ошибки именно туда, а его
        текст нужен пользователю. Исключения не пробрасываются — любая
        нештатная ситуация возвращается как ``(False, сообщение)``.
        """
        cli = self.warp_cli_path()
        if cli is None:
            return False, NOT_INSTALLED_HINT

        command = [str(cli)]
        if accept_tos:
            command.append("--accept-tos")
        command.extend(args)

        log.debug("Запуск warp-cli: %s", command)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=CREATE_NO_WINDOW,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            message = f"warp-cli не ответил за {timeout:g} с: {' '.join(args)}"
            log.error(message)
            return False, message
        except OSError as exc:  # нет прав, файл занят, клиент удалён
            message = f"Не удалось запустить warp-cli: {exc}"
            log.error(message)
            return False, message

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        output = "\n".join(part for part in (stdout, stderr) if part)
        log.debug("warp-cli %s -> код %s: %s", args, completed.returncode, output[:500])

        if completed.returncode != 0:
            return False, output or f"warp-cli вернул код {completed.returncode}"
        return True, output

    # ------------------------------------------------------------------
    #  Состояние и настройки
    # ------------------------------------------------------------------
    def status(self) -> WarpStatus:
        """Текущее состояние WARP (``warp-cli status``).

        Ошибку клиента тоже разбираем: в выводе может быть состояние, а
        код возврата — ненулевым. Если разобрать нечего, причина берётся из
        текста ошибки.
        """
        if self.warp_cli_path() is None:
            return WarpStatus(STATE_UNKNOWN, NOT_INSTALLED_HINT, "")

        ok, output = self._run("status")
        state, reason = parse_status(output)
        if state == STATE_UNKNOWN and not ok:
            reason = reason or _first_line(output)
        return WarpStatus(state, reason, output)

    def settings(self) -> dict:
        """Текущие настройки WARP (``warp-cli settings``).

        :return: словарь с ключами ``mode`` (ключ режима в нижнем регистре),
            ``mode_label`` (подпись для интерфейса), ``raw`` (полный вывод) и
            ``error`` (текст ошибки, если команда не удалась).
        """
        if self.warp_cli_path() is None:
            return {"mode": "", "mode_label": "", "raw": "", "error": NOT_INSTALLED_HINT}

        ok, output = self._run("settings")
        mode = parse_mode(output)
        return {
            "mode": mode,
            "mode_label": mode_label(mode),
            "raw": output,
            "error": "" if ok else _first_line(output),
        }

    def version(self) -> str:
        """Версия клиента (``warp-cli --version``); пустая строка — не узнать.

        Результат кэшируется: версия не меняется до переустановки WARP.
        """
        if self._version:
            return self._version
        if self.warp_cli_path() is None:
            return ""

        # У ``--version`` условия использования не запрашиваются, поэтому
        # ``--accept-tos`` здесь не нужен: команда обязана отвечать мгновенно.
        ok, output = self._run("--version", accept_tos=False, timeout=WARP_COMMAND_TIMEOUT)
        if not ok and not output:
            return ""
        match = _VERSION_RE.search(output)
        self._version = match.group("version") if match else _first_line(output)
        return self._version

    # ------------------------------------------------------------------
    #  Управление
    # ------------------------------------------------------------------
    def connect(self) -> tuple[bool, str]:
        """Подключает WARP. Уже подключённый WARP — тоже успех."""
        ok, output = self._run("connect")
        if ok or _mentions(output, "already connected"):
            return True, _first_line(output) or "WARP подключён"
        return False, _first_line(output) or "warp-cli не смог подключиться"

    def disconnect(self) -> tuple[bool, str]:
        """Отключает WARP. Уже отключённый WARP — тоже успех."""
        ok, output = self._run("disconnect")
        if ok or _mentions(output, "already disconnected"):
            return True, _first_line(output) or "WARP отключён"
        return False, _first_line(output) or "warp-cli не смог отключиться"

    def set_mode(self, mode: str) -> tuple[bool, str]:
        """Переключает режим WARP: ``warp``, ``doh`` или ``off``.

        В актуальном клиенте команда называется ``mode``
        (``warp-cli --accept-tos mode warp``); прежней ``set-mode`` в нём нет.
        Если клиент не знает ни одного известного варианта команды, метод не
        падает и не бросает исключение: он возвращает понятное сообщение, а
        :meth:`mode_switch_supported` начинает возвращать ``False`` — страница
        по нему блокирует селектор режимов.

        Смена режима не переподключает туннель: чтобы режим применился к уже
        поднятому соединению, пользователю нужно переподключиться (страница об
        этом напоминает).
        """
        key = (mode or "").strip().lower()
        if key not in VALID_MODES:
            return False, f"Неизвестный режим WARP: {mode!r}"
        if self._mode_unsupported:
            return False, MODE_UNSUPPORTED_HINT

        ok, output, args = self._change_mode(key)
        if ok:
            log.info(
                "Смена режима WARP: warp-cli %s -> %s",
                " ".join(args),
                _first_line(output) or "ок",
            )
            return True, f"Режим WARP: {mode_label(key)}"

        if is_unsupported_command(output):
            # Команду переименовали или убрали: это не сбой, а отсутствие
            # возможности — сообщаем понятным текстом и больше не пробуем.
            self._mode_unsupported = True
            log.warning(
                "warp-cli %s: смена режима не поддерживается (%s)",
                " ".join(args),
                _first_line(output) or "без вывода",
            )
            return False, MODE_UNSUPPORTED_HINT

        if key == MODE_OFF and is_unsupported_value(output):
            # В warp-cli 2026.7.1376.0 значения off у команды mode нет
            # (``warp-cli mode --help`` перечисляет warp, doh, warp+doh, dot,
            # warp+dot, proxy, tunnel_only). Эквивалент «выключено» —
            # отключить WARP: тогда он не вмешивается в соединения.
            log.info(
                "warp-cli %s: значения off нет — отключаю WARP как эквивалент",
                " ".join(args),
            )
            disconnected, message = self.disconnect()
            if disconnected:
                return True, MODE_OFF_EQUIVALENT_MESSAGE
            return False, message

        log.warning(
            "warp-cli %s -> %s",
            " ".join(args),
            _first_line(output) or "без вывода",
        )
        return False, _first_line(output) or f"Не удалось включить режим {mode_label(key)}"

    def mode_switch_supported(self) -> bool:
        """Умеет ли этот клиент менять режим.

        ``False`` — клиент ответил «unrecognized subcommand» на все известные
        варианты команды (см. :data:`MODE_COMMAND_CANDIDATES`): страница
        блокирует селектор режимов, а подключение и отключение продолжают
        работать.
        """
        return not self._mode_unsupported

    def _mode_commands(self) -> tuple[tuple[str, ...], ...]:
        """Варианты команды смены режима: проверенный — первым."""
        if self._mode_command is None:
            return MODE_COMMAND_CANDIDATES
        return (
            self._mode_command,
            *(item for item in MODE_COMMAND_CANDIDATES if item != self._mode_command),
        )

    def _change_mode(self, key: str) -> tuple[bool, str, tuple[str, ...]]:
        """Выполняет смену режима, подбирая понятную клиенту команду.

        Варианты перебираются, только пока клиент отвечает «не знаю такую
        подкоманду»; любая другая ошибка (служба остановлена, нет прав)
        возвращается сразу — перебор её не исправит. Сработавший вариант
        запоминается, чтобы следующие вызовы не повторяли поиск.

        :return: ``(успех, вывод, аргументы)`` — аргументы вместе со значением
            режима, чтобы в логе была видна точная выполненная команда.
        """
        args: tuple[str, ...] = (*MODE_COMMAND_CANDIDATES[0], key)
        output = ""
        for command in self._mode_commands():
            args = (*command, key)
            ok, output = self._run(*args)
            log.debug(
                "warp-cli %s -> %s: %s",
                " ".join(args),
                "ок" if ok else "ошибка",
                _short(output),
            )
            if ok:
                self._mode_command = command
                return True, output, args
            if not is_unsupported_command(output):
                break
        return False, output, args

    # ------------------------------------------------------------------
    #  Исключения Split Tunnel: поддержка и список
    # ------------------------------------------------------------------
    def tunnel_supported(self) -> bool:
        """Умеет ли этот клиент управлять исключениями Split Tunnel.

        Проверка одноразовая и **только для чтения**: разбирается справка
        ``warp-cli tunnel --help`` (см. :func:`parse_help_commands`), поэтому
        конфигурация WARP не меняется — адреса-«пробы» не добавляются и не
        удаляются. Версии, где команда только объявлена (``Not yet
        implemented``), и клиенты, которые подкоманду не знают, дают ``False``;
        результат кэшируется.

        Метод запускает ``warp-cli``, поэтому вызывать его из отрисовки
        нельзя — страница делает это в фоне (см. :meth:`tunnel_supported_cached`).
        """
        if self._tunnel_support is None:
            self._tunnel_support = self._probe_tunnel()
        return self._tunnel_support

    def tunnel_supported_cached(self) -> bool | None:
        """Результат проверки, если она уже шла; ``None`` — ещё не проверяли.

        Нужен интерфейсу: до первой проверки кнопки остаются неактивными.
        """
        return self._tunnel_support

    def tunnel_range_supported(self) -> bool:
        """Принимает ли клиент диапазоны адресов (``tunnel ip add-range``)."""
        self.tunnel_supported()
        return bool(self._range_support)

    def tunnel_host_supported(self) -> bool:
        """Умеет ли этот клиент управлять **доменами** в исключениях.

        Разбирается справка ``warp-cli tunnel host --help``: если в ней нет
        подкоманд ``add``/``remove`` (или клиент не знает саму ``tunnel host``),
        кнопки наборов доменов блокируются. Проверка только читает справку —
        конфигурация WARP не меняется. Результат кэшируется.

        Метод запускает ``warp-cli``, поэтому вызывать его из отрисовки нельзя:
        страница делает это в фоне (см. :meth:`tunnel_host_supported_cached`).
        """
        if self._host_support is None:
            self._host_support = self._probe_host()
        return self._host_support

    def tunnel_host_supported_cached(self) -> bool | None:
        """Результат проверки доменов, если она уже шла; ``None`` — ещё нет."""
        return self._host_support

    def _probe_host(self) -> bool:
        """Принимает ли клиент домены: справка ``warp-cli tunnel host --help``."""
        if self.warp_cli_path() is None or not self.tunnel_supported():
            return False

        _, output = self._run("tunnel", "host", "--help")
        commands = parse_help_commands(output)
        supported = all(name in commands for name in TUNNEL_HELP_HOST_COMMANDS)
        if not supported:
            log.warning(
                "warp-cli не управляет доменами в исключениях: %s",
                _first_line(output) or "нет add/remove в справке",
            )
        return supported

    def tunnel_unsupported_reason(self) -> str:
        """Ответ клиента, из-за которого исключения признаны неподдерживаемыми."""
        return self._tunnel_error

    def _probe_tunnel(self) -> bool:
        """Определяет поддержку исключений по справке клиента.

        Конфигурация WARP не меняется: вместо пробного добавления адреса
        разбирается вывод ``warp-cli tunnel --help``. Клиент, который не знает
        подкоманду, отвечает ошибкой, а версия с заглушкой — текстом
        ``Not yet implemented``: и то и другое означает «не поддерживается».
        """
        if self.warp_cli_path() is None:
            self._tunnel_error = NOT_INSTALLED_HINT
            return False

        ok, output = self._run("tunnel", "--help")
        text = output or ""
        commands = parse_help_commands(text)

        if is_unsupported_tunnel(text):
            # Команда объявлена, но не реализована в этой версии клиента.
            self._tunnel_error = _first_line(text)
        elif TUNNEL_HELP_COMMAND in commands:
            self._range_support = self._probe_range()
            log.info(
                "warp-cli поддерживает исключения Split Tunnel (диапазоны: %s)",
                "да" if self._range_support else "нет",
            )
            return True
        elif not ok:
            self._tunnel_error = _first_line(text) or (
                "warp-cli tunnel --help завершился ошибкой"
            )
        else:
            # Справка получена, но подкоманды исключений в ней нет.
            self._tunnel_error = TUNNEL_UNSUPPORTED_HINT

        log.warning(
            "warp-cli не поддерживает исключения Split Tunnel: %s",
            self._tunnel_error or "без вывода",
        )
        return False

    def _probe_range(self) -> bool:
        """Принимает ли клиент диапазоны: справка ``warp-cli tunnel ip --help``."""
        _, output = self._run("tunnel", "ip", "--help")
        commands = parse_help_commands(output)
        supported = all(name in commands for name in TUNNEL_HELP_RANGE_COMMANDS)
        if not supported:
            log.warning(
                "warp-cli не принимает диапазоны адресов: %s",
                _first_line(output) or "нет add-range в справке",
            )
        return supported

    def tunnel_entries(self, kind: str = KIND_IP) -> tuple[bool, list[TunnelEntry], str]:
        """Текущие исключения одного типа: ``(успех, записи, сообщение)``.

        :param kind: :data:`KIND_IP` (``tunnel ip list``) или :data:`KIND_HOST`
            (``tunnel host list``).
        :return: успех команды, разобранные записи (с пометкой «добавлено
            через CLI») и текст ошибки — пустой, если всё хорошо.
        """
        subcommand = _TUNNEL_SUBCOMMANDS.get(kind)
        if subcommand is None:
            return False, [], f"Неизвестный тип исключения: {kind!r}"
        if self.warp_cli_path() is None:
            return False, [], NOT_INSTALLED_HINT

        ok, output = self._run("tunnel", subcommand, "list")
        if not ok:
            if is_unsupported_tunnel(output):
                self._tunnel_support = False
                self._tunnel_error = _first_line(output)
                return False, [], TUNNEL_UNSUPPORTED_HINT
            return False, [], _first_line(output) or "warp-cli не отдал список исключений"
        return True, parse_tunnel_list(output), ""

    def tunnel_ip_list(self) -> list[str]:
        """Адреса и диапазоны в исключениях (``warp-cli tunnel ip list``)."""
        return [entry.value for entry in self.tunnel_entries(KIND_IP)[1]]

    def tunnel_host_list(self) -> list[str]:
        """Домены в исключениях (``warp-cli tunnel host list``)."""
        return [entry.value for entry in self.tunnel_entries(KIND_HOST)[1]]

    # ------------------------------------------------------------------
    #  Исключения Split Tunnel: добавление и удаление
    # ------------------------------------------------------------------
    def tunnel_ip_add(self, cidr: str) -> tuple[bool, str]:
        """Добавляет адрес или диапазон в исключения.

        Запись с маской (``10.0.0.0/8``) уходит в ``tunnel ip add-range``,
        без маски (``10.0.0.1``) — в ``tunnel ip add``: клиент не принимает
        одно вместо другого.
        """
        return self._tunnel_add(KIND_IP, cidr)

    def tunnel_ip_remove(self, cidr: str) -> tuple[bool, str]:
        """Удаляет адрес или диапазон из исключений."""
        return self._tunnel_remove(KIND_IP, cidr)

    def tunnel_host_add(self, domain: str) -> tuple[bool, str]:
        """Добавляет домен в исключения (клиент понимает и ``*.ru``)."""
        return self._tunnel_add(KIND_HOST, domain)

    def tunnel_host_remove(self, domain: str) -> tuple[bool, str]:
        """Удаляет домен из исключений."""
        return self._tunnel_remove(KIND_HOST, domain)

    def tunnel_add(self, value: str) -> tuple[bool, str]:
        """Добавляет запись, сам определяя её тип (IP/диапазон или домен)."""
        return self._tunnel_add(guess_kind(value), value)

    def tunnel_remove(self, value: str) -> tuple[bool, str]:
        """Удаляет запись, сам определяя её тип (IP/диапазон или домен)."""
        return self._tunnel_remove(guess_kind(value), value)

    def _tunnel_add(self, kind: str, value: str) -> tuple[bool, str]:
        """Выполняет добавление записи нужной командой клиента."""
        args = self._tunnel_arguments("add", kind, value)
        if args is None:
            return False, f"Неизвестный тип исключения: {kind!r}"
        if not args:
            return False, "Пустое значение: нечего добавлять в исключения."

        ok, output = self._run(*args)
        if ok:
            log.info("warp-cli %s -> %s", " ".join(args), _first_line(output) or "ок")
            return True, f"{value.strip()} добавлен в исключения"
        if is_unsupported_tunnel(output):
            self._tunnel_support = False
            self._tunnel_error = _first_line(output)
            return False, TUNNEL_UNSUPPORTED_HINT
        return False, _first_line(output) or f"warp-cli не добавил {value.strip()}"

    def _tunnel_remove(self, kind: str, value: str) -> tuple[bool, str]:
        """Выполняет удаление записи нужной командой клиента."""
        args = self._tunnel_arguments("remove", kind, value)
        if args is None:
            return False, f"Неизвестный тип исключения: {kind!r}"
        if not args:
            return False, "Пустое значение: нечего удалять из исключений."

        ok, output = self._run(*args)
        if ok:
            log.info("warp-cli %s -> %s", " ".join(args), _first_line(output) or "ок")
            return True, f"{value.strip()} удалён из исключений"
        if is_unsupported_tunnel(output):
            self._tunnel_support = False
            self._tunnel_error = _first_line(output)
            return False, TUNNEL_UNSUPPORTED_HINT
        return False, _first_line(output) or f"warp-cli не удалил {value.strip()}"

    @staticmethod
    def _tunnel_arguments(action: str, kind: str, value: str) -> tuple[str, ...] | None:
        """Собирает команду клиента для добавления/удаления записи.

        :return: аргументы команды, ``None`` — неизвестный тип записи и пустой
            кортеж — пустое значение.
        """
        subcommand = _TUNNEL_SUBCOMMANDS.get(kind)
        if subcommand is None:
            return None
        text = (value or "").strip()
        if not text:
            return ()
        if kind == KIND_IP and "/" in text:
            # Диапазон: у add и remove своя команда с суффиксом -range.
            return ("tunnel", "ip", f"{action}-range", text)
        return ("tunnel", subcommand, action, text)

    def tunnel_ip_clear(self) -> tuple[bool, str]:
        """Удаляет **добавленные приложением** адреса и диапазоны."""
        return self._tunnel_clear(KIND_IP)

    def tunnel_host_clear(self) -> tuple[bool, str]:
        """Удаляет **добавленные приложением** домены."""
        return self._tunnel_clear(KIND_HOST)

    def _tunnel_clear(self, kind: str) -> tuple[bool, str]:
        """Удаляет записи с пометкой ``(CLI exclude)``.

        Записи, которые WARP добавил сам (локальные сети, адреса Cloudflare),
        не трогаются: клиент их не помечает, а их удаление ломает маршрутизацию.
        """
        ok, entries, message = self.tunnel_entries(kind)
        if not ok:
            return False, message

        label = KIND_LABELS.get(kind, kind)
        ours = [entry for entry in entries if entry.cli]
        if not ours:
            return True, f"Записей ({label}), добавленных приложением, нет."

        removed = 0
        failed: list[tuple[str, str]] = []
        for entry in ours:
            ok, message = self._tunnel_remove(kind, entry.value)
            if ok:
                removed += 1
            else:
                failed.append((entry.value, message))

        if not failed:
            return True, f"Удалено записей ({label}): {removed}"
        text = f"Удалено ({label}): {removed}, не удалось: {len(failed)}"
        for value, message in failed[:WARP_EXCLUDE_ERROR_LIMIT]:
            text += f"\n• {value} — {message}"
        return False, text

    def add_tunnel_entries(
        self,
        values: Sequence[str],
        *,
        kind: str = KIND_IP,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> TunnelAddResult:
        """Массово добавляет записи в исключения.

        Клиент принимает записи только по одной, поэтому список добавляется
        циклом: это долго (сотни записей — десятки секунд), поэтому прогресс
        отдаётся в ``on_progress(готово, всего)``, а ``should_stop()``
        позволяет прервать операцию (кнопка «Остановить»).

        Уже добавленные записи пропускаются (``192.0.2.1`` и ``192.0.2.1/32``
        считаются одной записью), ошибки отдельных записей цикл не прерывают —
        они собираются в :class:`TunnelAddResult`.

        :param values: записи в порядке добавления.
        :param kind: :data:`KIND_IP` или :data:`KIND_HOST`.
        """
        if _TUNNEL_SUBCOMMANDS.get(kind) is None:
            return TunnelAddResult(error=f"Неизвестный тип исключения: {kind!r}")

        items = [str(value).strip() for value in values]
        items = [value for value in items if value]
        ok, entries, message = self.tunnel_entries(kind)
        if not ok:
            return TunnelAddResult(total=len(items), error=message)

        known = {dedup_key(kind, entry.value) for entry in entries}
        ranges_ok = self.tunnel_range_supported() if kind == KIND_IP else True

        added = skipped = unsupported = failed = 0
        errors: list[tuple[str, str]] = []
        stopped = False
        total = len(items)

        for index, value in enumerate(items, start=1):
            if should_stop is not None and should_stop():
                stopped = True
                break

            key = dedup_key(kind, value)
            if key in known:
                skipped += 1
            elif kind == KIND_IP and "/" in value and not ranges_ok:
                unsupported += 1
            else:
                success, message = self._tunnel_add(kind, value)
                if success:
                    added += 1
                    known.add(key)
                else:
                    failed += 1
                    if len(errors) < WARP_EXCLUDE_ERROR_LIMIT:
                        errors.append((value, message))
            if on_progress is not None:
                on_progress(index, total)

        return TunnelAddResult(
            total=total,
            added=added,
            skipped=skipped,
            unsupported=unsupported,
            failed=failed,
            first_errors=tuple(errors),
            stopped=stopped,
        )

    # ------------------------------------------------------------------
    #  Исключения Split Tunnel: готовые наборы доменов (YouTube, Discord)
    # ------------------------------------------------------------------
    def add_preset_exclusions(
        self,
        preset: str,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        """Добавляет домены набора в исключения WARP.

        Наборы (:data:`PRESET_DOMAINS`) — это домены YouTube и Discord: их
        обходит запрет, и WARP не должен перехватывать их трафик. Перед каждым
        добавлением список исключений перечитывается
        (:meth:`tunnel_entries`), поэтому уже добавленный домен не дублируется
        и попадает в счётчик «уже в исключениях».

        ``warp-cli`` принимает домены по одной, поэтому цикл отдаёт прогресс в
        ``on_progress(готово, всего)`` и умеет прерываться по ``should_stop()``.

        :param preset: :data:`PRESET_YOUTUBE` или :data:`PRESET_DISCORD`.
        :return: ``(успех, сообщение)``. Успех — ни один домен не дал ошибку;
            сообщение перечисляет добавленные, уже бывшие и ошибочные домены.
        """
        key = (preset or "").strip().lower()
        domains = preset_domains(key)
        if not domains:
            return False, f"Неизвестный набор исключений: {preset!r}"
        label = preset_label(key)

        if self.warp_cli_path() is None:
            return False, NOT_INSTALLED_HINT
        if not self.tunnel_supported():
            return False, TUNNEL_UNSUPPORTED_HINT
        if not self.tunnel_host_supported():
            return False, HOST_UNSUPPORTED_HINT

        ok, entries, message = self.tunnel_entries(KIND_HOST)
        if not ok:
            return False, message

        known = {dedup_key(KIND_HOST, entry.value) for entry in entries}
        added = skipped = failed = 0
        errors: list[tuple[str, str]] = []
        stopped = False
        total = len(domains)

        for index, domain in enumerate(domains, start=1):
            if should_stop is not None and should_stop():
                stopped = True
                break

            domain_key = dedup_key(KIND_HOST, domain)
            if domain_key in known:
                skipped += 1
            else:
                success, problem = self.tunnel_host_add(domain)
                if success:
                    added += 1
                    known.add(domain_key)
                else:
                    failed += 1
                    if len(errors) < WARP_EXCLUDE_ERROR_LIMIT:
                        errors.append((domain, problem))
            if on_progress is not None:
                on_progress(index, total)

        parts = [f"{label}: добавлено {added}"]
        if skipped:
            parts.append(f"уже в исключениях: {skipped}")
        if failed:
            parts.append(f"ошибок: {failed}")
        if stopped:
            parts.append("остановлено")
        text = ", ".join(parts)
        for domain, problem in errors:
            text += f"\n• {domain} — {problem}"
        if failed > len(errors):
            text += f"\n…и ещё ошибок: {failed - len(errors)}"
        return failed == 0, text

    def remove_preset_exclusions(
        self,
        preset: str,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        """Убирает домены набора из исключений WARP (откат кнопок добавления).

        Удаляются **только** домены набора: остальные записи (чужие исключения,
        адреса WarpBypass, записи самого WARP) не трогаются. Домены, которых в
        исключениях нет, пропускаются и попадают в счётчик «не в исключениях».

        :param preset: :data:`PRESET_YOUTUBE` или :data:`PRESET_DISCORD`.
        :return: ``(успех, сообщение)`` — см. :meth:`add_preset_exclusions`.
        """
        key = (preset or "").strip().lower()
        domains = preset_domains(key)
        if not domains:
            return False, f"Неизвестный набор исключений: {preset!r}"
        label = preset_label(key)

        if self.warp_cli_path() is None:
            return False, NOT_INSTALLED_HINT
        if not self.tunnel_supported():
            return False, TUNNEL_UNSUPPORTED_HINT
        if not self.tunnel_host_supported():
            return False, HOST_UNSUPPORTED_HINT

        ok, entries, message = self.tunnel_entries(KIND_HOST)
        if not ok:
            return False, message

        # Ключ -> значение в том виде, в каком его отдал клиент: удалять нужно
        # ровно то, что записано в конфигурации WARP.
        known = {dedup_key(KIND_HOST, entry.value): entry.value for entry in entries}
        removed = absent = failed = 0
        errors: list[tuple[str, str]] = []
        stopped = False
        total = len(domains)

        for index, domain in enumerate(domains, start=1):
            if should_stop is not None and should_stop():
                stopped = True
                break

            value = known.pop(dedup_key(KIND_HOST, domain), "")
            if not value:
                absent += 1
            else:
                success, problem = self.tunnel_host_remove(value)
                if success:
                    removed += 1
                else:
                    failed += 1
                    if len(errors) < WARP_EXCLUDE_ERROR_LIMIT:
                        errors.append((value, problem))
            if on_progress is not None:
                on_progress(index, total)

        parts = [f"{label}: удалено {removed}"]
        if absent:
            parts.append(f"не в исключениях: {absent}")
        if failed:
            parts.append(f"ошибок: {failed}")
        if stopped:
            parts.append("остановлено")
        text = ", ".join(parts)
        for value, problem in errors:
            text += f"\n• {value} — {problem}"
        if failed > len(errors):
            text += f"\n…и ещё ошибок: {failed - len(errors)}"
        return failed == 0, text

    # ------------------------------------------------------------------
    #  Исключения Split Tunnel: готовый список российских адресов
    # ------------------------------------------------------------------
    def download_warpbypass_ip(self) -> tuple[bool, str, str]:
        """Скачивает список российских IP-диапазонов WarpBypass.

        :return: ``(успех, текст файла, сообщение)``. При ошибке текст пустой,
            а сообщение объясняет причину — его и показывает страница.
        """
        return self._download_list(WARP_BYPASS_IP_URL, "Список российских IP")

    def download_warpbypass_domain(self) -> tuple[bool, str, str]:
        """Скачивает список российских доменов WarpBypass.

        :return: ``(успех, текст файла, сообщение)`` — см.
            :meth:`download_warpbypass_ip`.
        """
        return self._download_list(WARP_BYPASS_DOMAIN_URL, "Список российских доменов")

    def _download_list(self, url: str, title: str) -> tuple[bool, str, str]:
        """Скачивает текстовый список адресов и проверяет, что он разобран."""
        try:
            response = requests.get(
                url,
                timeout=WARP_BYPASS_TIMEOUT,
                headers={"User-Agent": CHECK_USER_AGENT},
            )
        except requests.Timeout:
            message = f"{title}: превышено время ожидания ({WARP_BYPASS_TIMEOUT:g} с)"
            log.warning("Не удалось скачать %s: таймаут", url)
            return False, "", message
        except requests.RequestException as exc:
            message = f"{title}: не удалось скачать — {_short(str(exc))}"
            log.warning("Не удалось скачать %s: %s", url, exc)
            return False, "", message

        if response.status_code != 200:
            message = f"{title}: сервер ответил HTTP {response.status_code}"
            log.warning("%s: HTTP %s", url, response.status_code)
            return False, "", message

        entries = parse_warpbypass_list(response.text)
        if not entries:
            message = f"{title}: файл пуст или не разобран"
            log.warning("%s: пустой список", url)
            return False, "", message
        return True, response.text, f"{title}: получено записей — {len(entries)}"

    # ------------------------------------------------------------------
    #  Исключения Split Tunnel: текущие соединения
    # ------------------------------------------------------------------
    def active_connections(self) -> ActiveConnections:
        """Активные удалённые адреса из таблицы соединений Windows.

        Берутся установленные соединения на порты :data:`config.WARP_ACTIVE_PORTS`
        (:func:`collect_remote_ips`) — их и предлагается исключить кнопкой
        «Добавить текущие соединения».

        ``psutil`` импортируется здесь, а не в начале модуля: без него
        приложение работает, просто кнопка сообщает, что модуль нужно
        установить. Отказ доступа (``AccessDenied``) тоже не исключение:
        причина возвращается в :attr:`ActiveConnections.warning`.
        """
        try:
            import psutil
        except ImportError:
            log.info("psutil не установлен — активные соединения недоступны")
            return ActiveConnections(
                warning=PSUTIL_MISSING_HINT, psutil_available=False
            )

        try:
            connections = psutil.net_connections(kind="inet")
        except psutil.AccessDenied:
            log.warning("Нет прав на чтение списка соединений (psutil.AccessDenied)")
            return ActiveConnections(warning=PSUTIL_DENIED_HINT)
        except OSError as exc:
            message = f"Не удалось прочитать список соединений: {_short(str(exc))}"
            log.warning(message)
            return ActiveConnections(warning=message)

        ips = collect_remote_ips(connections)
        established, web = connection_stats(connections)
        log.info(
            "Соединений: %s, установленных: %s, на порты %s: %s, удалённых адресов: %s",
            len(connections),
            established,
            "/".join(str(port) for port in WARP_ACTIVE_PORTS),
            web,
            len(ips),
        )
        return ActiveConnections(
            ips=tuple(ips), total=len(connections), established=established, web=web
        )

    def active_remote_ips(self) -> list[str] | None:
        """Только адреса активных соединений (подробности — active_connections).

        :return: уникальные публичные IPv4-адреса удалённой стороны; ``None`` —
        ``psutil`` не установлен, и это сигнал интерфейсу показать подсказку про
        установку пакета (:data:`PSUTIL_MISSING_HINT`); пустой список —
        соединений не нашлось.
        """
        connections = self.active_connections()
        if not connections.psutil_available:
            return None
        return list(connections.ips)

    def psutil_available(self) -> bool:
        """Установлен ли ``psutil`` — проверка без импорта модуля.

        :func:`importlib.util.find_spec` только ищет модуль в ``sys.path`` и не
        выполняет его: приложение запускается и без ``psutil``, а интерфейс
        заранее подсказывает, что пакет нужно поставить. Результат кэшируется:
        набор пакетов во время работы окна не меняется.
        """
        if self._psutil_available is None:
            try:
                self._psutil_available = find_spec("psutil") is not None
            except (ImportError, ValueError):  # сломанный sys.path — считаем, что нет
                self._psutil_available = False
        return self._psutil_available


__all__ = [
    "CLI_ENTRY_MARKER",
    "HOST_UNSUPPORTED_HINT",
    "KIND_HOST",
    "KIND_IP",
    "KIND_LABELS",
    "MODE_COMMAND_CANDIDATES",
    "MODE_LABELS",
    "MODE_OFF",
    "MODE_OFF_EQUIVALENT_MESSAGE",
    "MODE_UNSUPPORTED_HINT",
    "NOT_INSTALLED_HINT",
    "PRESET_DISCORD",
    "PRESET_DOMAINS",
    "PRESET_LABELS",
    "PRESET_YOUTUBE",
    "PSUTIL_DENIED_HINT",
    "PSUTIL_MISSING_HINT",
    "STATE_CONNECTED",
    "STATE_CONNECTING",
    "STATE_DISCONNECTED",
    "STATE_LABELS",
    "STATE_UNKNOWN",
    "TUNNEL_UNSUPPORTED_HINT",
    "VALID_MODES",
    "ActiveConnections",
    "TunnelAddResult",
    "TunnelEntry",
    "WarpManager",
    "WarpStatus",
    "collect_remote_ips",
    "connection_stats",
    "dedup_key",
    "guess_kind",
    "is_public_ipv4",
    "is_unsupported_command",
    "is_unsupported_tunnel",
    "is_unsupported_value",
    "mode_label",
    "parse_help_commands",
    "parse_mode",
    "parse_status",
    "parse_tunnel_list",
    "parse_warpbypass_list",
    "preset_domains",
    "preset_label",
]
