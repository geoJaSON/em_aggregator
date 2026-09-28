"""Power outages from OSI (Emerson) monarch "Public Outage Portal" (POP) maps.

Used by Black Hills Energy, Turlock Irrigation District and Redding Electric Utility, among others. The map
page lives under ``<site>/POP/`` and reads two JSON lists:

``<site>/POP/model/PopOutage?ServiceType=Electric``, one record per outage (a real Black Hills record)::

    [{"id": "6941e01ca9d2537c6f5c884c", "serviceType": "Electric", "currentAffected": 1,
      "fieldValues": {"energizationStatus": "Predicted", "currentAffected": "1",
                      "outageStepOffTime": "2025-12-16T22:41:27Z", "publishedEtr": "2025-12-17T00:31:32Z",
                      "geographicAorIDs": "Cheyenne South"},
      "lat": 41.10327501, "lon": -104.8232439, "geoJSONPolygon": null}]

``<site>/POP/model/PopOutageSummary?ServiceType=Electric``, customers affected / served per area. Each row
names the grouping it belongs to (``summaryField``, e.g. County or Town) and the area (``summaryFieldValue``)::

    [{"serviceType": "Electric", "summaryField": "County", "summaryFieldValue": "Pueblo",
      "affectedCount": 12, "totalCount": 43210}]

The utility total comes from one summary grouping (a county grouping when there is one, so a utility that
lists both counties and towns is not counted twice); county groupings also become county outlines. Without a
summary the total is the sum of the outage records. Force the total's grouping with ``summary_field: <name>``,
and name a county grouping the portal labels differently with ``county_field: <name>``.

Customers served: the configured ``customers_served`` wins. Otherwise the grouping's ``totalCount`` sum is used
only when the grouping also lists areas with no one out (so it is the full service area, not just the areas
with outages). A county name found in more than one of the utility's states (Custer: CO, MT, SD) is settled by
the outage points that fall in one of them. Outages whose ``energizationStatus`` starts with "Restored"
("Restored Pending Calls") are over and are left out.

Example (config.yaml)::

    - id: black_hills
      type: osi_pop
      name: Black Hills Energy
      states: [CO, MT, SD, WY]
      url: https://www.blackhillsenergy.com
      customers_served: 225000
      # service_type: Electric
      # county_reports: false   # summary gives the total only
      # outage_points: false    # skip PopOutage

Each poll makes at most two requests, run concurrently.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any, Callable, Iterator

from emagg import regions
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.local_time import safe_int
from emagg.sources.power_common import county_outage_event, outage_point_event, utility_total_event
from emagg.util import clean_text, num

_COUNTY = re.compile(r"count(y|ies)|parish|borough", re.I)
_STATE_SUFFIX = re.compile(r"^(?P<name>.+?)[\s,]*(?:\((?P<p>[A-Za-z]{2})\)|,\s*(?P<c>[A-Za-z]{2}))$")


def _service_ok(rec: dict[str, Any], service_type: str | None) -> bool:
    st = clean_text(rec.get("serviceType"))
    return not service_type or not st or st.lower() == service_type.lower()


OUTAGE_KEYS = ("outages", "data")
SUMMARY_KEYS = ("summary", "data")


def _records(payload: Any, *keys: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _is_list_payload(payload: Any, keys: tuple[str, ...]) -> bool:
    """A list, or an object holding one under a known key; anything else ({"error": ...}) is not data."""
    return isinstance(payload, list) or (isinstance(payload, dict) and any(isinstance(payload.get(k), list) for k in keys))


# --- PopOutage ------------------------------------------------------------------------------------------


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _positions(coords: Any, depth: int = 0) -> Iterator[tuple[float, float]]:
    """(lon, lat) pairs in a GeoJSON coordinates array; malformed parts are skipped (never recurses into text)."""
    if not isinstance(coords, list) or depth > 6:
        return
    if len(coords) >= 2 and _is_number(coords[0]) and _is_number(coords[1]):
        if -180 <= coords[0] <= 180 and -90 <= coords[1] <= 90:
            yield float(coords[0]), float(coords[1])
        return
    for c in coords:
        yield from _positions(c, depth + 1)


def _point(rec: dict[str, Any]) -> tuple[float, float] | None:
    lat, lon = num(rec.get("lat")), num(rec.get("lon"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0, 0):
        return lon, lat
    poly = rec.get("geoJSONPolygon")
    if isinstance(poly, dict):
        poly = poly.get("geometry", poly)
        pts = list(_positions(poly.get("coordinates"))) if isinstance(poly, dict) else []
        if pts:  # the bounding box centre
            xs, ys = [x for x, _ in pts], [y for _, y in pts]
            return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return None


def _restored(fv: dict[str, Any]) -> bool:
    return str(fv.get("energizationStatus") or "").strip().lower().startswith("restored")


def active_outages(payload: Any, service_type: str | None = "Electric") -> Iterator[tuple[dict[str, Any], dict[str, Any], int]]:
    """(record, fieldValues, customers affected) for each outage of the service that is not restored."""
    for rec in _records(payload, *OUTAGE_KEYS):
        if not isinstance(rec, dict) or not _service_ok(rec, service_type):
            continue
        fv = rec.get("fieldValues") if isinstance(rec.get("fieldValues"), dict) else {}
        if _restored(fv):
            continue  # "Restored Pending Calls": power is back, the utility is confirming
        out = safe_int(rec.get("currentAffected"))
        if out is None:
            out = safe_int(fv.get("currentAffected"))
        if out and out > 0:
            yield rec, fv, out


def parse_outages(
    payload: Any, utility: str, *, service_type: str | None = "Electric", link: str | None = None,
    max_points: int = 5000,
) -> list[Event]:
    """PopOutage -> one ``outage_point_event`` per active outage with customers affected."""
    events: dict[str, Event] = {}
    for rec, fv, out in active_outages(payload, service_type):
        try:
            pos = _point(rec)
            if pos is None:
                continue
            lon, lat = pos
            oid = clean_text(rec.get("id")) or f"pt-{lat:.4f},{lon:.4f}"
            if oid in events:
                continue
            ev = outage_point_event(
                utility, oid, lon, lat, out,
                etr=fv.get("publishedEtr"),
                started=fv.get("outageStepOffTime"),
                crew_status=fv.get("energizationStatus"),
                link=link,
            )
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        area = clean_text(fv.get("geographicAorIDs"))
        if area:
            ev.description = f"Area: {area}"
            ev.metrics["area"] = area
        events[oid] = ev
    out_events = list(events.values())
    if len(out_events) > max_points:
        out_events = sorted(out_events, key=lambda e: -e.metrics["customers_out"])[:max_points]
    return out_events


# --- PopOutageSummary -----------------------------------------------------------------------------------


def summary_groups(payload: Any, service_type: str | None = "Electric") -> dict[str, list[dict[str, Any]]]:
    """Summary rows grouped by ``summaryField`` (in feed order; rows without one are grouped under "")."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in _records(payload, *SUMMARY_KEYS):
        if isinstance(rec, dict) and _service_ok(rec, service_type):
            groups.setdefault(clean_text(rec.get("summaryField")) or "", []).append(rec)
    return groups


def pick_group(groups: dict[str, list[dict[str, Any]]], wanted: str | None = None, county_field: str | None = None) -> str | None:
    """The grouping the total comes from: ``wanted`` if given (None when absent), else a county grouping, else
    the first one."""
    def named(n: str) -> str | None:
        return next((g for g in groups if g.lower() == n.lower()), None)

    if wanted:
        return named(wanted)
    return (named(county_field) if county_field else None) or next(
        (g for g in groups if _COUNTY.search(g)), next(iter(groups), None))


def _find_county(
    value: str, states: list[str], located: Callable[[], set[str]] | None = None
) -> dict[str, Any] | None:
    """A county by name within the source's states; "Name, ST" / "Name (ST)" pick the state. A name found in
    more than one of the states (Custer: CO, MT, SD) is settled by ``located()``, the FIPS codes of the counties
    that hold outage points; still ambiguous -> None."""
    m = _STATE_SUFFIX.match(value)
    if m:
        st = (m.group("p") or m.group("c")).upper()
        if regions.state_name(st):
            return regions.find_county(st, m.group("name"))
    found = [c for st in states if (c := regions.find_county(st, value))]
    if len(found) > 1 and located is not None:
        found = [c for c in found if c["fips"] in located()]
    return found[0] if len(found) == 1 else None


def _locator(points: list[tuple[float, float]] | None) -> Callable[[], set[str]]:
    """The FIPS codes of the counties holding ``points``, worked out once and only when first needed."""
    cache: list[set[str]] = []

    def located() -> set[str]:
        if not cache:
            fips = set()
            for lon, lat in points or []:
                _, county = regions.locate(lon, lat)
                if county:
                    fips.add(county["fips"])
            cache.append(fips)
        return cache[0]

    return located


def parse_summary(
    payload: Any, utility: str, states: list[str], *, service_type: str | None = "Electric",
    summary_field: str | None = None, county_field: str | None = None, county_reports: bool = True,
    customers_served: int | None = None, link: str | None = None, outages: int | None = None,
    points: list[tuple[float, float]] | None = None,
) -> list[Event]:
    """PopOutageSummary -> the utility total (from one grouping) plus county outlines from county groupings
    (groupings named County/Parish, or the one named by ``county_field``). ``points`` are the outage positions
    (lon, lat), used to settle county names shared by several of ``states``."""
    groups = summary_groups(payload, service_type)
    chosen = pick_group(groups, summary_field, county_field)
    if chosen is None:
        return []
    rows = groups[chosen]
    out = sum(max(0, safe_int(r.get("affectedCount")) or 0) for r in rows)
    if not customers_served and any(safe_int(r.get("affectedCount")) == 0 for r in rows):
        customers_served = sum(max(0, safe_int(r.get("totalCount")) or 0) for r in rows) or None
    events = [utility_total_event(utility, out, customers_served, outages, link=link)]
    if not county_reports:
        return events
    located = _locator(points)
    counties: dict[str, Event] = {}
    for name, grp in groups.items():
        if not (_COUNTY.search(name) or (county_field and name.lower() == county_field.lower())):
            continue
        for r in grp:
            value = clean_text(r.get("summaryFieldValue"))
            n = safe_int(r.get("affectedCount")) or 0
            county = _find_county(value, states, located) if value and n > 0 else None
            if county is None or f"county-{county['fips']}" in counties:
                continue
            served = safe_int(r.get("totalCount"))
            ev = county_outage_event(utility, county["state"], county, n, served if served and served > 0 else None,
                                     link=link)
            counties[ev.id] = ev
    return events + list(counties.values())


@register
class OsiPopOutages(Source):
    type = "osi_pop"
    default_name = "Utility outages (OSI POP)"
    category = Category.power
    default_interval = 300
    required_options = ("url",)

    def _endpoint(self, name: str) -> str:
        return str(self.options["url"]).rstrip("/") + f"/POP/model/{name}"

    async def fetch(self) -> list[Event]:
        service = str(self.options.get("service_type") or "Electric")
        params = {"ServiceType": service}
        want_points = bool(self.options.get("outage_points", True))
        calls = [self.get_json(self._endpoint("PopOutageSummary"), params=params)]
        if want_points:
            calls.append(self.get_json(self._endpoint("PopOutage"), params=params))
        results = await asyncio.gather(*calls, return_exceptions=True)
        summary = results[0]
        outages = results[1] if want_points else None
        if not isinstance(summary, BaseException) and not _is_list_payload(summary, SUMMARY_KEYS):
            summary = SourceError("unexpected PopOutageSummary response (not a list)")  # e.g. {"error": ...}
        if isinstance(summary, BaseException) and (outages is None or isinstance(outages, BaseException)):
            raise summary
        link = self.options.get("link")
        served = safe_int(self.options.get("customers_served"))
        points: list[Event] = []
        n_outages = None
        fallback_total = 0
        if outages is not None and not isinstance(outages, BaseException):
            if not _is_list_payload(outages, OUTAGE_KEYS):  # the portal answers [] when nothing is out
                raise SourceError("unexpected PopOutage response (not a list)")
            points = parse_outages(outages, self.name, service_type=service, link=link,
                                   max_points=safe_int(self.options.get("max_points")) or 5000)
            active = [out for _, _, out in active_outages(outages, service)]
            n_outages, fallback_total = len(active), sum(active)
        events: list[Event] = []
        if not isinstance(summary, BaseException):
            events = parse_summary(
                summary, self.name, self.cfg.states, service_type=service,
                summary_field=self.options.get("summary_field"), county_field=self.options.get("county_field"),
                county_reports=bool(self.options.get("county_reports", True)),
                customers_served=served, link=link, outages=n_outages,
                points=[tuple(e.geometry["coordinates"][:2]) for e in points if e.geometry],
            )
        if not events:  # no usable summary: total from the active outage records
            events = [utility_total_event(self.name, fallback_total, served, n_outages, link=link)]
        return events + points
