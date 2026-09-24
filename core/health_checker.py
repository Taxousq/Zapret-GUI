"""Проверка доступности сайтов — своя, без встроенного тестера запрета.

Смысл проверки простой: **сайт открывается или нет**. Для этого достаточно
одного HTTP-запроса к каждому сайту из :data:`config.CHECK_SITES`; результат
описывается статусом и понятной причиной.

Статусы (:class:`CheckStatus`):

* ``OK`` — сайт ответил: любой код ниже 500, в том числе 401, 403 и 429
  (сайт доступен, но блокирует автоматические запросы) и перенаправления;
* ``FAILED`` — сервер ответил ошибкой 5xx: соединение есть, а сайт не отдаёт
  страницу;
* ``TIMEOUT`` — ответа не было дольше :data:`config.CHECK_TIMEOUT`;
* ``ERROR`` — соединение не установилось: ошибка TLS, DNS, прокси или сети.

Причина (поле :attr:`CheckResult.detail`) пишется коротко и по делу —
``HTTP 403``, ``HTTP 503``, ``Timeout 10s``, ``SSL error``,
``Connection error``, ``DNS error`` — и показывается в интерфейсе рядом со
статусом.

Сайты проверяются параллельно (:class:`~concurrent.futures.ThreadPoolExecutor`),
поэтому весь раунд занимает примерно столько же, сколько самый медленный сайт.
Проверка запускается из фонового потока (``gui.widgets.Worker``), так что
интерфейс не подвисает. Модуль ничего не знает про Qt.

Про время: ``requests`` считает таймаут отдельно для подключения и для ответа,
а при нескольких адресах у домена — ещё и для каждого адреса. Поэтому в худшем
случае проверка одного сайта может занять больше номинальных
:data:`config.CHECK_TIMEOUT` секунд; фактическое время видно в интерфейсе.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Sequence

import requests

from config import (
    CHECK_HEADERS,
    CHECK_SITES,
    CHECK_TIMEOUT,
    CHECK_USER_AGENT,
    CHECK_WORKERS,
)

log = logging.getLogger(__name__)

#: Коды ответа, при которых сайт считается доступным, хотя и блокирует
#: автоматические запросы (ботов, скрипты, «не браузер»).
BLOCKING_CODES = frozenset({401, 403, 407, 429})

#: Коды ответа, при которых запрос повторяется методом ``GET``: некоторые
#: серверы не поддерживают ``HEAD``.
GET_FALLBACK_CODES = frozenset({405, 501})

#: Сколько перенаправлений отслеживать, прежде чем признать петлю.
MAX_REDIRECTS = 5

#: Причины исключений ``requests``, означающие проблему с DNS.
DNS_ERROR_MARKERS = (
    "name or service not known",
    "getaddrinfo failed",
    "name resolution",
    "nodename nor servname",
    "no address associated with hostname",
    "temporary failure in name resolution",
    "не удалось разрешить",
)
#: Коды ошибок Windows/POSIX «имя не разрешено».
DNS_ERROR_CODES = frozenset({-2, -3, -5, -8, 11001, 11002, 11004})


class CheckStatus(Enum):
    """Результат проверки одного сайта."""

    OK = "OK"
    #: Провал: сервер ответил 5xx. Именно этот статус называется «FAIL» в GUI.
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"

    @property
    def ok(self) -> bool:
        """Считается ли сайт доступным."""
        return self is CheckStatus.OK

    @property
    def label(self) -> str:
        """Текст статуса для интерфейса."""
        return {
            CheckStatus.OK: "Доступен",
            CheckStatus.FAILED: "Недоступен",
            CheckStatus.TIMEOUT: "Таймаут",
            CheckStatus.ERROR: "Ошибка",
        }[self]


@dataclass
class CheckResult:
    """Результат проверки доступности одного сайта.

    :param response_time_ms: сколько заняла проверка (для таймаута — сколько
        ждали ответа).
    :param detail: причина словами: ``HTTP 403``, ``Timeout 10s`` и т. п.
    """

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
        """Время проверки для интерфейса (``—``, если оно неизвестно)."""
        return f"{self.response_time_ms} мс" if self.response_time_ms else "—"

    def tooltip(self) -> str:
        """Подробности для всплывающей подсказки."""
        return f"{self.name}: {self.url}\n{self.detail or self.status.label}"


class HealthChecker:
    """Проверяет список сайтов параллельно.

    :param sites: пары «имя, URL»; ``None`` — :data:`config.CHECK_SITES`.
    :param timeout: таймаут одного запроса в секундах.
    :param workers: сколько сайтов проверять одновременно.
    """

    def __init__(
        self,
        sites: Iterable[tuple[str, str]] | None = None,
        timeout: float = CHECK_TIMEOUT,
        workers: int = CHECK_WORKERS,
    ) -> None:
        self.sites: Sequence[tuple[str, str]] = tuple(sites or CHECK_SITES)
        self.timeout = float(timeout)
        self.workers = max(1, int(workers))
        # ``requests.Session`` не потокобезопасна: своя сессия на каждый поток.
        self._local = threading.local()

    # ------------------------------------------------------------------
    #  Инфраструктура
    # ------------------------------------------------------------------
    def _session(self) -> requests.Session:
        """Сессия ``requests`` текущего потока («как Chrome»)."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": CHECK_USER_AGENT,
                    **CHECK_HEADERS,
                    "Connection": "close",
                }
            )
            session.max_redirects = MAX_REDIRECTS
            self._local.session = session
        return session

    def _request(self, url: str) -> requests.Response:
        """Запрашивает страницу, не вычитывая её тело.

        Сначала идёт ``HEAD`` — он не тянет тело страницы. Если сервер метод не
        поддерживает (``405``/``501``), запрос повторяется через ``GET`` со
        ``stream=True``: тело в этом случае тоже не читается, а соединение
        закрывается сразу после получения заголовков.
        """
        session = self._session()
        response = session.head(url, timeout=self.timeout, allow_redirects=True)
        if response.status_code in GET_FALLBACK_CODES:
            response.close()
            response = session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            )
        return response

    # ------------------------------------------------------------------
    #  Проверка одного сайта
    # ------------------------------------------------------------------
    def check_site(self, name: str, url: str) -> CheckResult:
        """Проверяет один сайт. Исключения наружу не выходят.

        Любой ответ ниже 500 означает, что сайт доступен: соединение
        установилось и сервер ответил. Красный статус ставится только тогда,
        когда соединения нет (таймаут, TLS, DNS, сеть) или сервер ответил 5xx.
        """
        started = time.perf_counter()
        response: requests.Response | None = None
        try:
            response = self._request(url)
            elapsed_ms = self._elapsed_ms(started)
            status_code = response.status_code
            if status_code >= 500:
                log.info("Проверка %s: HTTP %s (ошибка сервера)", name, status_code)
                return CheckResult(
                    name=name,
                    url=url,
                    status=CheckStatus.FAILED,
                    response_time_ms=elapsed_ms,
                    detail=f"HTTP {status_code}",
                )
            if status_code in BLOCKING_CODES:
                detail = f"HTTP {status_code} (блокирует автозапросы)"
            else:
                detail = f"HTTP {status_code}"
            log.info("Проверка %s: %s за %s мс", name, detail, elapsed_ms)
            return CheckResult(
                name=name,
                url=url,
                status=CheckStatus.OK,
                response_time_ms=elapsed_ms,
                detail=detail,
            )
        except requests.exceptions.Timeout:
            log.warning("Проверка %s: нет ответа за %s с", name, self.timeout)
            return self._failure(
                name, url, started, CheckStatus.TIMEOUT, f"Timeout {self.timeout:g}s"
            )
        except requests.exceptions.ProxyError as exc:
            log.warning("Проверка %s: ошибка прокси: %s", name, exc)
            return self._failure(name, url, started, CheckStatus.ERROR, "Proxy error")
        except requests.exceptions.SSLError as exc:
            log.warning("Проверка %s: ошибка TLS: %s", name, exc)
            return self._failure(name, url, started, CheckStatus.ERROR, "SSL error")
        except requests.exceptions.TooManyRedirects as exc:
            log.warning("Проверка %s: петля перенаправлений: %s", name, exc)
            return self._failure(name, url, started, CheckStatus.ERROR, "Redirect loop")
        except requests.exceptions.ConnectionError as exc:
            reason = "DNS error" if self._is_dns_error(exc) else "Connection error"
            log.warning("Проверка %s: %s: %s", name, reason, exc)
            return self._failure(name, url, started, CheckStatus.ERROR, reason)
        except requests.RequestException as exc:
            log.warning("Проверка %s: ошибка запроса: %s", name, exc)
            return self._failure(name, url, started, CheckStatus.ERROR, "Request error")
        except Exception as exc:  # noqa: BLE001 — наружу отдаём статус, а не падение
            log.exception("Проверка %s: непредвиденная ошибка", name)
            return self._failure(
                name, url, started, CheckStatus.ERROR, type(exc).__name__
            )
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        """Сколько миллисекунд прошло с момента ``started``."""
        return int((time.perf_counter() - started) * 1000)

    def _failure(
        self,
        name: str,
        url: str,
        started: float,
        status: CheckStatus,
        detail: str,
    ) -> CheckResult:
        """Собирает результат неудачной проверки.

        В интерфейс уходит короткая и понятная причина (``Timeout 10s``,
        ``SSL error``, ``Connection error``), а подробности исключения
        остаются в журнале приложения.
        """
        return CheckResult(
            name=name,
            url=url,
            status=status,
            response_time_ms=self._elapsed_ms(started),
            detail=detail,
        )

    @staticmethod
    def _is_dns_error(exc: BaseException) -> bool:
        """Отличает ошибку разрешения имени от прочих сетевых ошибок."""
        for arg in getattr(exc, "args", ()):
            if isinstance(arg, OSError) and getattr(arg, "errno", None) in DNS_ERROR_CODES:
                return True
        message = str(exc).lower()
        return any(marker in message for marker in DNS_ERROR_MARKERS)

    # ------------------------------------------------------------------
    #  Проверка всех сайтов
    # ------------------------------------------------------------------
    def check_all(
        self,
        on_result: Callable[[CheckResult], None] | None = None,
    ) -> list[CheckResult]:
        """Проверяет все сайты параллельно.

        :param on_result: необязательный колбэк: вызывается по мере готовности
            каждого результата (из потока-исполнителя).
        :return: результаты в порядке :data:`config.CHECK_SITES`.
        """
        sites = list(self.sites)
        if not sites:
            return []

        results: dict[str, CheckResult] = {}
        with ThreadPoolExecutor(
            max_workers=min(self.workers, len(sites)),
            thread_name_prefix="health-check",
        ) as pool:
            futures = {
                pool.submit(self.check_site, name, url): (name, url)
                for name, url in sites
            }
            for future in as_completed(futures):
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
                        detail=f"{type(exc).__name__}: {str(exc)[:120]}",
                    )
                results[name] = result
                if on_result is not None:
                    try:
                        on_result(result)
                    except Exception:  # noqa: BLE001
                        log.exception("Колбэк on_result упал")

        # Возвращаем результаты в исходном порядке сайтов.
        return [results[name] for name, _ in sites if name in results]
