"""История проверок связи для графика пинга.

Модуль намеренно не знает об интерфейсе: он только накапливает результаты
проверок (:class:`core.health_checker.CheckResult`) и отдаёт их графику в
удобном виде.

**Один замер — одна точка.** Раунд проверки (несколько сайтов) больше не
сворачивается в средний пинг: каждый сайт даёт собственную точку, поэтому
после одного нажатия «Проверить связь» на графике появляется столько точек,
сколько сайтов проверялось. Чтобы точки одного раунда не слились на оси
времени в одну вертикальную черту, метки времени разнесены на
:data:`ROUND_STEP_MS` миллисекунд (для глаза это незаметно — сам раунд
длится секунды).

История **не ограничена**: список растёт столько, сколько работает
приложение. Ограничение (``CHART_POINT_LIMIT``) применяется лишь при
построении графика — рисовать десятки тысяч точек незачем, а хранить их
дёшево.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from core.health_checker import CheckResult

log = logging.getLogger(__name__)

#: Сколько последних точек отдавать графику (сама история не обрезается).
CHART_POINT_LIMIT = 1000

#: Сдвиг между точками одного раунда проверки, мс.
#:
#: Без него все сайты раунда получили бы одинаковую метку времени и точки
#: встали бы друг на друга — на графике это одна вертикальная черта вместо
#: линии.
ROUND_STEP_MS = 100


@dataclass(frozen=True)
class PingSample:
    """Один замер: когда сделан, какой сайт проверялся и сколько это заняло.

    ``ping_ms is None`` означает, что проверка не удалась (нет ответа,
    таймаут или ошибка соединения) — для графика это потеря.
    """

    timestamp: datetime
    site: str
    ping_ms: float | None

    @property
    def lost(self) -> bool:
        """Проверка не удалась."""
        return self.ping_ms is None


def _normalize(ping_ms: float | None) -> float | None:
    """Приводит пинг к неотрицательному числу (``None`` — потеря)."""
    if ping_ms is None:
        return None
    try:
        value = float(ping_ms)
    except (TypeError, ValueError):
        log.debug("Некорректный пинг %r — считаю потерей", ping_ms)
        return None
    # Отрицательного пинга не бывает, но обрезаем: ось Y начинается с нуля.
    return max(0.0, value)


class PingHistory:
    """Накопленная история пингов (общая для проверки связи и дашборда).

    Один замер — :class:`PingSample`, то есть **одна точка на сайт**. Раунд
    проверки добавляется одним вызовом :meth:`add_results`: замеры получают
    метки времени с шагом :data:`ROUND_STEP_MS`, так что график рисует по
    точке на каждый сайт.
    """

    def __init__(self) -> None:
        self._samples: list[PingSample] = []

    # ------------------------------------------------------------------
    #  Наполнение
    # ------------------------------------------------------------------
    def add(
        self,
        ping_ms: float | None,
        timestamp: datetime | None = None,
        site: str = "",
    ) -> PingSample:
        """Добавляет один замер (``None`` — потеря) и возвращает его."""
        sample = PingSample(
            timestamp=timestamp if timestamp is not None else datetime.now(),
            site=site,
            ping_ms=_normalize(ping_ms),
        )
        self._samples.append(sample)
        return sample

    def add_result(
        self,
        site: str,
        ping_ms: float | None,
        timestamp: datetime | None = None,
    ) -> PingSample:
        """Добавляет результат проверки одного сайта (одна точка графика).

        Время отклика берётся только у успешной проверки: у неудачной
        ``response_time_ms`` — это время до таймаута, а не пинг.
        """
        return self.add(ping_ms, timestamp, site=site)

    def add_results(
        self,
        results: Iterable[CheckResult],
        timestamp: datetime | None = None,
    ) -> list[PingSample]:
        """Добавляет результаты раунда: **по точке на каждый сайт**.

        Все замеры раунда получают общую метку времени со сдвигом
        :data:`ROUND_STEP_MS` на каждый следующий сайт — так точки стоят
        рядом на оси X, а не друг на друге.
        """
        stamp = timestamp if timestamp is not None else datetime.now()
        samples: list[PingSample] = []
        for index, result in enumerate(results):
            moment = stamp + timedelta(milliseconds=index * ROUND_STEP_MS)
            samples.append(
                self.add_result(
                    site=getattr(result, "name", ""),
                    ping_ms=result.response_time_ms if result.ok else None,
                    timestamp=moment,
                )
            )
        return samples

    def clear(self) -> None:
        """Забывает всю историю."""
        self._samples.clear()

    # ------------------------------------------------------------------
    #  Чтение
    # ------------------------------------------------------------------
    @property
    def samples(self) -> Sequence[PingSample]:
        """Все замеры по порядку добавления."""
        return tuple(self._samples)

    def __len__(self) -> int:
        return len(self._samples)

    def __bool__(self) -> bool:
        return bool(self._samples)

    def _tail(self, limit: int) -> list[PingSample]:
        """Последние ``limit`` замеров (``<= 0`` — все)."""
        if limit and limit > 0:
            return self._samples[-limit:]
        return list(self._samples)

    def chart_points(
        self, limit: int = CHART_POINT_LIMIT
    ) -> list[tuple[datetime, float | None, str]]:
        """Точки для графика: по одной на каждый замер (сайт раунда).

        :param limit: сколько последних точек вернуть (``<= 0`` — все).
        """
        return [
            (sample.timestamp, sample.ping_ms, sample.site)
            for sample in self._tail(limit)
        ]

    def chart_segments(
        self, limit: int = CHART_POINT_LIMIT
    ) -> list[list[tuple[datetime, float]]]:
        """Непрерывные участки пинга: списки ``(timestamp, ping_ms)``.

        Участок обрывается на потере: ``QLineSeries`` разрывов не умеет,
        поэтому на каждый участок график рисует отдельную линию. Если не
        ответил ни один сайт, список пуст — рисовать нечего.
        """
        segments: list[list[tuple[datetime, float]]] = []
        current: list[tuple[datetime, float]] = []
        for sample in self._tail(limit):
            if sample.ping_ms is None:
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append((sample.timestamp, sample.ping_ms))
        if current:
            segments.append(current)
        return segments

    def loss_points(self, limit: int = CHART_POINT_LIMIT) -> list[tuple[datetime, float]]:
        """Потери для графика: ``(timestamp, 0.0)`` — красные метки на нуле."""
        return [
            (sample.timestamp, 0.0)
            for sample in self._tail(limit)
            if sample.ping_ms is None
        ]
