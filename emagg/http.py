"""The shared outbound HTTP client (server and CLI use the same settings)."""

from __future__ import annotations

import httpx

from emagg.config import Config

# Polls are also bounded by the scheduler's semaphore, so the pool never becomes the bottleneck; waiting for a
# free connection is not counted against a feed (pool=None).
MAX_CONNECTIONS = 64


def make_http_client(config: Config, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": config.app.user_agent},
        timeout=httpx.Timeout(config.app.request_timeout, pool=None),
        limits=httpx.Limits(max_connections=MAX_CONNECTIONS, max_keepalive_connections=32),
        follow_redirects=True,
        transport=transport,
    )
