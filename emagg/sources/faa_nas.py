"""FAA National Airspace System status: airport closures, ground stops, ground delay programs and delays.

Public XML at nasstatus.faa.gov, no key. Airports are identified by FAA code only; coordinates come from a
bundled table (airportsdata, MIT) in emagg/data/us_airports.json.gz.
"""

from __future__ import annotations

import gzip
import json
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text

API = "https://nasstatus.faa.gov/api/airport-status-information"
AIRPORTS = Path(__file__).resolve().parent.parent / "data" / "us_airports.json.gz"


@lru_cache(maxsize=1)
def airports() -> dict[str, list]:
    with gzip.open(AIRPORTS, "rt") as f:
        return json.load(f)["airports"]


def _t(el: ET.Element | None, tag: str) -> str | None:
    return clean_text(el.findtext(tag)) if el is not None else None


def _minutes(text: str | None) -> int | None:
    """'1 hour and 24 minutes' -> 84."""
    if not text:
        return None
    hours = re.search(r"(\d+)\s*hour", text)
    mins = re.search(r"(\d+)\s*minute", text)
    if not hours and not mins:
        return None
    return int(hours.group(1)) * 60 * (1 if hours else 0) + (int(mins.group(1)) if mins else 0) if hours else int(mins.group(1))


def parse_nas(xml_text: str, include_delays: bool = True) -> list[Event]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"invalid FAA XML: {exc}") from exc
    items: list[dict[str, Any]] = []
    for dt in root.iter("Delay_type"):
        for prog in dt.findall("Ground_Stop_List/Program"):
            items.append({"kind": "ground_stop", "arpt": _t(prog, "ARPT"), "reason": _t(prog, "Reason"),
                          "until": _t(prog, "End_Time")})
        for gd in dt.findall("Ground_Delay_List/Ground_Delay"):
            items.append({"kind": "ground_delay", "arpt": _t(gd, "ARPT"), "reason": _t(gd, "Reason"),
                          "avg": _t(gd, "Avg"), "max": _t(gd, "Max")})
        for ap in dt.findall("Airport_Closure_List/Airport"):
            items.append({"kind": "airport_closure", "arpt": _t(ap, "ARPT"), "reason": _t(ap, "Reason"),
                          "start": _t(ap, "Start"), "reopen": _t(ap, "Reopen")})
        if include_delays:
            for d in dt.findall("Arrival_Departure_Delay_List/Delay"):
                ad = d.find("Arrival_Departure")
                items.append({"kind": "delay", "arpt": _t(d, "ARPT"), "reason": _t(d, "Reason"),
                              "direction": ad.get("Type") if ad is not None else None,
                              "min": _t(ad, "Min"), "max": _t(ad, "Max"), "trend": _t(ad, "Trend")})

    table = airports()
    labels = {
        "airport_closure": ("Airport closed", Severity.severe),
        "ground_stop": ("Ground stop", Severity.moderate),
        "ground_delay": ("Ground delay program", Severity.minor),
        "delay": ("Delays", Severity.info),
    }
    events: dict[str, Event] = {}
    for it in items:
        code = (it["arpt"] or "").upper()
        if not code:
            continue
        label, sev = labels[it["kind"]]
        ap = table.get(code)
        name = f"{ap[2]} ({code})" if ap else code
        if it["kind"] == "delay" and it.get("direction"):
            label = f"{it['direction']} delays"
        details = {
            "airport_closure": [f"Reopens: {it.get('reopen')}" if it.get("reopen") else None],
            "ground_stop": [f"Until: {it.get('until')}" if it.get("until") else None],
            "ground_delay": [f"Average {it.get('avg')}, max {it.get('max')}" if it.get("avg") else None],
            "delay": [f"{it.get('min')} – {it.get('max')}, {it.get('trend') or 'steady'}" if it.get("min") else None],
        }[it["kind"]]
        desc = "\n".join(x for x in [it.get("reason"), *details] if x) or None
        eid = f"{it['kind']}-{code}" + (f"-{it['direction'].lower()}" if it.get("direction") else "")
        events[eid] = Event(
            id=eid,
            category=Category.transport,
            title=f"{label}: {name}",
            severity=sev,
            description=desc,
            area=f"{ap[3]}, {ap[4]}" if ap else None,
            geometry=point(ap[1], ap[0]) if ap else None,
            url="https://nasstatus.faa.gov/",
            metrics={
                "kind": it["kind"],
                "airport": code,
                "reason": it.get("reason"),
                "avg_delay_min": _minutes(it.get("avg")),
                "max_delay_min": _minutes(it.get("max")),
                "reopen": it.get("reopen"),
            },
        )
    return list(events.values())


@register
class FAANASStatus(Source):
    type = "faa_nas"
    default_name = "Airport status (FAA)"
    category = Category.transport
    default_interval = 180

    async def fetch(self) -> list[Event]:
        resp = await self.ctx.http.get(API, headers={"Accept": "application/xml"})
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from FAA NAS status")
        return parse_nas(resp.text, include_delays=bool(self.options.get("include_delays", True)))
