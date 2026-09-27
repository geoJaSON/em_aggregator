"""State 511 traveler-information systems on the IBI/Arcadis "Travel-IQ" 511 platform.

On the Gulf/Southeast coast: 511LA, 511GA, DriveNC and FL511 (also NY, NV, WI, ID and others). Each issues a
developer key (free self-service for LA, GA and NC). Two API generations exist:

* v2 (default): ``/api/v2/get/event?key=K&format=json`` — dates are epoch seconds, ``IsFullClosure`` and
  ``EncodedPolyline`` present. Rate limit is ~10 calls/minute per key.
* legacy: ``/api/getevents`` — dates are ``dd/MM/yyyy HH:mm:ss`` strings, polyline in ``MapEncodedPolyline``,
  no ``IsFullClosure``.

Parsing is case-insensitive and tolerant of both.
"""

from __future__ import annotations

from typing import Any

from datetime import datetime, timezone

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


def _time(v: Any):
    t = parse_time(v)
    if t is None and isinstance(v, str) and v.strip():
        try:  # legacy API: dd/MM/yyyy HH:mm:ss
            t = datetime.strptime(v.strip(), "%d/%m/%Y %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return t


def _polyline(encoded: str) -> list[list[float]]:
    """Decode, stopping at the first out-of-range point or implausible jump (some feeds send corrupt lines)."""
    try:
        pts = decode_polyline(encoded)
    except (IndexError, ValueError):
        return []
    out: list[list[float]] = []
    for lat, lon in pts:
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            break
        if out and (abs(lon - out[-1][0]) > 2 or abs(lat - out[-1][1]) > 2):
            break
        out.append([round(lon, 6), round(lat, 6)])
    return out


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
        if eid is None or lat is None or lon is None or (lat == 0 and lon == 0):
            continue
        geometry = point(lon, lat)
        encoded = get_ci(it, "EncodedPolyline", "MapEncodedPolyline")
        if isinstance(encoded, str) and encoded:
            line = _polyline(encoded)
            if len(line) >= 2:
                geometry = {"type": "LineString", "coordinates": line}
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
                starts_at=_time(get_ci(it, "StartDate", "Reported")),
                updated_at=_time(get_ci(it, "LastUpdated")),
                expires_at=_time(get_ci(it, "PlannedEndDate")),
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

    async def fetch(self) -> list[Event]:
        base = self.options["base_url"].rstrip("/")
        path = "/api/getevents" if str(self.options.get("api_version", "v2")) == "legacy" else "/api/v2/get/event"
        payload = await self.get_json(f"{base}{path}", params={"key": self.options["api_key"], "format": "json"})
        if isinstance(payload, dict) and get_ci(payload, "error", "message") and not get_ci(payload, "events"):
            raise SourceError(f"511 API error: {get_ci(payload, 'error', 'message')}")
        return parse_511(
            payload,
            event_types=self.options.get("event_types"),
            closures_only=bool(self.options.get("closures_only", False)),
        )
