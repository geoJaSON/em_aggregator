"""Power outages from co-op outage maps hosted by NISC (``<tenant>.outagemap.coop``).

Hundreds of electric co-ops and small public utilities use NISC's hosted outage map. The map is a
single-page app that reads two static JSON files per utility ("tenant")::

    https://outagemap-data.cloud.coop/<tenant>/Hosted_Outage_Map/summary.json   (outages, totals, regions)
    https://outagemap-data.cloud.coop/<tenant>/Hosted_Outage_Map/config.json    (map settings)

summary.json::

    {"totalServed": 47046, "lastUpdate": 1785131228367, "configurationId": 1740084366762,
     "outages": [{"id": "349902", "nbrOut": 92, "timeOff": <ms>, "estimateTime": <ms>, "cause": "WEATHER   ",
                  "comment": "...", "planned": false, "crewAssigned": true, "x": 3535, "y": 95257}],
     "regionDataSets": [{"id": "Counties", "description": "County",
                         "regions": [{"id": "Mohave", "numberOut": 92, "numberServed": 30112}]}, ...]}

**Outage locations.** ``x``/``y`` are Web Mercator (EPSG:3857) metres measured from the south-west corner of the
map extent in config.json ``mapSettings.boundaryExtent`` = ``[xmin, ymin, xmax, ymax]``::

    lon = (xmin + x) / 20037508.342789244 * 180
    lat = degrees(2 * atan(exp((ymin + y) / 6378137)) - pi / 2)

Evidence: vmanam1/az-power-outage-archive (hourly since 2026-07, decode "matches the utilities' own map markers",
and the decoded points fall inside the tenants' own region polygons, which are absolute Web Mercator) and
codebooker/AmericaMap both use ``boundaryExtent``; alecbibat/gsoc-monitor uses ``fullExtent`` the same way, so it
is the fallback. Two hand-calibrated tenants in the-roseburg-community/utility-outages back-solve to one fixed
origin per tenant (within ~1 km), which confirms plain metre offsets with no scaling.

**Regions.** ``regionDataSets`` are the tables the map shows: counties, ZIP codes, board districts or service areas,
varying by tenant. Only a county dataset (id/description mentions county/parish/borough, or the configured
``county_dataset``) becomes county events. A region's ``id`` is its label (e.g. ``"Mohave"``/``"Mohave County"``).

**Planned outages.** ``planned: true`` (or a CLPUD-style ``lifeCycleStatus`` of "Planned") does not reliably mean
scheduled maintenance: archived Trico snapshots carry it on "Pending Investigation", equipment-fault and dig-in
outages, including a 1,380-customer one. Like every evidence repo, the utility total therefore counts them; points
keep their normal severity and are marked ``metrics.planned``. ``include_planned: false`` leaves them out of the
total (the total still reports ``planned_outages``/``planned_customers_out`` either way).

Example::

    - id: nisc_samhouston_tx
      type: nisc_hosted
      name: Sam Houston Electric Coop
      states: [TX]
      tenant: samhouston
      link: https://samhouston.outagemap.coop/
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from emagg import regions
from emagg.geo import bboxes_intersect
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.power_common import county_outage_event, outage_point_event, utility_total_event
from emagg.util import clean_text, num, parse_time

BASE = "https://outagemap-data.cloud.coop"
_HALF_WORLD = 20037508.342789244  # Web Mercator x at 180 degrees
_EARTH_R = 6378137.0
# Offsets from the extent corner are at most a few hundred km; values this large are already absolute metres.
_ABSOLUTE_MIN = 5_000_000
_COUNTY_DATASET = re.compile(r"count(?:y|ies)|parish|borough", re.I)
_STATE_SUFFIX = re.compile(r"^(?P<name>.+?)\s*(?:,\s*|\s+|\()(?P<st>[A-Za-z]{2})\)?$")
# "Jackson Co", "JACKSON CO.", "Liberty Cnty": a county abbreviation, not a state (not after a comma or "(").
_COUNTY_ABBR = re.compile(r"(?<=[^\s,(])\s+(?:co|cnty|cty)\.?$", re.I)
_PLANNED = re.compile(r"\bplanned\b", re.I)
_NOT_PLANNED = re.compile(r"\b(?:un|not\s+|non-?)planned\b", re.I)


def _count(value: Any) -> int:
    """A customer count: 0 for missing, negative, non-numeric or non-finite values ("NaN", Infinity)."""
    n = num(value)
    if n is None or not math.isfinite(n) or n <= 0:
        return 0
    return int(round(n))


def _option_int(value: Any, default: int) -> int:
    n = num(value)
    return int(n) if n is not None and math.isfinite(n) and n >= 0 else default


def _when(value: Any) -> datetime | None:
    """timeOff/estimateTime/lastUpdate (epoch ms) as a datetime; None for sentinels such as 0 or -1."""
    dt = parse_time(value)
    return dt if dt is not None and dt.year >= 2000 else None


# --- coordinates ------------------------------------------------------------------------------------------------


def valid_extent(raw: Any) -> list[float] | None:
    """[xmin, ymin, xmax, ymax] as floats when it is a real Web Mercator box (finite, in range, non-empty)."""
    if isinstance(raw, dict):  # tolerate the ESRI {xmin, ymin, xmax, ymax} form
        raw = [raw.get("xmin"), raw.get("ymin"), raw.get("xmax"), raw.get("ymax")]
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    vals = [num(v) for v in raw[:4]]
    if any(v is None or not math.isfinite(v) or abs(v) > _HALF_WORLD + 1 for v in vals):
        return None
    if not (vals[2] > vals[0] and vals[3] > vals[1]):
        return None
    return vals


def extent_from_config(config: Any) -> list[float] | None:
    """[xmin, ymin, xmax, ymax] in Web Mercator metres from config.json (boundaryExtent, else fullExtent)."""
    settings = (config or {}).get("mapSettings") if isinstance(config, dict) else None
    if not isinstance(settings, dict):
        return None
    for key in ("boundaryExtent", "fullExtent"):
        extent = valid_extent(settings.get(key))
        if extent:
            return extent
    return None


def mercator_to_lonlat(mx: float, my: float) -> tuple[float, float]:
    lon = mx / _HALF_WORLD * 180.0
    lat = math.degrees(2.0 * math.atan(math.exp(my / _EARTH_R)) - math.pi / 2.0)
    return lon, lat


def xy_to_lonlat(x: Any, y: Any, extent: list[float] | None) -> tuple[float, float] | None:
    """An outage's x/y (metre offsets from the extent's south-west corner) as (lon, lat); None if implausible."""
    fx, fy = num(x), num(y)
    if fx is None or fy is None or not (math.isfinite(fx) and math.isfinite(fy)):
        return None
    extent = valid_extent(extent)
    if abs(fx) >= _ABSOLUTE_MIN:
        mx, my = fx, fy  # already absolute Web Mercator
    elif extent is None:
        return None  # offsets mean nothing without the utility's own map extent
    else:
        mx, my = extent[0] + fx, extent[1] + fy
    if abs(mx) > _HALF_WORLD + 1 or abs(my) > _HALF_WORLD + 1:
        return None
    if extent is not None:
        w, h = extent[2] - extent[0], extent[3] - extent[1]
        if not (extent[0] - w / 2 <= mx <= extent[2] + w / 2 and extent[1] - h / 2 <= my <= extent[3] + h / 2):
            return None  # far outside the utility's own map: bad record or a different origin
    try:
        lon, lat = mercator_to_lonlat(mx, my)
    except (OverflowError, ValueError):
        return None
    if not (-180.0 <= lon <= 180.0 and -85.0 <= lat <= 85.0):
        return None
    return round(lon, 6), round(lat, 6)


def extent_bbox(extent: list[float] | None) -> tuple[float, float, float, float] | None:
    extent = valid_extent(extent)
    if extent is None:
        return None
    lon0, lat0 = mercator_to_lonlat(extent[0], extent[1])
    lon1, lat1 = mercator_to_lonlat(extent[2], extent[3])
    return (min(lon0, lon1), min(lat0, lat1), max(lon0, lon1), max(lat0, lat1))


# --- outages ----------------------------------------------------------------------------------------------------


def is_planned(outage: dict[str, Any]) -> bool:
    """``planned: true``, or a CLPUD-style ``lifeCycleStatus`` naming "Planned" (not "Unplanned"/"Not Planned")."""
    if outage.get("planned") is True:
        return True
    status = str(outage.get("lifeCycleStatus") or "")
    return bool(_PLANNED.search(status)) and not _NOT_PLANNED.search(status)


def _cause(value: Any) -> str | None:
    text = clean_text(value)
    if text and text.isupper():
        text = text.title()  # "WEATHER                 " -> "Weather"
    return text


def _outage_id(o: dict[str, Any]) -> str:
    raw = o.get("id") if o.get("id") not in (None, "") else o.get("outageId")
    if raw not in (None, ""):
        text = str(raw).strip()
        # never shadow the utility total or a county event
        return f"outage-{text}" if text == "total" or text.startswith("county-") else text
    key = f"{o.get('x')}:{o.get('y')}:{o.get('timeOff')}"
    return "xy-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def parse_outages(
    summary: dict[str, Any], utility: str, extent: list[float] | None, link: str | None = None, max_points: int = 1000
) -> list[Event]:
    """One point event per located outage (largest ``max_points``). Records sharing an id are added up."""
    grouped: dict[str, list[Any]] = {}  # id -> [first record, position, customers, planned]
    for o in summary.get("outages") or []:
        if not isinstance(o, dict):
            continue
        n = _count(o.get("nbrOut"))
        if n <= 0:
            continue
        pos = xy_to_lonlat(o.get("x"), o.get("y"), extent)
        if pos is None:
            continue
        oid = _outage_id(o)
        if oid in grouped:
            grouped[oid][2] += n
            grouped[oid][3] = grouped[oid][3] or is_planned(o)
        else:
            grouped[oid] = [o, pos, n, is_planned(o)]
    updated = _when(summary.get("lastUpdate"))
    events = []
    for oid, (o, pos, n, planned) in grouped.items():
        ev = outage_point_event(
            utility, oid, pos[0], pos[1], n,
            cause=_cause(o.get("cause")), etr=_when(o.get("estimateTime")), started=_when(o.get("timeOff")),
            updated=updated, crew_status="Crew assigned" if o.get("crewAssigned") is True else None, link=link,
        )
        ev.description = clean_text(o.get("comment"))
        ev.metrics["planned"] = planned
        if planned:  # the flag is unreliable (see module doc): label it, keep the severity
            ev.title = f"Planned outage: {ev.title}"
        events.append(ev)
    events.sort(key=lambda e: -e.metrics["customers_out"])
    return events[: _option_int(max_points, 1000)]


# --- regions ----------------------------------------------------------------------------------------------------


def county_dataset(summary: dict[str, Any], dataset_id: str | None = None) -> dict[str, Any] | None:
    """The regionDataSets entry holding counties: ``dataset_id`` when given, else the first whose id or
    description names counties/parishes. ZIP, district and service-area datasets are not used."""
    sets = [d for d in summary.get("regionDataSets") or [] if isinstance(d, dict)]
    if dataset_id:
        want = str(dataset_id).strip().lower()
        return next((d for d in sets if str(d.get("id", "")).strip().lower() == want), None)
    return next((d for d in sets if _COUNTY_DATASET.search(f"{d.get('id', '')} {d.get('description', '')}")), None)


def _in_bbox(lon: float, lat: float, bb: list[float] | tuple[float, ...]) -> bool:
    return bb[0] <= lon <= bb[2] and bb[1] <= lat <= bb[3]


def _in_states(county: dict[str, Any] | None, states: list[str]) -> dict[str, Any] | None:
    return county if county and (not states or county["state"] in states) else None


def match_county(
    label: Any,
    states: list[str],
    service_bbox: tuple[float, float, float, float] | None = None,
    points: list[tuple[float, float]] | None = None,
) -> dict[str, Any] | None:
    """County record for a region label ("Mohave", "MOHAVE COUNTY", "Jackson Co", "Washington, AR", "04015")
    within the utility's states. A name found in several of those states is resolved by the utility's own outage
    points, then its map extent; if still ambiguous it is skipped rather than guessed."""
    states = [str(s).upper() for s in states if s]
    text = clean_text(label)
    if not text:
        return None
    if text.isdigit():
        return _in_states(regions.county_by_fips(text), states) if len(text) == 5 else None
    text = _COUNTY_ABBR.sub("", text)
    m = _STATE_SUFFIX.match(text)
    if m:
        st = m.group("st").upper()
        if st in regions.state_codes() and (not states or st in states):
            found = regions.find_county(st, _COUNTY_ABBR.sub("", m.group("name").strip()))
            if found:
                return found
    cands: dict[str, dict[str, Any]] = {}
    for st in states:
        if c := regions.find_county(st, text):
            cands[c["fips"]] = c
    options = list(cands.values())
    if len(options) > 1 and points:
        hit = [c for c in options if any(_in_bbox(lon, lat, c["bbox"]) for lon, lat in points)]
        options = hit or options
    if len(options) > 1 and service_bbox:
        near = [c for c in options if bboxes_intersect(tuple(c["bbox"]), service_bbox)]
        options = near or options
    return options[0] if len(options) == 1 else None


def parse_regions(
    summary: dict[str, Any],
    utility: str,
    states: list[str],
    link: str | None = None,
    dataset_id: str | None = None,
    extent: list[float] | None = None,
    points: list[tuple[float, float]] | None = None,
) -> list[Event]:
    """County events from the county region dataset (nothing when the tenant publishes no county table)."""
    ds = county_dataset(summary, dataset_id)
    if ds is None:
        return []
    service_bbox = extent_bbox(extent)
    totals: dict[str, list[Any]] = {}  # fips -> [county, out, served]; a county listed twice is added up
    for r in ds.get("regions") or []:
        if not isinstance(r, dict):
            continue
        out = _count(r.get("numberOut"))
        if out <= 0:
            continue
        county = None
        for key in ("name", "description", "id"):
            if r.get(key) not in (None, "") and (county := match_county(r.get(key), states, service_bbox, points)):
                break
        if county is None:
            continue
        row = totals.setdefault(county["fips"], [county, 0, 0])
        row[1] += out
        row[2] += _count(r.get("numberServed"))
    updated = _when(summary.get("lastUpdate"))
    return [
        county_outage_event(utility, county["state"], county, out, served or None, updated=updated, link=link)
        for county, out, served in totals.values()
    ]


# --- whole summary ----------------------------------------------------------------------------------------------


def parse_summary(
    summary: dict[str, Any],
    utility: str,
    states: list[str] | None = None,
    extent: list[float] | None = None,
    *,
    link: str | None = None,
    customers_served: int | None = None,
    include_planned: bool = True,
    counties: bool = True,
    county_dataset_id: str | None = None,
    outage_points: bool = True,
    max_points: int = 1000,
) -> list[Event]:
    """summary.json (+ the config.json extent) to a utility total, county events and outage points."""
    states = list(states or [])
    raw = [o for o in summary.get("outages") or [] if isinstance(o, dict)]
    planned = [o for o in raw if is_planned(o)]
    active = raw if include_planned else [o for o in raw if not is_planned(o)]
    out = sum(_count(o.get("nbrOut")) for o in active)
    if not raw:  # no outage list at all: fall back to the region tables (each partitions the same customers)
        sets = [d for d in summary.get("regionDataSets") or [] if isinstance(d, dict)]
        out = max(
            (sum(_count(r.get("numberOut")) for r in d.get("regions") or [] if isinstance(r, dict)) for d in sets),
            default=0,
        )
    served = _count(customers_served) or _count(summary.get("totalServed"))
    total = utility_total_event(
        utility, out, served or None, len(active) if raw else None,
        updated=_when(summary.get("lastUpdate")), link=link, states=states,
    )
    planned_out = sum(_count(o.get("nbrOut")) for o in planned)
    total.metrics["planned_outages"] = len(planned)
    total.metrics["planned_customers_out"] = planned_out
    if planned:
        what = f"{len(planned):,} outage{'s' if len(planned) != 1 else ''} flagged planned ({planned_out:,} customers)"
        note = f"Includes {what}." if include_planned else f"Excludes {what}."
        total.description = f"{total.description} {note}" if total.description else note
    events = [total]
    points = parse_outages(summary, utility, extent, link, max_points) if outage_points else []
    if counties:
        located = [tuple(p.geometry["coordinates"][:2]) for p in points if p.geometry]
        events.extend(parse_regions(summary, utility, states, link, county_dataset_id, extent, located))
    events.extend(points)
    return events


@register
class NiscHostedOutages(Source):
    type = "nisc_hosted"
    default_name = "Co-op outages (NISC)"
    category = Category.power
    default_interval = 300
    required_options = ("tenant",)

    def _url(self, name: str) -> str:
        base = str(self.options.get("base_url") or BASE).rstrip("/")
        return f"{base}/{self.options['tenant']}/Hosted_Outage_Map/{name}"

    async def fetch(self) -> list[Event]:
        headers = self.options.get("headers") or {}
        summary = await self.get_json(self._url("summary.json"), headers=headers)
        if not isinstance(summary, dict) or not any(
            k in summary for k in ("outages", "totalServed", "regionDataSets", "lastUpdate")
        ):
            raise SourceError("unexpected summary.json (no outages/totalServed/regionDataSets)")
        points = bool(self.options.get("outage_points", True))
        extent = None
        if points and any(isinstance(o, dict) and "x" in o for o in summary.get("outages") or []):
            extent = await self._extent(summary.get("configurationId"), headers)
        return parse_summary(
            summary, self.name, self.cfg.states, extent,
            link=self.options.get("link"),
            customers_served=_count(self.options.get("customers_served")) or None,
            include_planned=bool(self.options.get("include_planned", True)),
            counties=bool(self.options.get("counties", True)),
            county_dataset_id=self.options.get("county_dataset"),
            outage_points=points,
            max_points=_option_int(self.options.get("max_points"), 1000),
        )

    async def _extent(self, configuration_id: Any, headers: dict[str, Any]) -> list[float] | None:
        """The map extent from config.json, cached per published configuration. A missing config only costs
        the outage points; totals and counties still come through."""
        key = f"nisc:extent:{self.options.get('base_url') or BASE}:{self.options['tenant']}:{configuration_id}"
        cached = valid_extent(self.ctx.store.kv_get(key, max_age=timedelta(days=7)))
        if cached:
            return cached
        try:
            config = await self.get_json(self._url("config.json"), headers=headers)
        except (SourceError, httpx.HTTPError):
            return None
        extent = extent_from_config(config)
        if extent:
            self.ctx.store.kv_set(key, extent)
        return extent
