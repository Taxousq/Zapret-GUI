"""Страницы разделов приложения.

Каждый раздел бокового меню — отдельная страница внутри ``QStackedWidget``
главного окна. Страницы не знают друг о друге: они общаются сигналами, а
фоновые операции запускает главное окно.
"""

from __future__ import annotations

from gui.pages.base import Page, WindowApi
from gui.pages.health_page import HealthPage
from gui.pages.lists_page import ListsPage
from gui.pages.logs_page import LogsPage
from gui.pages.overview_page import OverviewPage
from gui.pages.service_page import ServicePage
from gui.pages.settings_page import SettingsPage
from gui.pages.strategies_page import StrategiesPage
from gui.pages.testing_page import TestingPage
from gui.pages.update_page import UpdatePage
from gui.pages.warp_page import WarpPage

__all__ = [
    "HealthPage",
    "ListsPage",
    "LogsPage",
    "OverviewPage",
    "Page",
    "ServicePage",
    "SettingsPage",
    "StrategiesPage",
    "TestingPage",
    "UpdatePage",
    "WarpPage",
    "WindowApi",
]
