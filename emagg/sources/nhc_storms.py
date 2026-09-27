"""National Hurricane Center active tropical cyclones (CurrentStorms.json). No key.

Storm positions are kept even when outside the area bbox by default: a hurricane 500 miles out is
exactly what an emergency manager wants to see.
"""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import clean_text, num, parse_time

FEED = "https://www.nhc.noaa.gov/CurrentStorms.json"

CLASSIFICATIONS = {
    "TD": "Tropical Depression",
    "TS": "Tropical Storm",
    "HU": "Hurricane",
    "MH": "Major Hurricane",
    "STD": "Subtropical Depression",
    "STS": "Subtropical Storm",
    "PTC": "Potential Tropical Cyclone",
    "PC": "Post-Tropical Cyclone",
    "TY": "Typhoon",
}


def saffir_simpson(wind_kt: float | None) -> int | None:
    if wind_kt is None or wind_kt < 64:
        return None
    for cat, floor in ((5, 137), (4, 113), (3, 96), (2, 83), (1, 64)):
        if wind_kt >= floor:
            return cat
    return None


def _coord(numeric: Any, text: Any) -> float | None:
    value = num(numeric)
    if value is not None:
        return value
    if isinstance(text, str) and text:
        v = num(text[:-1])
        if v is not None:
            return -v if text[-1].upper() in ("S", "W") else v
    return None


def parse_storms(payload: dict[str, Any]) -> list[Event]:
    events = []
    for s in payload.get("activeStorms") or []:
        sid = s.get("id")
        lat = _coord(s.get("latitudeNumeric"), s.get("latitude"))
        lon = _coord(s.get("longitudeNumeric"), s.get("longitude"))
        if not sid or lat is None or lon is None:
            continue
        cls = str(s.get("classification") or "").upper()
        wind_kt = num(s.get("intensity"))
        cat = saffir_simpson(wind_kt) if cls in ("HU", "MH", "TY") else None
        label = CLASSIFICATIONS.get(cls, "Tropical Cyclone")
        name = clean_text(s.get("name")) or sid.upper()
        title = f"{label} {name}"
        details = []
        if cat:
            details.append(f"Cat {cat}")
        if wind_kt is not None:
            details.append(f"{round(wind_kt * 1.15078):d} mph")
        if details:
            title += f" ({', '.join(details)})"
        if cat and cat >= 3:
            sev = Severity.extreme
        elif cls in ("HU", "MH", "TY"):
            sev = Severity.severe
        elif cls in ("TS", "STS"):
            sev = Severity.moderate
        else:
            sev = Severity.minor
        movement = None
        if s.get("movementDir") is not None and s.get("movementSpeed") is not None:
            d = s.get("movementDir")
            movement = f"{d}{'°' if num(d) is not None else ''} at {s.get('movementSpeed')} kt"
        adv = s.get("publicAdvisory") or {}
        events.append(
            Event(
                id=str(sid),
                category=Category.tropical,
                title=title,
                severity=sev,
                description=(f"Moving {movement}. " if movement else "")
                + (f"Min pressure {s.get('pressure')} mb." if s.get("pressure") else ""),
                area=f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'} {abs(lon):.1f}°{'W' if lon < 0 else 'E'}",
                geometry=point(lon, lat),
                updated_at=parse_time(s.get("lastUpdate")),
                url=adv.get("url") or "https://www.nhc.noaa.gov/",
                metrics={
                    "classification": cls,
                    "saffir_simpson": cat,
                    "wind_kt": wind_kt,
                    "pressure_mb": num(s.get("pressure")),
                    "movement": movement,
                    "advisory": adv.get("advNum"),
                },
            )
        )
    return events


@register
class NHCStorms(Source):
    type = "nhc_storms"
    default_name = "Tropical cyclones (NHC)"
    category = Category.tropical
    default_interval = 600
    default_ignore_area = True

    async def fetch(self) -> list[Event]:
        return parse_storms(await self.get_json(FEED))
