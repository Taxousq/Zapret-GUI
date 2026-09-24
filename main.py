"""Zapret GUI — точка входа.

Запуск::

    python main.py          # из исходников
    start.bat               # то же, но с установкой зависимостей в venv

Приложение работает только на Windows: управление идёт через службу Windows
(sc/net) и журнал событий (wevtutil).

Если рядом с приложением лежит файл ``portable.flag`` (portable-сборка на
флешке), настройки и журнал хранятся в папке приложения, а не в реестре и не
в ``%LOCALAPPDATA%`` — см. :mod:`core.portable`.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("zapret_gui")


def _install_excepthook() -> None:
    """Показывает необработанные исключения, а не молча их глотает."""

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Необработанная ошибка", exc_info=(exc_type, exc_value, exc_tb))
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                QMessageBox.critical(
                    None,
                    "Непредвиденная ошибка",
                    f"{exc_type.__name__}: {exc_value}\n\n"
                    "Подробности записаны в журнал приложения.",
                )
        except Exception:  # noqa: BLE001 — падение внутри обработчика недопустимо
            pass

    sys.excepthook = hook


def main() -> int:
    """Запускает приложение. Возвращает код выхода."""
    from config import APP_NAME, APP_VERSION, setup_logging
    from core import portable

    # Portable-режим включается до первого QSettings: если рядом с exe лежит
    # portable.flag, настройки и журнал уходят в папку приложения, а не в реестр.
    portable.enable_if_portable()

    setup_logging()
    _install_excepthook()
    log.info("Запуск %s %s", APP_NAME, APP_VERSION)
    if portable.enabled:
        log.info("Приложение запущено в portable-режиме")

    if sys.platform != "win32":
        message = "Zapret GUI работает только на Windows (управление службой через sc/net)."
        log.error(message)
        print(message, file=sys.stderr)
        return 1

    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        message = (
            "Не найден PyQt6.\n"
            "Установите зависимости командой:\n\n"
            "    pip install -r requirements.txt\n"
        )
        log.error("PyQt6 не установлен")
        print(message, file=sys.stderr)
        return 1

    # Чтобы Windows показывала иконку приложения, а не python.exe.
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ZapretGUI")
    except Exception:  # noqa: BLE001 — косметика, не критично
        log.debug("Не удалось задать AppUserModelID", exc_info=True)

    from gui.icons import window_icon
    from gui.main_window import MainWindow
    from gui.theme import Theme

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("ZapretGUI")

    # Тема по умолчанию — тёмная; выбранная пользователем хранится в QSettings.
    Theme.load()
    Theme.apply(app)
    app.setWindowIcon(window_icon())

    # Закрытие окна сворачивает приложение в трей, а не завершает его.
    app.setQuitOnLastWindowClosed(False)

    try:
        window = MainWindow()
    except Exception as exc:  # noqa: BLE001 — понятное сообщение вместо трейсбека
        log.exception("Не удалось создать главное окно")
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.critical(None, "Ошибка запуска", f"Не удалось создать окно:\n{exc}")
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
