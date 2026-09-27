"""NWS Local Storm Reports (spotter, EM and public reports) via the Iowa Environmental Mesonet. No key.

LSRs are ground truth: trees and power lines down, roads flooded, storm surge, tornado damage. The feed
returns reports from the last N hours, so reports age out on their own.
"""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import clean_text, num, parse_time

API = "https://mesonet.agron.iastate.edu/geojson/lsr.geojson"

# LSR type code -> (category, severity, label)
TYPES: dict[str, tuple[Category, Severity, str]] = {
    "T": (Category.weather, Severity.severe, "Tornado"),
    "1": (Category.flood, Severity.severe, "Storm surge"),
    "F": (Category.flood, Severity.severe, "Flash flooding"),
    "v": (Category.flood, Severity.moderate, "Coastal flooding"),
    "E": (Category.flood, Severity.moderate, "Flooding"),
    "R": (Category.flood, Severity.minor, "Heavy rain"),
    "0": (Category.weather, Severity.severe, "Hurricane conditions"),
    "Q": (Category.weather, Severity.moderate, "Tropical storm conditions"),
    "D": (Category.weather, Severity.moderate, "Thunderstorm wind damage"),
    "O": (Category.weather, Severity.moderate, "Wind damage"),
    "G": (Category.weather, Severity.minor, "Thunderstorm wind gust"),
    "N": (Category.weather, Severity.minor, "Wind gust"),
    "H": (Category.weather, Severity.minor, "Hail"),
}


def parse_lsr(payload: dict[str, Any], include_all: bool = False) -> list[Event]:
    events = []
    for f in payload.get("features") or []:
        p = f.get("properties") or {}
        code = str(p.get("type") or "")
        rule = TYPES.get(code)
        if rule is None and not include_all:
            continue
        cat, sev, label = rule or (Category.weather, Severity.minor, (clean_text(p.get("typetext")) or "Report").capitalize())
        lat, lon = num(p.get("lat")), num(p.get("lon"))
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if (lat is None or lon is None) and len(coords) >= 2:
            lon, lat = coords[0], coords[1]
        if lat is None or lon is None:
            continue
        mag = num(p.get("magnitude"))
        if code == "H" and mag and mag >= 2:
            sev = Severity.moderate
        if code in ("G", "N") and mag and mag >= 75:
            sev = Severity.moderate
        remark = clean_text(p.get("remark")) or ""
        if "power line" in remark.lower() or "powerline" in remark.lower():
            sev = max(sev, Severity.moderate, key=lambda s: s.rank)
        city, st = clean_text(p.get("city")), clean_text(p.get("st") or p.get("state"))
        where = ", ".join(x for x in (city, st) if x)
        mag_txt = f" ({mag:g} {p.get('unit') or ''}".rstrip() + ")" if mag else ""
        valid = p.get("valid")
        events.append(
            Event(
                id=f"{valid}|{round(lat, 4)}|{round(lon, 4)}|{code}",
                category=cat,
                title=f"{label}{mag_txt}: {where or 'unknown location'}",
                severity=sev,
                description="\n".join(x for x in (remark, f"Source: {p.get('source')}" if p.get("source") else None) if x) or None,
                area=", ".join(x for x in (clean_text(p.get("county")) and f"{clean_text(p.get('county'))} County", st) if x) or None,
                geometry=point(lon, lat),
                starts_at=parse_time(valid),
                updated_at=parse_time(valid),
                url="https://mesonet.agron.iastate.edu/lsr/",
                states=[st] if st and len(st) == 2 else [],
                metrics={
                    "kind": "storm_report",
                    "report_type": clean_text(p.get("typetext")),
                    "magnitude": mag,
                    "unit": clean_text(p.get("unit")),
                    "reported_by": clean_text(p.get("source")),
                    "wfo": p.get("wfo"),
                },
            )
        )
    return events


@register
class IEMLocalStormReports(Source):
    type = "iem_lsr"
    default_name = "Local storm reports (NWS via IEM)"
    category = Category.weather
    default_interval = 180

    async def fetch(self) -> list[Event]:
        params: dict[str, Any] = {"hours": int(self.options.get("hours", 12))}
        area = self.ctx.area
        if area.states:
            params["states"] = ",".join(area.states)
        elif area.bbox:
            params.update(west=area.bbox[0], south=area.bbox[1], east=area.bbox[2], north=area.bbox[3])
        payload = await self.get_json(API, params=params)
        return parse_lsr(payload, include_all=bool(self.options.get("include_all_types", False)))
