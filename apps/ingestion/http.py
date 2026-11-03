"""Вежливый HTTP-клиент для сбора (README §4, шаг 1): robots.txt, паузы, user-agent бота.

Один клиент — на один запуск сбора: паузы между запросами к одному хосту держатся внутри
запуска, а параллельные запуски по одному источнику исключены блокировкой в tasks.py.
"""

import time
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.core.cache import cache

from .robots import Robots

ROBOTS_TTL = 24 * 60 * 60
ROBOTS_ERROR_TTL = 60 * 60


def _site(host: str) -> str:
    """Грубо «сайт» хоста: две последние части (www.idnes.cz и servis.idnes.cz → idnes.cz)."""
    return ".".join(host.lower().split(".")[-2:])


class RobotsDisallowed(Exception):
    """robots.txt запрещает адрес — не скачиваем."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    headers: httpx.Headers
    encoding: str | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


@dataclass
class PoliteClient:
    delay: float = 2.0
    timeout: float = 20.0
    transport: httpx.BaseTransport | None = None
    user_agent: str = field(default_factory=lambda: settings.TROLLTRACE["CRAWLER_USER_AGENT"])

    def __post_init__(self):
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "cs,en;q=0.5"},
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        )
        self._last_request: dict[str, float] = {}
        self.stats: Counter = Counter()

    # --- robots.txt --------------------------------------------------------------------

    def _robots(self, url: str) -> Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        key = f"ingestion:robots:{origin}"
        cached = cache.get(key)
        if cached is None:
            ttl = ROBOTS_TTL
            try:
                response = self._request(f"{origin}/robots.txt")
                if response.status_code >= 500:
                    cached, ttl = "Disallow-all", ROBOTS_ERROR_TTL  # сервер болен — не лезем
                elif response.status_code >= 400:
                    cached = ""  # robots.txt нет — ограничений нет
                else:
                    cached = response.text
            except httpx.HTTPError:
                cached, ttl = "Disallow-all", ROBOTS_ERROR_TTL
            cache.set(key, cached, ttl)
        if cached == "Disallow-all":
            return Robots(disallow_all=True)
        return Robots(cached)

    def allowed(self, url: str) -> bool:
        return self._robots(url).can_fetch(self.user_agent, url)

    # --- запросы -----------------------------------------------------------------------

    def _wait(self, host: str):
        last = self._last_request.get(host)
        if last is not None:
            pause = self.delay - (time.monotonic() - last)
            if pause > 0:
                time.sleep(pause)
        self._last_request[host] = time.monotonic()

    def _request(self, url: str, headers: dict | None = None) -> httpx.Response:
        self._wait(urlsplit(url).netloc)
        self.stats["requests"] += 1
        try:
            response = self._client.get(url, headers=headers)
        except httpx.TimeoutException:
            self.stats["timeouts"] += 1
            raise
        self.stats[f"http_{response.status_code}"] += 1
        if _site(response.url.host) != _site(urlsplit(url).hostname or ""):
            self.stats["redirect_offsite"] += 1  # согласие с cookies, антибот, смена домена
        return response

    def get(self, url: str, headers: dict | None = None) -> FetchResult:
        if not self.allowed(url):
            self.stats["robots_disallowed"] += 1
            raise RobotsDisallowed(url)
        started = time.monotonic()
        response = self._request(url, headers)
        return FetchResult(
            url=url,
            final_url=str(response.url),
            status=response.status_code,
            content=response.content,
            headers=response.headers,
            encoding=response.encoding,
            elapsed=time.monotonic() - started,
        )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
