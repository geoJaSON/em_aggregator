"""NOAA CO-OPS tide gauges: coastal flooding and storm surge against official flood thresholds. No key.

Each station's latest 6-minute water level is compared with its NWS coastal flood thresholds (falling back to
NOS thresholds). Thresholds are relative to station datum (STND), so levels are requested with datum=STND.
Station metadata and thresholds are cached; each poll costs one request per station in the area.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from emagg.geo import bbox_of, bboxes_intersect, point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text, num, parse_time

MDAPI = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi"
DATAGETTER = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

LEVELS = [("major", Severity.extreme), ("moderate", Severity.severe), ("minor", Severity.moderate)]


def thresholds(flood: dict[str, Any]) -> tuple[dict[str, float], str] | None:
    """Minor/moderate/major thresholds (ft above STND): NWS values when present, else NOS."""
    for prefix, label in (("nws", "NWS"), ("nos", "NOS")):
        t = {lvl: num(flood.get(f"{prefix}_{lvl}")) for lvl in ("minor", "moderate", "major")}
        if t["minor"] is not None:
            return {k: v for k, v in t.items() if v is not None}, label
    return None


def classify(level: float, t: dict[str, float], near_margin: float) -> tuple[str, Severity] | None:
    for name, sev in LEVELS:
        if name in t and level >= t[name]:
            return name, sev
    if level >= t["minor"] - near_margin:
        return "near", Severity.minor
    return None


def station_event(station: dict[str, Any], latest: dict[str, Any], flood: dict[str, Any], near_margin: float = 0.5) -> Event | None:
    th = thresholds(flood or {})
    rows = latest.get("data") or []
    if not th or not rows:
        return None
    t, source = th
    level = num(rows[-1].get("v"))
    if level is None:
        return None
    cls = classify(level, t, near_margin)
    if not cls:
        return None
    name, sev = cls
    label = {"near": "Near coastal flood stage", "minor": "Minor coastal flooding",
             "moderate": "Moderate coastal flooding", "major": "Major coastal flooding"}[name]
    st_name = clean_text(station.get("name")) or station["id"]
    obs_time = parse_time((rows[-1].get("t") or "").replace(" ", "T") + ":00Z" if rows[-1].get("t") else None)
    above_minor = round(level - t["minor"], 2)
    desc = (
        f"Water level {level:.2f} ft (station datum), {abs(above_minor):.2f} ft "
        f"{'above' if above_minor >= 0 else 'below'} minor flood threshold.\n"
        + ", ".join(f"{k} {v:.2f} ft" for k, v in t.items()) + f" ({source} thresholds)."
    )
    return Event(
        id=str(station["id"]),
        category=Category.flood,
        title=f"{label}: {st_name}",
        severity=sev,
        description=desc,
        area=", ".join(x for x in (st_name, station.get("state")) if x),
        geometry=point(station["lng"], station["lat"]),
        updated_at=obs_time,
        url=f"https://tidesandcurrents.noaa.gov/stationhome.html?id={station['id']}",
        states=[station["state"]] if station.get("state") else [],
        metrics={
            "kind": "tide_gauge",
            "flood_category": name,
            "water_level_ft_stnd": level,
            "ft_above_minor": above_minor,
            "minor_ft": t.get("minor"),
            "moderate_ft": t.get("moderate"),
            "major_ft": t.get("major"),
            "threshold_source": source,
        },
    )


@register
class COOPSWaterLevels(Source):
    type = "coops_water_levels"
    default_name = "Coastal water levels (NOAA CO-OPS)"
    category = Category.flood
    default_interval = 360  # data every 6 minutes

    async def _stations(self) -> list[dict[str, Any]]:
        key = "coops:stations"
        cached = self.ctx.store.kv_get(key, max_age=timedelta(days=1))
        if cached is None:
            data = await self.get_json(f"{MDAPI}/stations.json", params={"type": "waterlevels", "units": "english"})
            cached = [
                {"id": s.get("id"), "name": s.get("name"), "lat": s.get("lat"), "lng": s.get("lng"),
                 "state": (s.get("state") or "").upper() or None}
                for s in data.get("stations") or []
                if s.get("id") and s.get("lat") is not None and s.get("lng") is not None
            ]
            self.ctx.store.kv_set(key, cached)
        area = self.ctx.area
        out = []
        for s in cached:
            if area.states and s["state"] and s["state"] not in area.states:
                continue
            if area.bbox and not bboxes_intersect(bbox_of(point(s["lng"], s["lat"])), area.bbox):
                continue
            out.append(s)
        return out[: int(self.options.get("max_stations", 400))]

    async def _flood_levels(self, sid: str) -> dict[str, Any]:
        key = f"coops:flood:{sid}"
        cached = self.ctx.store.kv_get(key, max_age=timedelta(days=7))
        if cached is None:
            try:
                cached = await self.get_json(f"{MDAPI}/stations/{sid}/floodlevels.json", params={"units": "english"})
            except SourceError:
                cached = {}
            self.ctx.store.kv_set(key, cached)
        return cached

    async def fetch(self) -> list[Event]:
        stations = await self._stations()
        sem = asyncio.Semaphore(8)
        margin = float(self.options.get("near_margin_ft", 0.5))
        failures = 0

        async def one(s: dict[str, Any]) -> Event | None:
            nonlocal failures
            async with sem:
                flood = await self._flood_levels(s["id"])
                if not thresholds(flood or {}):
                    return None  # no official thresholds for this station
                try:
                    latest = await self.get_json(DATAGETTER, params={
                        "date": "latest", "station": s["id"], "product": "water_level", "datum": "STND",
                        "units": "english", "time_zone": "gmt", "format": "json", "application": "em_aggregator",
                    })
                except SourceError:
                    failures += 1
                    return None
            if latest.get("error"):
                return None  # station offline / no recent data
            return station_event(s, latest, flood, margin)

        results = await asyncio.gather(*(one(s) for s in stations))
        if stations and failures == len(stations):
            raise SourceError("every station request failed")
        return [e for e in results if e]
