"""Параллельная проверка доступности сайтов.

Каждый сайт проверяется в отдельном потоке (:class:`~concurrent.futures.
ThreadPoolExecutor`), поэтому проверка всех сайтов занимает примерно столько
же времени, сколько самая медленная из них.

Важно: для задачи «работает ли обход» доступностью считается **любой
полученный HTTP-ответ**. Даже 403 от Instagram означает, что соединение
установилось и сайт ответил — значит, блокировка обходится. Красный статус
(``FAILED``/``TIMEOUT``/``ERROR``) ставится только тогда, когда соединение
не удалось установить.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Sequence

import requests

from config import CHECK_SITES, CHECK_TIMEOUT, CHECK_USER_AGENT, CHECK_WORKERS

log = logging.getLogger(__name__)


class CheckStatus(Enum):
    """Результат проверки одного сайта."""

    OK = "OK"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"

    @property
    def ok(self) -> bool:
        return self is CheckStatus.OK

    @property
    def label(self) -> str:
        """Текст для интерфейса."""
        return {
            CheckStatus.OK: "Доступен",
            CheckStatus.FAILED: "Недоступен",
            CheckStatus.TIMEOUT: "Таймаут",
            CheckStatus.ERROR: "Ошибка",
        }[self]


@dataclass
class CheckResult:
    """Результат проверки доступности одного сайта."""

    name: str
    url: str
    status: CheckStatus
    response_time_ms: int
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status.ok

    @property
    def time_text(self) -> str:
        return f"{self.response_time_ms} мс" if self.response_time_ms else "—"


class HealthChecker:
    """Проверяет список сайтов параллельно.

    Особенность ``requests``: таймаут задаётся отдельно на подключение и на
    ожидание ответа, поэтому в худшем случае сайт может проверяться примерно
    вдвое дольше номинала (5 с на подключение + 5 с на ответ). Проверка идёт
    в фоновом потоке, поэтому интерфейс это не блокирует, а фактическое время
    видно в интерфейсе рядом с каждым сайтом.
    """

    def __init__(
        self,
        sites: Iterable[tuple[str, str]] | None = None,
        timeout: float = CHECK_TIMEOUT,
        workers: int = CHECK_WORKERS,
    ) -> None:
        self.sites: Sequence[tuple[str, str]] = tuple(sites or CHECK_SITES)
        self.timeout = timeout
        self.workers = max(1, workers)
        # requests.Session не гарантирует потокобезопасность, поэтому
        # держим по одной сессии на поток.
        self._local = threading.local()

    # -- инфраструктура ----------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": CHECK_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Connection": "close",
                }
            )
            self._local.session = session
        return session

    # -- проверка одного сайта --------------------------------------------
    def check_site(self, name: str, url: str) -> CheckResult:
        """Проверяет один сайт. Исключения не выбрасываются наружу.

        Редиректы намеренно не отслеживаются: нам важно лишь то, что сайт
        ответил. Иначе цепочка переходов умножает таймаут (5 секунд на каждый
        хоп), и проверка перестаёт укладываться в заявленный бюджет времени.
        """
        started = time.perf_counter()
        try:
            response = self._session().get(
                url,
                timeout=(self.timeout, self.timeout),
                allow_redirects=False,
                stream=True,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            status_code = response.status_code
            is_redirect = 300 <= status_code < 400
            # Закрываем соединение, не вычитывая тело целиком.
            response.close()
            detail = f"HTTP {status_code}"
            if is_redirect:
                detail += " (редирект)"
            return CheckResult(
                name=name,
                url=url,
                status=CheckStatus.OK,
                response_time_ms=elapsed_ms,
                detail=detail,
            )
        except requests.exceptions.Timeout:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CheckResult(
                name=name,
                url=url,
                status=CheckStatus.TIMEOUT,
                response_time_ms=elapsed_ms,
                detail=f"Нет ответа (таймаут {self.timeout:g} с)",
            )
        except requests.exceptions.SSLError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning("Проверка %s: ошибка TLS: %s", url, exc)
            return CheckResult(
                name=name,
                url=url,
                status=CheckStatus.FAILED,
                response_time_ms=elapsed_ms,
                detail="Ошибка TLS (возможно, соединение подменяется)",
            )
        except requests.exceptions.ConnectionError as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning("Проверка %s: соединение не установлено: %s", url, exc)
            return CheckResult(
                name=name,
                url=url,
                status=CheckStatus.FAILED,
                response_time_ms=elapsed_ms,
                detail="Соединение не установлено",
            )
        except Exception as exc:  # noqa: BLE001 — наружу отдаём статус, а не падение
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.exception("Проверка %s: непредвиденная ошибка", url)
            return CheckResult(
                name=name,
                url=url,
                status=CheckStatus.ERROR,
                response_time_ms=elapsed_ms,
                detail=f"{type(exc).__name__}: {exc}"[:160],
            )

    # -- проверка всех сайтов ---------------------------------------------
    def check_all(
        self,
        on_result: Callable[[CheckResult], None] | None = None,
    ) -> list[CheckResult]:
        """Проверяет все сайты параллельно.

        :param on_result: необязательный колбэк, вызывается по мере готовности
            каждого результата (из потока-исполнителя).
        """
        results: dict[str, CheckResult] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.workers, max(1, len(self.sites))),
            thread_name_prefix="health-check",
        ) as pool:
            futures = {
                pool.submit(self.check_site, name, url): (name, url)
                for name, url in self.sites
            }
            for future in futures:
                name, url = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001 — см. check_site
                    log.exception("Проверка %s завершилась ошибкой", url)
                    result = CheckResult(
                        name=name,
                        url=url,
                        status=CheckStatus.ERROR,
                        response_time_ms=0,
                        detail=f"{type(exc).__name__}: {exc}"[:160],
                    )
                results[name] = result
                if on_result is not None:
                    try:
                        on_result(result)
                    except Exception:  # noqa: BLE001
                        log.exception("Колбэк on_result упал")

        # Сохраняем исходный порядок сайтов.
        return [results[name] for name, _ in self.sites if name in results]
