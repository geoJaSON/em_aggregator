"""Current wildland fire incidents from NIFC's WFIGS ArcGIS service (IRWIN-derived). No key."""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.sources.mapped import query_arcgis
from emagg.util import clean_text, num, parse_time

LAYER = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/WFIGS_Incident_Locations_Current/FeatureServer/0"


def acres_severity(acres: float | None, contained: float | None) -> Severity:
    if contained is not None and contained >= 100:
        return Severity.minor
    if acres is None:
        return Severity.minor
    if acres >= 10000:
        return Severity.extreme
    if acres >= 1000:
        return Severity.severe
    if acres >= 100:
        return Severity.moderate
    return Severity.minor


def parse_fires(features: list[dict[str, Any]]) -> list[Event]:
    events = []
    for f in features:
        p = f.get("properties") or {}
        if p.get("FireOutDateTime"):
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        eid = p.get("IrwinID") or p.get("UniqueFireIdentifier") or f.get("id")
        if not eid or len(coords) < 2:
            continue
        name = clean_text(p.get("IncidentName")) or "Unnamed"
        acres = num(p.get("IncidentSize"))
        if acres is None:
            acres = num(p.get("CalculatedAcres")) or num(p.get("DailyAcres"))
        contained = num(p.get("PercentContained"))
        bits = []
        if acres is not None:
            bits.append(f"{acres:,.0f} ac")
        if contained is not None:
            bits.append(f"{contained:.0f}% contained")
        kind = "Complex" if p.get("IncidentTypeCategory") == "CX" else "Fire"
        title = f"{name.title()} {kind}" + (f" ({', '.join(bits)})" if bits else "")
        state = str(p.get("POOState") or "").replace("US-", "")
        county = clean_text(p.get("POOCounty"))
        area = ", ".join(x for x in (f"{county} County" if county else None, state or None) if x) or None
        events.append(
            Event(
                id=str(eid).strip("{}"),
                category=Category.fire,
                title=title,
                severity=acres_severity(acres, contained),
                description=clean_text(p.get("IncidentShortDescription")),
                area=area,
                geometry=point(coords[0], coords[1]),
                starts_at=parse_time(p.get("FireDiscoveryDateTime")),
                updated_at=parse_time(p.get("ModifiedOnDateTime_dt") or p.get("ModifiedOnDateTime")),
                url=None,
                metrics={
                    "acres": acres,
                    "percent_contained": contained,
                    "cause": p.get("FireCause"),
                    "incident_type": p.get("IncidentTypeCategory"),
                    "unique_fire_id": p.get("UniqueFireIdentifier"),
                },
            )
        )
    return events


@register
class NIFCWildfires(Source):
    type = "nifc_wildfires"
    default_name = "Wildfires (NIFC)"
    category = Category.fire
    default_interval = 300

    async def fetch(self) -> list[Event]:
        types = ["'WF'", "'CX'"] + (["'RX'"] if self.options.get("include_prescribed") else [])
        features = await query_arcgis(
            self,
            self.options.get("url", LAYER),
            where=f"IncidentTypeCategory IN ({','.join(types)})",
            area=self.ctx.area,
        )
        events = parse_fires(features)
        min_acres = num(self.options.get("min_acres"))
        if min_acres:
            events = [e for e in events if (e.metrics.get("acres") or 0) >= min_acres]
        return events
