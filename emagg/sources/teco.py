"""Tampa Electric (TECO) outages, from the data service behind its public outage map.

TECO's public outage map (https://outage.tecoenergy.com/, reached from tampaelectric.com/poweroutages) is fed by an
Azure Front Door endpoint in front of an Elasticsearch index of outage "geopoints". The working collectors make
two calls::

    GET  https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net/api/v1/config
    POST https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net/api/v1/outage-tiles
    Content-Type: application/json

    {"size": 10000,
     "query": {"bool": {"must": {"match_all": {}},
                        "filter": {"geo_bounding_box": {"polygonCenter": {
                            "top_left": {"lat": 31.1, "lon": -87.7},
                            "bottom_right": {"lat": 24.4, "lon": -79.9}}}}}},
     "sort": [{"updateTime": "asc"}, {"incidentId": "asc"}],
     "_source": ["updateTime", "status", "reason", "customerCount", "polygonCenter", "incidentId",
                 "polygonPointsGoogle", "estimatedTimeOfRestoration"]}

    -> {"hits": {"total": {"value": 2, "relation": "eq"},
                 "hits": [{"_index": "geopoints-prod-…", "_id": "…", "sort": [1751397908000, "A…"],
                           "_source": {"incidentId": "A202518231109", "polygonCenter": [-82.2391, 27.9646],
                                       "customerCount": 5, "estimatedTimeOfRestoration": "2025-07-01T23:20:00",
                                       "reason": "Under investigation", "status": "We're working onsite",
                                       "updateTime": "2025-07-01T19:25:08",
                                       "polygonPointsGoogle": [{"lat": 27.9648, "lng": -82.2393}, …]}}]},
        "aggregations": {"customerCountSum": {"value": 15.0}},
        "_tiles": {"generated": "…", "performance": {"totalTimeMs": 41}}}

**Evidence.** The URLs, the request body (the Florida bounding box, ``size`` 10000, the sort and the ``_source``
list) and the fields read from the response are exactly those of the working collector in codebooker/floridamap
and codebooker/AmericaMap ``proxy.py`` (2026-09). pdichone/teco-api (2025-07) independently uses the same two
calls and reads ``hits.total.value``, ``aggregations.customerCountSum.value`` and ``_tiles.generated``. The shape
of each hit is confirmed by responses of the map's previous endpoint recorded by simonw/scrape-florida-outages
(2024-10 and 2025-07; ``emagg/demo_data/teco_outage_tiles_2*.json``). That endpoint required a Basic-auth
credential copied from the map's JavaScript, so it is not used.

**The config call** returns map settings and sets the map's session cookie (``MIC-X-API-V2``), which
pdichone/teco-api sends back with the tiles request. It is made first, as the map does; the shared HTTP
client keeps the cookie. A failed config call is ignored, and the tiles call decides whether the poll succeeds.

**Totals.** ``aggregations.customerCountSum.value`` sums ``customerCount`` over every matching record, even
beyond ``size``. ``hits.total.value`` is the outage count. There is no county table and no customers-served
figure, so the catalog sets ``customers_served`` (EIA-861) to give a percentage.

**Records and ids.** Each record is one outage with the feed's ``incidentId``. A few incidents appear as several
records with the same centre and different ``customerCount`` (6 of 2,648 incidents in the 2024-10-11 Hurricane Milton
capture). Their customers add up in the aggregation. Such records are merged into one event: customers are
summed; status and reason come from the largest record; the latest ETR wins; the areas are combined.

**Geometry.** ``polygonCenter`` is an Elasticsearch geo-point, ``[lon, lat]``. ``polygonPointsGoogle`` is the
outline the map draws for the outage, as Google ``{"lat", "lng"}`` points. Both collectors above draw it as the
outage's area. In pdichone's README sample, the same outline data (its ``polygonPoints`` field) lies within about
50 m of the centre. No recorded response of the current endpoint was available, so the ``{"lat", "lng"}`` shape
comes from those two parsers. An encoded-polyline string, ``[lon, lat]`` pairs and GeoJSON are accepted as well.
The outline is used as the event geometry only when it is a plausible ring around the centre; otherwise, or with
``polygons: false``, the event is the centre point.

**Times** (``updateTime``, ``estimatedTimeOfRestoration``) are local Eastern wall-clock times without an offset.
In the recorded snapshots checked (2024-10, 2025-07), ``updateTime`` trails the UTC capture time by the EDT
offset. All records in a snapshot share it, so it is the feed's refresh time. They are read in America/New_York
(``timezone`` option). Times with an explicit offset or ``Z`` are taken as given.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from emagg import regions
from emagg.geo import decode_polyline
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.power_common import outage_point_event, utility_total_event
from emagg.util import clean_text, get_ci, num, parse_time, to_int

log = logging.getLogger(__name__)

UTILITY = "Tampa Electric"
BASE_URL = "https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net"
CONFIG_PATH = "/api/v1/config"
TILES_PATH = "/api/v1/outage-tiles"
LINK = "https://outage.tecoenergy.com/"
MAP_ORIGIN = "https://outage.tecoenergy.com"
# The collectors' Florida bounds (west, south, east, north). TECO serves Hillsborough and parts of Pasco, Pinellas
# and Polk counties, so this box filters nothing out.
FL_BBOX = (-87.7, 24.4, -79.9, 31.1)
MAX_SIZE = 10000  # Elasticsearch's default result window, and what both collectors request
SOURCE_FIELDS = [
    "updateTime",
    "status",
    "reason",
    "customerCount",
    "polygonCenter",
    "incidentId",
    "polygonPointsGoogle",
    "estimatedTimeOfRestoration",
]
DEFAULT_TZ = "America/New_York"
# An outage outline wider than this (degrees from its centre) is treated as bad data and drawn as a point.
MAX_OUTLINE_SPAN = 0.25


def request_body(
    bbox: Iterable[float] = FL_BBOX, size: int = MAX_SIZE, fields: list[str] | None = None
) -> dict[str, Any]:
    """The search the map (and the working collectors) send to ``outage-tiles``."""
    west, south, east, north = (float(v) for v in bbox)
    return {
        "size": int(size),
        "query": {
            "bool": {
                "must": {"match_all": {}},
                "filter": {
                    "geo_bounding_box": {
                        "polygonCenter": {
                            "top_left": {"lat": north, "lon": west},
                            "bottom_right": {"lat": south, "lon": east},
                        }
                    }
                },
            }
        },
        "sort": [{"updateTime": "asc"}, {"incidentId": "asc"}],
        "_source": list(fields or SOURCE_FIELDS),
    }


def zone(name: Any = None) -> ZoneInfo | None:
    try:
        return ZoneInfo(str(name or DEFAULT_TZ))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_time(value: Any, tz: ZoneInfo | None) -> datetime | None:
    """An ISO time without an offset read as local wall-clock time in ``tz``; offsets, ``Z`` and epochs as given."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return parse_time(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return parse_time(text)
    if dt.year < 2000:  # .NET-style "0001-01-01T00:00:00" placeholders mean "none"
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _lonlat(lon: Any, lat: Any) -> tuple[float, float] | None:
    x, y = num(lon), num(lat)
    if x is None or y is None or not (math.isfinite(x) and math.isfinite(y)):
        return None
    if not (-180 <= x <= 180 and -90 <= y <= 90) or (x == 0 and y == 0):
        return None
    return x, y


def center_of(value: Any) -> tuple[float, float] | None:
    """(lon, lat) of an Elasticsearch geo-point: ``[lon, lat]``, ``{"lat", "lon"}`` or ``"lat,lon"``."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _lonlat(value[0], value[1])
    if isinstance(value, dict):
        return _lonlat(get_ci(value, "lon", "lng", "longitude"), get_ci(value, "lat", "latitude"))
    if isinstance(value, str) and "," in value:
        lat, lon = value.split(",", 1)
        return _lonlat(lon, lat)
    return None


def _pair(item: Any, center: tuple[float, float] | None) -> tuple[float, float] | None:
    if isinstance(item, dict):
        return _lonlat(get_ci(item, "lng", "lon", "longitude"), get_ci(item, "lat", "latitude"))
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        a, b = _lonlat(item[0], item[1]), _lonlat(item[1], item[0])
        if a and b and center:  # [lon, lat] or [lat, lon]: whichever lies next to the centre
            return min((a, b), key=lambda p: abs(p[0] - center[0]) + abs(p[1] - center[1]))
        return a or b
    return None


def _rings(raw: Any, center: tuple[float, float] | None) -> list[list[tuple[float, float]]]:
    """Candidate outlines as lists of (lon, lat)."""
    if isinstance(raw, str):
        try:
            return [[(lon, lat) for lat, lon in decode_polyline(raw.strip())]] if raw.strip() else []
        except (IndexError, ValueError):
            return []
    if isinstance(raw, dict) and raw.get("type") in ("Polygon", "MultiPolygon"):
        coords = raw.get("coordinates") or []
        polys = [coords] if raw["type"] == "Polygon" else coords
        return [r for p in polys if isinstance(p, list) and p for r in p[:1]]  # outer rings only
    if not isinstance(raw, list) or not raw:
        return []
    if all(isinstance(r, (list, str)) and r and not isinstance(r[0], (int, float)) for r in raw):
        return [ring for r in raw for ring in _rings(r, center)]  # a list of rings
    return [raw]


def outline(raw: Any, center: tuple[float, float] | None = None) -> list[list[list[float]]]:
    """Closed GeoJSON rings from ``polygonPointsGoogle``; empty unless each is a plausible ring around ``center``."""
    out = []
    for ring in _rings(raw, center):
        pts = [p for p in (_pair(item, center) for item in ring) if p]
        if len(pts) != len(ring) or len({(round(x, 7), round(y, 7)) for x, y in pts}) < 3:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if center:
            span = max(max(abs(x - center[0]) for x in xs), max(abs(y - center[1]) for y in ys))
            pad = 0.01
            inside = min(xs) - pad <= center[0] <= max(xs) + pad and min(ys) - pad <= center[1] <= max(ys) + pad
            if span > MAX_OUTLINE_SPAN or not inside:
                continue
        coords = [[round(x, 6), round(y, 6)] for x, y in pts]
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        out.append(coords)
    return out


def _geometry(rings: list[list[list[float]]]) -> dict[str, Any] | None:
    if not rings:
        return None
    if len(rings) == 1:
        return {"type": "Polygon", "coordinates": [rings[0]]}
    return {"type": "MultiPolygon", "coordinates": [[r] for r in rings]}


def _records(payload: Any) -> list[dict[str, Any]]:
    hits = payload.get("hits") if isinstance(payload, dict) else None
    items = hits.get("hits") if isinstance(hits, dict) else None
    out = []
    for hit in items if isinstance(items, list) else []:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if isinstance(src, dict):
            out.append(src)
    return out


def total_outages(payload: Any) -> int | None:
    hits = payload.get("hits") if isinstance(payload, dict) else None
    total = hits.get("total") if isinstance(hits, dict) else None
    if isinstance(total, dict):
        return to_int(total.get("value"))
    return to_int(total)  # Elasticsearch < 7 returns a bare number


def customers_out(payload: Any) -> int | None:
    """``aggregations.customerCountSum.value`` (all matching records), when present."""
    aggs = payload.get("aggregations") if isinstance(payload, dict) else None
    agg = aggs.get("customerCountSum") if isinstance(aggs, dict) else None
    value = to_int(agg.get("value")) if isinstance(agg, dict) else None
    return value if value is not None and value >= 0 else None


def parse_outage_tiles(
    payload: Any,
    utility: str = UTILITY,
    *,
    states: Iterable[str] = ("FL",),
    customers_served: int | None = None,
    link: str | None = LINK,
    tz: ZoneInfo | None = None,
    polygons: bool = True,
    outage_points: bool = True,
    max_points: int = 5000,
) -> list[Event]:
    """A utility total plus one event per incident from an ``outage-tiles`` response. Never raises on bad records.

    A payload without a ``hits`` object gives no events (not a zero total).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), dict):
        return []
    tz = tz or zone()
    records = _records(payload)

    incidents: dict[str, dict[str, Any]] = {}
    unmapped: set[str] = set()  # incidents without a usable position still count as outages
    summed = 0
    latest: datetime | None = None
    for i, rec in enumerate(records):
        customers = max(0, to_int(get_ci(rec, "customerCount")) or 0)
        summed += customers
        updated = local_time(get_ci(rec, "updateTime"), tz)
        if updated and (latest is None or updated > latest):
            latest = updated
        center = center_of(get_ci(rec, "polygonCenter"))
        rings = outline(get_ci(rec, "polygonPointsGoogle", "polygonPoints"), center) if polygons else []
        if center is None and rings:  # no centre: the middle of the outline
            xs = [x for r in rings for x, _ in r]
            ys = [y for r in rings for _, y in r]
            center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        inc_id = clean_text(get_ci(rec, "incidentId"))
        if center is None:
            unmapped.add(inc_id or f"#{i}")
            continue  # still counted in the total
        inc_id = inc_id or f"pt-{center[1]:.4f},{center[0]:.4f}"
        etr = local_time(get_ci(rec, "estimatedTimeOfRestoration"), tz)
        cur = incidents.get(inc_id)
        if cur is None:
            incidents[inc_id] = {
                "center": center, "customers": customers, "top": customers, "rec": rec, "rings": rings,
                "etr": etr, "updated": updated,
            }
            continue
        cur["customers"] += customers
        cur["rings"] += [r for r in rings if r not in cur["rings"]]
        if customers > cur["top"]:
            cur.update(top=customers, rec=rec, center=center)
        if etr and (cur["etr"] is None or etr > cur["etr"]):
            cur["etr"] = etr
        if updated and (cur["updated"] is None or updated > cur["updated"]):
            cur["updated"] = updated

    agg = customers_out(payload)
    n_out = total_outages(payload)
    events = [
        utility_total_event(
            utility,
            agg if agg is not None else summed,
            customers_served=customers_served,
            outages=n_out if n_out is not None else len(incidents.keys() | unmapped),
            updated=latest,
            link=link,
            states=list(states),
        )
    ]
    if not outage_points:
        return events

    ranked = sorted(incidents.items(), key=lambda kv: (-kv[1]["customers"], kv[0]))
    for inc_id, inc in ranked[: max(0, int(max_points))]:
        lon, lat = inc["center"]
        rec = inc["rec"]
        ev = outage_point_event(
            utility,
            inc_id,
            lon,
            lat,
            inc["customers"],
            cause=get_ci(rec, "reason"),
            etr=inc["etr"],
            updated=inc["updated"],
            crew_status=get_ci(rec, "status"),
            link=link,
        )
        geometry = _geometry(inc["rings"])
        if geometry:
            # The scheduler only derives state/county from points, so tag the outline by its centre here.
            ev.geometry = geometry
            state, county = regions.locate(lon, lat)
            if state:
                ev.states = [state]
                ev.fips = county["fips"] if county else None
        events.append(ev)
    return events


def _bbox(value: Any) -> tuple[float, float, float, float]:
    vals = [num(v) for v in value] if isinstance(value, (list, tuple)) and len(value) == 4 else []
    if len(vals) == 4 and all(v is not None for v in vals):
        west, south, east, north = vals
        if -180 <= west < east <= 180 and -90 <= south < north <= 90:
            return west, south, east, north
    return FL_BBOX


@register
class TampaElectric(Source):
    """Tampa Electric outages (utility total + incidents). All options have working defaults::

        - id: teco_fl
          type: teco
          name: Tampa Electric
          states: [FL]
          customers_served: 849876   # EIA-861 (2024); the feed has no served count
          # url: https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net   bbox: [W, S, E, N]
          # size: 10000   max_points: 5000   polygons: true   timezone: America/New_York
    """

    type = "teco"
    default_name = UTILITY
    category = Category.power
    default_interval = 300

    def _states(self) -> list[str]:
        return list(self.cfg.states or ["FL"])

    async def fetch(self) -> list[Event]:
        opts = self.options
        base = str(opts.get("url") or BASE_URL).rstrip("/")
        headers = {"Origin": MAP_ORIGIN, "Referer": MAP_ORIGIN + "/", "Accept": "application/json, */*"}
        headers.update(opts.get("headers") or {})

        config_path = opts.get("config_path", CONFIG_PATH)
        if config_path:
            try:  # the map's first call; sets its session cookie. Its body is not needed.
                await self.ctx.http.get(base + str(config_path), headers=headers)
            except Exception as exc:  # noqa: BLE001 - best effort; the tiles call below decides
                log.debug("TECO config call failed: %s", exc)

        size = min(max(to_int(opts.get("size")) or MAX_SIZE, 1), MAX_SIZE)
        body = request_body(_bbox(opts.get("bbox")), size)
        url = base + TILES_PATH
        resp = await self.ctx.http.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from {url}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceError(f"invalid JSON from {url}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), dict):
            raise SourceError(f"no 'hits' in the response from {url}")
        if not isinstance(payload["hits"].get("hits", []), list):
            raise SourceError(f"'hits.hits' is not a list in the response from {url}")

        served = to_int(opts.get("customers_served"))
        return parse_outage_tiles(
            payload,
            self.name,
            states=self._states(),
            customers_served=served if served and served > 0 else None,
            link=str(opts.get("link") or LINK),
            tz=zone(opts.get("timezone")),
            polygons=bool(opts.get("polygons", True)),
            outage_points=bool(opts.get("outage_points", True)),
            max_points=to_int(opts.get("max_points")) or 5000,
        )
