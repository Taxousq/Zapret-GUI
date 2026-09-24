"""Поиск и разбор стратегий обхода.

Стратегии запрета — это обычные .bat-файлы в корне папки запрета, например
``general (ALT11).bat``. Имя стратегии берётся из имени файла по маске
``general (XXX).bat``. Файлы, которые не матчатся (``general.bat``), тоже
попадают в список — их имя равно имени файла без расширения.

Служебные .bat-файлы (service.bat, uninstall.bat, ...) пропускаются.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config import (
    PREFERRED_STRATEGIES,
    SKIP_STRATEGY_FILES,
    STRATEGY_RE,
    ZAPRET_PATH,
)

log = logging.getLogger(__name__)

#: «ALT11», «alt 11», «ALT-11» -> 11
_ALT_RE = re.compile(r"^ALT[\s_-]*(\d+)$", re.IGNORECASE)
#: Полное совпадение имени файла: general (XXX).bat
_STRATEGY_FILE_RE = re.compile(STRATEGY_RE, re.IGNORECASE)


@dataclass(frozen=True)
class Strategy:
    """Одна стратегия обхода — конкретный .bat-файл."""

    name: str
    """Имя стратегии, извлечённое из имени файла (например, ``ALT11``)."""

    filename: str
    """Имя файла целиком (например, ``general (ALT11).bat``)."""

    path: Path
    """Полный путь к .bat-файлу."""

    alt_number: int | None = None
    """Номер ALT-стратегии, если имя имеет вид ``ALT<N>``."""

    @property
    def is_preferred(self) -> bool:
        """Рекомендованная стратегия (ALT11/ALT12) — показывается первой."""
        return self.name.upper() in {p.upper() for p in PREFERRED_STRATEGIES}

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """Порядок вывода: сначала ALT11/ALT12, затем остальные ALT, затем прочее."""
        if self.is_preferred:
            rank = 0
        elif self.alt_number is not None:
            rank = 1
        else:
            rank = 2
        return (rank, self.alt_number if self.alt_number is not None else 9999, self.name.lower())

    @property
    def tooltip(self) -> str:
        return f"Файл: {self.path}"


def _strategy_name_from_filename(filename: str) -> str | None:
    """Извлекает имя стратегии из имени файла. ``None`` — файл не подходит."""
    match = _STRATEGY_FILE_RE.fullmatch(filename)
    if match:
        return match.group(1).strip()
    # general.bat и любые другие .bat без скобок — тоже стратегии,
    # но служебные имена отсеиваются в StrategyParser.parse().
    stem = Path(filename).stem
    return stem.strip() or None


class StrategyParser:
    """Сканирует папку запрета и возвращает список доступных стратегий."""

    def __init__(self, zapret_path: Path | None = None) -> None:
        self.zapret_path = Path(zapret_path) if zapret_path else Path(ZAPRET_PATH)

    # -- состояние ---------------------------------------------------------
    @property
    def directory_exists(self) -> bool:
        try:
            return self.zapret_path.is_dir()
        except OSError:
            return False

    def hint(self) -> str:
        """Подсказка для пустого списка стратегий."""
        if not self.directory_exists:
            return (
                f"Папка запрета не найдена:\n{self.zapret_path}\n\n"
                "Укажите правильный путь в разделе «Настройки»."
            )
        return (
            f"В папке запрета нет .bat-стратегий:\n{self.zapret_path}\n\n"
            "Проверьте, что запрет распакован полностью."
        )

    # -- основной разбор ---------------------------------------------------
    def parse(self) -> list[Strategy]:
        """Возвращает отсортированный список стратегий.

        Ошибки доступа к папке не приводят к падению: возвращается пустой
        список, а причина попадает в журнал.
        """
        if not self.directory_exists:
            log.warning("Папка запрета не найдена: %s", self.zapret_path)
            return []

        try:
            entries = list(self.zapret_path.iterdir())
        except OSError as exc:
            log.error("Не удалось прочитать папку запрета %s: %s", self.zapret_path, exc)
            return []

        strategies: list[Strategy] = []
        seen: set[str] = set()
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue

            filename = entry.name
            lowered = filename.lower()
            if not lowered.endswith(".bat"):
                continue
            if lowered in SKIP_STRATEGY_FILES or lowered.startswith("service"):
                log.debug("Пропущен служебный файл: %s", filename)
                continue

            name = _strategy_name_from_filename(filename)
            if not name:
                continue
            # Одна и та же стратегия не должна дублироваться (регистр имён файлов).
            if name.lower() in seen:
                continue
            seen.add(name.lower())

            alt_match = _ALT_RE.match(name)
            strategies.append(
                Strategy(
                    name=name,
                    filename=filename,
                    path=entry,
                    alt_number=int(alt_match.group(1)) if alt_match else None,
                )
            )

        strategies.sort(key=lambda s: s.sort_key)
        log.info("Найдено стратегий: %d (%s)", len(strategies), self.zapret_path)
        return strategies

    def find(self, name: str) -> Strategy | None:
        """Находит стратегию по имени (без учёта регистра)."""
        if not name:
            return None
        key = name.strip().lower()
        for strategy in self.parse():
            if strategy.name.lower() == key or strategy.filename.lower() == key:
                return strategy
        return None
