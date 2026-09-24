"""Portable-режим: запуск с флешки без записи в реестр.

Если рядом с ``ZapretGUI.exe`` лежит файл-маркер ``portable.flag``, приложение
переходит в portable-режим:

* настройки (QSettings) хранятся в INI-файле ``config.ini`` рядом с exe,
  а не в реестре Windows;
* журнал пишется в ``logs\\zapret-gui.log`` рядом с exe, а не в
  ``%LOCALAPPDATA%``;
* папка запрета может лежать рядом — ``<папка exe>\\zapret``
  (см. :class:`core.zapret_locator.ZapretLocator`).

Отдельный модуль нужен потому, что portable-режим включается **до** создания
первого ``QSettings`` (в :func:`main.main`): сменить хранилище настроек
на лету Qt не позволяет.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

#: Имя файла-маркера portable-режима (лежит рядом с exe).
PORTABLE_FLAG = "portable.flag"

#: Имя INI-файла с настройками в portable-режиме.
PORTABLE_INI = "config.ini"

#: Имена папок, которые считаются временными (сравнение без учёта регистра).
TEMP_DIR_NAMES = frozenset({"temp", "tmp", "temporary", "tempdir"})

#: Включён ли portable-режим (заполняется :func:`enable_if_portable`).
enabled: bool = False

#: Папка portable-режима (рядом с exe или с исходниками). ``None`` — обычный режим.
base_dir: Path | None = None


def application_dir() -> Path:
    """Папка приложения: рядом с exe в сборке, иначе — корень проекта.

    В собранном PyInstaller-приложении ``sys.executable`` — это путь к exe,
    а ``sys.frozen`` выставлен. При запуске из исходников берётся папка
    ``main.py`` (родитель пакета ``core``).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _temp_roots() -> tuple[Path, ...]:
    """Корни временных каталогов: ``%TEMP%``, ``%TMP%`` и папка распаковки exe."""
    roots: list[Path] = []
    for value in (
        os.environ.get("TEMP"),
        os.environ.get("TMP"),
        tempfile.gettempdir(),
    ):
        if value:
            try:
                roots.append(Path(value).resolve())
            except OSError:  # pragma: no cover — недоступный носитель
                continue
    # В onefile-сборке PyInstaller ``sys._MEIPASS`` — это ``%TEMP%\_MEIxxxxxx``.
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        try:
            roots.append(Path(mei).resolve())
        except OSError:  # pragma: no cover — недоступный носитель
            pass
    return tuple(roots)


def is_temp_path(path: Path | str | None) -> bool:
    """Ведёт ли путь во временную папку (``%TEMP%``, ``%LOCALAPPDATA%\\Temp``).

    Запрет никогда не устанавливается во временном каталоге, поэтому такой путь
    нельзя брать как папку запрета. Проверка появилась после ошибки «Тестер не
    найден: ...\\AppData\\Local\\Temp\\zapret\\utils\\test zapret.ps1»: в
    onefile-сборке PyInstaller ``__file__`` модулей лежит внутри
    ``%TEMP%\\_MEIxxxxxx``, поэтому путь ``%TEMP%\\zapret`` выглядел обычной
    папкой рядом с приложением.

    Путь, выбранный пользователем вручную, этой проверкой не запрещается: GUI
    лишь предупреждает (см. :meth:`core.zapret_locator.ZapretLocator.temp_warning`).

    :param path: проверяемый путь (может быть ``None``);
    :returns: ``True``, если путь лежит во временном каталоге.
    """
    if path is None:
        return False
    try:
        candidate = Path(path)
    except (TypeError, ValueError):  # pragma: no cover — Path почти всегда можно
        return False

    try:
        resolved = candidate.resolve()
    except OSError:  # pragma: no cover — недоступный носитель
        resolved = candidate

    for root in _temp_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True

    folded = str(resolved).replace("\\", "/").casefold()
    if "appdata/local/temp" in folded:
        return True
    return any(part.casefold() in TEMP_DIR_NAMES for part in candidate.parts)


def is_portable() -> bool:
    """Есть ли рядом с приложением маркер ``portable.flag``."""
    try:
        return (application_dir() / PORTABLE_FLAG).is_file()
    except OSError:  # недоступный носитель и т. п. — не повод падать
        return False


def settings_ini_path() -> Path:
    """Путь к ``config.ini`` portable-режима (рядом с exe)."""
    return application_dir() / PORTABLE_INI


def log_file_path() -> Path:
    """Путь к журналу в portable-режиме (``logs`` рядом с exe)."""
    return application_dir() / "logs" / "zapret-gui.log"


def apply_settings_path() -> None:
    """Указывает Qt держать INI-настройки приложения рядом с exe.

    Вызывать нужно до создания первого ``QSettings``. Формат INI задаётся
    явно при каждом создании настроек (см. :mod:`core.settings`), потому что
    конструктор ``QSettings(organization, application)`` использует
    ``NativeFormat`` (реестр) независимо от ``setDefaultFormat``.
    """
    from PyQt6.QtCore import QSettings

    directory = str(application_dir())
    try:
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, directory)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.SystemScope, directory)
    except Exception:  # noqa: BLE001 — portable-режим не должен ронять запуск
        log.exception("Не удалось включить portable-хранилище настроек")


def enable_if_portable() -> bool:
    """Включает portable-режим, если рядом с приложением есть ``portable.flag``.

    Возвращает ``True``, если режим включён. Функция идемпотентна: повторный
    вызов ничего не ломает.
    """
    global enabled, base_dir

    if not is_portable():
        enabled = False
        base_dir = None
        return False

    enabled = True
    base_dir = application_dir()
    apply_settings_path()
    log.info("Portable-режим: настройки и журнал хранятся в %s", base_dir)
    return True
