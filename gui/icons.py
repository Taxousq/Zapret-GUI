"""Векторные иконки, нарисованные кодом.

Отдельный модуль без зависимостей от виджетов: иконки нужны и сайдбару, и
шапке, и (при желании) трею. PNG-файлов в проекте нет — все иконки рисуются
через :class:`QPainter` штрихами толщиной 1.5 px, поэтому они остаются
резкими при любом масштабе и перекрашиваются вместе с темой.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap

from gui.theme import Theme

#: Толщина штриха иконок.
STROKE = 1.5

#: Буква на иконке приложения и в трее.
APP_LETTER = "Z"


def _painter(pixmap: QPixmap, color: str, width: float = STROKE) -> QPainter:
    """Готовит рисовальщик: сглаживание, круглые концы, заданный цвет кисти."""
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    pen = QPen(QColor(Theme.color(color, color) if color in Theme.colors() else color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    return painter


# ---------------------------------------------------------------------------
#  Отдельные иконки разделов
# ---------------------------------------------------------------------------
def draw_shield(painter: QPainter, s: float) -> None:
    """Щит — раздел «Служба»."""
    path = QPainterPath(QPointF(12 * s, 3.5 * s))
    path.lineTo(19.5 * s, 6.5 * s)
    path.lineTo(19.5 * s, 12.0 * s)
    path.cubicTo(
        19.5 * s, 16.6 * s,
        16.4 * s, 19.6 * s,
        12.0 * s, 20.5 * s,
    )
    path.cubicTo(
        7.6 * s, 19.6 * s,
        4.5 * s, 16.6 * s,
        4.5 * s, 12.0 * s,
    )
    path.lineTo(4.5 * s, 6.5 * s)
    path.closeSubpath()
    painter.drawPath(path)


def draw_arrows(painter: QPainter, s: float) -> None:
    """Две стрелки вверх/вниз — раздел «Стратегии»."""
    painter.drawLine(QPointF(9.0 * s, 20.0 * s), QPointF(9.0 * s, 5.0 * s))
    painter.drawLine(QPointF(5.0 * s, 9.0 * s), QPointF(9.0 * s, 5.0 * s))
    painter.drawLine(QPointF(13.0 * s, 9.0 * s), QPointF(9.0 * s, 5.0 * s))
    painter.drawLine(QPointF(15.0 * s, 4.0 * s), QPointF(15.0 * s, 19.0 * s))
    painter.drawLine(QPointF(11.0 * s, 15.0 * s), QPointF(15.0 * s, 19.0 * s))
    painter.drawLine(QPointF(19.0 * s, 15.0 * s), QPointF(15.0 * s, 19.0 * s))


def draw_flask(painter: QPainter, s: float) -> None:
    """Колба — раздел «Тестирование»."""
    painter.drawLine(QPointF(9.5 * s, 3.5 * s), QPointF(14.5 * s, 3.5 * s))
    painter.drawLine(QPointF(10.3 * s, 3.8 * s), QPointF(10.3 * s, 9.5 * s))
    painter.drawLine(QPointF(13.7 * s, 3.8 * s), QPointF(13.7 * s, 9.5 * s))
    path = QPainterPath(QPointF(10.3 * s, 9.5 * s))
    path.lineTo(5.5 * s, 18.3 * s)
    path.cubicTo(5.0 * s, 19.4 * s, 5.7 * s, 20.5 * s, 7.0 * s, 20.5 * s)
    path.lineTo(17.0 * s, 20.5 * s)
    path.cubicTo(18.3 * s, 20.5 * s, 19.0 * s, 19.4 * s, 18.5 * s, 18.3 * s)
    path.lineTo(13.7 * s, 9.5 * s)
    painter.drawPath(path)
    # Уровень жидкости в колбе — чтобы силуэт читался как колба, а не как воронка.
    painter.drawLine(QPointF(7.5 * s, 16.1 * s), QPointF(16.5 * s, 16.1 * s))


def draw_magnifier(painter: QPainter, s: float) -> None:
    """Лупа — раздел «Проверка связи»."""
    painter.drawEllipse(QRectF(4.0 * s, 4.0 * s, 11.5 * s, 11.5 * s))
    painter.drawLine(QPointF(16.6 * s, 16.6 * s), QPointF(20.5 * s, 20.5 * s))


def draw_download(painter: QPainter, s: float) -> None:
    """Стрелка вниз и линия — раздел «Обновление»."""
    painter.drawLine(QPointF(12.0 * s, 3.5 * s), QPointF(12.0 * s, 15.5 * s))
    painter.drawLine(QPointF(7.8 * s, 11.3 * s), QPointF(12.0 * s, 15.5 * s))
    painter.drawLine(QPointF(16.2 * s, 11.3 * s), QPointF(12.0 * s, 15.5 * s))
    painter.drawLine(QPointF(5.0 * s, 20.0 * s), QPointF(19.0 * s, 20.0 * s))


def draw_grid(painter: QPainter, s: float) -> None:
    """Решётка из четырёх плиток — раздел «Обзор» (дашборд)."""
    radius = 1.5 * s
    for x, y in ((4.0, 4.0), (13.0, 4.0), (4.0, 13.0), (13.0, 13.0)):
        painter.drawRoundedRect(QRectF(x * s, y * s, 7.0 * s, 7.0 * s), radius, radius)


def draw_document(painter: QPainter, s: float) -> None:
    """Документ — раздел «Логи»."""
    painter.drawRoundedRect(QRectF(5.5 * s, 3.5 * s, 13.0 * s, 17.0 * s), 2.0 * s, 2.0 * s)
    painter.drawLine(QPointF(8.5 * s, 8.5 * s), QPointF(15.5 * s, 8.5 * s))
    painter.drawLine(QPointF(8.5 * s, 12.0 * s), QPointF(15.5 * s, 12.0 * s))
    painter.drawLine(QPointF(8.5 * s, 15.5 * s), QPointF(13.0 * s, 15.5 * s))


def draw_list(painter: QPainter, s: float) -> None:
    """Документ с маркерами — раздел «Списки»."""
    painter.drawRoundedRect(QRectF(4.5 * s, 3.5 * s, 15.0 * s, 17.0 * s), 2.0 * s, 2.0 * s)
    for y in (8.0, 12.0, 16.0):
        painter.drawEllipse(QRectF(6.8 * s, (y - 0.95) * s, 1.9 * s, 1.9 * s))
        painter.drawLine(QPointF(10.6 * s, y * s), QPointF(17.2 * s, y * s))


def draw_gear(painter: QPainter, s: float) -> None:
    """Шестерёнка — раздел «Настройки»."""
    painter.drawEllipse(QRectF(8.4 * s, 8.4 * s, 7.2 * s, 7.2 * s))
    # Восемь лучей-зубьев: короткие штрихи от центра наружу.
    center = QPointF(12.0 * s, 12.0 * s)
    for dx, dy in (
        (1.0, 0.0),
        (-1.0, 0.0),
        (0.0, 1.0),
        (0.0, -1.0),
        (0.71, 0.71),
        (-0.71, 0.71),
        (0.71, -0.71),
        (-0.71, -0.71),
    ):
        painter.drawLine(
            QPointF(center.x() + dx * 6.0 * s, center.y() + dy * 6.0 * s),
            QPointF(center.x() + dx * 8.6 * s, center.y() + dy * 8.6 * s),
        )


def draw_globe(painter: QPainter, s: float) -> None:
    """Глобус — раздел «Обход (WARP)» (глобальная сеть Cloudflare)."""
    painter.drawEllipse(QRectF(3.5 * s, 3.5 * s, 17.0 * s, 17.0 * s))
    # Меридиан: эллипс уже круга — так шар читается объёмным.
    painter.drawEllipse(QRectF(8.6 * s, 3.5 * s, 6.8 * s, 17.0 * s))
    # Параллели: экватор и две симметричные линии.
    painter.drawLine(QPointF(3.5 * s, 12.0 * s), QPointF(20.5 * s, 12.0 * s))
    painter.drawLine(QPointF(5.4 * s, 7.4 * s), QPointF(18.6 * s, 7.4 * s))
    painter.drawLine(QPointF(5.4 * s, 16.6 * s), QPointF(18.6 * s, 16.6 * s))


#: Имя иконки -> функция рисования. Ключи совпадают с ключами иконок разделов
#: в :mod:`gui.sidebar`.
DRAWERS = {
    "grid": draw_grid,
    "shield": draw_shield,
    "arrows": draw_arrows,
    "flask": draw_flask,
    "magnifier": draw_magnifier,
    "download": draw_download,
    "document": draw_document,
    "list": draw_list,
    "gear": draw_gear,
    "globe": draw_globe,
}


# ---------------------------------------------------------------------------
#  Публичные функции
# ---------------------------------------------------------------------------
def draw_icon_pixmap(name: str, color: str, size: int = 24) -> QPixmap:
    """Рисует иконку ``name`` цветом ``color`` и возвращает pixmap ``size`` x ``size``.

    Неизвестное имя — пустая (прозрачная) иконка: интерфейс не должен падать
    из-за опечатки в имени.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    drawer = DRAWERS.get(name)
    if drawer is None:
        return pixmap
    painter = _painter(pixmap, color)
    drawer(painter, size / 24.0)
    painter.end()
    return pixmap


def make_icon(name: str, size: int = 24, color: str = "") -> QIcon:
    """Иконка раздела приложения (обзор, щит, колба, лупа и т. д.).

    Порядок аргументов — «имя, размер, цвет»: так удобнее вызывать из
    страниц, где размер известен заранее, а цвет берётся из темы.
    Пустой цвет означает приглушённый цвет активной темы.
    """
    return QIcon(make_pixmap(name, size, color or Theme.color("muted")))


def make_pixmap(name: str, size: int = 24, color: str = "") -> QPixmap:
    """Растровая иконка ``size`` x ``size`` цветом ``color``.

    Нужна там, где иконка ставится на ``QLabel`` (крупная иконка щита на
    плитке дашборда): ``QLabel`` принимает pixmap, а не ``QIcon``.
    """
    return draw_icon_pixmap(name, color or Theme.color("muted"), size)


def make_app_icon(color: str = "#4d6bfe", letter: str = APP_LETTER, size: int = 64) -> QIcon:
    """Иконка приложения и трея: круг с буквой ``Z``.

    Цвет берётся из темы в момент вызова: иконку в трее при смене темы
    перерисовывать не нужно (трей — системная область).
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    margin = size * 0.07
    body = QRectF(margin, margin, size - margin * 2, size - margin * 2)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(Theme.color(color, color) if color in Theme.colors() else color))
    painter.drawEllipse(body)

    # Тёмная полупрозрачная окантовка, чтобы иконка читалась на светлом фоне
    # панели задач.
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 90), max(1.0, size * 0.03)))
    painter.drawEllipse(body)

    font = QFont("Segoe UI")
    font.setPointSizeF(size * 0.42)
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#ffffff")))
    painter.drawText(body, Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()

    return QIcon(pixmap)


def window_icon() -> QIcon:
    """Иконка окна приложения (акцентный цвет активной темы)."""
    return make_app_icon(Theme.color("accent"), APP_LETTER, 64)
