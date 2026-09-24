"""Пользовательские списки запрета: чтение, запись и откат.

Раздел «Списки» работает с двумя текстовыми файлами в папке ``lists`` внутри
папки запрета::

    lists\\list-general-user.txt  — домены, которые пользователь добавляет в обход;
    lists\\list-exclude.txt       — исключения: домены, которые обходить не нужно.

Формат простой: одна строка — один домен, строка, начинающаяся с ``#``, —
комментарий. Запрет читает эти файлы при запуске службы, поэтому изменения
вступают в силу только после её перезапуска.

Файлы пользовательские, и правка их «на живую» рискованна: перед первой
записью рядом создаётся копия ``<имя>.bak``, из которой потом можно
откатиться. Бэкап создаётся **один раз** — до первого изменения, а не при
каждом сохранении: иначе «Откатить» возвращал бы к предыдущему нажатию
«Сохранить», а не к состоянию до правок. Если нужен свежий бэкап, его можно
перезаписать текущим состоянием (:meth:`ListsManager.create_backup`).

Запись идёт через временный файл с последующим ``replace``: оборванная запись
(например, из-за нехватки прав или питания) не оставит вместо списка пустой
файл. Переводы строк — ``\\r\\n``: так записаны штатные списки запрета, и
winws.exe читает их без лишнего ``\\r`` в конце домена.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

#: Файлы списков, доступные для редактирования в GUI. Остальные файлы папки
#: ``lists`` (``list-general.txt``, ``ipset-*.txt``) обновляются вместе с
#: запретом, и трогать их из интерфейса нельзя.
MANAGED_LISTS: tuple[str, ...] = ("list-general-user.txt", "list-exclude.txt")

#: Суффикс файла резервной копии.
BACKUP_SUFFIX = ".bak"

#: Суффикс временного файла, из которого запись переименовывается в целевой.
TEMP_SUFFIX = ".tmp"

#: Формат даты бэкапа для интерфейса («23.09.2026 15:30»).
BACKUP_TIME_FORMAT = "%d.%m.%Y %H:%M"

#: Комментарий в списке: строка, начинающаяся с решётки.
COMMENT_PREFIX = "#"


def normalize_lines(lines) -> list[str]:
    """Готовит строки к записи: обрезает пробелы и выбрасывает пустые.

    Комментарии сохраняются: они полезны пользователю как пометки в файле.
    Дубликаты не удаляются — за это отвечает интерфейс, который лишь
    предупреждает о них.
    """
    prepared: list[str] = []
    for raw in lines:
        text = str(raw).strip()
        if text:
            prepared.append(text)
    return prepared


class ListsManager:
    """Чтение, запись и откат пользовательских списков запрета."""

    def __init__(self, zapret_path: Path | None) -> None:
        """:param zapret_path: папка запрета (файлы лежат в её подпапке ``lists``)."""
        self.zapret_path = Path(zapret_path) if zapret_path else None
        #: Предупреждение последнего чтения (кодировка, ошибка доступа).
        #: Интерфейс показывает его в информационной панели, не мешая работе.
        self.last_warning: str | None = None

    # ------------------------------------------------------------------
    #  Пути
    # ------------------------------------------------------------------
    @property
    def lists_dir(self) -> Path | None:
        """Папка ``lists`` внутри папки запрета (``None`` — путь не задан)."""
        if self.zapret_path is None:
            return None
        return self.zapret_path / "lists"

    def path_for(self, name: str) -> Path | None:
        """Путь к файлу списка (``None``, если путь к запрету не задан)."""
        directory = self.lists_dir
        if directory is None:
            return None
        return directory / name

    def backup_path_for(self, name: str) -> Path | None:
        """Путь к резервной копии файла списка."""
        path = self.path_for(name)
        return None if path is None else _backup_path(path)

    @staticmethod
    def is_managed(name: str) -> bool:
        """Относится ли файл к спискам, которые разрешено редактировать."""
        return name in MANAGED_LISTS

    # ------------------------------------------------------------------
    #  Состав списков
    # ------------------------------------------------------------------
    def available_lists(self) -> list[str]:
        """Имена файлов списков, которые есть в папке ``lists``.

        Возвращаются только существующие файлы. Отсутствующие в комбобоксе
        дополняет страница «Списки»: их можно создать сохранением, и без этого
        пользователь не смог бы завести файл, которого в папке ещё нет.
        """
        directory = self.lists_dir
        if directory is None:
            return []
        try:
            return [name for name in MANAGED_LISTS if (directory / name).is_file()]
        except OSError as exc:  # pragma: no cover — редкая ошибка доступа
            log.warning("Не удалось перечислить файлы списков: %s", exc)
            return []

    def lists_dir_exists(self) -> bool:
        """Есть ли папка ``lists`` (без неё запись невозможна)."""
        directory = self.lists_dir
        try:
            return directory is not None and directory.is_dir()
        except OSError:  # pragma: no cover — редкая ошибка доступа
            return False

    # ------------------------------------------------------------------
    #  Чтение
    # ------------------------------------------------------------------
    def read_list(self, name: str) -> list[str]:
        """Читает файл списка и возвращает строки без пустых и без переводов.

        Отсутствующий файл — это пустой список (а не ошибка): интерфейс должен
        показать пустое поле, из которого файл можно создать сохранением.
        Кодировка: сначала UTF-8, затем cp1251 (списки из старых сборок).
        """
        self.last_warning = None
        if not self.is_managed(name):
            self.last_warning = f"Неизвестный файл списка: {name}"
            log.warning("Запрошен неизвестный файл списка: %s", name)
            return []

        path = self.path_for(name)
        if path is None:
            self.last_warning = "Путь к папке запрета не задан."
            return []
        if not path.is_file():
            return []

        try:
            data = path.read_bytes()
        except OSError as exc:
            self.last_warning = f"Не удалось прочитать {name}: {exc}"
            log.warning("Не удалось прочитать %s: %s", path, exc)
            return []

        return normalize_lines(self._decode(data, path.name).splitlines())

    def _decode(self, data: bytes, filename: str) -> str:
        """Декодирует содержимое файла: UTF-8, иначе cp1251.

        Если не подошла ни одна кодировка, текст показывается с заменами —
        «кракозябры» лучше пустого поля, но пользователь получает
        предупреждение (см. :attr:`last_warning`).
        """
        for encoding in ("utf-8", "cp1251"):
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                continue
            if encoding != "utf-8":
                self.last_warning = (
                    f"{filename} прочитан в кодировке {encoding} — файл не в UTF-8. "
                    "После сохранения он будет записан в UTF-8."
                )
                log.warning("%s прочитан как %s", filename, encoding)
            return text

        self.last_warning = (
            f"Не удалось определить кодировку {filename}: часть символов заменена."
        )
        log.warning("%s: не подошли ни UTF-8, ни cp1251", filename)
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    #  Запись
    # ------------------------------------------------------------------
    def write_list(self, name: str, lines) -> tuple[bool, str]:
        """Записывает строки в файл списка, создав бэкап перед первой правкой.

        :return: ``(успех, сообщение)`` — сообщение пригодно для показа
            пользователю как есть.
        """
        if not self.is_managed(name):
            return False, f"Неизвестный файл списка: {name}"

        path = self.path_for(name)
        if path is None:
            return False, "Путь к папке запрета не задан."

        directory = path.parent
        if not self.lists_dir_exists():
            return False, f"Папка lists не найдена:\n{directory}"

        prepared = normalize_lines(lines)

        try:
            # Бэкап — только если его ещё нет: «Откатить» должен возвращать к
            # состоянию до первой правки, а не к предыдущему сохранению.
            backup = _backup_path(path)
            if path.is_file() and not backup.exists():
                shutil.copy2(path, backup)

            temp = path.with_suffix(path.suffix + TEMP_SUFFIX)
            # Пишем байтами: переводы строк в тексте уже нужные (CRLF), а
            # текстовый режим на Windows превратил бы "\r\n" в "\r\r\n".
            temp.write_bytes(_join_lines(prepared).encode("utf-8"))
            temp.replace(path)
        except PermissionError:
            log.warning("Нет прав на запись в %s", path)
            return False, (
                f"Нет прав на запись:\n{path}\n\n"
                "Запустите приложение от имени администратора."
            )
        except OSError as exc:
            log.error("Не удалось записать %s: %s", path, exc)
            return False, f"Не удалось записать {name}:\n{exc}"

        log.info("Список %s сохранён (%d строк)", name, len(prepared))
        return True, f"Сохранено строк: {len(prepared)}"

    # ------------------------------------------------------------------
    #  Бэкап и откат
    # ------------------------------------------------------------------
    def has_backup(self, name: str) -> bool:
        """Есть ли резервная копия файла (от неё зависит кнопка «Откатить»)."""
        backup = self.backup_path_for(name)
        try:
            return backup is not None and backup.is_file()
        except OSError:  # pragma: no cover — редкая ошибка доступа
            return False

    def backup_age(self, name: str) -> str | None:
        """Время создания бэкапа в читаемом виде («23.09.2026 15:30»)."""
        backup = self.backup_path_for(name)
        if backup is None:
            return None
        try:
            if not backup.is_file():
                return None
            return datetime.fromtimestamp(backup.stat().st_mtime).strftime(
                BACKUP_TIME_FORMAT
            )
        except (OSError, OverflowError, ValueError) as exc:
            log.warning("Не удалось определить дату бэкапа %s: %s", backup, exc)
            return None

    def rollback(self, name: str) -> tuple[bool, str]:
        """Восстанавливает файл из бэкапа (текущее содержимое теряется)."""
        if not self.is_managed(name):
            return False, f"Неизвестный файл списка: {name}"

        path = self.path_for(name)
        backup = self.backup_path_for(name)
        if path is None or backup is None:
            return False, "Путь к папке запрета не задан."
        if not backup.is_file():
            return False, f"Бэкап для {name} не найден."

        try:
            shutil.copy2(backup, path)
        except PermissionError:
            log.warning("Нет прав на восстановление %s", path)
            return False, (
                f"Нет прав на запись:\n{path}\n\n"
                "Запустите приложение от имени администратора."
            )
        except OSError as exc:
            log.error("Не удалось восстановить %s из бэкапа: %s", path, exc)
            return False, f"Не удалось восстановить {name} из бэкапа:\n{exc}"

        log.info("Список %s восстановлен из бэкапа", name)
        return True, f"{name} восстановлен из бэкапа."

    def create_backup(self, name: str) -> tuple[bool, str]:
        """Перезаписывает бэкап текущим содержимым файла.

        Нужна, когда пользователь уверен в текущем состоянии списка и хочет
        сделать его новой точкой отката.
        """
        if not self.is_managed(name):
            return False, f"Неизвестный файл списка: {name}"

        path = self.path_for(name)
        backup = self.backup_path_for(name)
        if path is None or backup is None:
            return False, "Путь к папке запрета не задан."
        if not path.is_file():
            return False, f"Файл {name} ещё не создан — бэкап делать нечего."

        try:
            shutil.copy2(path, backup)
        except PermissionError:
            return False, (
                f"Нет прав на запись:\n{backup}\n\n"
                "Запустите приложение от имени администратора."
            )
        except OSError as exc:
            log.error("Не удалось создать бэкап %s: %s", backup, exc)
            return False, f"Не удалось создать бэкап {name}:\n{exc}"

        log.info("Бэкап списка %s обновлён", name)
        return True, f"Бэкап {name} обновлён."


# ---------------------------------------------------------------------------
#  Вспомогательные функции
# ---------------------------------------------------------------------------
def _backup_path(path: Path) -> Path:
    """``list-exclude.txt`` -> ``list-exclude.txt.bak``."""
    return path.with_suffix(path.suffix + BACKUP_SUFFIX)


def _join_lines(lines: list[str]) -> str:
    """Собирает текст файла: по строке на домен, перевод строки — Windows.

    Пустой список даёт пустой файл (без одинокой пустой строки).
    """
    if not lines:
        return ""
    return "".join(f"{line}\r\n" for line in lines)
