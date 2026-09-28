"""Power outages from co-op and municipal outage maps built on Milsoft's Web Outage Viewer (WOV).

Hundreds of electric co-ops and municipal utilities run Milsoft's DisSPatch outage management system and
publish its public "Web Outage Viewer": a map at a host such as ``https://outage.<coop>.coop/``, often on a
port like 7575, 7576 or 8181. The page title is "Web Outage Viewer". The map loads three static JSON files
from ``data/`` under its root, which is all this adapter reads:

``data/outageSummary.json?v=2``, the utility total::

    {"customersOutNow": 8951, "customersServed": 37900, "updateTime": "2026-09-27T17:58:00Z"}

``data/outages.json?v=2``, one record per outage. The outage id is ``outageRecID`` (``outageRecId`` in some
installs)::

    [{"outageRecID": "2025-03-07-0445", "outageName": "TRF_1335752010",
      "outagePoint": {"lat": 31.2825, "lng": -90.2519}, "outageStartTime": "2025-03-07T13:13:02-06:00",
      "estimatedTimeOfRestoral": null, "outageEndTime": null, "verified": false, "cause": null, "code": null,
      "crewAssigned": false, "customersOutInitially": 1, "customersOutNow": 1, "customersRestored": 0,
      "streetsAffected": null, "isPlanned": false, "outageModifiedTime": "2025-03-07T13:19:14.16-06:00",
      "outageWorkStatus": ""}]

``data/boundaries.json?v=2``, the viewer's "Summary" tab. It is a list of boundary layers configured by the
utility. Each layer's ``name`` says what it holds (counties or parishes, or districts, zip codes or
substations), ``nameField`` names the attribute used as the label, and ``boundaries`` holds one row per
area::

    [{"name": "County", "nameField": "NAME",
      "boundaries": [{"name": "Allen", "customersAffected": 4, "customersOutNow": 3, "customersServed": 4100}]}]

The keys (name, nameField, boundaries; name, customersAffected, customersOutNow, customersServed) are the
ones scottarver/outage-tracker types for Beauregard Electric (2020). The layer name "County", the nameField
"NAME" and the summary's updateTime format above are not from a recorded payload. scottarver keys the groups
by their top-level name and calls them parishes, so a file may instead hold one single-row group per county;
both shapes are read.

Only a county or parish layer becomes ``county_outage_event``s, and only one layer is used, so zip-code or
district layers never double-count. A layer is treated as counties when its name says so (County, Counties,
Parish, but not "County Commission District"), or when it has no telling name and most of its rows are county
names in the source's states. Force a layer with ``boundary_layer: <name>``.

Outage points outside the source's ``states`` are dropped, so list every state the utility serves. A 403 or
an HTML page in place of outages.json fails the poll (the previous events are kept); a 404 means the viewer
has no such file.

Example (config.yaml)::

    - id: my_coop
      type: milsoft_wov
      name: Example Electric Cooperative
      states: [TX]
      url: https://outage.example.coop/      # the viewer root; the data files are read from <url>/data/
      # params: {}            # recorded without "?v=2" (default query is v=2)
      # county_reports: false  # skip boundaries.json
      # outage_points: false   # skip outages.json
      # include_planned: true  # also show planned (scheduled) outages
      # boundary_layer: County
      # max_points: 5000       # largest outages kept when a storm lists thousands

Each poll makes at most three requests, one per file, run concurrently.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any

import httpx

from emagg import regions
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.power_common import (
    county_outage_event,
    outage_point_event,
    utility_total_event,
)
from emagg.util import clean_text, num, parse_time

DATA_FILES = {"summary": "outageSummary", "outages": "outages", "boundaries": "boundaries"}

# Layers that are never counties, even when the label also says "county" ("County Commission District").
_NOT_COUNTY_LAYER = re.compile(
    r"\bzip|\bzcta|\bpostal|\bdistrict|\bsubstation|\bfeeder|\bcircuit|\bprecinct|\bwards?\b", re.IGNORECASE
)
_COUNTY_LAYER = re.compile(r"count(y|ies)|parish|borough", re.IGNORECASE)
_OTHER_LAYER = re.compile(
    r"zip|postal|district|substation|feeder|circuit|office|town|city|cities|exchange|precinct|member|service|"
    r"territory|region|area|ward|township",
    re.IGNORECASE,
)
_CO_SUFFIX = re.compile(r"\s+(co|cnty|cty)\.?$", re.IGNORECASE)
# Short forms of county names that regions.find_county does not know, per state.
_COUNTY_ALIASES = {
    ("LA", "jeff davis"): "Jefferson Davis",
    ("MS", "jeff davis"): "Jefferson Davis",
}


def _int(value: Any) -> int | None:
    """Integer or None; NaN, Infinity and non-numbers are None (Python's json accepts NaN and Infinity)."""
    n = num(value)
    return round(n) if n is not None and math.isfinite(n) else None


def _time(value: Any) -> Any:
    """The value if it parses as a time (or is not a time at all), else None. parse_time raises OverflowError on
    dates at the edge of the calendar ('9999-12-31T23:59:59-06:00'); that must drop the field, not the record."""
    try:
        parse_time(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return value


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    """First value that is present and not empty (0 counts as present)."""
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return bool(value)


# --- summary -------------------------------------------------------------------------------------------


def summary_fields(summary: Any) -> dict[str, Any]:
    """customers out / served, update time and (if given) outage count from outageSummary.json."""
    if not isinstance(summary, dict):
        return {"out": None, "served": None, "updated": None, "outages": None}
    inner = summary.get("summary") if isinstance(summary.get("summary"), dict) else {}
    src = {**inner, **{k: v for k, v in summary.items() if v is not None}}
    return {
        "out": _int(_first(src, "customersOutNow", "customersOut", "customersAffected")),
        "served": _int(_first(src, "customersServed", "totalCustomers")),
        "updated": _time(_first(src, "updateTime", "updatedTime", "lastUpdated", "lastUpdate")),
        "outages": _int(_first(src, "totalOutages", "outageCount", "numOutages", "activeOutages")),
    }


# --- outages.json --------------------------------------------------------------------------------------


def _outage_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("outages"), list):
        return payload["outages"]
    return []


def _outage_event(rec: dict[str, Any], utility: str, include_planned: bool, link: str | None) -> Event | None:
    planned = _truthy(rec.get("isPlanned"))
    if planned and not include_planned:
        return None
    out = _int(_first(rec, "customersOutNow", "customersOut", "customersAffected"))
    if not out or out <= 0:
        return None  # restored (still listed) or no customer count
    point = rec.get("outagePoint")
    if not isinstance(point, dict):
        return None
    lat, lon = num(point.get("lat")), num(_first(point, "lng", "lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    rec_id = clean_text(_first(rec, "outageRecID", "outageRecId", "outageId", "id"))
    crew = clean_text(rec.get("outageWorkStatus")) or ("Crew assigned" if _truthy(rec.get("crewAssigned")) else None)
    ev = outage_point_event(
        utility, rec_id or f"pt-{lat:.4f},{lon:.4f}", lon, lat, out,
        cause=rec.get("cause"),
        etr=_time(rec.get("estimatedTimeOfRestoral")),
        started=_time(rec.get("outageStartTime")),
        updated=_time(rec.get("outageModifiedTime")),
        crew_status=crew,
        link=link,
    )
    streets = rec.get("streetsAffected")
    if isinstance(streets, str):
        streets = [streets]
    if isinstance(streets, list):
        names = [s for s in (clean_text(x) for x in streets) if s]
        if names:
            ev.description = "Streets affected: " + ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    ev.metrics["verified"] = _truthy(rec.get("verified")) if rec.get("verified") is not None else None
    if planned:
        ev.title = "Planned outage: " + ev.title
        ev.metrics["planned"] = True
    return ev


def parse_outages(
    payload: Any, utility: str, *, include_planned: bool = False, link: str | None = None, max_points: int = 5000
) -> list[Event]:
    """outages.json -> one ``outage_point_event`` per active outage (planned ones skipped by default)."""
    events: dict[str, Event] = {}
    for rec in _outage_records(payload):
        if not isinstance(rec, dict):
            continue
        try:
            ev = _outage_event(rec, utility, include_planned, link)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue  # one malformed record must not hide the others
        if ev is not None and ev.id not in events:
            events[ev.id] = ev
    out = list(events.values())
    if len(out) > max_points:  # keep the largest when a storm produces thousands of single-meter outages
        out = sorted(out, key=lambda e: -e.metrics["customers_out"])[:max_points]
    return out


# --- boundaries.json -----------------------------------------------------------------------------------


def _layers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        inner = payload.get("boundaries")
        if isinstance(inner, list) and inner and all(isinstance(g, dict) and "boundaries" in g for g in inner):
            return inner  # wrapped list of layers
        payload = [payload]  # a single layer
    if not isinstance(payload, list):
        return []
    return [g for g in payload if isinstance(g, dict) and isinstance(g.get("boundaries"), list)]


def layer_kind(layer: dict[str, Any]) -> str:
    """'county', 'other' (zip, district, substation...) or 'unknown', from the layer's name, then nameField.

    Zip/district/substation words win over "county", so "County Commission District" is not a county layer.
    """
    for key in ("name", "nameField"):
        label = clean_text(layer.get(key)) or ""
        if _NOT_COUNTY_LAYER.search(label):
            return "other"
        if _COUNTY_LAYER.search(label):
            return "county"
        if _OTHER_LAYER.search(label):
            return "other"
    return "unknown"


def _row_name(row: dict[str, Any], name_field: str | None) -> str | None:
    value = row.get("name")
    if (value is None or value == "") and name_field:
        value = row.get(name_field)
    if value is None or value == "":
        value = _first(row, "NAME", "Name", "county", "County", "COUNTY")
    return clean_text(value)


def _split_state(name: str, states: list[str]) -> tuple[str, str | None]:
    """'Cherokee, NC' / 'Cherokee (NC)' / 'Cherokee County, North Carolina' -> ('Cherokee', 'NC')."""
    known = regions.state_codes()
    m = re.match(r"^(.+?),\s*([A-Za-z][A-Za-z .]+)$", name)
    if m:
        st = m.group(2).strip()
        code = st.upper() if len(st) == 2 else regions.state_code_for_name(st)
        if code in known:
            return m.group(1).strip(), code
    m = re.match(r"^(.+?)[\s(\-/]+([A-Z]{2})\)?$", name)
    if m and m.group(2) in known and (not states or m.group(2) in states):
        return m.group(1).strip(), m.group(2)
    return name, None


def _find_county(state: str, name: str) -> dict[str, Any] | None:
    county = regions.find_county(state, name)
    if county is None:
        key = re.sub(r"\s+(county|parish)$", "", " ".join(name.lower().split()))
        alias = _COUNTY_ALIASES.get((state, key))
        county = regions.find_county(state, alias) if alias else None
    return county


def _add_counties(found: dict[str, dict[str, Any]], name: str, states: list[str]) -> None:
    variants = [name]
    stripped = _CO_SUFFIX.sub("", name)
    if stripped != name:
        variants.append(stripped)
    for st in states:
        for variant in variants:
            county = _find_county(st, variant) if st and variant else None
            if county:
                found.setdefault(county["fips"], county)
                break


def county_candidates(name: str, states: list[str]) -> list[dict[str, Any]]:
    """Counties (in ``states``, or the state named in the label) that a boundary label can mean."""
    name = name.strip()
    base, hinted = _split_state(name, states)
    found: dict[str, dict[str, Any]] = {}
    if hinted:
        _add_counties(found, base, [hinted])
    # Also search the utility's states when the label's state matched nothing, or when that "state" is a
    # trailing all-caps CO, which is as likely the county abbreviation ("DUNDY CO" for a Colorado/Nebraska
    # co-op is Dundy County, NE; "WASHINGTON CO" is ambiguous and left to the outage points).
    if not hinted or not found or (hinted == "CO" and _CO_SUFFIX.search(name)):
        _add_counties(found, name, states)
    return list(found.values())


def _mostly_counties(layer: dict[str, Any], states: list[str]) -> bool:
    name_field = clean_text(layer.get("nameField"))
    names = [n for n in (_row_name(r, name_field) for r in layer["boundaries"] if isinstance(r, dict)) if n]
    if not names or not states:
        return False
    matched = sum(1 for n in names if county_candidates(n, states))
    return matched >= max(1, 0.6 * len(names))


def _per_area_layer(layers: list[dict[str, Any]], states: list[str]) -> dict[str, Any] | None:
    """One synthetic county layer when boundaries.json holds one group per county or parish, each with a
    single row, instead of one layer with a row per county. scottarver/outage-tracker keys Beauregard
    Electric's groups by their top-level name and calls them parishes, reading only ``boundaries[0]``, which
    suggests this shape. Groups must be named after distinct counties in ``states``."""
    if len(layers) < 2 or not states:
        return None
    if any(sum(isinstance(r, dict) for r in g["boundaries"]) > 1 for g in layers):
        return None
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for g in layers:
        group_name = clean_text(g.get("name"))
        cands = county_candidates(group_name, states) if group_name else []
        row = next((r for r in g["boundaries"] if isinstance(r, dict)), None)
        if not cands or row is None:
            continue
        key = tuple(sorted(c["fips"] for c in cands))
        if key in seen:
            return None  # the same county twice: several layers of one area, not one group per area
        seen.add(key)
        label = _row_name(row, clean_text(g.get("nameField")))
        rows.append({**row, "name": label if label and county_candidates(label, states) else group_name})
    if len(rows) < 2 or len(rows) < 0.6 * len(layers):
        return None
    return {"name": "per-area groups", "nameField": "name", "boundaries": rows}


def select_county_layer(payload: Any, states: list[str], layer: str | None = None) -> dict[str, Any] | None:
    """The one layer to read county numbers from: ``layer`` by name if given; else the groups merged when the
    file has one single-row group per county; else the first county/parish layer whose rows are mostly
    county names (the first county/parish layer when none is); else the first unlabelled layer whose rows are
    mostly county names. None when there is none."""
    layers = _layers(payload)
    if layer:
        want = str(layer).strip().lower()
        return next((g for g in layers if (clean_text(g.get("name")) or "").lower() == want), None)
    merged = _per_area_layer(layers, states)
    if merged is not None:
        return merged
    kinds = [(g, layer_kind(g)) for g in layers]
    county_layers = [g for g, kind in kinds if kind == "county"]
    if county_layers:
        return next((g for g in county_layers if _mostly_counties(g, states)), county_layers[0])
    for g, kind in kinds:
        if kind == "unknown" and _mostly_counties(g, states):
            return g
    return None


def parse_boundaries(
    payload: Any,
    utility: str,
    states: list[str],
    *,
    layer: str | None = None,
    updated: Any = None,
    link: str | None = None,
    point_fips: set[str] | None = None,
) -> list[Event]:
    """boundaries.json -> ``county_outage_event`` per county with customers out (county layers only).

    A label that matches counties in more than one of the utility's states (Clay, GA / Clay, NC) is resolved
    by the state named in the label, else by where the utility's outage points fall, else skipped: a county in
    the wrong state is worse than a missing one.
    """
    chosen = select_county_layer(payload, states, layer)
    if chosen is None:
        return []
    name_field = clean_text(chosen.get("nameField"))
    totals: dict[str, list[Any]] = {}  # fips -> [county, out, served]
    for row in chosen["boundaries"]:
        if not isinstance(row, dict):
            continue
        try:
            name = _row_name(row, name_field)
            out = _int(_first(row, "customersOutNow", "customersOut", "customersAffected"))
            if not name or not out or out <= 0:
                continue
            cands = county_candidates(name, states)
            if len(cands) > 1:
                cands = [c for c in cands if point_fips and c["fips"] in point_fips]
            if len(cands) != 1:
                continue
            county = cands[0]
            served = _int(row.get("customersServed"))
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        slot = totals.setdefault(county["fips"], [county, 0, None])
        slot[1] += out
        if served:
            slot[2] = (slot[2] or 0) + served
    return [
        county_outage_event(utility, county["state"], county, out, served, updated=updated, link=link)
        for county, out, served in totals.values()
    ]


# --- all three files -----------------------------------------------------------------------------------


def _place_points(points: list[Event], states: list[str]) -> tuple[list[Event], set[str]]:
    """Drop outage points that fall in a state the utility does not serve, and return the rest with the FIPS
    codes of the counties they fall in. A bad or sign-flipped position would otherwise be attributed to
    another state's power rollup (codebooker's Otter Tail and Keys parsers filter the same way). Points in no
    state (just offshore) are kept. With no ``states`` nothing is dropped."""
    if not states:
        return points, set()
    kept: list[Event] = []
    fips: set[str] = set()
    for ev in points:
        lon, lat = ev.geometry["coordinates"][:2]
        state, county = regions.locate(lon, lat)
        if state and state not in states:
            continue
        kept.append(ev)
        if county:
            fips.add(county["fips"])
    return kept, fips


def parse_wov(
    summary: Any,
    outages: Any,
    boundaries: Any,
    utility: str,
    states: list[str],
    *,
    options: dict[str, Any] | None = None,
    link: str | None = None,
) -> list[Event]:
    """Utility total + county events + outage points from the three WOV files (None = not fetched)."""
    opts = options or {}
    max_points = _int(opts.get("max_points"))
    points = parse_outages(
        outages, utility, include_planned=_truthy(opts.get("include_planned", False)), link=link,
        max_points=max_points if max_points and max_points > 0 else 5000,
    ) if outages is not None else []
    points, point_fips = _place_points(points, states)
    fields = summary_fields(summary)
    counties: list[Event] = []
    if boundaries is not None:
        counties = parse_boundaries(
            boundaries, utility, states, layer=opts.get("boundary_layer"), updated=fields["updated"], link=link,
            point_fips=point_fips,
        )
    active = [p for p in points if not p.metrics.get("planned")]
    out = fields["out"]
    if out is None:  # no usable summary: fall back to the county layer, then the points
        out = sum(e.metrics["customers_out"] for e in counties) if counties else sum(p.metrics["customers_out"] for p in active)
    served = _int(opts.get("customers_served")) or fields["served"]
    n_outages = len(active) if outages is not None else fields["outages"]
    total = utility_total_event(utility, out or 0, served, n_outages, updated=fields["updated"], link=link)
    return [total, *counties, *points]


@register
class MilsoftWebOutageViewer(Source):
    type = "milsoft_wov"
    default_name = "Co-op outages"
    category = Category.power
    default_interval = 300
    required_options = ("url",)

    @property
    def root(self) -> str:
        return str(self.options["url"]).split("?", 1)[0].rstrip("/") + "/"

    def data_url(self, kind: str) -> str:
        explicit = self.options.get(f"{kind}_url")
        return str(explicit) if explicit else f"{self.root}data/{DATA_FILES[kind]}.json"

    async def _get(self, kind: str) -> Any:
        """One data file. outageSummary.json is required. outages.json or boundaries.json that does not exist
        (404/410; for boundaries.json also an HTML page in place of JSON) is None. Anything else, including a
        403 from a firewall or rate limit, raises, so the poll keeps the previous events instead of marking
        every outage cleared and then re-creating them all on the next poll."""
        params = self.options.get("params")
        params = params if isinstance(params, dict) else {"v": "2"}
        headers = {"Accept": "application/json, text/plain, */*", "Referer": self.root}
        headers.update(self.options.get("headers") or {})
        url = self.data_url(kind)
        resp = await self.ctx.http.get(url, params=params or None, headers=headers)
        if kind != "summary" and resp.status_code in (404, 410):
            return None
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from {url}")
        try:
            return resp.json()
        except ValueError as exc:
            if kind == "boundaries":
                return None
            hint = " (set outage_points: false if this viewer has no outages.json)" if kind == "outages" else ""
            raise SourceError(f"invalid JSON from {url}{hint}") from exc

    async def fetch(self) -> list[Event]:
        kinds = ["summary"]
        if self.options.get("outage_points", True):
            kinds.append("outages")
        if self.options.get("county_reports", True):
            kinds.append("boundaries")
        results = await asyncio.gather(*(self._get(k) for k in kinds), return_exceptions=True)
        got: dict[str, Any] = {"outages": None, "boundaries": None}
        for kind, result in zip(kinds, results):
            if isinstance(result, (SourceError, httpx.HTTPError)):
                detail = str(result) or type(result).__name__  # e.g. ConnectError often has no message
                raise SourceError(f"{DATA_FILES[kind]}.json: {detail}") from result
            if isinstance(result, BaseException):
                raise result
            got[kind] = result
        if not isinstance(got["summary"], dict):
            raise SourceError("unexpected outageSummary.json response (not an object)")
        if got["outages"] is not None and not _outage_records(got["outages"]) and got["outages"] not in ([], {}):
            got["outages"] = None  # an unrecognised shape: keep the total rather than report zero points
        link = self.options.get("link") or self.root
        return parse_wov(got["summary"], got["outages"], got["boundaries"], self.name, self.cfg.states,
                         options=self.options, link=link)
