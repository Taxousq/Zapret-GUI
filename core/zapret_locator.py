"""Автопоиск папки запрета и её скачивание с GitHub.

Запрет (zapret от Flowseal) — это обычная папка с ``service.bat``,
``bin\\winws.exe`` и ``.bat``-стратегиями. Путь к ней настраивается в GUI и
хранится в ``QSettings("ZapretGUI", "Paths")`` (ключ ``zapret_path``), но при
первом запуске настройки ещё нет — тогда путь ищется автоматически здесь:

1. ``D:\\Zapret`` — исторический путь;
2. ``C:\\zapret``;
3. ``zapret`` рядом с приложением (рядом с папкой проекта или с exe);
4. ``zapret`` на одном уровне с папкой приложения;
5. ``%USERPROFILE%\\zapret``;
6. ``%LOCALAPPDATA%\\zapret``;
7. ``zapret`` рядом с exe в portable-режиме (флешка).

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

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from core import portable

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


def _default_candidates() -> list[Path]:
    """Типичные места установки запрета в порядке приоритета.

    Пути считаются от папки приложения (:func:`core.portable.application_dir`),
    а не от ``__file__``: в onefile-сборке PyInstaller ``__file__`` лежит во
    временной папке распаковки и ``%TEMP%\\zapret`` попадал в кандидаты.
    """
    app_dir = portable.application_dir()
    candidates: list[Path] = [
        Path(r"D:\Zapret"),
        Path(r"C:\zapret"),
        app_dir / "zapret",  # рядом с приложением (в т. ч. portable-режим)
        app_dir.parent / "zapret",  # на одном уровне с папкой приложения
        Path.home() / "zapret",
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

    return candidates


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

    # ------------------------------------------------------------------
    #  Поиск
    # ------------------------------------------------------------------
    def candidates(self) -> list[Path]:
        """Пути-кандидаты в порядке приоритета (без дубликатов).

        Временные папки (``%TEMP%`` и подобные) из списка исключаются: запрет
        там не устанавливается, а в сборке PyInstaller такие пути появляются
        сами собой (``%TEMP%\\zapret``).
        """
        ordered: list[Path] = [*self._extra, *_default_candidates()]
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
