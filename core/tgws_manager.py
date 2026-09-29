"""Управление TG WS Proxy — локальным MTProto-прокси для Telegram.

`TG WS Proxy <https://github.com/Flowseal/tg-ws-proxy>`_ (автор тот же, что и у
запрета — Flowseal) — отдельное приложение: оно поднимает локальный
MTProto-прокси (по умолчанию ``127.0.0.1:1443``) и живёт в системном трее.
Telegram подключается к этому прокси через трей приложения («Открыть в
Telegram»), а не через GUI запрета.

Раздел «TG WS Proxy» ничего не реализует сам и ничего не устанавливает: он
управляет **уже скачанным** приложением:

* показывает состояние процесса ``TgWsProxy_windows.exe``;
* по кнопке запускает exe, если процесс не запущен.

Путь к exe можно задать вручную (кнопка «Указать путь к exe» на странице
раздела): он хранится в ``QSettings("ZapretGUI", "Paths")`` под ключом
``tgws_path`` и приоритетнее автопоиска — скачанный файл вполне может
называться не по маске ``TgWsProxy*.exe``.

Обновление (проверка релизов и замена exe) живёт отдельно —
:mod:`core.tgws_updater`, а карточка обновления — на странице «Обновление».

Несколько важных деталей:

* **проверяется только процесс**, а не порт: прокси может слушать другой порт
  (пользователь мог его поменять), а имя процесса остаётся прежним. Порт в
  конфиге приложения не читается и ссылки ``tg://proxy`` не собираются — secret
  генерируется динамически, и без чтения конфига прокси его не узнать;
* **запуск идёт обычным** ``Popen``, **без** ``CREATE_NO_WINDOW``: у TG WS Proxy
  своё трей-приложение, и оно должно появиться. Это отличается от
  :mod:`core.warp_manager`, где флаг обязателен (там консольная утилита
  ``warp-cli``, и без флага на каждую команду мигало бы чёрное окно);
* **остановка не входит в обычную работу**: пользователь управляет прокси через
  его собственный трей. Процесс закрывается только при обновлении —
  :meth:`TgwsManager.kill_process` (``taskkill``) вызывается страницей после
  подтверждения, иначе Windows не даст заменить занятый exe;
* ``psutil`` нужен **только** для проверки процесса и импортируется лениво
  (:func:`importlib.util.find_spec`): без него приложение запускается, а
  страница показывает подсказку об установке пакета
  (:data:`PSUTIL_MISSING_HINT`). Запуск exe от ``psutil`` не зависит.

Все методы не бросают исключений наружу: ошибки возвращаются текстом, чтобы
страница показала их в статусе и не упала (exe может быть не найден, путь
может быть недоступен, прав может не хватать).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import subprocess
import time
from importlib.util import find_spec
from pathlib import Path

from config import (
    TGWS_ASSET_HINT,
    TGWS_COMMAND_TIMEOUT,
    TGWS_GITHUB_API_LATEST,
    TGWS_GITHUB_RELEASES_URL,
    TGWS_GITHUB_REPO,
    TGWS_VERSION_TIMEOUT,
)
from core import portable
from core.settings import tgws_path
from core.win_utils import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

#: Имя исполняемого файла TG WS Proxy в релизах (Windows).
TGWS_EXE_NAME = "TgWsProxy_windows.exe"

#: Маска поиска exe: версия и суффиксы в имени меняются между релизами.
TGWS_EXE_GLOB = "TgWsProxy*.exe"

#: Часть имени ассета релиза, по которой ищется exe для Windows.
#: Реэкспорт :data:`config.TGWS_ASSET_HINT` — им пользуется
#: :mod:`core.tgws_updater`, чтобы не тянуть конфиг дважды.
ASSET_HINT = TGWS_ASSET_HINT

#: Что показать, если exe не найден.
NOT_INSTALLED_HINT = (
    "TG WS Proxy не найден. Скачайте его с "
    "https://github.com/Flowseal/tg-ws-proxy/releases — раздел управляет уже "
    "скачанным приложением."
)

#: Что показать, если нет ``psutil`` — без него процесс не проверить.
PSUTIL_MISSING_HINT = (
    "Модуль psutil не установлен, статус процесса проверить нельзя. "
    "Установите его командой: pip install psutil"
)

#: Что показать, когда прокси работает: подключение идёт через трей прокси.
RUNNING_HINT = (
    "TG WS Proxy работает. Для подключения Telegram откройте его трей → "
    "«Открыть в Telegram»."
)

#: Версия в выводе ``TgWsProxy_windows.exe --version`` (``1.2`` и длиннее).
#: Одно число не подходит: это была бы не версия, а шум в тексте вывода.
_VERSION_RE = re.compile(r"(?P<version>\d+(?:\.\d+){1,})")

#: Сколько ждать после запуска exe, прежде чем проверять процесс (секунды).
#: Мгновенная проверка показала бы «не запущен»: Windows ещё не зарегистрировала
#: новый процесс, а трей-приложение только начинает стартовать.
START_SETTLE_SEC = 1.0

#: Код возврата ``taskkill``, означающий «процесс не найден»: это тоже успех —
#: закрывать нечего.
TASKKILL_NOT_FOUND_CODE = 128


def _candidate_dirs() -> list[Path]:
    """Папки, где ищется ``TgWsProxy*.exe``, в порядке приоритета.

    Список собирается на месте (а не один раз при импорте): папку пользователь
    мог создать уже после старта приложения.
    """
    try:
        home = Path.home()
    except (RuntimeError, OSError):  # pragma: no cover — нет профиля пользователя
        log.debug("Домашняя папка недоступна — ищу exe только рядом с приложением")
        home = None

    dirs: list[Path] = []
    if home is not None:
        dirs.extend((home / "Downloads", home / "Desktop"))
    # Рядом с приложением: portable-сборка и распакованный архив релиза.
    dirs.append(portable.application_dir())
    dirs.extend((Path("D:\\"), Path("C:\\")))

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        dirs.append(Path(local_appdata) / "Programs" / "TgWsProxy")
    return dirs


def _is_exe(path: Path) -> bool:
    """Существует ли файл (недоступный путь — «нет», а не исключение)."""
    try:
        return path.is_file()
    except OSError:  # pragma: no cover — нет прав на путь
        return False


def _kill_orphan(exc: subprocess.TimeoutExpired) -> None:
    """Снимает процесс, повисший после ``subprocess.run(timeout=...)``.

    ``subprocess.run`` при таймауте убивает процесс и **ждёт** его завершения,
    но у GUI-приложения без консоли может остаться запущенной дочерняя копия.
    Она не нужна: команда была служебной (``--version``).

    :param exc: исключение таймаута — из него берётся объект процесса.
    """
    process = getattr(exc, "process", None)
    if process is None:  # pragma: no cover — старая версия Python
        return
    try:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)
    except (OSError, subprocess.SubprocessError) as error:  # pragma: no cover
        log.debug("Не удалось снять зависший процесс --version: %s", error)


class TgwsManager:
    """Запуск TG WS Proxy и проверка его процесса.

    :param exe_path: явный путь к exe; ``None`` — взять путь, выбранный вручную
        на странице «TG WS Proxy» (``QSettings``, ключ ``tgws_path``), а если
        его нет — искать самим (см. :meth:`tgws_exe_path`).
    """

    def __init__(self, exe_path: Path | str | None = None) -> None:
        if exe_path:
            # Путь задан явно — приоритетнее всего: настройки в этом случае
            # даже не читаются (так менеджер можно проверить на чужом exe).
            self._exe_path: Path | None = Path(exe_path)
        else:
            # Путь, выбранный вручную, приоритетнее автопоиска: пользователь
            # задаёт его именно тогда, когда маска ``TgWsProxy*.exe`` файл не
            # нашла (``tg-ws-proxy.exe``, ``TgWsProxy (1).exe``, вложенная
            # папка). Несуществующий файл игнорируем: иначе раздел «залип» бы
            # на удалённом exe, а автопоиск был бы перекрыт навсегда.
            saved = tgws_path()
            self._exe_path = Path(saved) if saved and _is_exe(Path(saved)) else None
        #: Установлен ли ``psutil``; ``None`` — ещё не проверяли (см.
        #: :meth:`psutil_available`). Модуль не импортируется.
        self._psutil_available: bool | None = None
        #: Последняя прочитанная версия приложения: заполняется в
        #: :meth:`version`. Нужна интерфейсу, который не может запускать exe
        #: из отрисовки (смена темы) — см. :meth:`known_version`.
        self._version: str = ""
        #: Флага ``--version`` у сборки нет: команда не ответила за таймаут.
        #: Повторять её на каждое открытие раздела нельзя — exe при этом
        #: запускается и висит, поэтому страница спрашивает только один раз
        #: (см. :meth:`version_supported`).
        self._version_unsupported: bool = False

    # ------------------------------------------------------------------
    #  Поиск приложения
    # ------------------------------------------------------------------
    def tgws_exe_path(self) -> Path | None:
        """Путь к ``TgWsProxy*.exe`` или ``None``, если приложения нет.

        Путь ищется при каждом вызове, пока не найден: exe можно скачать уже
        во время работы окна, и раздел должен увидеть его без перезапуска. Путь,
        выбранный вручную (``tgws_path`` или :meth:`set_exe_path`), проверяется
        первым — автопоиск включается только если такого файла нет.
        """
        if self._exe_path is not None and _is_exe(self._exe_path):
            return self._exe_path
        self._exe_path = self._find_exe()
        return self._exe_path

    def _find_exe(self) -> Path | None:
        """Автопоиск exe по типичным местам (первое найденное — ответ).

        Недоступные папки (нет прав, диск отключён) пропускаются: ``Path.glob``
        и ``is_file`` на них могут бросить ``OSError``, а автопоиск не должен
        из-за этого ломать открытие раздела.
        """
        for directory in _candidate_dirs():
            try:
                if not directory.is_dir():
                    continue
                found = self._first_match(directory)
            except OSError as exc:
                log.debug("Папка автопоиска недоступна (%s): %s", directory, exc)
                continue
            if found is not None:
                log.info("TG WS Proxy найден: %s", found)
                return found
        log.info("TG WS Proxy не найден — раздел покажет подсказку о загрузке")
        return None

    @staticmethod
    def _first_match(directory: Path) -> Path | None:
        """Первый подходящий exe в папке (отсортирован по имени).

        Сортировка нужна для предсказуемости: в папке загрузок может лежать и
        ``TgWsProxy_windows.exe``, и ``TgWsProxy_windows (1).exe`` — выбирать
        между ними случайно нельзя.
        """
        try:
            matches = sorted(directory.glob(TGWS_EXE_GLOB))
        except OSError as exc:  # pragma: no cover — нет прав на листинг
            log.debug("Не удалось прочитать папку %s: %s", directory, exc)
            return None
        for candidate in matches:
            if candidate.is_file():
                return candidate
        return None

    def is_installed(self) -> bool:
        """Найден ли exe (установлен ли TG WS Proxy)."""
        return self.tgws_exe_path() is not None

    def set_exe_path(self, path: Path | str) -> None:
        """Задаёт путь к exe вручную (кнопка «Указать путь к exe»).

        В ``QSettings`` путь **не** пишется: сохранение — дело страницы
        (``core.settings.set_tgws_path``). Менеджер только запоминает новый
        файл, чтобы следующие проверки и запуск шли по нему.
        """
        self._exe_path = Path(path)

    def is_probably_tgws(self, path: Path | str) -> bool:
        """Похоже ли имя файла на TG WS Proxy (без обращения к диску).

        Автопоиск ищет строго ``TgWsProxy*.exe``; файл пользователя может
        называться иначе (``tg-ws-proxy.exe``, ``TgWsProxy (1).exe``) — для
        ручного выбора маска слишком строга, но и принимать любой exe нельзя.
        Поэтому проверка мягкая: расширение ``.exe`` и «tgws» или «proxy» в
        имени файла. Окончательное решение остаётся за пользователем — страница
        при несовпадении спрашивает подтверждение.

        Существование файла здесь не проверяется намеренно: это отдельный
        вопрос, и на него отвечает :meth:`tgws_exe_path`.
        """
        name = Path(path).name.lower()
        return name.endswith(".exe") and ("tgws" in name or "proxy" in name)

    # ------------------------------------------------------------------
    #  Состояние процесса
    # ------------------------------------------------------------------
    def psutil_available(self) -> bool:
        """Установлен ли ``psutil`` — проверка без импорта модуля.

        :func:`importlib.util.find_spec` только ищет модуль в ``sys.path`` и не
        выполняет его: приложение запускается и без ``psutil``, а страница
        заранее подсказывает, что пакет нужно поставить. Результат кэшируется:
        набор пакетов во время работы окна не меняется.
        """
        if self._psutil_available is None:
            try:
                self._psutil_available = find_spec("psutil") is not None
            except (ImportError, ValueError):  # сломанный sys.path — считаем, что нет
                self._psutil_available = False
        return self._psutil_available

    def is_running(self) -> bool:
        """Запущен ли процесс ``TgWsProxy*.exe``.

        Имя процесса сверяется с маской :data:`TGWS_EXE_GLOB` через
        :func:`fnmatch.fnmatch`: маска содержит ``*``, а ``str.startswith``
        понимает его как обычный символ, а не как wildcard — на этом сравнение
        раньше всегда давало ``False``. Обе строки приводятся к нижнему
        регистру: ``fnmatch`` в Windows регистрозависим, а имя файла релиза
        может быть записано иначе.

        Без ``psutil`` проверка невозможна — метод возвращает ``False``, а
        страница показывает подсказку по :meth:`psutil_available`.
        """
        if not self.psutil_available():
            log.debug("psutil не установлен — состояние процесса TG WS Proxy неизвестно")
            return False

        import psutil  # ленивый импорт: без пакета приложение работает

        try:
            for process in psutil.process_iter(["name"]):
                try:
                    name = str(process.info.get("name") or "")
                except (psutil.Error, OSError):
                    # Процесс завершился, пока мы его читали: это не ошибка.
                    continue
                if fnmatch.fnmatch(name.lower(), TGWS_EXE_GLOB.lower()):
                    # Первого совпадения достаточно: bootloader PyInstaller
                    # onefile и его дочерний процесс носят одно имя.
                    return True
        except (psutil.Error, OSError) as exc:
            # Отказ доступа к чужому процессу — не ошибка: считаем, что наш
            # процесс не найден, и пишем причину в журнал.
            log.debug("Не удалось прочитать список процессов: %s", exc)
            return False
        return False

    def running_text(self) -> str:
        """Строка состояния процесса для строки «Статус процесса».

        Отличает «не запущен» от «неизвестно»: без ``psutil`` нельзя утверждать
        ни то, ни другое, поэтому текст говорит об этом прямо.
        """
        if not self.psutil_available():
            return "неизвестен (psutil не установлен)"
        return "запущен" if self.is_running() else "не запущен"

    # ------------------------------------------------------------------
    #  Версия
    # ------------------------------------------------------------------
    def version(self) -> str:
        """Версия приложения из ``TgWsProxy_windows.exe --version``.

        Пустая строка означает «узнать не удалось» (exe не отвечает, версия не
        разобрана). Запасного источника (метаданные файла) нет намеренно:
        показать чужую версию хуже, чем показать «неизвестно».

        Метод запускает процесс, поэтому вызывать его из отрисовки нельзя:
        страница делает это в фоне (``window.run_async``). Удачный ответ
        кэшируется — его отдаёт :meth:`known_version`.
        """
        exe = self.tgws_exe_path()
        if exe is None:
            return ""

        try:
            completed = subprocess.run(
                [str(exe), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                # Консольное окно на долю секунды не должно мелькать: у exe
                # своё трей-приложение, а эта команда — служебная.
                creationflags=CREATE_NO_WINDOW,
                timeout=TGWS_VERSION_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            # У GUI-приложения флага версии может не быть вовсе: тогда оно не
            # печатает ничего и не завершается. Такой процесс нужно снять —
            # иначе каждая проверка оставляла бы висеть ещё одну копию, — а
            # саму команду больше не пробовать (см. version_supported).
            log.warning(
                "--version не ответил за %.1f с: похоже, сборка не поддерживает "
                "флаг версии — больше не пробую",
                TGWS_VERSION_TIMEOUT,
            )
            _kill_orphan(exc)
            self._version_unsupported = True
            return ""
        except OSError as exc:  # нет прав, файл занят, exe удалён
            log.warning("Не удалось запустить %s --version: %s", exe.name, exc)
            return ""

        output = "\n".join(
            part.strip()
            for part in (completed.stdout or "", completed.stderr or "")
            if part and part.strip()
        )
        match = _VERSION_RE.search(output)
        if match is None:
            log.debug("Вывод --version не разобран: %s", output[:200])
            return ""
        self._version = match.group("version")
        return self._version

    def known_version(self) -> str:
        """Версия, прочитанная ранее; пустая строка — ещё не читали.

        Нужна интерфейсу там, где запускать exe нельзя (перерисовка темы):
        страница показывает кэш, а не спрашивает приложение заново.
        """
        return self._version

    def version_supported(self) -> bool:
        """Отвечает ли сборка на ``--version`` (``False`` — не поддерживает).

        ``False`` выставляется после первого таймаута: страница больше не
        запускает exe ради версии. Отдельная ветка нужна потому, что у сборок
        Flowseal флага может не быть, а каждый такой запуск — это пятисекундное
        ожидание и лишний процесс.
        """
        return not self._version_unsupported

    def reset_version_cache(self) -> None:
        """Сбрасывает кэш версии (после замены exe при обновлении).

        Флаг «флаг версии не поддерживается» не сбрасывается: он про сборку,
        а не про конкретный файл, и новая версия его не меняет.
        """
        self._version = ""

    # ------------------------------------------------------------------
    #  Запуск и остановка
    # ------------------------------------------------------------------
    def start(self) -> tuple[bool, str]:
        """Запускает TG WS Proxy.

        ``Popen`` вызывается **без** ``CREATE_NO_WINDOW``: у приложения своё
        трей-приложение, и оно должно появиться на экране. Это осознанное
        отличие от :mod:`core.warp_manager` (там скрывается консоль
        ``warp-cli``).

        :returns: ``(успех, сообщение)``. Успех подтверждается проверкой
            процесса (с короткой паузой на его регистрацию в системе); если
            ``psutil`` не установлен, процесс проверить нельзя, и успешный
            ``Popen`` считается запуском — иначе кнопка всегда сообщала бы об
            ошибке запуска.
        """
        exe = self.tgws_exe_path()
        if exe is None:
            return False, NOT_INSTALLED_HINT

        try:
            subprocess.Popen([str(exe)], cwd=str(exe.parent), shell=False)
        except OSError as exc:
            message = f"Не удалось запустить TG WS Proxy: {exc}"
            log.error(message)
            return False, message

        log.info("TG WS Proxy запущен: %s", exe)
        time.sleep(START_SETTLE_SEC)
        if self.psutil_available() and not self.is_running():
            # Процесс запустился и сразу завершился: показываем ошибку, а не
            # «запущен». Без psutil такой вывод сделать нельзя (см. выше).
            message = (
                "TG WS Proxy запущен, но процесс не найден — "
                "проверьте его трей или журнал приложения."
            )
            log.warning(message)
            return False, message
        return True, "TG WS Proxy запущен"

    def kill_process(self) -> tuple[bool, str]:
        """Закрывает процесс TG WS Proxy (``taskkill``).

        Вызывается **только при обновлении**: в обычной работе приложение не
        останавливает прокси — пользователь управляет им через свой трей. Без
        закрытия процесса Windows не даст заменить занятый exe.

        :returns: ``(успех, сообщение)``. «Процесс не найден» тоже успех —
        закрывать нечего.
        """
        command = ["taskkill", "/IM", TGWS_EXE_NAME, "/F"]
        log.info("Закрываю TG WS Proxy: %s", " ".join(command))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=CREATE_NO_WINDOW,
                timeout=TGWS_COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            message = f"taskkill не ответил за {TGWS_COMMAND_TIMEOUT:g} с"
            log.error(message)
            return False, message
        except OSError as exc:  # taskkill недоступен (не Windows), нет прав
            message = f"Не удалось выполнить taskkill: {exc}"
            log.error(message)
            return False, message

        output = "\n".join(
            part.strip()
            for part in (completed.stdout or "", completed.stderr or "")
            if part and part.strip()
        )
        if completed.returncode == 0:
            log.info("TG WS Proxy закрыт: %s", output[:200] or "ок")
            return True, "процесс закрыт"
        if completed.returncode == TASKKILL_NOT_FOUND_CODE:
            # «Процесс не найден»: он и так не запущен — цель достигнута.
            log.info("Процесс TG WS Proxy не запущен — закрывать нечего")
            return True, "процесс не запущен"
        message = output or f"taskkill вернул код {completed.returncode}"
        log.warning("taskkill не закрыл TG WS Proxy: %s", message)
        return False, message

    # ------------------------------------------------------------------
    #  Ссылки
    # ------------------------------------------------------------------
    @staticmethod
    def releases_url() -> str:
        """Страница релизов TG WS Proxy (кнопка «Открыть на GitHub»)."""
        return TGWS_GITHUB_RELEASES_URL

    @staticmethod
    def api_latest_url() -> str:
        """Ссылка GitHub API на последний релиз."""
        return TGWS_GITHUB_API_LATEST

    @staticmethod
    def repo() -> str:
        """Репозиторий TG WS Proxy (``Flowseal/tg-ws-proxy``)."""
        return TGWS_GITHUB_REPO


__all__ = [
    "ASSET_HINT",
    "NOT_INSTALLED_HINT",
    "PSUTIL_MISSING_HINT",
    "RUNNING_HINT",
    "START_SETTLE_SEC",
    "TASKKILL_NOT_FOUND_CODE",
    "TGWS_EXE_GLOB",
    "TGWS_EXE_NAME",
    "TgwsManager",
]