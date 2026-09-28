"""Polls every configured source on its own interval, with backoff, and publishes changes."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import zlib
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


BULK_KINDS = ("outage", "outage_cluster")
DEFAULT_MAX_POINTS = 1000  # per source; counties and utility totals carry the magnitude beyond that
MAX_CONCURRENT_POLLS = 48
STARTUP_SPREAD = 120  # seconds over which the first polls are spread


def tidy_power_events(events: list[Event], source_states: list[str], max_points: int) -> list[Event]:
    """Apply the power conventions every adapter shares:

    * a utility total of 0 customers is not an event (so a restored utility shows as cleared, and quiet
      utilities don't fill the list),
    * outage points located in a state the utility does not serve are dropped (bad coordinates),
    * at most ``max_points`` outage points per source, largest first.
    """
    out, points = [], []
    served = set(source_states or [])
    for e in events:
        kind = e.metrics.get("kind")
        if kind == "utility_total" and e.metrics.get("customers_out") == 0:
            continue
        if kind in BULK_KINDS:
            if served and e.states and not (set(e.states) & served):
                continue
            points.append(e)
            continue
        out.append(e)
    if len(points) > max_points:
        points.sort(key=lambda e: e.metrics.get("customers_out") or 0, reverse=True)
        log.info("keeping the %d largest of %d outage points", max_points, len(points))
        points = points[:max_points]
    return out + points


class Scheduler:
    def __init__(self, sources: list[Source], store: Store, notifier: Notifier, area: AreaConfig, retention_hours: int = 72):
        self.sources = {s.id: s for s in sources}
        self.store = store
        self.notifier = notifier
        self.area = area
        self.retention_hours = retention_hours
        self._triggers = {s.id: asyncio.Event() for s in sources}
        self._tasks: list[asyncio.Task] = []
        self._healthy: dict[str, bool] = {}
        self._last_error: dict[str, str] = {}
        self._slots: asyncio.Semaphore | None = None

    async def poll(self, src: Source) -> dict[str, Any]:
        """Poll one source once and store the result. Never raises."""
        if self._slots is None:
            self._slots = asyncio.Semaphore(MAX_CONCURRENT_POLLS)
        async with self._slots:  # acquired before the timeout clock starts
            return await self._poll(src)

    async def _poll(self, src: Source) -> dict[str, Any]:
        started = time.monotonic()
        changes = None
        try:
            events = await asyncio.wait_for(src.fetch(), timeout=min(max(60, src.interval), 300))
            for e in events:
                attribute(e, src.cfg.states)
            if not src.ignore_area:
                events = [e for e in events if in_area(e, self.area)]
            events = tidy_power_events(events, src.cfg.states, int(src.options.get("max_points", DEFAULT_MAX_POINTS)))
            changes = self.store.replace_source_events(src.id, events)
            ms = int((time.monotonic() - started) * 1000)
            self.store.record_poll(src.id, ok=True, event_count=len(events), duration_ms=ms)
            result = {"source": src.id, "ok": True, "events": len(events), "ms": ms, **changes.counts()}
            if changes:
                log.info("%s: %d events (%s)", src.id, len(events), changes.counts())
            if self._last_error.pop(src.id, None):
                log.info("%s recovered", src.id)
        except Exception as exc:  # noqa: BLE001 - a bad feed must never take the scheduler down
            ms = int((time.monotonic() - started) * 1000)
            msg = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self.store.record_poll(src.id, ok=False, error=msg, duration_ms=ms)
            # Log a failing feed when it starts failing or its error changes, not on every retry.
            if self._last_error.get(src.id) != msg:
                log.warning("%s failed: %s", src.id, msg)
            else:
                log.debug("%s still failing: %s", src.id, msg)
            self._last_error[src.id] = msg
            result = {"source": src.id, "ok": False, "error": msg, "ms": ms}
        health_changed = self._healthy.get(src.id) != result["ok"]
        self._healthy[src.id] = result["ok"]
        # Dashboards refetch on every message, so only announce polls that changed something.
        if changes or health_changed:
            self.notifier.publish({"type": "source", **result, "health_changed": health_changed, "at": utcnow().isoformat()})
        return result

    async def poll_all(self) -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(self.poll(s) for s in self.sources.values())))

    def _startup_delay(self, src: Source) -> float:
        """Spread first polls over up to two minutes, stably per source, so ~300 feeds don't fire at once."""
        spread = min(src.interval, STARTUP_SPREAD) if len(self.sources) > 20 else 2
        return (zlib.crc32(src.id.encode()) % 1000) / 1000 * spread

    async def _loop(self, src: Source) -> None:
        await asyncio.sleep(self._startup_delay(src))
        failures = 0
        trigger = self._triggers[src.id]
        while True:
            trigger.clear()
            result = await self.poll(src)
            failures = 0 if result["ok"] else failures + 1
            delay = src.interval if not failures else min(src.interval * 2 ** (failures - 1), max(src.interval, MAX_BACKOFF))
            delay *= random.uniform(0.9, 1.1)
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
