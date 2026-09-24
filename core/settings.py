"""Единая точка доступа к ``QSettings``.

Настройки приложения разложены по трём областям:

* ``QSettings("ZapretGUI", "Paths")`` — путь к папке запрета (ключ
  ``zapret_path``);
* ``QSettings("ZapretGUI", "Theme")`` — выбранная тема (ключ ``theme``);
* ``QSettings("ZapretGUI", "Warp")`` — дата последней загрузки списка
  российских адресов (ключ ``last_bypass_download``);
* ``QSettings("ZapretGUI", "Updates")`` — автообновление приложения (ключ
  ``check_on_startup``);
* ``QSettings("ZapretGUI", "ZapretGUI")`` — состояние окна (геометрия).

Область — это просто имя приложения в реестре, поэтому у каждой группы
настроек своя «папка». Здесь же решается вопрос формата: в portable-режиме
(маркер ``portable.flag`` рядом с exe, см. :mod:`core.portable`) настройки
пишутся в INI-файл рядом с exe, а не в реестр. Формат задаётся явно, потому
что конструктор ``QSettings(organization, application)`` всегда использует
``NativeFormat`` (реестр) независимо от ``QSettings.setDefaultFormat``.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import QSettings

from core import portable

log = logging.getLogger(__name__)

#: Организация в реестре (или в INI-файле) для всех настроек.
SETTINGS_ORG = "ZapretGUI"
#: Область настроек с путями (ключ ``zapret_path``).
PATHS_APP = "Paths"
#: Область настроек с темой (ключ ``theme``).
THEME_APP = "Theme"
#: Область настроек раздела «Обход (WARP)» (ключ ``last_bypass_download``).
WARP_APP = "Warp"
#: Область настроек автообновления приложения (ключ ``check_on_startup``).
UPDATES_APP = "Updates"
#: Область состояния окна (ключ ``geometry``).
WINDOW_APP = "ZapretGUI"

#: Ключ с путём к папке запрета.
ZAPRET_PATH_KEY = "zapret_path"
#: Ключ с датой последней загрузки списка российских адресов WarpBypass.
WARP_DOWNLOAD_KEY = "last_bypass_download"
#: Ключ «проверять обновления приложения при запуске».
UPDATE_CHECK_KEY = "check_on_startup"


def _make(application: str) -> QSettings:
    """Создаёт ``QSettings`` нужной области с учётом portable-режима.

    Режим проверяется в момент вызова, а не при импорте модуля: portable
    включается уже после старта (:func:`core.portable.enable_if_portable`).
    """
    if portable.enabled:
        # В portable-режиме путь INI-файла уже задан (core.portable), а формат
        # задаём явно: иначе Qt полез бы в реестр.
        return QSettings(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            SETTINGS_ORG,
            application,
        )
    return QSettings(SETTINGS_ORG, application)


def paths_settings() -> QSettings:
    """Настройки путей (ключ ``zapret_path``)."""
    return _make(PATHS_APP)


def theme_settings() -> QSettings:
    """Настройки темы (ключ ``theme``)."""
    return _make(THEME_APP)


def warp_settings() -> QSettings:
    """Настройки раздела «Обход (WARP)» (ключ ``last_bypass_download``)."""
    return _make(WARP_APP)


def window_settings() -> QSettings:
    """Настройки состояния главного окна (ключ ``geometry``)."""
    return _make(WINDOW_APP)


def updates_settings() -> QSettings:
    """Настройки автообновления приложения (ключ ``check_on_startup``)."""
    return _make(UPDATES_APP)


def _as_bool(value: Any, default: bool = True) -> bool:
    """Приводит значение из QSettings к bool.

    QSettings на разных платформах отдаёт ``bool``, строку ``"true"``/``"false"``
    или число — сравнивать результат с ``True`` напрямую нельзя.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def check_updates_on_startup() -> bool:
    """Проверять ли обновления приложения при запуске (по умолчанию — да)."""
    try:
        value: Any = updates_settings().value(UPDATE_CHECK_KEY, True)
    except Exception:  # noqa: BLE001 — настройки не должны ронять запуск
        log.exception("Не удалось прочитать настройку проверки обновлений")
        return True
    return _as_bool(value, True)


def set_check_updates_on_startup(enabled: bool) -> bool:
    """Сохраняет настройку «проверять обновления при запуске»."""
    value = bool(enabled)
    try:
        settings = updates_settings()
        settings.setValue(UPDATE_CHECK_KEY, value)
        settings.sync()
    except Exception:  # noqa: BLE001 — сохранение настроек не критично
        log.exception("Не удалось сохранить настройку проверки обновлений")
    return value


def warp_last_download() -> str:
    """Когда последний раз загружали список российских адресов.

    Возвращает сохранённую строку с датой и временем или пустую строку, если
    список ещё не загружали (или настройки недоступны).
    """
    try:
        value: Any = warp_settings().value(WARP_DOWNLOAD_KEY, "")
    except Exception:  # noqa: BLE001 — настройки не должны ронять раздел
        log.exception("Не удалось прочитать дату загрузки списка WarpBypass")
        return ""
    return str(value).strip() if value is not None else ""


def set_warp_last_download(text: str) -> str:
    """Сохраняет дату последней загрузки списка и возвращает её."""
    stamp = str(text).strip() if text is not None else ""
    try:
        settings = warp_settings()
        settings.setValue(WARP_DOWNLOAD_KEY, stamp)
        settings.sync()
    except Exception:  # noqa: BLE001 — сохранение настроек не критично
        log.exception("Не удалось сохранить дату загрузки списка WarpBypass")
    return stamp


def zapret_path() -> str:
    """Сохранённый путь к запрету. Пустая строка — настройки нет."""
    try:
        value: Any = paths_settings().value(ZAPRET_PATH_KEY, "")
    except Exception:  # noqa: BLE001 — настройки не должны ронять запуск
        log.exception("Не удалось прочитать путь к запрету из настроек")
        return ""
    return str(value).strip() if value is not None else ""


def set_zapret_path(path: object) -> str:
    """Сохраняет путь к запрету и возвращает его строкой."""
    text = str(path).strip() if path is not None else ""
    try:
        settings = paths_settings()
        settings.setValue(ZAPRET_PATH_KEY, text)
        settings.sync()
    except Exception:  # noqa: BLE001 — сохранение настроек не критично
        log.exception("Не удалось сохранить путь к запрету в настройках")
    return text


def settings_file() -> str:
    """Файл, в котором сейчас лежат настройки (для справки в интерфейсе).

    В обычном режиме это путь в реестре (``HKEY_CURRENT_USER\\...``),
    в portable — путь к ``config.ini``.
    """
    try:
        return paths_settings().fileName()
    except Exception:  # noqa: BLE001
        return ""
