"""Work Zone Data Exchange (WZDx) feeds published by state/local DOTs.

WZDx is the USDOT standard for work zones and road restrictions (GeoJSON). Most feeds are public; a few
need an API key. Find feeds in the USDOT WZDx feed registry. Supports v4.x (``core_details``) and the
older flat v3 layout. By default only full closures that are in effect now are kept.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from emagg.geo import simplify_geometry
from emagg.models import Category, Event, Severity, utcnow
from emagg.sources.base import Source, register
from emagg.util import clean_text, parse_time

IMPACT_SEVERITY = {
    "all-lanes-closed": Severity.severe,
    "alternating-one-way": Severity.moderate,
}
IMPACT_LABEL = {
    "all-lanes-closed": "Road closed",
    "alternating-one-way": "Alternating one-way traffic",
}


def impact_from_lanes(lanes: list[dict[str, Any]] | None) -> str | None:
    """Some feeds (e.g. Kentucky) leave vehicle_impact 'unknown' and describe lanes instead. Conservative: a full
    closure only when 2+ travel lanes are listed and all are closed (feeds often list just the affected lane)."""
    travel = [lane for lane in lanes or [] if "shoulder" not in str(lane.get("type", "")) and "ramp" not in str(lane.get("type", ""))]
    closed = [lane for lane in travel if str(lane.get("status", "")).lower() == "closed"]
    if not closed:
        return None
    if len(travel) >= 2 and len(closed) == len(travel):
        return "all-lanes-closed"
    return "some-lanes-closed"


def parse_wzdx(payload: dict[str, Any], closures_only: bool = True, now: datetime | None = None) -> list[Event]:
    now = now or utcnow()
    events = []
    for i, f in enumerate(payload.get("features") or []):
        props = f.get("properties") or {}
        core = props.get("core_details") or props
        impact = str(props.get("vehicle_impact") or "unknown").lower()
        if impact == "unknown":
            impact = impact_from_lanes(props.get("lanes")) or impact
        if closures_only and impact != "all-lanes-closed":
            continue
        start, end = parse_time(props.get("start_date")), parse_time(props.get("end_date"))
        if (start and start > now) or (end and end < now):
            continue
        roads = core.get("road_names") or ([core["road_name"]] if core.get("road_name") else [])
        road = " / ".join(str(r) for r in roads) or "unnamed road"
        direction = clean_text(core.get("direction"))
        if direction and direction.lower() in ("unknown", "undefined"):
            direction = None
        label = IMPACT_LABEL.get(impact, "Lane closure" if "closed" in impact else "Work zone")
        eid = f.get("id") or props.get("road_event_id") or core.get("road_event_id") or f"idx-{i}"
        events.append(
            Event(
                id=str(eid),
                category=Category.roads,
                title=f"{label}: {road}" + (f" {direction}" if direction else ""),
                severity=IMPACT_SEVERITY.get(impact, Severity.minor),
                description=clean_text(core.get("description")),
                area=road,
                geometry=simplify_geometry(f.get("geometry"), tolerance=0.0002),
                starts_at=start,
                updated_at=parse_time(core.get("update_date") or props.get("update_date")),
                expires_at=end,
                metrics={
                    "kind": "work_zone",
                    "vehicle_impact": impact,
                    "event_type": core.get("event_type"),
                    "data_source_id": core.get("data_source_id"),
                    "beginning_milepost": props.get("beginning_milepost"),
                    "ending_milepost": props.get("ending_milepost"),
                },
            )
        )
    return events


@register
class WZDxFeed(Source):
    type = "wzdx"
    default_name = "Work zones (WZDx)"
    category = Category.roads
    default_interval = 300
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        payload = await self.get_json(self.option_url(), headers=self.options.get("headers") or {})
        return parse_wzdx(payload, closures_only=self.options.get("closures_only", True))
