"""Power outages from WEC Energy Group outage maps: We Energies and Wisconsin Public Service.

Each utility's outage map ("outagesummary") reads one JSON list, ``<site>/outagesummary/view/OutageEventJSON``,
with one entry per outage. ``Slices`` break an outage's customers down by city / county / ZIP / region, and
carry the customers served in each (``CountyCusts`` is the county's customer count, not an outage count). A
real We Energies record, trimmed::

    [{"Latitude": 43.0077978, "Longitude": -88.3831588, "OffTime": "Aug 5, 2:44 p.m.", "ETR": "Aug 5, 6:00 p.m.",
      "CrewStatus": "Assigned", "Cause": "Not yet determined", "LastUpdated": "Aug 5, 3:50 p.m.", "IsGlobal": false,
      "Slices": [{"AffectedCusts": 93, "City": "dousman", "CityCusts": 3849, "County": "waukesha",
                  "CountyCusts": 203383, "Zip": "53118", "ZipCusts": 3845, "Region": "southeast wi",
                  "RegionCusts": 1073423}]}]

Times are local (Central) wall-clock times without a year. Outages have no id, so an outage's id is its position
plus its start time, which do not change while it lasts. County names are matched within the source's states;
a name found in more than one (Iron, Menominee: WI and MI) is settled by the region's state suffix ("upper
peninsula mi"), then by where the outage is. Regions are service regions, not states: "upper peninsula mi"
also covers Vilas County, WI. The feed misspells Manitowoc as "manitowic" (``COUNTY_ALIASES``); any other name
that matches no county is placed by where the outage is, but only when all of the outage's slices name that
one county.

Example (config.yaml)::

    - id: we_energies
      type: wec_outages
      name: We Energies
      states: [WI, MI]
      url: https://www.we-energies.com/outagesummary/view/OutageEventJSON
      customers_served: 1260000   # the feed has no utility-wide figure; its regions' RegionCusts add up to this
      # county_reports: false
      # outage_points: false
      # timezone: America/Chicago

Each poll makes one request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from emagg import regions
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.local_time import parse_any, safe_int, zone
from emagg.sources.power_common import county_outage_event, outage_point_event, utility_total_event
from emagg.util import clean_text, num

TIME_FORMATS = ("%b %d, %I:%M %p", "%B %d, %I:%M %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p")
# County names as the feed spells them -> the real name (tmoody1973/mke-power-outage-tracker history, 2026:
# 94 "manitowic" slices, all in Manitowoc County).
COUNTY_ALIASES = {"manitowic": "manitowoc"}


def _time(value: Any, tz: ZoneInfo | None, now: datetime | None) -> datetime | None:
    return parse_any(value, TIME_FORMATS, tz, now)


def _text(value: Any) -> str | None:
    return clean_text(" ".join(str(value).replace("\xa0", " ").split())) if value is not None else None


def _slices(rec: dict[str, Any]) -> list[dict[str, Any]]:
    s = rec.get("Slices")
    return [x for x in s if isinstance(x, dict)] if isinstance(s, list) else []


def _custs(value: Any) -> int:
    return max(0, safe_int(value) or 0)


def _affected(rec: dict[str, Any]) -> int:
    return sum(_custs(s.get("AffectedCusts")) for s in _slices(rec))


def _region_state(region: Any) -> str | None:
    last = (clean_text(region) or "").split()[-1:] or [""]
    code = last[0].upper()
    return code if len(code) == 2 and regions.state_name(code) else None


def _county_name(sl: dict[str, Any]) -> str | None:
    name = clean_text(sl.get("County"))
    return COUNTY_ALIASES.get(name.lower(), name) if name else None


def slice_county(
    sl: dict[str, Any], states: list[str], point: tuple[float, float] | None = None, sole_county: bool = False
) -> dict[str, Any] | None:
    """The slice's county. ``point`` (lon, lat) settles a name found in several states; with ``sole_county`` (all
    of the outage's slices name this county) it also places a name that matches no county."""
    name = _county_name(sl)
    if not name:
        return None
    region_st = _region_state(sl.get("Region"))
    candidates = list(states) or ([region_st] if region_st else [])
    found = [c for st in candidates if (c := regions.find_county(st, name))]
    if len(found) == 1:
        return found[0]
    if len(found) > 1 and region_st:
        pick = [c for c in found if c["state"] == region_st]
        if len(pick) == 1:
            return pick[0]
    if point is not None and (found or sole_county):
        st, located = regions.locate(*point)
        if located and (any(c["fips"] == located["fips"] for c in found) or (not found and st in candidates)):
            return located
    return None


def _position(rec: dict[str, Any]) -> tuple[float, float] | None:
    lat, lon = num(rec.get("Latitude")), num(rec.get("Longitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lon, lat


def _places(rec: dict[str, Any]) -> str | None:
    names: list[str] = []
    for sl in _slices(rec):
        city = clean_text(sl.get("City"))
        county = clean_text(sl.get("County"))
        label = city.title() if city else (f"{county.title()} County" if county else None)
        if label and label not in names:
            names.append(label)
    if not names:
        return None
    return ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")


def parse_points(
    payload: Any, utility: str, *, link: str | None = None, tz: ZoneInfo | None = None,
    now: datetime | None = None, max_points: int = 5000,
) -> list[Event]:
    """One ``outage_point_event`` per outage: its customers are the sum of its slices."""
    events: dict[str, Event] = {}
    for rec in payload if isinstance(payload, list) else []:
        if not isinstance(rec, dict):
            continue
        try:
            out = _affected(rec)
            pos = _position(rec)
            if out <= 0 or pos is None:
                continue
            lon, lat = pos
            started = _time(rec.get("OffTime"), tz, now)
            base = f"{lat:.4f},{lon:.4f}" + (f"@{started:%Y%m%dT%H%MZ}" if started else "")
            eid, k = base, 2
            while eid in events:
                eid, k = f"{base}#{k}", k + 1
            etr_raw = _text(rec.get("ETR"))
            ev = outage_point_event(
                utility, eid, lon, lat, out,
                cause=_text(rec.get("Cause")),
                etr=_time(etr_raw, tz, now) or etr_raw,
                started=started,
                updated=_time(rec.get("LastUpdated"), tz, now),
                crew_status=_text(rec.get("CrewStatus")),
                link=link,
            )
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        places = _places(rec)
        if places:
            ev.description = places
        events[eid] = ev
    out_events = list(events.values())
    if len(out_events) > max_points:
        out_events = sorted(out_events, key=lambda e: -e.metrics["customers_out"])[:max_points]
    return out_events


def parse_counties(payload: Any, utility: str, states: list[str], *, link: str | None = None, updated: Any = None) -> list[Event]:
    """Slices summed per county; customers served is the county's CountyCusts (the largest value seen)."""
    totals: dict[str, dict[str, Any]] = {}
    for rec in payload if isinstance(payload, list) else []:
        if not isinstance(rec, dict):
            continue
        pos = _position(rec)
        slices = _slices(rec)
        sole = len({(_county_name(sl) or "").lower() for sl in slices}) == 1
        touched: set[str] = set()
        for sl in slices:
            n = _custs(sl.get("AffectedCusts"))
            county = slice_county(sl, states, pos, sole) if n > 0 else None
            if county is None:
                continue
            t = totals.setdefault(county["fips"], {"county": county, "out": 0, "served": None, "outages": 0})
            t["out"] += n
            served = safe_int(sl.get("CountyCusts"))
            if served and served > (t["served"] or 0):
                t["served"] = served
            if county["fips"] not in touched:
                t["outages"] += 1
                touched.add(county["fips"])
    events = []
    for t in totals.values():
        c = t["county"]
        served = t["served"] if t["served"] and t["served"] >= t["out"] else None
        ev = county_outage_event(utility, c["state"], c, t["out"], served, updated=updated, link=link)
        ev.metrics["outages"] = t["outages"]
        events.append(ev)
    return events


def parse_wec(
    payload: Any, utility: str, states: list[str], *, customers_served: int | None = None, county_reports: bool = True,
    outage_points: bool = True, link: str | None = None, tz: ZoneInfo | None = None, now: datetime | None = None,
    max_points: int = 5000,
) -> list[Event]:
    """The OutageEventJSON list -> utility total, county outlines and outage points."""
    records = [r for r in payload if isinstance(r, dict)] if isinstance(payload, list) else []
    out = sum(_affected(r) for r in records)
    updated = max((t for r in records if (t := _time(r.get("LastUpdated"), tz, now))), default=None)
    events = [utility_total_event(utility, out, customers_served, len(records), updated=updated, link=link)]
    if county_reports:
        events += parse_counties(records, utility, states, link=link, updated=updated)
    if outage_points:
        events += parse_points(records, utility, link=link, tz=tz, now=now, max_points=max_points)
    return events


@register
class WecOutages(Source):
    type = "wec_outages"
    default_name = "WEC Energy Group outages"
    category = Category.power
    default_interval = 300
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        payload = await self.get_json(self.option_url(), headers=self.options.get("headers") or {})
        if not isinstance(payload, list):
            raise SourceError("unexpected OutageEventJSON response (not a list)")
        states = list(self.cfg.states)
        return parse_wec(
            payload, self.name, states,
            customers_served=safe_int(self.options.get("customers_served")),
            county_reports=bool(self.options.get("county_reports", True)),
            outage_points=bool(self.options.get("outage_points", True)),
            link=self.options.get("link"),
            tz=zone(self.options.get("timezone") or "America/Chicago", states),
            max_points=safe_int(self.options.get("max_points")) or 5000,
        )
