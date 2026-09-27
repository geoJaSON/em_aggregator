"""NOAA National Water Prediction Service river gauges with observed/forecast flood categories. No key."""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import clean_text, num, parse_time

API = "https://api.water.noaa.gov/nwps/v1/gauges"

# NWPS category -> (rank, label). Anything else (no_flooding, not_defined, obs_not_current, ...) is rank 0.
FLOOD_CATEGORIES = {
    "action": (1, "Action stage"),
    "minor": (2, "Minor flooding"),
    "moderate": (3, "Moderate flooding"),
    "major": (4, "Major flooding"),
}
RANK_TO_SEVERITY = {1: Severity.minor, 2: Severity.moderate, 3: Severity.severe, 4: Severity.extreme}


def _cat(block: dict[str, Any] | None) -> tuple[int, str | None]:
    raw = str((block or {}).get("floodCategory") or "").lower()
    rank, _ = FLOOD_CATEGORIES.get(raw, (0, None))
    return rank, raw or None


def _val(value) -> float | None:
    n = num(value)
    return None if n is None or n < -900 else n  # NWPS uses -999 for missing values


def _fmt(value: float | None, unit: str | None) -> str:
    return "n/a" if value is None else f"{value:g} {unit or ''}".strip()


def parse_gauges(payload: Any, min_category: str = "action", include_forecast: bool = True) -> list[Event]:
    gauges = payload.get("gauges", []) if isinstance(payload, dict) else payload or []
    floor = FLOOD_CATEGORIES.get(min_category, (1, ""))[0]
    events = []
    for g in gauges:
        status = g.get("status") or {}
        obs, fc = status.get("observed") or {}, status.get("forecast") or {}
        obs_rank, obs_raw = _cat(obs)
        fc_rank, fc_raw = _cat(fc)
        if not include_forecast:
            fc_rank = 0
        if max(obs_rank, fc_rank) < floor:
            continue
        lid = g.get("lid")
        lat, lon = num(g.get("latitude")), num(g.get("longitude"))
        if not lid or lat is None or lon is None:
            continue
        name = clean_text(g.get("name")) or lid
        if obs_rank >= floor:
            title = f"{FLOOD_CATEGORIES[obs_raw][1]}: {name}"
            if fc_rank > obs_rank:
                title += f" (forecast {FLOOD_CATEGORIES[fc_raw][1].split()[0].lower()})"
        else:
            title = f"Forecast {FLOOD_CATEGORIES[fc_raw][1].lower()}: {name}"
        obs_stage, fc_stage = _val(obs.get("primary")), _val(fc.get("primary"))
        obs_flow = _val(obs.get("secondary"))
        lines = [
            f"Observed: {_fmt(obs_stage, obs.get('primaryUnit'))}"
            + (f", {_fmt(obs_flow, obs.get('secondaryUnit'))}" if obs_flow is not None else "")
            + f" — {(obs_raw or 'unknown').replace('_', ' ')}",
        ]
        if fc_stage is not None or fc_rank:
            lines.append(
                f"Forecast: {_fmt(fc_stage, fc.get('primaryUnit'))} — {(fc_raw or 'unknown').replace('_', ' ')}"
                + (f" (valid {fc.get('validTime')})" if fc.get("validTime") else "")
            )
        state = (g.get("state") or {}).get("abbreviation") if isinstance(g.get("state"), dict) else g.get("state")
        county = clean_text(g.get("county"))
        area = ", ".join(x for x in (f"{county} County" if county else None, state) if x) or None
        events.append(
            Event(
                id=lid,
                category=Category.flood,
                title=title,
                severity=RANK_TO_SEVERITY[max(obs_rank, fc_rank)],
                description="\n".join(lines),
                area=area,
                geometry=point(lon, lat),
                updated_at=parse_time(obs.get("validTime")),
                url=f"https://water.noaa.gov/gauges/{lid.lower()}",
                states=[state] if state else [],
                metrics={
                    "kind": "river_gauge",
                    "lid": lid,
                    "usgs_id": g.get("usgsId"),
                    "observed_category": obs_raw,
                    "observed_stage": obs_stage,
                    "stage_unit": obs.get("primaryUnit"),
                    "observed_flow": obs_flow,
                    "flow_unit": obs.get("secondaryUnit"),
                    "forecast_category": fc_raw,
                    "forecast_stage": fc_stage,
                    "forecast_time": fc.get("validTime") or None,
                    "wfo": (g.get("wfo") or {}).get("abbreviation") if isinstance(g.get("wfo"), dict) else None,
                },
            )
        )
    return events


@register
class NWPSGauges(Source):
    type = "nwps_gauges"
    default_name = "River gauges (NWPS)"
    category = Category.flood
    default_interval = 600

    async def fetch(self) -> list[Event]:
        bbox = self.options.get("bbox") or self.ctx.area.bbox or (-180.0, 15.0, -60.0, 72.0)
        params = {
            "bbox.xmin": bbox[0],
            "bbox.ymin": bbox[1],
            "bbox.xmax": bbox[2],
            "bbox.ymax": bbox[3],
            "srid": "EPSG_4326",
        }
        payload = await self.get_json(API, params=params)
        return parse_gauges(
            payload,
            min_category=self.options.get("min_category", "action"),
            include_forecast=self.options.get("include_forecast", True),
        )
