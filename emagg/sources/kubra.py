"""Power outages from utility outage maps hosted on KUBRA Storm Center (kubra.io).

Many large US utilities publish their public outage map through Storm Center, which serves plain JSON.
To configure a utility, open its outage map with browser dev tools and find a request like::

    https://kubra.io/stormcenter/api/v1/stormcenters/<INSTANCE_ID>/views/<VIEW_ID>/currentState?preview=false

Flow (same as the open-source ``kubra`` scraper): currentState -> summary totals -> (optionally) the
outage cluster tiles, which are keyed by quadkey and descended until clusters split into outages.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from emagg.geo import (
    bbox_of,
    bboxes_intersect,
    decode_polyline,
    point,
    quadkey_to_tile,
    quadkeys_for_bbox,
    tile_bbox,
)
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text, num, parse_time, to_int

BASE = "https://kubra.io"


def customers_severity(customers: int | None) -> Severity:
    """Severity of a single outage or cluster. Capped at severe: "extreme" is reserved for utility-wide
    impact (see parse_summary) so individual outages never outrank life-safety warnings."""
    if not customers:
        return Severity.info
    if customers >= 5000:
        return Severity.severe
    if customers >= 1000:
        return Severity.moderate
    return Severity.minor


def parse_summary(summary: dict[str, Any], utility: str, customers_served: int | None = None, link: str | None = None) -> Event:
    sfd = summary.get("summaryFileData") or {}
    totals = (sfd.get("totals") or [{}])[0]
    out = to_int(totals.get("total_cust_a")) or 0
    served = customers_served or to_int(totals.get("total_cust_s"))
    outages = to_int(totals.get("total_outages"))
    pct = (out / served * 100.0) if served else num(totals.get("total_percent_cust_a"))
    if pct is not None:
        if pct >= 20:
            sev = Severity.extreme
        elif pct >= 5:
            sev = Severity.severe
        elif pct >= 1:
            sev = Severity.moderate
        else:
            sev = Severity.minor if out else Severity.info
    else:
        sev = Severity.extreme if out >= 50000 else Severity.severe if out >= 10000 else Severity.moderate if out >= 1000 else Severity.minor if out else Severity.info
    title = f"{utility}: {out:,} customers without power"
    if pct is not None and out:
        title += f" ({pct:.1f}%)"
    return Event(
        id="total",
        category=Category.power,
        title=title,
        severity=sev,
        description=f"{outages:,} active outages." if outages is not None else None,
        area=utility,
        geometry=None,
        updated_at=parse_time(sfd.get("date_generated")),
        url=link,
        metrics={
            "kind": "utility_total",
            "utility": utility,
            "customers_out": out,
            "customers_served": served,
            "percent_out": round(pct, 3) if pct is not None else None,
            "outages": outages,
        },
    )


def _text(v: Any) -> str | None:
    if isinstance(v, dict):  # localized, e.g. {"EN-US": "Weather"}
        v = next(iter(v.values()), None) if v else None
    return clean_text(v)


def parse_tile(data: dict[str, Any], quadkey: str, utility: str) -> list[Event]:
    events = []
    for item in data.get("file_data") or []:
        desc, geom = item.get("desc") or {}, item.get("geom") or {}
        geometry, lat, lon = None, None, None
        try:
            if geom.get("a"):
                rings = [[[round(x, 5), round(y, 5)] for y, x in decode_polyline(r)] for r in geom["a"] if r]
                rings = [r + [r[0]] if r and r[0] != r[-1] else r for r in rings]
                rings = [r for r in rings if len(r) >= 4]
                if rings:
                    geometry = {"type": "Polygon", "coordinates": rings}
                    bb = bbox_of(geometry)
                    lon, lat = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            if geometry is None and geom.get("p"):
                lat, lon = decode_polyline(geom["p"][0])[0]
                geometry = point(lon, lat)
        except (IndexError, ValueError, TypeError):
            continue
        if geometry is None:
            continue
        cluster = bool(desc.get("cluster"))
        customers = to_int(desc.get("cust_a")) or 0
        n_out = to_int(desc.get("n_out")) or 1
        cause = _text(desc.get("cause"))
        etr_raw = desc.get("etr")
        etr = parse_time(etr_raw)
        if cluster:
            eid = f"cluster-{quadkey}-{lat:.3f},{lon:.3f}"
            title = f"{customers:,} customers out ({n_out} outages)"
        else:
            inc = desc.get("inc_id") or item.get("id")
            eid = str(inc) if inc else f"pt-{lat:.4f},{lon:.4f}"
            title = f"{customers:,} customer{'s' if customers != 1 else ''} out" + (f" — {cause}" if cause else "")
        events.append(
            Event(
                id=eid,
                category=Category.power,
                title=title,
                severity=customers_severity(customers) if customers else Severity.minor,
                description=None,
                area=utility,
                geometry=geometry,
                starts_at=parse_time(desc.get("start_time")),
                metrics={
                    "kind": "outage_cluster" if cluster else "outage",
                    "utility": utility,
                    "customers_out": customers,
                    "outages": n_out,
                    "etr": etr.isoformat() if etr else (clean_text(etr_raw) if etr_raw and "NULL" not in str(etr_raw) else None),
                    "cause": cause,
                    "crew_status": _text(desc.get("crew_status")),
                },
            )
        )
    return events


@register
class KubraOutages(Source):
    type = "kubra"
    default_name = "Utility outages"
    category = Category.power
    default_interval = 300
    required_options = ("instance_id", "view_id")

    START_ZOOM = 7
    MAX_ZOOM = 14

    @property
    def _api(self) -> str:
        return f"{BASE}/stormcenter/api/v1/stormcenters/{self.options['instance_id']}/views/{self.options['view_id']}"

    async def fetch(self) -> list[Event]:
        state = await self.get_json(f"{self._api}/currentState", params={"preview": "false"})
        data = state.get("data") or {}
        path = data.get("interval_generation_data")
        if not path:
            raise SourceError("unexpected currentState response (no interval_generation_data)")
        summary = await self.get_json(f"{BASE}/{path}/public/summary-1/data.json")
        events = [
            parse_summary(summary, self.name, to_int(self.options.get("customers_served")), self.options.get("link"))
        ]
        if self.options.get("outage_points", True) and events[0].metrics.get("customers_out"):
            events.extend(await self._outage_points(state))
        return events

    async def _outage_points(self, state: dict[str, Any]) -> list[Event]:
        store = self.ctx.store
        deployment = state.get("stormcenterDeploymentId")
        cluster_path = (state.get("data") or {}).get("cluster_interval_generation_data")
        if not deployment or not cluster_path:
            return []

        layer_key = f"kubra:layer:{self.options['instance_id']}:{deployment}"
        layer_id = store.kv_get(layer_key)
        if not layer_id:
            cfg = await self.get_json(f"{self._api}/configuration/{deployment}", params={"preview": "false"})
            layers = (((cfg.get("config") or {}).get("layers") or {}).get("data") or {}).get("interval_generation_data") or []
            layer_id = next((l.get("id") for l in layers if str(l.get("type", "")).startswith("CLUSTER_LAYER")), None)
            if not layer_id:
                raise SourceError("no cluster layer in Storm Center configuration")
            store.kv_set(layer_key, layer_id)

        bbox = await self._service_area_bbox(state)
        if bbox is None:
            return []
        area = self.ctx.area.bbox
        if area and not self.ignore_area:
            if not bboxes_intersect(bbox, area):
                return []
            bbox = (max(bbox[0], area[0]), max(bbox[1], area[1]), min(bbox[2], area[2]), min(bbox[3], area[3]))

        budget = int(self.options.get("max_tile_requests", 150))
        max_zoom = min(int(self.options.get("max_zoom", self.MAX_ZOOM)), self.MAX_ZOOM)
        level = quadkeys_for_bbox(bbox, self.START_ZOOM)
        used = 0
        results: list[Event] = []
        sem = asyncio.Semaphore(6)

        async def load(qk: str) -> tuple[str, list[Event]] | None:
            url = f"{BASE}/{cluster_path.replace('{qkh}', qk[-3:][::-1])}/public/{layer_id}/{qk}.json"
            async with sem:
                resp = await self.ctx.http.get(url)
            if resp.status_code in (403, 404):
                return None  # empty tile
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} from Storm Center tile")
            return qk, parse_tile(resp.json(), qk, self.name)

        while level:
            used += len(level)
            tiles = [t for t in await asyncio.gather(*(load(qk) for qk in level)) if t]
            next_level: list[str] = []
            for qk, evs in tiles:
                has_clusters = any(e.metrics["kind"] == "outage_cluster" for e in evs)
                children = [qk + d for d in "0123"]
                if has_clusters and len(qk) < max_zoom and used + len(next_level) + 4 <= budget:
                    next_level.extend(c for c in children if self._tile_in(c, bbox))
                else:
                    results.extend(evs)
            level = next_level
        return results

    @staticmethod
    def _tile_in(quadkey: str, bbox) -> bool:
        return bboxes_intersect(tile_bbox(*quadkey_to_tile(quadkey)), bbox)

    async def _service_area_bbox(self, state: dict[str, Any]):
        key = f"kubra:bbox:{self.options['instance_id']}:{self.options['view_id']}"
        cached = self.ctx.store.kv_get(key, max_age=timedelta(days=7))
        if cached:
            return tuple(cached)
        static = state.get("datastatic") or {}
        if not static:
            return None
        regions_key, regions = next(iter(static.items()))
        data = await self.get_json(f"{BASE}/{regions}/{regions_key}/serviceareas.json")
        pts: list[tuple[float, float]] = []
        for area in data.get("file_data") or []:
            for ring in (area.get("geom") or {}).get("a") or []:
                pts.extend(decode_polyline(ring))
        if not pts:
            return None
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        bbox = (min(lons), min(lats), max(lons), max(lats))
        self.ctx.store.kv_set(key, list(bbox))
        return bbox
