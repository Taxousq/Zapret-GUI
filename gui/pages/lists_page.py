"""Страница «Списки»: правка пользовательских списков запрета.

Раздел показывает и редактирует два файла из папки ``lists`` внутри папки
запрета (см. :class:`core.lists_manager.ListsManager`)::

    list-general-user.txt — домены, которые нужно обходить;
    list-exclude.txt      — исключения: обходить не нужно.

Одна строка — один домен. Строки, начинающиеся с ``#``, считаются
комментариями и подсвечиваются приглушённым цветом. Содержимое не проверяется
на «корректность»: запрет сам игнорирует строки, которые не понимает, а любая
проверка в GUI давала бы ложные срабатывания на валидных записях (wildcard-
домены, IP-адреса). Поэтому сохранение ничего не блокирует — в файл попадает
всё, что введено, кроме пустых строк.

Правка списков рискованна: запрет читает их при запуске службы, поэтому перед
первой записью рядом создаётся копия ``<имя>.bak``, из которой можно
откатиться («Откатить»). После сохранения страница напоминает, что изменения
вступят в силу только после перезапуска службы zapret, и предлагает
перезапустить её сразу.

Подсветка включается не на каждое нажатие клавиши, а по таймеру (150 мс), и
только для первых :data:`HIGHLIGHT_LINE_LIMIT` строк: списки бывают длинными,
а полная перерисовка на каждый символ заметно тормозила бы ввод. Счётчики при
этом считаются по всему файлу.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QTextEdit,
    QWidget,
)

from core.lists_manager import (
    COMMENT_PREFIX,
    MANAGED_LISTS,
    ListsManager,
    normalize_lines,
)
from core.service_manager import ServiceManager
from core.zapret_locator import is_zapret_dir
from gui.pages.base import Page, WindowApi
from gui.theme import Theme
from gui.widgets import Card, ModernCard

log = logging.getLogger(__name__)

#: Сколько первых строк подсвечивать (дальше — только счётчики).
HIGHLIGHT_LINE_LIMIT = 5000

#: Пауза перед пересчётом подсветки после правки текста, мс.
HIGHLIGHT_DELAY_MS = 150

#: Подсказка ограниченного режима (запрет не найден).
NO_ZAPRET_HINT = (
    "Запрет не найден. Укажите папку в Настройках — тогда списки станут "
    "доступны для правки."
)

#: Напоминание о том, когда изменения вступят в силу.
RESTART_HINT = (
    "Изменения вступят в силу после перезапуска службы zapret."
)

#: Подсказка о формате файла.
FORMAT_HINT = (
    "Одна строка — один домен или IP-адрес. Строки, начинающиеся с «#», — "
    "комментарии. Пустые строки при сохранении удаляются."
)


class ListsPage(Page):
    """Раздел «Списки»: просмотр и правка пользовательских списков запрета.

    Наследуется от :class:`gui.pages.base.Page` (это обычный ``QWidget`` с
    прокручиваемой областью): на маленьком окне карточки не помещаются
    целиком, и без прокрутки нижняя часть — информационная панель с датой
    бэкапа — оказалась бы недоступной.
    """

    #: Сообщение для шапки окна: (текст, уровень info/success/error).
    status_message = pyqtSignal(str, str)
    #: Ошибка операции: (заголовок, сообщение) — окно показывает диалог.
    failed = pyqtSignal(str, str)
    #: Служба перезапущена — окну нужно обновить её состояние.
    service_changed = pyqtSignal()

    def __init__(
        self,
        window: WindowApi,
        service_manager: ServiceManager | None = None,
        zapret_path: Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            window,
            "Списки",
            parent=parent,
            spacing=12,
            margins=(18, 14, 18, 14),
        )
        self.service_manager = service_manager
        self.manager = ListsManager(zapret_path)
        self._zapret_available = False
        self._current_name = ""
        self._restarting = False
        #: Счётчики последнего пересчёта: всего строк и дубликатов.
        self._total_lines = 0
        self._duplicate_lines = 0

        # Пересчёт подсветки — по таймеру: на каждый символ это слишком дорого
        # для списка в тысячи строк.
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.setInterval(HIGHLIGHT_DELAY_MS)
        self._highlight_timer.timeout.connect(self._apply_highlight)

        self._build()
        self.set_zapret_path(zapret_path)

    # ==================================================================
    #  Построение
    # ==================================================================
    def _build(self) -> None:
        self.card = ModernCard("Списки запрета", FORMAT_HINT)
        # Карточка растягивается по высоте: свободное место получает поле
        # ввода, а не пустая полоса под карточками.
        self.card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self._build_toolbar()
        self._build_editor()
        self._build_buttons()
        self._build_notice()
        self.add_card(self.card)

        self.info_card = Card("Информация о файле")
        self._build_info()
        self.add_card(self.info_card)

        self._update_actions()

    def _build_toolbar(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        caption = QLabel("Файл:")
        caption.setObjectName("Muted")
        row.addWidget(caption)

        self.file_combo = QComboBox()
        self.file_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.file_combo.setToolTip(
            "Список, который редактируется: добавление доменов в обход или исключения"
        )
        self.file_combo.currentIndexChanged.connect(self._on_file_changed)
        row.addWidget(self.file_combo, 1)

        self.reload_button = self.make_button(
            "Обновить",
            tooltip="Перечитать файл с диска (несохранённые правки будут потеряны)",
        )
        self.reload_button.clicked.connect(self.reload_current)
        row.addWidget(self.reload_button)

        self.card.add_layout(row)

    def _build_editor(self) -> None:
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setPlaceholderText(
            "Пусто. Введите по одному домену на строку или нажмите «Добавить домен»."
        )
        self.editor.setMinimumHeight(200)
        self.editor.setTabChangesFocus(True)
        # Моноширинный шрифт: семейство задаёт QSS темы, а подсказка о
        # моноширинности нужна на случай запуска страницы без темы.
        font = self.editor.font()
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.editor.setFont(font)
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.selectionChanged.connect(self._update_actions)
        self.card.add_widget(self.editor)

        self.counts_label = QLabel("Строк: 0 · Дубликатов: 0")
        self.counts_label.setObjectName("Muted")
        self.counts_label.setWordWrap(True)
        self.card.add_widget(self.counts_label)

    def _build_buttons(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.save_button = self.make_button(
            "Сохранить",
            "primary",
            tooltip="Записать файл на диск (перед первой записью создаётся .bak)",
        )
        self.save_button.clicked.connect(self.on_save)
        row.addWidget(self.save_button)

        self.rollback_button = self.make_button(
            "Откатить",
            "danger",
            tooltip="Восстановить файл из резервной копии (.bak)",
        )
        self.rollback_button.clicked.connect(self.on_rollback)
        row.addWidget(self.rollback_button)

        self.add_button = self.make_button("Добавить домен", tooltip="Добавить строку в конец списка")
        self.add_button.clicked.connect(self.on_add_domain)
        row.addWidget(self.add_button)

        self.remove_button = self.make_button(
            "Удалить выбранное", tooltip="Удалить выделенные строки"
        )
        self.remove_button.clicked.connect(self.on_remove_selected)
        row.addWidget(self.remove_button)

        self.update_backup_button = self.make_button(
            "Обновить бэкап",
            tooltip="Перезаписать .bak текущим содержимым файла (новая точка отката)",
        )
        self.update_backup_button.clicked.connect(self.on_update_backup)
        row.addWidget(self.update_backup_button)

        row.addStretch(1)

        self.restart_button = self.make_button(
            "Перезапустить службу",
            tooltip="Применить изменения: перезапустить службу zapret",
        )
        self.restart_button.clicked.connect(self.on_restart_service)
        row.addWidget(self.restart_button)

        self.card.add_layout(row)

    def _build_notice(self) -> None:
        self.hint_label = QLabel(NO_ZAPRET_HINT)
        self.hint_label.setObjectName("Warning")
        self.hint_label.setWordWrap(True)
        self.hint_label.setVisible(False)
        self.card.add_widget(self.hint_label)

        self.restart_hint_label = QLabel(RESTART_HINT)
        self.restart_hint_label.setObjectName("Hint")
        self.restart_hint_label.setWordWrap(True)
        self.card.add_widget(self.restart_hint_label)

    def _build_info(self) -> None:
        self.path_value = self._info_row("Путь к файлу")
        self.lines_value = self._info_row("Строк в файле")
        self.duplicates_value = self._info_row("Дубликатов")
        self.backup_value = self._info_row("Резервная копия")
        self.notice_value = self._info_row("Перезапуск службы")
        self.notice_value.setText(RESTART_HINT)

    def _info_row(self, caption: str) -> QLabel:
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(f"{caption}:")
        label.setObjectName("Muted")
        label.setMinimumWidth(150)
        value = QLabel("—")
        value.setObjectName("Value")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(label)
        row.addWidget(value, 1)
        self.info_card.add_layout(row)
        return value

    # ==================================================================
    #  Путь к запрету и список файлов
    # ==================================================================
    def set_zapret_path(self, zapret_path: Path | None) -> None:
        """Переключает страницу на другую папку запрета и перечитывает списки.

        Вызывается главным окном после смены пути в «Настройках».
        """
        self.manager = ListsManager(zapret_path)
        try:
            self._zapret_available = zapret_path is not None and is_zapret_dir(
                Path(zapret_path)
            )
        except (OSError, TypeError):  # pragma: no cover — защита от чужого пути
            self._zapret_available = False
        if not self._zapret_available:
            log.debug("Раздел «Списки»: запрет недоступен (%s)", zapret_path)

        self.hint_label.setVisible(not self._zapret_available)
        self._reload_file_list()

    def current_file(self) -> str:
        """Имя выбранного файла списка (пустая строка — ничего не выбрано)."""
        return self.file_combo.currentText().strip()

    def _reload_file_list(self) -> None:
        """Наполняет комбобокс файлами списков, сохраняя текущий выбор."""
        names = self._file_names() if self._zapret_available else []
        previous = self._current_name or self.current_file()

        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.addItems(names)
        if previous in names:
            self.file_combo.setCurrentIndex(names.index(previous))
        self.file_combo.blockSignals(False)

        self._current_name = self.current_file()
        self.reload_current()

    def _file_names(self) -> list[str]:
        """Файлы для комбобокса: существующие плюс ещё не созданные.

        Оба списка показываются всегда: отсутствующий файл создаётся первым
        сохранением, и без него пользователь не смог бы завести список,
        которого в папке ещё нет.
        """
        names = list(self.manager.available_lists())
        for name in MANAGED_LISTS:
            if name not in names:
                names.append(name)
        return names

    def _on_file_changed(self, _index: int) -> None:
        self._current_name = self.current_file()
        self.reload_current()

    # ==================================================================
    #  Чтение и показ файла
    # ==================================================================
    def reload_current(self) -> None:
        """Перечитывает выбранный файл с диска и обновляет подсветку."""
        name = self.current_file()
        self._current_name = name

        if not self._zapret_available or not name:
            self.editor.blockSignals(True)
            self.editor.clear()
            self.editor.blockSignals(False)
            self.editor.document().setModified(False)
            self._apply_highlight()
            self._refresh_info()
            self._update_actions()
            return

        lines = self.manager.read_list(name)
        self.editor.blockSignals(True)
        self.editor.setPlainText("\n".join(lines))
        self.editor.blockSignals(False)
        self.editor.document().setModified(False)
        self._apply_highlight()
        self._refresh_info()
        self._update_actions()

        warning = self.manager.last_warning
        if warning:
            # Файл прочитан, но с оговоркой (кодировка или ошибка доступа):
            # показываем её в шапке, не мешая работе с содержимым.
            self.status_message.emit(warning, "error" if "Не удалось" in warning else "info")

    def _on_text_changed(self) -> None:
        # Счётчики и подсветка — одним пересчётом по таймеру: так правка
        # длинного списка не тормозит ввод.
        self._highlight_timer.start()
        self._update_actions()

    def _apply_highlight(self) -> None:
        """Красит комментарии приглушённым цветом и считает строки.

        Содержимое строк не проверяется: подсветка — только удобство чтения.
        Форматирование строится для первых :data:`HIGHLIGHT_LINE_LIMIT` строк,
        счётчики — по всему файлу.
        """
        document = self.editor.document()
        comment_color = QColor(Theme.color("muted"))

        selections: list[QTextEdit.ExtraSelection] = []
        block = document.begin()
        index = 0
        while block.isValid():
            is_comment = block.text().strip().startswith(COMMENT_PREFIX)

            if index < HIGHLIGHT_LINE_LIMIT and is_comment:
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(block)
                fmt = QTextCharFormat()
                fmt.setForeground(comment_color)
                selection.format = fmt
                selections.append(selection)

            block = block.next()
            index += 1

        self.editor.setExtraSelections(selections)
        # Число строк берём из текста, а не из числа блоков документа: у
        # пустого QPlainTextEdit блок всё равно один, и «Строк: 1» на пустом
        # файле вводило бы в заблуждение.
        text = self.editor.toPlainText()
        lines = text.split("\n") if text else []
        self._total_lines = len(lines)
        self._duplicate_lines = self._count_duplicates(lines)
        self._refresh_counts()
        self._refresh_info()

    def _count_duplicates(self, lines: list[str]) -> int:
        """Сколько строк повторяются (без учёта регистра, без комментариев)."""
        seen: set[str] = set()
        duplicates = 0
        for raw in lines:
            text = raw.strip()
            if not text or text.startswith(COMMENT_PREFIX):
                continue
            key = text.lower()
            if key in seen:
                duplicates += 1
            else:
                seen.add(key)
        return duplicates

    def _refresh_counts(self) -> None:
        """Обновляет счётчики под редактором и их цвет.

        Дубликаты — просто информация: они не мешают сохранению (запрет
        разбирается с повторами сам), поэтому счётчик лишь желтеет.
        """
        parts = [
            f"Строк: {self._total_lines}",
            f"Дубликатов: {self._duplicate_lines}",
        ]
        if self._total_lines > HIGHLIGHT_LINE_LIMIT:
            parts.append(f"подсветка — первые {HIGHLIGHT_LINE_LIMIT} строк")
        self.counts_label.setText(" · ".join(parts))

        color = "warning" if self._duplicate_lines else "muted"
        self.counts_label.setStyleSheet(f"color: {Theme.color(color)};")

    def _refresh_info(self) -> None:
        """Обновляет информационную панель: путь, строки, дата бэкапа."""
        name = self.current_file()
        path = self.manager.path_for(name) if name else None
        self.path_value.setText(str(path) if path is not None else "не задан")

        if not name:
            self.lines_value.setText("—")
        else:
            suffix = "" if self._file_exists(name) else " (файл ещё не создан)"
            self.lines_value.setText(f"{self._total_lines}{suffix}")

        self.duplicates_value.setText(str(self._duplicate_lines))

        age = self.manager.backup_age(name) if name else None
        if age:
            self.backup_value.setText(f"Бэкап от {age}")
            self.backup_value.setToolTip(
                str(self.manager.backup_path_for(name) or "")
            )
        elif name and self.manager.has_backup(name):
            self.backup_value.setText("Бэкап есть (дату определить не удалось)")
        else:
            self.backup_value.setText("нет — будет создан при первом сохранении")
            self.backup_value.setToolTip("")

    def _file_exists(self, name: str) -> bool:
        path = self.manager.path_for(name)
        try:
            return path is not None and path.is_file()
        except OSError:  # pragma: no cover — редкая ошибка доступа
            return False

    # ==================================================================
    #  Состояние кнопок
    # ==================================================================
    def _update_actions(self) -> None:
        """Включает и выключает элементы по доступности запрета и файла."""
        available = self._zapret_available
        has_file = bool(self.current_file())
        editable = available and has_file and not self._restarting

        self.file_combo.setEnabled(available and self.file_combo.count() > 0)
        self.reload_button.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.add_button.setEnabled(editable)
        self.editor.setReadOnly(not editable)

        has_selection = self.editor.textCursor().hasSelection()
        self.remove_button.setEnabled(editable and has_selection)
        self.rollback_button.setEnabled(
            editable and self.manager.has_backup(self._current_name)
        )
        self.update_backup_button.setEnabled(
            editable and self._file_exists(self._current_name)
        )
        self.restart_button.setEnabled(available and not self._restarting)

    # ==================================================================
    #  Действия
    # ==================================================================
    def on_save(self) -> None:
        """Записывает содержимое поля в файл и предлагает перезапуск службы."""
        name = self.current_file()
        if not name:
            return

        lines = self.editor.toPlainText().split("\n")
        prepared = normalize_lines(lines)
        duplicates = self._count_duplicates(lines)

        ok, message = self.manager.write_list(name, prepared)
        if not ok:
            self.status_message.emit(message.splitlines()[0], "error")
            self.failed.emit("Сохранение списка", message)
            return

        log.info("Список %s сохранён из интерфейса (%d строк)", name, len(prepared))
        self.status_message.emit(f"{name}: {message}", "success")
        # Перечитываем записанное: так поле показывает ровно то, что лежит в
        # файле (пустые строки уже выброшены), а метка бэкапа — актуальна.
        self.reload_current()

        if self._ask_restart(duplicates=duplicates):
            self.on_restart_service()

    def _ask_restart(self, duplicates: int = 0) -> bool:
        """Спрашивает, перезапустить ли службу сразу после сохранения."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Списки сохранены")
        box.setText(
            "Списки сохранены. Изменения вступят в силу после перезапуска "
            "службы zapret."
        )
        notes: list[str] = []
        if duplicates:
            notes.append(f"Найдено дубликатов: {duplicates} (сохранены как есть).")
        notes.append("Перезапустить службу сейчас?")
        box.setInformativeText("\n".join(notes))
        yes_button = box.addButton("Да", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Позже", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(yes_button)
        box.exec()
        return box.clickedButton() is yes_button

    def on_rollback(self) -> None:
        """Восстанавливает файл из ``.bak`` после подтверждения."""
        name = self.current_file()
        if not name:
            return
        if not self.manager.has_backup(name):
            QMessageBox.information(
                self,
                "Откат",
                f"Для {name} нет резервной копии: откатывать нечего.\n\n"
                "Копия создаётся автоматически при первом сохранении.",
            )
            return

        age = self.manager.backup_age(name)
        answer = QMessageBox.question(
            self,
            "Откат изменений",
            "Восстановить файл из бэкапа? Текущие изменения будут потеряны.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        ok, message = self.manager.rollback(name)
        if not ok:
            self.status_message.emit(message.splitlines()[0], "error")
            self.failed.emit("Откат списка", message)
            return

        self.reload_current()
        hint = f" (бэкап от {age})" if age else ""
        self.status_message.emit(f"{name} восстановлен из бэкапа{hint}", "success")

    def on_update_backup(self) -> None:
        """Перезаписывает ``.bak`` текущим содержимым файла (новая точка отката)."""
        name = self.current_file()
        if not name:
            return
        if QMessageBox.question(
            self,
            "Обновить бэкап",
            f"Перезаписать резервную копию {name}.bak текущим содержимым файла?\n\n"
            "Прежняя точка отката будет потеряна.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        ok, message = self.manager.create_backup(name)
        if not ok:
            self.status_message.emit(message.splitlines()[0], "error")
            self.failed.emit("Бэкап списка", message)
            return
        self._refresh_info()
        self._update_actions()
        self.status_message.emit(message, "success")

    def on_add_domain(self) -> None:
        """Добавляет домен в конец списка (в поле, без записи на диск)."""
        name = self.current_file()
        if not name:
            return

        value, accepted = QInputDialog.getText(
            self, "Добавить домен", "Домен или IP-адрес:"
        )
        if not accepted:
            return
        value = value.strip()
        if not value:
            return

        lines = normalize_lines(self.editor.toPlainText().split("\n"))

        if value.lower() in {line.lower() for line in lines}:
            if QMessageBox.question(
                self,
                "Добавить домен",
                f"«{value}» уже есть в списке. Добавить ещё раз?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        lines.append(value)
        self.editor.setPlainText("\n".join(lines))
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        self.status_message.emit(
            f"«{value}» добавлено в поле. Нажмите «Сохранить», чтобы записать файл.", "info"
        )

    def on_remove_selected(self) -> None:
        """Удаляет выделенные строки после подтверждения."""
        name = self.current_file()
        if not name:
            return

        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            QMessageBox.information(
                self,
                "Удаление строк",
                "Сначала выделите строки, которые нужно удалить.",
            )
            return

        document = self.editor.document()
        first = document.findBlock(cursor.selectionStart()).blockNumber()
        last = document.findBlock(cursor.selectionEnd()).blockNumber()
        count = last - first + 1

        if QMessageBox.question(
            self,
            "Удаление строк",
            f"Удалить {count} строк?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        lines = self.editor.toPlainText().split("\n")
        del lines[first : last + 1]
        self.editor.setPlainText("\n".join(lines))
        self.status_message.emit(
            f"Удалено строк: {count}. Нажмите «Сохранить», чтобы записать файл.", "info"
        )

    def on_restart_service(self) -> None:
        """Перезапускает службу zapret в фоне (изменения списков вступают в силу)."""
        if self.service_manager is None:
            self.status_message.emit("Управление службой недоступно", "error")
            return
        if not self._zapret_available:
            QMessageBox.warning(self, "Перезапуск службы", NO_ZAPRET_HINT)
            return
        if self._restarting:
            return

        self._restarting = True
        self._update_actions()
        self.status_message.emit("Перезапуск службы zapret...", "info")

        worker = self.window.run_async(
            self.service_manager.restart,
            busy_message="Перезапуск службы zapret...",
        )
        if worker is None:  # вызов не из потока окна — запрос уже переадресован
            self._restarting = False
            self._update_actions()
            return

        worker.finished_signal.connect(self._on_restart_finished)
        worker.error_signal.connect(self._on_restart_error)

    def _on_restart_finished(self, result: object) -> None:
        self._restarting = False
        ok, message = (False, "Неожиданный ответ службы.")
        if isinstance(result, tuple) and len(result) == 2:
            ok, message = bool(result[0]), str(result[1])
        message = message or "Служба zapret"

        if ok:
            log.info("Служба перезапущена из раздела «Списки»: %s", message)
            self.status_message.emit(message.splitlines()[0], "success")
        else:
            self.status_message.emit(message.splitlines()[0], "error")
            self.failed.emit("Перезапуск службы", message)

        self.service_changed.emit()
        self._update_actions()

    def _on_restart_error(self, message: str) -> None:
        self._restarting = False
        self._update_actions()
        self.failed.emit("Перезапуск службы", message)
        self.service_changed.emit()

    # ==================================================================
    #  События и тема
    # ==================================================================
    def showEvent(self, event) -> None:  # noqa: N802, ANN001 — Qt API
        """Перечитывает файл при открытии раздела, если правок нет.

        Несохранённый текст не трогаем: иначе переход по разделам стирал бы
        работу пользователя.
        """
        super().showEvent(event)
        if not self.editor.document().isModified():
            self.reload_current()

    def refresh_theme(self) -> None:
        """Перекрашивает подсветку и подписи под активную тему."""
        super().refresh_theme()
        self.card.refresh_theme()
        self.info_card.refresh_theme()
        self._apply_highlight()
