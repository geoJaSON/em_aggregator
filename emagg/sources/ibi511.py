"""State 511 traveler-information systems built on the IBI Group / Iteris "511" platform.

Several states (e.g. NY, GA, AZ, WI, ID, AK, UT, CT, LA) and provinces run this platform and expose
``/api/getevents?key=...&format=json`` (a free developer key is issued on request). Field names vary a
little between deployments, so parsing is case-insensitive and tolerant.

EXPERIMENTAL: written from the platform's published developer docs, not yet verified against a live key.
"""

from __future__ import annotations

from typing import Any

from emagg.geo import decode_polyline, point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text, get_ci, num, parse_time

TYPE_LABELS = {
    "closures": "Road closure",
    "accidentsandincidents": "Incident",
    "roadwork": "Roadwork",
    "specialevents": "Special event",
    "weatherconditions": "Weather condition",
    "generalinfo": "Info",
}

DEFAULT_TYPES = ["closures", "accidentsAndIncidents", "roadwork", "weatherConditions"]


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def parse_511(payload: Any, event_types: list[str] | None = None, closures_only: bool = False) -> list[Event]:
    items = payload if isinstance(payload, list) else (get_ci(payload or {}, "events", "Events", default=[]) or [])
    wanted = {t.lower() for t in (event_types or DEFAULT_TYPES)}
    events = []
    for it in items:
        etype = str(get_ci(it, "EventType", default="") or "")
        full_closure = _truthy(get_ci(it, "IsFullClosure", default=False))
        if etype.lower() not in wanted and not full_closure:
            continue
        if closures_only and not full_closure and etype.lower() != "closures":
            continue
        eid = get_ci(it, "ID", "Id", "EventId")
        lat, lon = num(get_ci(it, "Latitude")), num(get_ci(it, "Longitude"))
        if eid is None or lat is None or lon is None:
            continue
        geometry = point(lon, lat)
        encoded = get_ci(it, "EncodedPolyline")
        if isinstance(encoded, str) and encoded:
            try:
                pts = decode_polyline(encoded)
                if len(pts) >= 2:
                    geometry = {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for y, x in pts]}
            except (IndexError, ValueError):
                pass
        road = clean_text(get_ci(it, "RoadwayName"))
        direction = clean_text(get_ci(it, "DirectionOfTravel"))
        label = "Road closed" if full_closure else TYPE_LABELS.get(etype.lower(), etype or "Traffic event")
        sev_text = str(get_ci(it, "Severity", default="") or "").lower()
        if full_closure:
            sev = Severity.severe
        elif sev_text == "major":
            sev = Severity.moderate
        else:
            sev = Severity.minor
        events.append(
            Event(
                id=str(eid),
                category=Category.roads,
                title=f"{label}: {road or 'unnamed road'}" + (f" {direction}" if direction and direction != "None" else ""),
                severity=sev,
                description=clean_text(get_ci(it, "Description")),
                area=road,
                geometry=geometry,
                starts_at=parse_time(get_ci(it, "StartDate", "Reported")),
                updated_at=parse_time(get_ci(it, "LastUpdated")),
                expires_at=parse_time(get_ci(it, "PlannedEndDate")),
                metrics={
                    "kind": "511_event",
                    "event_type": etype,
                    "event_subtype": get_ci(it, "EventSubType"),
                    "full_closure": full_closure,
                    "lanes_affected": get_ci(it, "LanesAffected"),
                },
            )
        )
    return events


@register
class IBI511(Source):
    type = "ibi511"
    default_name = "State 511"
    category = Category.roads
    default_interval = 180
    required_options = ("base_url", "api_key")
    note = "experimental"

    async def fetch(self) -> list[Event]:
        base = self.options["base_url"].rstrip("/")
        payload = await self.get_json(
            f"{base}/api/getevents", params={"key": self.options["api_key"], "format": "json"}
        )
        if isinstance(payload, dict) and get_ci(payload, "error", "message") and not get_ci(payload, "events"):
            raise SourceError(f"511 API error: {get_ci(payload, 'error', 'message')}")
        return parse_511(
            payload,
            event_types=self.options.get("event_types"),
            closures_only=bool(self.options.get("closures_only", False)),
        )
