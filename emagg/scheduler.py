"""Polls every configured source on its own interval, with backoff, and publishes changes."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from emagg.config import AreaConfig, SourceConfig
from emagg import regions
from emagg.geo import bbox_of, bboxes_intersect
from emagg.models import Event, utcnow
from emagg.sources import REGISTRY, Source, SourceContext
from emagg.store import Store

log = logging.getLogger("emagg.scheduler")

MAX_BACKOFF = 1800


class Notifier:
    """Fan-out of small change messages to Server-Sent-Event subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, message: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass  # a stalled client just misses messages; it re-syncs on its periodic refresh


def build_sources(configs: list[SourceConfig], ctx: SourceContext) -> tuple[list[Source], dict[str, str]]:
    """Instantiate enabled sources. Returns (sources, {source_id: reason}) for ones that cannot run."""
    sources, problems = [], {}
    for cfg in configs:
        if not cfg.enabled:
            continue
        cls = REGISTRY.get(cfg.type)
        if cls is None:
            problems[cfg.id] = f"unknown source type '{cfg.type}'"
            continue
        src = cls(cfg, ctx)
        err = src.config_error()
        if err:
            problems[cfg.id] = err
            continue
        sources.append(src)
    return sources, problems


def attribute(event: Event, source_states: list[str]) -> None:
    """Fill in states / county FIPS from the geometry when the feed did not say."""
    event.states = regions.valid_states(event.states)
    geom = event.geometry
    if geom and geom.get("type") == "Point" and (not event.states or not event.fips):
        lon, lat = geom["coordinates"][:2]
        state, county = regions.locate(lon, lat)
        if state and not event.states:
            event.states = [state]
        if county and not event.fips and state in event.states:
            event.fips = county["fips"]
    elif geom and not event.states:
        event.states = regions.states_for_geometry(geom)
    if not event.states and geom is None and source_states:
        event.states = list(source_states)


def in_area(event: Event, area: AreaConfig) -> bool:
    if area.states and event.states:
        return bool(set(event.states) & set(area.states))
    if not area.bbox or event.geometry is None:
        return True
    bb = bbox_of(event.geometry)
    return bb is None or bboxes_intersect(bb, area.bbox)


class Scheduler:
    def __init__(self, sources: list[Source], store: Store, notifier: Notifier, area: AreaConfig, retention_hours: int = 72):
        self.sources = {s.id: s for s in sources}
        self.store = store
        self.notifier = notifier
        self.area = area
        self.retention_hours = retention_hours
        self._triggers = {s.id: asyncio.Event() for s in sources}
        self._tasks: list[asyncio.Task] = []

    async def poll(self, src: Source) -> dict[str, Any]:
        """Poll one source once and store the result. Never raises."""
        started = time.monotonic()
        try:
            events = await asyncio.wait_for(src.fetch(), timeout=min(max(60, src.interval), 300))
            for e in events:
                attribute(e, src.cfg.states)
            if not src.ignore_area:
                events = [e for e in events if in_area(e, self.area)]
            changes = self.store.replace_source_events(src.id, events)
            ms = int((time.monotonic() - started) * 1000)
            self.store.record_poll(src.id, ok=True, event_count=len(events), duration_ms=ms)
            result = {"source": src.id, "ok": True, "events": len(events), "ms": ms, **changes.counts()}
            if changes:
                log.info("%s: %d events (%s)", src.id, len(events), changes.counts())
        except Exception as exc:  # noqa: BLE001 - a bad feed must never take the scheduler down
            ms = int((time.monotonic() - started) * 1000)
            msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.store.record_poll(src.id, ok=False, error=msg, duration_ms=ms)
            log.warning("%s failed: %s", src.id, msg)
            result = {"source": src.id, "ok": False, "error": msg, "ms": ms}
        self.notifier.publish({"type": "source", **result, "at": utcnow().isoformat()})
        return result

    async def poll_all(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(self.poll(s) for s in self.sources.values())))

    async def _loop(self, src: Source) -> None:
        await asyncio.sleep(random.uniform(0, 2))  # stagger start-up
        failures = 0
        trigger = self._triggers[src.id]
        while True:
            trigger.clear()
            result = await self.poll(src)
            failures = 0 if result["ok"] else failures + 1
            delay = src.interval if not failures else min(src.interval * 2 ** (failures - 1), max(src.interval, MAX_BACKOFF))
            delay *= random.uniform(0.95, 1.05)
            try:
                await asyncio.wait_for(trigger.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                self.store.prune(self.retention_hours)
            except Exception:  # noqa: BLE001
                log.exception("prune failed")

    def trigger(self, source_id: str) -> bool:
        ev = self._triggers.get(source_id)
        if ev is None:
            return False
        ev.set()
        return True

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(s), name=f"poll:{s.id}") for s in self.sources.values()]
        self._tasks.append(asyncio.create_task(self._housekeeping(), name="housekeeping"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
