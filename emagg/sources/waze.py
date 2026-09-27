"""Waze for Cities (Connected Citizens) partner data feed.

Free for government agencies that join the program. Gives crowd-reported road closures, flooded roads,
downed trees/objects, crashes and traffic-signal outages, typically refreshed every ~2 minutes.
Configure the partner feed URL (it embeds your token) via an environment variable.
"""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import clean_text, num, parse_time

# subtype (or type when subtype is empty) -> (category, severity, label)
ALERT_RULES: dict[str, tuple[Category, Severity, str]] = {
    "ROAD_CLOSED": (Category.roads, Severity.severe, "Road closed"),
    "ROAD_CLOSED_EVENT": (Category.roads, Severity.severe, "Road closed"),
    "ROAD_CLOSED_HAZARD": (Category.roads, Severity.severe, "Road closed (hazard)"),
    "ROAD_CLOSED_CONSTRUCTION": (Category.roads, Severity.moderate, "Road closed (construction)"),
    "HAZARD_WEATHER_FLOOD": (Category.flood, Severity.severe, "Flooded road"),
    "HAZARD_WEATHER_MONSOON": (Category.flood, Severity.severe, "Flooding / monsoon"),
    "HAZARD_WEATHER_HURRICANE": (Category.weather, Severity.severe, "Hurricane conditions reported"),
    "HAZARD_WEATHER_TORNADO": (Category.weather, Severity.extreme, "Tornado reported"),
    "HAZARD_WEATHER_HAIL": (Category.weather, Severity.moderate, "Hail reported"),
    "HAZARD_WEATHER_HEAVY_SNOW": (Category.weather, Severity.moderate, "Heavy snow reported"),
    "HAZARD_WEATHER_FREEZING_RAIN": (Category.weather, Severity.moderate, "Freezing rain reported"),
    "HAZARD_ON_ROAD_ICE": (Category.roads, Severity.moderate, "Ice on road"),
    "HAZARD_ON_ROAD_OBJECT": (Category.roads, Severity.minor, "Object/debris on road"),
    "HAZARD_ON_ROAD_LANE_CLOSED": (Category.roads, Severity.minor, "Lane closed"),
    "HAZARD_ON_ROAD_TRAFFIC_LIGHT_FAULT": (Category.roads, Severity.moderate, "Traffic signal out"),
    "HAZARD_ON_ROAD_EMERGENCY_VEHICLE": (Category.roads, Severity.minor, "Emergency vehicle on road"),
    "ACCIDENT_MAJOR": (Category.roads, Severity.moderate, "Major crash"),
    "ACCIDENT": (Category.roads, Severity.minor, "Crash"),
    "ACCIDENT_MINOR": (Category.roads, Severity.minor, "Minor crash"),
}

DEFAULT_TYPES = [
    "ROAD_CLOSED",
    "ROAD_CLOSED_EVENT",
    "ROAD_CLOSED_HAZARD",
    "ROAD_CLOSED_CONSTRUCTION",
    "HAZARD_WEATHER_FLOOD",
    "HAZARD_WEATHER_MONSOON",
    "HAZARD_WEATHER_HURRICANE",
    "HAZARD_WEATHER_TORNADO",
    "HAZARD_ON_ROAD_OBJECT",
    "HAZARD_ON_ROAD_TRAFFIC_LIGHT_FAULT",
    "HAZARD_ON_ROAD_ICE",
    "ACCIDENT_MAJOR",
]


def parse_waze(
    payload: dict[str, Any],
    types: list[str] | None = None,
    min_reliability: int = 0,
    include_jams: bool = False,
    min_jam_level: int = 4,
) -> list[Event]:
    wanted = set(types or DEFAULT_TYPES)
    events = []
    for a in payload.get("alerts") or []:
        key = a.get("subtype") or a.get("type") or ""
        if key not in wanted and a.get("type") not in wanted:
            continue
        rule = ALERT_RULES.get(key) or ALERT_RULES.get(a.get("type", ""))
        if not rule:
            rule = (Category.roads, Severity.minor, key.replace("_", " ").title())
        if (num(a.get("reliability")) or 0) < min_reliability:
            continue
        loc = a.get("location") or {}
        x, y = num(loc.get("x")), num(loc.get("y"))
        uid = a.get("uuid") or a.get("id")
        if x is None or y is None or not uid:
            continue
        cat, sev, label = rule
        street, city = clean_text(a.get("street")), clean_text(a.get("city"))
        where = ", ".join(v for v in (street, city) if v)
        events.append(
            Event(
                id=str(uid),
                category=cat,
                title=f"{label}: {street or city or 'unnamed road'}",
                severity=sev,
                description=clean_text(a.get("reportDescription")),
                area=where or None,
                geometry=point(x, y),
                starts_at=parse_time(a.get("pubMillis")),
                updated_at=parse_time(a.get("pubMillis")),
                url=None,
                metrics={
                    "kind": "waze_alert",
                    "waze_type": a.get("type"),
                    "waze_subtype": a.get("subtype"),
                    "reliability": num(a.get("reliability")),
                    "confidence": num(a.get("confidence")),
                    "thumbs_up": num(a.get("nThumbsUp")),
                },
            )
        )
    if include_jams:
        for j in payload.get("jams") or []:
            level = num(j.get("level")) or 0
            line = [[num(p.get("x")), num(p.get("y"))] for p in j.get("line") or []]
            line = [p for p in line if None not in p]
            if level < min_jam_level or len(line) < 2 or not (j.get("uuid") or j.get("id")):
                continue
            street = clean_text(j.get("street"))
            events.append(
                Event(
                    id=f"jam-{j.get('uuid') or j.get('id')}",
                    category=Category.roads,
                    title=f"{'Standstill' if level >= 5 else 'Heavy'} traffic: {street or 'unnamed road'}",
                    severity=Severity.minor,
                    area=", ".join(v for v in (street, clean_text(j.get("city"))) if v) or None,
                    geometry={"type": "LineString", "coordinates": line},
                    updated_at=parse_time(j.get("pubMillis")),
                    metrics={
                        "kind": "waze_jam",
                        "level": level,
                        "delay_s": num(j.get("delay")),
                        "speed_kmh": num(j.get("speedKMH")),
                    },
                )
            )
    return events


@register
class WazeFeed(Source):
    type = "waze"
    default_name = "Waze for Cities"
    category = Category.roads
    default_interval = 120
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        payload = await self.get_json(self.option_url())
        return parse_waze(
            payload,
            types=self.options.get("types"),
            min_reliability=int(self.options.get("min_reliability", 0)),
            include_jams=bool(self.options.get("include_jams", False)),
            min_jam_level=int(self.options.get("min_jam_level", 4)),
        )
