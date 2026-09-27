"""USGS real-time earthquake feed (GeoJSON summary feeds). No key."""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import clean_text, num, parse_time

FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{feed}.geojson"

# PAGER alert level (estimated impact) overrides magnitude-based severity when higher.
PAGER = {"yellow": Severity.severe, "orange": Severity.extreme, "red": Severity.extreme}


def magnitude_severity(mag: float | None) -> Severity:
    if mag is None:
        return Severity.info
    if mag >= 6:
        return Severity.extreme
    if mag >= 5:
        return Severity.severe
    if mag >= 4:
        return Severity.moderate
    return Severity.minor


def parse_quakes(payload: dict[str, Any], min_magnitude: float = 0.0) -> list[Event]:
    events = []
    for f in payload.get("features") or []:
        p = f.get("properties") or {}
        mag = num(p.get("mag"))
        if mag is not None and mag < min_magnitude:
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or not f.get("id"):
            continue
        sev = magnitude_severity(mag)
        pager = PAGER.get(str(p.get("alert") or "").lower())
        if pager and pager.rank > sev.rank:
            sev = pager
        events.append(
            Event(
                id=str(f["id"]),
                category=Category.seismic,
                title=clean_text(p.get("title")) or f"M {mag} earthquake",
                severity=sev,
                description=None,
                area=clean_text(p.get("place")),
                geometry=point(coords[0], coords[1]),
                starts_at=parse_time(p.get("time")),
                updated_at=parse_time(p.get("updated")),
                url=p.get("url"),
                metrics={
                    "magnitude": mag,
                    "depth_km": num(coords[2]) if len(coords) > 2 else None,
                    "pager_alert": p.get("alert"),
                    "felt_reports": p.get("felt"),
                    "mmi": num(p.get("mmi")),
                    "tsunami_flag": bool(p.get("tsunami")),
                },
            )
        )
    return events


@register
class USGSEarthquakes(Source):
    type = "usgs_earthquakes"
    default_name = "Earthquakes (USGS)"
    category = Category.seismic
    default_interval = 120

    async def fetch(self) -> list[Event]:
        feed = self.options.get("feed", "2.5_day")
        payload = await self.get_json(FEED.format(feed=feed))
        return parse_quakes(payload, float(self.options.get("min_magnitude", 0)))
