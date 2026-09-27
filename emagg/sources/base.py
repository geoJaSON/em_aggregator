"""Base class and registry for source adapters.

An adapter turns one upstream feed into a list of normalized ``Event`` objects. Keep the network part
(``fetch``) thin and put the interpretation in a pure ``parse`` function so it can be tested offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from emagg.config import AreaConfig, SourceConfig
from emagg.models import Category, Event
from emagg.store import Store

REGISTRY: dict[str, type["Source"]] = {}


def register(cls: type["Source"]) -> type["Source"]:
    REGISTRY[cls.type] = cls
    return cls


class SourceError(RuntimeError):
    """A problem worth showing to the operator (bad response, missing option)."""


@dataclass
class SourceContext:
    http: httpx.AsyncClient
    area: AreaConfig
    store: Store


class Source:
    type: ClassVar[str]
    default_name: ClassVar[str]
    category: ClassVar[Category]
    default_interval: ClassVar[int] = 300
    # Options that must be non-empty for the source to run (e.g. an API key).
    required_options: ClassVar[tuple[str, ...]] = ()
    # Whether events outside the area bbox are kept by default.
    default_ignore_area: ClassVar[bool] = False
    # Short note shown in the UI next to the source (e.g. "experimental").
    note: ClassVar[str | None] = None

    def __init__(self, cfg: SourceConfig, ctx: SourceContext):
        self.cfg = cfg
        self.id = cfg.id
        self.name = cfg.name or self.default_name
        self.interval = max(15, cfg.interval or self.default_interval)
        self.options: dict[str, Any] = cfg.options
        self.ctx = ctx
        self.ignore_area = cfg.ignore_area if cfg.ignore_area is not None else self.default_ignore_area
        if self.options.get("category"):
            self.category = Category(self.options["category"])

    def config_error(self) -> str | None:
        needed = list(self.required_options) + list(self.options.get("requires") or [])
        if "{api_key}" in str(self.options.get("url", "")) and "api_key" not in needed:
            needed.append("api_key")
        missing = [k for k in needed if not self.options.get(k)]
        if missing:
            return "not configured: set " + ", ".join(missing)
        return None

    def option_url(self, key: str = "url") -> str:
        """A URL option with ``{api_key}`` filled from the api_key option (usually an env var)."""
        return str(self.options[key]).replace("{api_key}", str(self.options.get("api_key") or ""))

    async def fetch(self) -> list[Event]:
        raise NotImplementedError

    # --- helpers -----------------------------------------------------------------------------------

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.ctx.http.get(url, **kwargs)
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from {_short(url)}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"invalid JSON from {_short(url)}") from exc

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "category": self.category.value,
            "interval": self.interval,
            "note": self.note,
        }


def _short(url: str) -> str:
    return url.split("?", 1)[0]
