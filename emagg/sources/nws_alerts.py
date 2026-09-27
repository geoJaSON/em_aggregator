"""National Weather Service active alerts (api.weather.gov). No key; requires an identifying User-Agent."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from emagg.geo import merge_polygons, simplify_geometry
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import clean_text, parse_time

API = "https://api.weather.gov/alerts/active"

SEVERITY_MAP = {
    "extreme": Severity.extreme,
    "severe": Severity.severe,
    "moderate": Severity.moderate,
    "minor": Severity.minor,
}

FLOOD_WORDS = ("flood", "hydrologic")


def categorize(event_name: str) -> Category:
    name = event_name.lower()
    if any(w in name for w in FLOOD_WORDS):
        return Category.flood
    if name.startswith("fire warning") or name.startswith("extreme fire"):
        return Category.fire
    return Category.weather


def parse_alerts(payload: dict[str, Any], exclude_events: list[str] | None = None) -> list[Event]:
    """Normalize an alerts FeatureCollection.

    Alerts that another alert in the same payload references (updates, cancellations) are dropped, and the
    newer alert records what it supersedes so the store keeps the original first-seen time.
    """
    exclude = {e.lower() for e in (exclude_events or [])}
    features = payload.get("features") or []
    referenced: set[str] = set()
    for f in features:
        for ref in (f.get("properties") or {}).get("references") or []:
            rid = ref.get("identifier") or ref.get("@id")
            if rid:
                referenced.add(rid.rsplit("/", 1)[-1])

    events = []
    for f in features:
        p = f.get("properties") or {}
        alert_id = p.get("id") or str(f.get("id", "")).rsplit("/", 1)[-1]
        if not alert_id or alert_id in referenced:
            continue
        if p.get("messageType") == "Cancel" or (p.get("status") and p.get("status") != "Actual"):
            continue
        name = p.get("event") or "Weather alert"
        if name.lower() in exclude:
            continue
        severity = SEVERITY_MAP.get(str(p.get("severity", "")).lower(), Severity.info)
        parts = [clean_text(p.get("headline")), clean_text(p.get("description")), clean_text(p.get("instruction"))]
        ugc = (p.get("geocode") or {}).get("UGC") or []
        states = sorted({code[:2] for code in ugc if isinstance(code, str) and len(code) >= 2})
        supersedes = [
            (ref.get("identifier") or ref.get("@id", "")).rsplit("/", 1)[-1] for ref in p.get("references") or []
        ]
        events.append(
            Event(
                id=alert_id,
                category=categorize(name),
                title=name,
                severity=severity,
                description="\n\n".join(x for x in parts if x) or None,
                area=clean_text(p.get("areaDesc")),
                geometry=f.get("geometry"),
                starts_at=parse_time(p.get("onset") or p.get("effective")),
                updated_at=parse_time(p.get("sent")),
                expires_at=parse_time(p.get("ends") or p.get("expires")),
                url=f.get("id") if str(f.get("id", "")).startswith("http") else None,
                states=states,
                metrics={
                    "event": name,
                    "nws_severity": p.get("severity"),
                    "urgency": p.get("urgency"),
                    "certainty": p.get("certainty"),
                    "sender": p.get("senderName"),
                    "message_type": p.get("messageType"),
                    "affected_zones": p.get("affectedZones") or [],
                },
                supersedes=[s for s in supersedes if s],
            )
        )
    return events


@register
class NWSAlerts(Source):
    type = "nws_alerts"
    default_name = "NWS alerts"
    category = Category.weather
    default_interval = 60
    # Zone outlines fetched per poll for alerts issued by zone; the cache fills over a few polls nationally.
    DEFAULT_ZONE_BUDGET = 120

    ZONE_TTL = timedelta(days=30)

    async def fetch(self) -> list[Event]:
        params = {"status": "actual"}
        states = self.cfg.states or self.ctx.area.states
        if states:
            params["area"] = ",".join(states)
        payload = await self.get_json(API, params=params, headers={"Accept": "application/geo+json"})
        events = parse_alerts(payload, self.options.get("exclude_events"))
        min_sev = self.options.get("min_severity")
        if min_sev:
            floor = Severity(min_sev).rank
            events = [e for e in events if e.severity.rank >= floor]
        if self.options.get("resolve_zones", True):
            await self._attach_zone_geometry(events)
        for e in events:
            e.metrics.pop("affected_zones", None)
        return events

    async def _attach_zone_geometry(self, events: list[Event]) -> None:
        """Many alerts are issued by zone/county with no polygon; draw them from the zone outlines."""
        store = self.ctx.store
        needed: list[str] = []
        for e in events:
            if e.geometry is None:
                for z in e.metrics.get("affected_zones", []):
                    if z not in needed and store.kv_get("nwszone:" + z, self.ZONE_TTL) is None:
                        needed.append(z)
        budget = int(self.options.get("max_zone_fetch", self.DEFAULT_ZONE_BUDGET))
        sem = asyncio.Semaphore(6)

        async def load(url: str) -> None:
            async with sem:
                try:
                    data = await self.get_json(url, headers={"Accept": "application/geo+json"})
                except Exception:
                    return
                geom = simplify_geometry(data.get("geometry"), tolerance=0.004, ndigits=4)
                # Cache misses too (as {}), so a zone without geometry is not re-fetched every minute.
                store.kv_set("nwszone:" + url, geom or {})

        await asyncio.gather(*(load(u) for u in needed[:budget]))
        for e in events:
            if e.geometry is None:
                geoms = [store.kv_get("nwszone:" + z) for z in e.metrics.get("affected_zones", [])]
                e.geometry = merge_polygons(g for g in geoms if g)
