"""Power outages from PacifiCorp's outage maps: Rocky Mountain Power (UT, WY, ID) and Pacific Power (OR, WA, CA).

Each state's map reads two static JSON files from the utility's web site. Both carry the state totals
(``totalState`` customers out, ``count`` outages and ``last_upd``); a real Wyoming pair, trimmed:

``<site>/etc/pcorp/datafiles/outagemap/mapWY.json``, one entry per outage or group of nearby outages::

    {"count": 3, "totalState": 46,
     "outages": [{"icon": "standard", "longitude": -105.583, "latitude": 41.331,
                  "etr": "Before 07:30 PM on 09/27", "outCount": 2, "custOut": 45,
                  "cause": "Multiple outages in the area", "crewStatus": "Crews Arrived",
                  "reported": "08:25 AM on 09/27", "zip": "82072"}]}

``<site>/etc/pcorp/datafiles/outagemap/listWY.json``, the same numbers by county and by ZIP code, split into
planned and unplanned::

    {"count": 3, "totalState": 46,
     "counties": [{"countyName": "Albany", "outCountPlan": 0, "outCountUnplan": 2,
                   "custOutPlan": 0, "custOutUnplan": 45}],
     "zips": [{"zipCode": "82072", "outCountPlan": 0, "outCountUnplan": 2, "custOutPlan": 0, "custOutUnplan": 45}]}

Times are local wall-clock times without a year ("02:04 PM on 09/27"). Outages have no id, so an outage's id is
its position plus its reported time (``40.745,-111.863@20260927T2212Z``), which do not change while it lasts. Planned outages (``icon: planned``)
are included and flagged, as they are in the utility's totals; ``include_planned: false`` leaves them out.

Example (config.yaml)::

    - id: rmp_ut
      type: pacificorp
      name: Rocky Mountain Power (UT)
      states: [UT]
      site: https://www.rockymountainpower.net
      state: UT
      # county_reports: false   # no county outlines (list<ST>.json is still read when outage_points is off)
      # outage_points: false    # skip map<ST>.json
      # include_planned: false
      # timezone: America/Denver  # default: the state's zone

Each poll makes at most two requests, run concurrently.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from emagg import regions
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.local_time import parse_any, safe_int, zone
from emagg.sources.power_common import county_outage_event, outage_point_event, utility_total_event
from emagg.util import clean_text, num

DATA_PATH = "/etc/pcorp/datafiles/outagemap/"
TIME_FORMATS = ("%I:%M %p on %m/%d", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%m/%d %I:%M %p")


def _time(value: Any, tz: ZoneInfo | None, now: datetime | None) -> datetime | None:
    return parse_any(value, TIME_FORMATS, tz, now)


def _etr(value: Any, tz: ZoneInfo | None, now: datetime | None) -> datetime | str | None:
    """ "Before 06:00 PM on 09/27" -> that time; "Assessing" stays text."""
    text = clean_text(value)
    if not text:
        return None
    body = text[len("before "):] if text.lower().startswith("before ") else text
    return _time(body, tz, now) or text


def _planned(rec: dict[str, Any]) -> bool:
    return "plan" in str(rec.get("icon") or "").lower()


def _state_code(state: Any) -> str:
    return str(state or "").strip().upper()


# --- map<ST>.json ---------------------------------------------------------------------------------------


def parse_points(
    payload: Any, utility: str, *, include_planned: bool = True, link: str | None = None,
    tz: ZoneInfo | None = None, now: datetime | None = None, updated: Any = None, max_points: int = 5000,
) -> list[Event]:
    """map<ST>.json -> one ``outage_point_event`` per outage (a group of nearby outages when outCount > 1)."""
    records = payload.get("outages") if isinstance(payload, dict) else None
    events: dict[str, Event] = {}
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        try:
            planned = _planned(rec)
            if planned and not include_planned:
                continue
            out = safe_int(rec.get("custOut"))
            lat, lon = num(rec.get("latitude")), num(rec.get("longitude"))
            if not out or out <= 0 or lat is None or lon is None:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                continue
            n_out = max(1, safe_int(rec.get("outCount")) or 1)
            reported = clean_text(rec.get("reported"))
            started = _time(reported, tz, now)
            base = f"{lat:.3f},{lon:.3f}" + (f"@{started:%Y%m%dT%H%MZ}" if started else "")
            eid, k = base, 2
            while eid in events:  # two outages reported at the same place and minute
                eid, k = f"{base}#{k}", k + 1
            ev = outage_point_event(
                utility, eid, lon, lat, out, cluster=n_out > 1, outages=n_out,
                cause=rec.get("cause"), etr=_etr(rec.get("etr"), tz, now), started=started,
                updated=updated, crew_status=rec.get("crewStatus"), link=link,
            )
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue  # one malformed record must not hide the others
        z = clean_text(rec.get("zip"))
        if z:
            ev.description = f"ZIP {z}"
            ev.metrics["zip"] = z
        if planned:
            ev.title = "Planned outage: " + ev.title
            ev.metrics["planned"] = True
        events[eid] = ev
    out_events = list(events.values())
    if len(out_events) > max_points:
        out_events = sorted(out_events, key=lambda e: -e.metrics["customers_out"])[:max_points]
    return out_events


# --- list<ST>.json --------------------------------------------------------------------------------------


def _n(value: Any) -> int:
    return max(0, safe_int(value) or 0)


def _county_counts(rec: dict[str, Any], include_planned: bool) -> tuple[int, int]:
    out = _n(rec.get("custOutUnplan")) + (_n(rec.get("custOutPlan")) if include_planned else 0)
    n = _n(rec.get("outCountUnplan")) + (_n(rec.get("outCountPlan")) if include_planned else 0)
    return out, n


def parse_counties(
    payload: Any, utility: str, state: str, *, include_planned: bool = True, link: str | None = None,
    updated: Any = None,
) -> list[Event]:
    """list<ST>.json -> one ``county_outage_event`` per county with customers out (no customers-served figure)."""
    records = payload.get("counties") if isinstance(payload, dict) else None
    st = _state_code(state)
    totals: dict[str, list[Any]] = {}  # fips -> [county, customers out, outages]; a county listed twice adds up
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        name = clean_text(rec.get("countyName"))
        out, n = _county_counts(rec, include_planned)
        county = regions.find_county(st, name) if name and st else None
        if county is None or out <= 0:
            continue
        t = totals.setdefault(county["fips"], [county, 0, 0])
        t[1] += out
        t[2] += n
    events = []
    for county, out, n in totals.values():
        ev = county_outage_event(utility, county["state"], county, out, None, updated=updated, link=link)
        ev.metrics["outages"] = n
        events.append(ev)
    return events


# --- totals ---------------------------------------------------------------------------------------------


def parse_total(
    map_payload: Any, list_payload: Any, utility: str, state: str, *, include_planned: bool = True,
    customers_served: int | None = None, link: str | None = None, tz: ZoneInfo | None = None,
    now: datetime | None = None,
) -> Event | None:
    """The state total from whichever file came back (they carry the same totalState / count)."""
    payloads = [p for p in (list_payload, map_payload) if isinstance(p, dict)]
    head = next((p for p in payloads if safe_int(p.get("totalState")) is not None), None)
    if head is None:
        return None
    out = max(0, safe_int(head.get("totalState")) or 0)
    count = safe_int(head.get("count"))
    if not include_planned:
        counties = list_payload.get("counties") if isinstance(list_payload, dict) else None
        points = map_payload.get("outages") if isinstance(map_payload, dict) else None
        if isinstance(counties, list):
            planned = [(_n(c.get("custOutPlan")), _n(c.get("outCountPlan")))
                       for c in counties if isinstance(c, dict)]
        elif isinstance(points, list):
            planned = [(_n(p.get("custOut")), max(1, _n(p.get("outCount"))))
                       for p in points if isinstance(p, dict) and _planned(p)]
        else:
            planned = []
        out = max(0, out - sum(c for c, _ in planned))
        count = max(0, count - sum(n for _, n in planned)) if count is not None else None
    updated = max((t for p in payloads if (t := _time(p.get("last_upd"), tz, now))), default=None)
    return utility_total_event(utility, out, customers_served, count, updated=updated, link=link,
                               states=[_state_code(state)] if state else None)


def parse_pacificorp(
    map_payload: Any, list_payload: Any, utility: str, state: str, *, include_planned: bool = True,
    customers_served: int | None = None, link: str | None = None, tz: ZoneInfo | None = None,
    now: datetime | None = None, county_reports: bool = True,
) -> list[Event]:
    """Both files (either may be None) -> the state total, county outlines (unless ``county_reports`` is off)
    and outage points."""
    total = parse_total(map_payload, list_payload, utility, state, include_planned=include_planned,
                        customers_served=customers_served, link=link, tz=tz, now=now)
    if total is None:
        return []
    events = [total]
    updated = total.updated_at
    if list_payload is not None and county_reports:
        events += parse_counties(list_payload, utility, state, include_planned=include_planned, link=link, updated=updated)
    if map_payload is not None:
        events += parse_points(map_payload, utility, include_planned=include_planned, link=link, tz=tz, now=now,
                               updated=updated)
    return events


def loads_lenient(text: str) -> Any:
    """JSON, or the first object when the file holds several back to back (seen on mapOR.json)."""
    text = text.lstrip("\ufeff")  # a UTF-8 byte-order mark stays in httpx's response.text
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return json.JSONDecoder().raw_decode(text.strip())[0]
    except ValueError:
        return None


@register
class PacifiCorpOutages(Source):
    type = "pacificorp"
    default_name = "PacifiCorp outages"
    category = Category.power
    default_interval = 300
    required_options = ("site", "state")

    def _url(self, kind: str) -> str:
        key = f"{kind}_url"
        if self.options.get(key):
            return str(self.options[key])
        return str(self.options["site"]).rstrip("/") + DATA_PATH + f"{kind}{_state_code(self.options['state'])}.json"

    async def _get(self, url: str) -> Any:
        resp = await self.ctx.http.get(url, headers=self.options.get("headers") or {})
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from {url}")
        data = loads_lenient(resp.text)
        if not isinstance(data, dict):
            raise SourceError(f"unexpected response from {url} (no JSON object)")
        return data

    async def fetch(self) -> list[Event]:
        kinds = []
        if self.options.get("outage_points", True):
            kinds.append("map")
        if self.options.get("county_reports", True) or not kinds:
            kinds.append("list")
        results = await asyncio.gather(*(self._get(self._url(k)) for k in kinds), return_exceptions=True)
        got = dict(zip(kinds, results))
        errors = [r for r in results if isinstance(r, BaseException)]
        if len(errors) == len(results):
            raise errors[0]
        state = _state_code(self.options["state"])
        payloads = {k: (v if isinstance(v, dict) else None) for k, v in got.items()}
        events = parse_pacificorp(
            payloads.get("map"), payloads.get("list"), self.name, state,
            include_planned=bool(self.options.get("include_planned", True)),
            customers_served=safe_int(self.options.get("customers_served")),
            link=self.options.get("link"),
            tz=zone(self.options.get("timezone"), [state]),
            county_reports=bool(self.options.get("county_reports", True)),
        )
        if not events:
            raise SourceError("no totalState in the outage map files")
        return events
