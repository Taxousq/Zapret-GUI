"""Автопоиск папки запрета и её скачивание с GitHub.

Запрет (zapret от Flowseal) — это обычная папка с ``service.bat``,
``bin\\winws.exe`` и ``.bat``-стратегиями. Путь к ней настраивается в GUI и
хранится в ``QSettings("ZapretGUI", "Paths")`` (ключ ``zapret_path``), но при
первом запуске настройки ещё нет — тогда путь ищется автоматически здесь:

1. пути, переданные в конструктор (:attr:`ZapretLocator._extra`);
2. папка службы ``zapret`` из реестра (:meth:`ZapretLocator._registry_candidates`)
   — самый надёжный источник, если служба уже установлена;
3. подпапки с именем на ``zapret`` в типичных родительских каталогах
   (:meth:`ZapretLocator._mask_candidates`) — так находятся папки вида
   ``Downloads\\zapret-discord-youtube-1.9.2``;
4. типичные места установки (:func:`_default_candidates`): ``D:\\Zapret``,
   ``C:\\zapret``, ``zapret`` рядом с приложением и на одном уровне с ним,
   ``%USERPROFILE%\\zapret``, ``%LOCALAPPDATA%\\zapret``, ``Program Files``,
   ``%USERPROFILE%\\Downloads|Desktop|Documents``, ``zapret`` в корне каждого
   локального диска и ``zapret`` рядом с exe в portable-режиме (флешка).

Этот список быстрый: он не обходит диски. Если запрет лежит в неожиданном
месте, мастер первого запуска может запустить полный обход дисков —
:meth:`ZapretLocator.deep_search` (только по явному нажатию кнопки «Искать
везде», потому что операция долгая).

Временные папки (``%TEMP%``, ``%LOCALAPPDATA%\\Temp``) в автопоиске не
участвуют: запрет там никогда не устанавливается. Это важно ещё и потому, что
в onefile-сборке PyInstaller ``__file__`` лежит внутри ``%TEMP%\\_MEIxxxxxx``,
из-за чего путь ``%TEMP%\\zapret`` выглядел «папкой рядом с приложением» и
приводил к ошибке «Тестер не найден». Путь, выбранный пользователем вручную,
такой проверкой не запрещается — GUI только предупреждает
(:meth:`ZapretLocator.temp_warning`).

Если ничего не нашлось, GUI предлагает скачать последний релиз с GitHub
(используется :class:`core.updater.Updater`, который уже умеет и скачивать
архив, и распаковывать его) или указать папку вручную.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import string
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable

try:
    import winreg
except ImportError:  # pragma: no cover — не Windows (например, линтер на CI)
    # Приложение Windows-only, но импорт модуля не должен падать: реестр —
    # лишь один из источников кандидатов, без него автопоиск работает.
    winreg = None  # type: ignore[assignment]

from core import portable
from core.win_utils import CREATE_NO_WINDOW

log = logging.getLogger(__name__)

#: Прогресс скачивания: (скачано байт, всего байт или 0).
ProgressCallback = Callable[[int, int], None]
#: Текстовая стадия операции (например, «Распаковка архива...»).
StatusCallback = Callable[[str], None]

#: Тексты, показываемые пользователю при неудаче.
DOWNLOAD_FAILED_HINT = (
    "Не удалось скачать. Проверьте интернет или укажите папку вручную."
)
DOWNLOAD_OK_HINT = (
    "Запустите service.bat от имени администратора и выберите "
    "«Install Service». После этого нажмите OK."
)

#: Файлы и папки, по которым папка опознаётся как запрет.
SERVICE_BAT = "service.bat"
WINWS_EXE = Path("bin") / "winws.exe"
#: Маска стратегий: хотя бы один ``general*.bat`` в корне папки.
STRATEGY_GLOB = "general*.bat"

#: Папки релиза запрета: если внутри архива одна такая папка, её содержимое
#: считается содержимым папки запрета (а не вложенной папкой).
_RELEASE_SUBDIRS = frozenset(
    {"bin", "lists", "utils", "service", "docs", ".service", "windivert"}
)

#: Сколько ждать ответа на проверку доступности папки (в файловых операциях
#: папка на отключённом носителе может «висеть» — поэтому все проверки в try).
_SAFE_ERRORS = (OSError, ValueError)

#: Служба запрета и её параметр с путём к ``winws.exe`` в реестре.
_SERVICE_KEY = r"SYSTEM\CurrentControlSet\Services\zapret"
_SERVICE_IMAGE_PATH = "ImagePath"

#: Глубина обхода дисков в :meth:`ZapretLocator.deep_search`, считая корень
#: диска за 0. Трёх уровней хватает на ``D:\\Games\\zapret`` и подобное, а
#: полный обход диска занял бы десятки минут.
DEEP_SEARCH_MAX_DEPTH = 3

#: Как часто сообщать о проверяемой папке при полном поиске (секунды).
_PROGRESS_INTERVAL_S = 0.15

#: Папки, в которые обход дисков не спускается: системные, служебные и
#: заведомо «не запрет». Сравнение — по имени, регистр не важен.
_SKIP_DIR_NAMES = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "system volume information",
        "recovery",
        "perflogs",
        # Мусор разработчика и окружения Python: запрет там не лежит, а
        # файлов внутри очень много.
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "site-packages",
        # Профили пользователей — это тоже служебные данные.
        "appdata",
    }
)


def _default_candidates() -> list[Path]:
    """Типичные места установки запрета в порядке приоритета.

    Пути считаются от папки приложения (:func:`core.portable.application_dir`),
    а не от ``__file__``: в onefile-сборке PyInstaller ``__file__`` лежит во
    временной папке распаковки и ``%TEMP%\\zapret`` попадал в кандидаты.
    """
    app_dir = portable.application_dir()
    home = Path.home()
    candidates: list[Path] = [
        Path(r"D:\Zapret"),
        Path(r"C:\zapret"),
        app_dir / "zapret",  # рядом с приложением (в т. ч. portable-режим)
        app_dir.parent / "zapret",  # на одном уровне с папкой приложения
        home / "zapret",
    ]

    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "zapret")

    program_files = os.environ.get("ProgramFiles", "").strip()
    if program_files:
        candidates.append(Path(program_files) / "Zapret")

    # Portable-режим: запрет может лежать рядом с exe на флешке.
    if portable.enabled and portable.base_dir is not None:
        candidates.append(Path(portable.base_dir) / "zapret")

    # Запрет часто распаковывают в «Загрузки» или на рабочий стол, а не в
    # корень диска: указать эти места дешевле, чем обходить диск целиком.
    candidates.extend(
        [
            home / "Downloads" / "zapret",
            home / "Desktop" / "zapret",
            home / "Documents" / "zapret",
        ]
    )

    # Запрет мог быть установлен в корень любого локального диска, а не только
    # C: или D:. Проверка корня — один вызов ``is_dir``, это дёшево.
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.is_dir():
                candidates.append(root / "zapret")
        except _SAFE_ERRORS:
            continue

    return candidates


def _local_drive_roots() -> list[Path]:
    """Корни локальных дисков (без сети и без отключённых носителей).

    Обход сетевого диска или кардридера без карты может «висеть» минутами,
    поэтому в полный обход берутся только фиксированные диски. При ошибке
    определения типа диска (например, на нестандартном носителе) диск
    считается локальным — лучше проверить, чем пропустить запрет.
    """
    if os.name != "nt":  # pragma: no cover — приложение Windows-only
        return []

    drive_fixed = 3  # DRIVE_FIXED из WinBase.h
    roots: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if not root.is_dir():
                continue
            kind = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
        except (OSError, AttributeError, ValueError):
            kind = drive_fixed
        if kind in (0, drive_fixed):  # 0 — тип неизвестен, проверяем
            roots.append(root)
    return roots


def _describe(path: Path) -> str:
    """Строка для журнала: путь без падения на недоступном носителе."""
    try:
        return str(path)
    except _SAFE_ERRORS:  # pragma: no cover — Path всегда приводится к строке
        return repr(path)


def _existence(path: Path) -> dict[str, bool]:
    """Какие из признаков запрета есть в папке (без исключений)."""
    found = {"service": False, "winws": False, "strategy": False}
    try:
        if not path.is_dir():
            return found
        found["service"] = (path / SERVICE_BAT).is_file()
        found["winws"] = (path / WINWS_EXE).is_file()
        found["strategy"] = any(
            entry.is_file() for entry in path.glob(STRATEGY_GLOB)
        )
    except _SAFE_ERRORS as exc:
        log.debug("Не удалось проверить папку %s: %s", _describe(path), exc)
    return found


class ZapretLocator:
    """Ищет папку запрета, проверяет её и скачивает запрет с GitHub."""

    def __init__(self, extra_candidates: Iterable[Path] | None = None) -> None:
        self._extra: tuple[Path, ...] = tuple(
            Path(item) for item in (extra_candidates or ())
        )
        #: Время последнего сообщения о прогрессе обхода — чтобы не заваливать
        #: интерфейс сигналами на больших папках (см. :meth:`_report_progress`).
        self._last_progress_at = 0.0

    # ------------------------------------------------------------------
    #  Поиск
    # ------------------------------------------------------------------
    def candidates(self) -> list[Path]:
        """Пути-кандидаты в порядке приоритета (без дубликатов).

        Порядок: явные пути вызывающей стороны, папка службы из реестра,
        подпапки с именем на ``zapret``, затем типичные места установки.
        Реестр и маска идут раньше типичных мест: они указывают на реальную
        папку запрета, а не на предположение.

        Временные папки (``%TEMP%`` и подобные) из списка исключаются: запрет
        там не устанавливается, а в сборке PyInstaller такие пути появляются
        сами собой (``%TEMP%\\zapret``).
        """
        ordered: list[Path] = [
            *self._extra,
            *self._registry_candidates(),
            *self._mask_candidates(),
            *_default_candidates(),
        ]
        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in ordered:
            key = _describe(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if portable.is_temp_path(candidate):
                log.warning(
                    "Путь-кандидат во временной папке пропущен: %s",
                    _describe(candidate),
                )
                continue
            unique.append(candidate)
        return unique

    def _registry_candidates(self) -> list[Path]:
        """Папка запрета по данным службы Windows (реестр).

        При установке службы ``service.bat`` пишет в
        ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\zapret`` параметр
        ``ImagePath`` — полную команду запуска, например
        ``"D:\\Zapret\\bin\\winws.exe" --wf-tcp=80,443``. Это самый надёжный
        источник: путь настоящий, а не угаданный.

        ``winws.exe`` лежит в ``bin``, поэтому папкой запрета считается
        каталог **двумя** уровнями выше. Если службы нет, нет прав на чтение
        (обычная ситуация без прав администратора) или значение не разобрать —
        возвращается пустой список: вызывающая сторона просто продолжит по
        остальным кандидатам.
        """
        if winreg is None or os.name != "nt":  # pragma: no cover — не Windows
            return []
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _SERVICE_KEY) as key:
                image_path, _ = winreg.QueryValueEx(key, _SERVICE_IMAGE_PATH)
        except FileNotFoundError:
            log.debug("Служба zapret не установлена — реестр не используем")
            return []
        except (OSError, PermissionError, ValueError) as exc:
            # Нет прав на чтение HKLM — это нормально для обычного запуска.
            log.debug("Не удалось прочитать путь службы из реестра: %s", exc)
            return []

        exe = self._service_exe_path(str(image_path))
        if exe is None:
            log.debug("Не удалось разобрать ImagePath службы zapret: %r", image_path)
            return []
        # ``<папка запрета>\\bin\\winws.exe`` → ``<папка запрета>``.
        return [exe.parent.parent]

    @staticmethod
    def _service_exe_path(image_path: str) -> Path | None:
        """Достаёт путь к ``winws.exe`` из значения ``ImagePath``.

        Формат значения не фиксирован: путь может быть в кавычках (обычный
        случай — ``"D:\\Zapret\\bin\\winws.exe" --wf-tcp=80,443``) или без них,
        а перед ним может стоять команда-обёртка (``cmd.exe /c ...``). Поэтому
        путь ищется не до первого пробела, а по последнему ``winws.exe``:
        начало берётся от буквы диска (``D:``) — так в путь не попадают ни
        аргументы, ни обёртка. ``None`` — разобрать не удалось.
        """
        text = (image_path or "").strip()
        if not text:
            return None

        exe_name = WINWS_EXE.name  # ``winws.exe``
        index = text.lower().rfind(exe_name.lower())
        if index == -1:
            return None

        # Ищем начало пути — букву диска. ``text`` уже без пробелов по краям,
        # поэтому короткая строка вида ``C:\\`` сюда не попадает.
        start = -1
        for position in range(index - 1, 0, -1):
            if text[position] == ":" and text[position - 1].isalpha():
                start = position - 1
                break
        if start == -1:
            return None

        raw = text[start : index + len(exe_name)].strip().strip('"')
        return Path(raw) if raw else None

    def _mask_candidates(self) -> list[Path]:
        """Подпапки с именем на ``zapret`` в типичных родительских каталогах.

        Релизы запрета распаковывают как ``zapret-discord-youtube-1.9.2``, и
        точное имя ``zapret`` (как в :func:`_default_candidates`) такую папку
        не находит. Перебор ограничен одним уровнем вложения в заранее
        известных каталогах — это быстро, в отличие от обхода дисков.
        """
        home = Path.home()
        parents: list[Path] = [
            Path("D:\\"),
            Path("C:\\"),
            home,
            Path(os.environ.get("LOCALAPPDATA", "") or "."),
            Path(os.environ.get("ProgramFiles", "") or "."),
            home / "Downloads",
            home / "Desktop",
        ]

        found: list[Path] = []
        for parent in parents:
            try:
                if not parent.is_dir():
                    continue
                for entry in parent.iterdir():
                    if entry.is_dir() and entry.name.lower().startswith("zapret"):
                        found.append(entry)
            except (OSError, PermissionError) as exc:
                # Недоступная папка (нет прав, отключён носитель) — не повод
                # прерывать поиск: остальные родительские каталоги рабочие.
                log.debug("Пропущен каталог %s: %s", _describe(parent), exc)
                continue
        return found

    def find(self) -> Path | None:
        """Возвращает первую папку, похожую на запрет. ``None`` — не найдено."""
        for candidate in self.candidates():
            if self.is_valid(candidate):
                log.info("Папка запрета найдена: %s", _describe(candidate))
                return candidate
        log.info("Папка запрета не найдена — проверено мест: %d", len(self.candidates()))
        return None

    def is_valid(self, path: Path | None, *, allow_temp: bool = False) -> bool:
        """Похожа ли папка на запрет.

        Критерий: есть ``service.bat`` **или** ``bin\\winws.exe`` **или**
        хотя бы один ``general*.bat``. Пустая папка (или файл вместо папки)
        запретом не считается.

        Путь во временной папке (``%TEMP%``, ``%LOCALAPPDATA%\\Temp``) запретом
        не считается: запрет туда не устанавливают, а в onefile-сборке
        PyInstaller путь ``%TEMP%\\zapret`` появляется сам собой. Если папку
        указал пользователь вручную, проверку можно ослабить:
        ``allow_temp=True`` — GUI при этом показывает предупреждение.

        :param path: проверяемая папка (``None`` — невалидно);
        :param allow_temp: разрешить путь во временной папке (явный выбор
            пользователя), по умолчанию такие пути отвергаются.
        """
        if path is None:
            return False
        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return False
        if not allow_temp and portable.is_temp_path(candidate):
            log.warning(
                "Путь к запрету во временной папке — не использую: %s",
                _describe(candidate),
            )
            return False
        found = _existence(candidate)
        return found["service"] or found["winws"] or found["strategy"]

    def temp_warning(self, path: Path | None) -> str | None:
        """Предупреждение, если папка запрета лежит во временной папке.

        ``None`` — путь обычный. Текст показывается пользователю: такой путь
        не запрещается (пользователь мог выбрать его осознанно), но о
        подозрительном месте его честно предупреждают.
        """
        if path is None or not portable.is_temp_path(path):
            return None
        return (
            "Папка запрета находится во временной папке:\n"
            f"{path}\n\n"
            "Запрет обычно устанавливают в другое место (например, C:\\zapret): "
            "Windows может удалить временные файлы, и запрет перестанет работать."
        )

    def describe(self, path: Path | None) -> str:
        """Короткое описание того, что найдено в папке (для интерфейса)."""
        if path is None:
            return "папка не указана"
        found = _existence(Path(path))
        parts = []
        if found["service"]:
            parts.append(SERVICE_BAT)
        if found["winws"]:
            parts.append(str(WINWS_EXE))
        if found["strategy"]:
            parts.append(STRATEGY_GLOB)
        if not parts:
            return "признаков запрета нет"
        return "найдено: " + ", ".join(parts)

    # ------------------------------------------------------------------
    #  Полный обход дисков
    # ------------------------------------------------------------------
    def deep_search(
        self,
        on_progress: Callable[[str], None] | None = None,
        on_found: Callable[[Path], None] | None = None,
        stop_flag: Callable[[], bool] | None = None,
    ) -> Path | None:
        """Ищет папку запрета обходом локальных дисков (долгая операция).

        В :meth:`candidates` этот метод **не** входит: обход диска занимает
        минуты, поэтому запускается только явно — кнопкой «Искать везде» в
        мастере первого запуска. Обходятся локальные диски (сетевые и
        съёмные пропускаются: они могут «висеть»), глубина ограничена
        :data:`DEEP_SEARCH_MAX_DEPTH` уровнями от корня диска.

        :param on_progress: вызывается с путём проверяемой папки — для статуса
            в интерфейсе. Может вызываться из фонового потока: трогать
            Qt-виджеты внутри него нельзя, мастер доставляет текст сигналом;
        :param on_found: вызывается с найденной папкой (необязательно, результат
            и так возвращается);
        :param stop_flag: функция без аргументов; ``True`` — пользователь
            нажал «Остановить», обход немедленно завершается с ``None``;
        :returns: папка запрета или ``None``, если не найдена либо остановлена.

        Исключения не выбрасываются: недоступные папки (``PermissionError``,
        отключённый носитель) пропускаются.
        """
        roots = _local_drive_roots()
        log.info("Полный поиск запрета: дисков найдено %d", len(roots))
        checked = 0
        for root in roots:
            result, checked = self._walk_drive(
                root, on_progress, on_found, stop_flag, checked
            )
            if result is not None:
                log.info("Полный поиск запрета: найдено %s", _describe(result))
                return result
            if stop_flag is not None and stop_flag():
                break
        log.info(
            "Полный поиск запрета завершён без результата (проверено папок: %d)",
            checked,
        )
        return None

    def _walk_drive(
        self,
        root: Path,
        on_progress: Callable[[str], None] | None,
        on_found: Callable[[Path], None] | None,
        stop_flag: Callable[[], bool] | None,
        checked: int,
    ) -> tuple[Path | None, int]:
        """Обходит один диск. Возвращает ``(найденная папка, счётчик папок)``.

        Порядок «сверху вниз»: сначала проверяется сама папка, потом её
        содержимое — поэтому путь покороче (``D:\\Zapret``) находится раньше
        вложенного (``D:\\Games\\Zapret``).
        """
        found = self._check_dir(root, on_found)
        if found is not None:
            return found, checked + 1
        if stop_flag is not None and stop_flag():
            return None, checked

        # Стек обхода: (папка, глубина). Так рекурсия не ограничена лимитом
        # Python, а остановка проверяется на каждом шаге.
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = sorted(
                    entry for entry in current.iterdir() if entry.is_dir()
                )
            except (OSError, PermissionError) as exc:
                log.debug("Пропущена папка %s: %s", _describe(current), exc)
                continue
            for entry in entries:
                if stop_flag is not None and stop_flag():
                    return None, checked
                if entry.name.lower() in _SKIP_DIR_NAMES:
                    continue
                checked += 1
                if on_progress is not None:
                    self._report_progress(on_progress, entry)
                found = self._check_dir(entry, on_found)
                if found is not None:
                    return found, checked
                if depth + 1 < DEEP_SEARCH_MAX_DEPTH:
                    stack.append((entry, depth + 1))
        return None, checked

    def _check_dir(
        self,
        candidate: Path,
        on_found: Callable[[Path], None] | None,
    ) -> Path | None:
        """Возвращает папку, если она похожа на запрет, иначе ``None``.

        Проверка идёт с ``allow_temp=True``: временную папку всё равно не
        обойти (``%TEMP%`` и ``%LOCALAPPDATA%\\Temp`` исключены как служебные),
        а предупреждение на каждую проверенную папку засорило бы журнал —
        окончательно результат проверяет мастер вызовом :meth:`is_valid`.
        """
        if not self.is_valid(candidate, allow_temp=True):
            return None
        if on_found is not None:
            try:
                on_found(candidate)
            except Exception:  # noqa: BLE001 — колбэк интерфейса не должен ломать обход
                log.exception("Обработчик найденной папки завершился ошибкой")
        return candidate

    def _report_progress(self, on_progress: Callable[[str], None], path: Path) -> None:
        """Сообщает о проверяемой папке, не роняя обход из-за колбэка.

        Сообщения ограничены по частоте: на диске с сотнями тысяч папок без
        этого в очередь главного потока попали бы сотни тысяч сигналов, и
        интерфейс начал бы отставать. Первое сообщение отправляется сразу —
        чтобы статус не пустовал.
        """
        now = time.monotonic()
        if now - self._last_progress_at < _PROGRESS_INTERVAL_S:
            return
        self._last_progress_at = now
        try:
            on_progress(str(path))
        except Exception:  # noqa: BLE001 — см. :meth:`_check_dir`
            log.exception("Обработчик прогресса поиска завершился ошибкой")

    # ------------------------------------------------------------------
    #  Скачивание с GitHub
    # ------------------------------------------------------------------
    def download_from_github(
        self,
        target_dir: Path,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> tuple[bool, str]:
        """Скачивает последний релиз запрета и распаковывает его в ``target_dir``.

        Используется :class:`core.updater.Updater`: он берёт ``.zip`` из
        последнего релиза, скачивает его во временную папку и распаковывает.
        Служба Windows здесь **не** устанавливается — после скачивания
        пользователь сам запускает ``service.bat`` от администратора.

        :param target_dir: куда распаковать запрет (папка создаётся);
        :param on_progress: ``(скачано, всего)`` — для прогресс-бара;
        :param on_status: текстовые стадии («Скачивание архива...»).
        :returns: ``(успех, сообщение)``; исключения не выбрасываются.

        Имена параметров совпадают с :meth:`core.updater.Updater.download` и
        :meth:`core.updater.Updater.download_and_install`, а также с тем, что
        передают мастер первого запуска и страница настроек
        (``on_progress=``/``on_status=``).
        """
        from core.updater import Updater, UpdaterError

        target = Path(target_dir)
        workdir: Path | None = None

        def status(text: str) -> None:
            if on_status is not None:
                on_status(text)

        try:
            if not self._target_ready(target):
                return False, (
                    f"Не удалось создать папку:\n{target}\n"
                    "Выберите другую папку — например, на диске D:."
                )

            updater = Updater(target_dir=target)

            status("Получение сведений о последнем релизе...")
            info = updater.get_latest()

            status(f"Скачивание архива {info.asset_name}...")
            workdir = Path(tempfile.mkdtemp(prefix="zapret-download-"))
            archive = updater.download(info, on_progress=on_progress, workdir=workdir)

            status("Распаковка архива...")
            extracted = updater.extract(archive, workdir / "extracted")
            source = self._release_root(extracted, target)

            status("Копирование файлов...")
            ok, message = self._place(source, target)
            if not ok:
                return False, message

            if not self.is_valid(target):
                return False, (
                    "Архив распакован, но папка не похожа на запрет "
                    f"(нет {SERVICE_BAT} или {WINWS_EXE}):\n{target}"
                )

            log.info("Запрет скачан в %s (%s)", _describe(target), message)
            return True, f"Запрет скачан в {target}.\n\n{DOWNLOAD_OK_HINT}"
        except UpdaterError as exc:
            log.error("Скачивание запрета не удалось: %s", exc)
            return False, f"{exc}\n\n{DOWNLOAD_FAILED_HINT}"
        except (OSError, shutil.Error) as exc:
            log.exception("Скачивание запрета не удалось")
            return False, f"Не удалось распаковать архив:\n{exc}\n\n{DOWNLOAD_FAILED_HINT}"
        except Exception as exc:  # noqa: BLE001 — сеть/диск могут упасть как угодно
            log.exception("Скачивание запрета не удалось")
            return False, f"{exc}\n\n{DOWNLOAD_FAILED_HINT}"
        finally:
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------
    #  Вспомогательное для скачивания
    # ------------------------------------------------------------------
    @staticmethod
    def _target_ready(target: Path) -> bool:
        """Готовит папку-приёмник; ``False`` — создать не удалось."""
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target.is_dir()
        except _SAFE_ERRORS as exc:
            log.error("Не удалось создать папку %s: %s", _describe(target), exc)
            return False

    @staticmethod
    def _release_root(extracted: Path, target: Path) -> Path:
        """Возвращает папку с файлами релиза.

        Архивы релизов бывают двух видов: с корневой папкой
        (``zapret-discord-youtube-<версия>/...``) и «плоские». Если внутри
        ровно одна папка и она не похожа на служебную папку самого запрета,
        файлы берутся из неё.
        """
        try:
            entries = [item for item in extracted.iterdir() if item.name != "__MACOSX"]
        except _SAFE_ERRORS:
            return extracted
        if len(entries) == 1 and entries[0].is_dir():
            only = entries[0]
            if only.name.lower() in _RELEASE_SUBDIRS:
                return extracted
            if only.resolve() == target.resolve():
                return extracted
            return only
        return extracted

    def _place(self, source: Path, target: Path) -> tuple[bool, str]:
        """Переносит файлы релиза в папку-приёмник.

        Если папка-приёмник пуста (обычный первый запуск) и релиз лежит в
        отдельной корневой папке, она становится папкой запрета: иначе путь
        получился бы с лишним уровнем вложения (``Запрет\\zapret-1.9.2\\bin``).
        """
        if source.resolve() == target.resolve():
            return True, "Файлы распакованы."

        try:
            target_empty = not any(target.iterdir())
        except _SAFE_ERRORS as exc:
            return False, f"Не удалось прочитать папку:\n{target}\n{exc}"

        if target_empty:
            try:
                if source.parent.resolve() == target.parent.resolve():
                    # Корневая папка релиза лежит рядом с приёмником: просто
                    # переименовываем её.
                    if target.exists():
                        target.rmdir()
                    source.rename(target)
                    return True, "Файлы перенесены."
                # Приёмник к этому моменту уже создан и пуст, поэтому
                # ``shutil.move`` перенёс бы папку релиза **внутрь** него —
                # получился бы лишний уровень вложения
                # (``Запрет\\zapret-1.9.2\\bin``), и :meth:`is_valid` не нашёл бы
                # запрет. Убираем пустой приёмник: перенос становится
                # переименованием, и содержимое релиза оказывается прямо в
                # папке запрета. Если папку успели наполнить — ``rmdir``
                # не сработает, и ниже сработает копирование «поверх».
                if target.exists():
                    target.rmdir()
                shutil.move(str(source), str(target))
                return True, "Файлы перенесены."
            except (OSError, shutil.Error) as exc:
                log.warning("Не удалось перенести файлы (%s), копирую по одному", exc)

        # Копирование «поверх»: используется, если папка не пуста или перенос
        # не удался (например, разные носители).
        try:
            self._copy_tree(source, target)
        except (OSError, shutil.Error) as exc:
            return False, f"Не удалось скопировать файлы:\n{exc}"
        return True, "Файлы скопированы."

    @staticmethod
    def _copy_tree(source: Path, target: Path) -> None:
        """Копирует дерево ``source`` в ``target`` с заменой файлов."""
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not item.is_file():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def find_zapret_path(extra_candidates: Iterable[Path] | None = None) -> Path | None:
    """Удобная обёртка: ``ZapretLocator(...).find()``."""
    return ZapretLocator(extra_candidates).find()


def is_zapret_dir(path: Path | None) -> bool:
    """Похожа ли папка на запрет (для уже выбранного пути).

    Временная папка здесь допускается (``allow_temp=True``): функция отвечает
    на вопрос «можно ли работать с этим путём», а путь уже выбрал человек
    (настройки, мастер первого запуска, «Настройки»). Автопоиск же временные
    папки не рассматривает — см. :meth:`ZapretLocator.find`.
    """
    return ZapretLocator().is_valid(path, allow_temp=True)
