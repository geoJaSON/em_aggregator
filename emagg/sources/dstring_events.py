"""Power outages from outage maps that return their outage list as a JSON string inside ``{"d": "..."}``.

That wrapper is what ASP.NET page methods return. NorthWestern Energy's map
(``https://www.northwesternenergy.com/get-outage-map-data``) serves its outage-management rows this way, with
Hexagon/Intergraph InService-style column names::

    {"d": "[{\\"EVENTID\\": \\"...\\", \\"NUM_CUST\\": 212, \\"XCOORD\\": -112.53, \\"YCOORD\\": 45.99,
             \\"LOCAL_OFF_DTS\\": \\"...\\", \\"OFF_DTS\\": \\"...\\", \\"LOCAL_ERT\\": \\"...\\",
             \\"EST_REP_TIME\\": \\"...\\", \\"EVENT_STATUS\\": \\"...\\", \\"CAUSE_CODE\\": \\"...\\",
             \\"DISPATCHGROUP\\": \\"BUTTE\\", ...}]"}

The columns read by default (each a list of alternatives, first readable one wins) are in ``DEFAULT_FIELDS``;
override any of them with ``fields: {customers: CUST_OUT, ...}``. Rows whose status is in ``status_exclude``
(default ``[ARCHIVED]``) are dropped. Rows that share an event id are one outage: their customers are added up.

Times: the ``LOCAL_*`` columns are the utility's local time and are read first (codebooker/AmericaMap reads
``LOCAL_OFF_DTS`` / ``LOCAL_ERT`` before ``OFF_DTS`` / ``EST_REP_TIME``). In the other columns an ISO-8601 time
without an offset is read as UTC, which is what .NET serializers write for a raw database time
(``iso_local: true`` reads it as local time instead). Epoch milliseconds, ``/Date(ms)/`` and ``YYYYMMDDHHMMSS``
stamps with a US zone code are unambiguous; US-style display strings ("09/27/2026 9:30 AM") are local time.
Dates before 2000 mean "none", and a start time more than an hour in the future is ignored.

Coordinates may be degrees or Web Mercator metres; a swapped latitude/longitude pair is put right, and a point
outside ``bbox`` (default: the US and its nearby territories, ``[-180, 15, -60, 72]``) is not drawn (its
customers still count in the total). The feed has no customers-served figure and one list for all states, so
the utility total is the sum of the rows and is shown for the utility as a whole (multi-state), while outage
points land in their own state.

Example (config.yaml)::

    - id: northwestern
      type: dstring_events
      name: NorthWestern Energy
      states: [MT, SD]
      url: https://www.northwesternenergy.com/get-outage-map-data
      # timezone: America/Denver   # default: the first state's zone

Each poll makes one request.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.local_time import parse_any, safe_int, zone
from emagg.sources.power_common import outage_point_event, utility_total_event
from emagg.util import clean_text, num

DEFAULT_FIELDS: dict[str, list[str]] = {
    "customers": ["NUM_CUST"],
    "lon": ["XCOORD"],
    "lat": ["YCOORD"],
    "id": ["EVENTID", "EVENTNUM", "NUM_1"],
    "started": ["LOCAL_OFF_DTS", "OFF_DTS"],
    "etr": ["LOCAL_ERT", "EST_REP_TIME"],
    "updated": ["UPDATE_DTS"],
    "status": ["EVENT_STATUS", "EVENT_STATUS_DESCRIPTION"],
    "cause": ["CAUSE_CODE"],
    "area": ["DISPATCHGROUP", "SUBSTATION"],
}
TIME_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%b-%d %I:%M %p", "%b %d %I:%M %p", "%b %d, %I:%M %p",
)
US_BBOX = (-180.0, 15.0, -60.0, 72.0)  # west, south, east, north
WEB_MERCATOR_MAX = 20037509.0


def rows_from(payload: Any) -> list[dict[str, Any]]:
    """The row list from ``{"d": "<json>"}``, ``{"d": [...]}`` or a bare list; [] for anything else."""
    data = payload.get("d") if isinstance(payload, dict) else payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    if isinstance(data, dict):  # {"d": "{\"outages\": [...]}"} style
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _fields(overrides: dict[str, Any] | None) -> dict[str, list[str]]:
    out = {k: list(v) for k, v in DEFAULT_FIELDS.items()}
    for k, v in (overrides or {}).items() if isinstance(overrides, dict) else ():
        if v:
            out[k] = [str(x) for x in v] if isinstance(v, list) else [str(v)]
    return out


def _get(row: dict[str, Any], names: list[str]) -> Any:
    for n in names:
        v = row.get(n)
        if v is not None and v != "":
            return v
    return None


def _pretty(value: Any) -> str | None:
    """OMS codes are often upper case ("WIND", "ASSIGNED"); show them as "Wind", "Assigned"."""
    text = clean_text(value)
    return text.replace("_", " ").title() if text and text.isupper() else text


def _time_of(
    row: dict[str, Any], names: list[str], tz: ZoneInfo | None, now: datetime | None, *, iso_local: bool = False,
    not_after: datetime | None = None,
) -> datetime | None:
    """The first of the columns that holds a usable time (LOCAL_OFF_DTS, then OFF_DTS...). Zone-less ISO times
    are local in LOCAL_* columns and UTC in the others (unless ``iso_local``)."""
    for n in names:
        local = iso_local or n.upper().startswith("LOCAL")
        t = parse_any(row.get(n), TIME_FORMATS, tz, now, naive_utc=not local)
        if t is not None and (not_after is None or t <= not_after):
            return t
    return None


def _bbox(value: Any) -> tuple[float, float, float, float]:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        nums = [num(v) for v in value]
        if all(n is not None and math.isfinite(n) for n in nums):
            w, s, e, n = nums  # type: ignore[misc]
            if w < e and s < n:
                return w, s, e, n
    return US_BBOX


def _inside(b: tuple[float, float, float, float], lon: float, lat: float) -> bool:
    return b[0] <= lon <= b[2] and b[1] <= lat <= b[3]


def _lonlat(x: Any, y: Any, bbox: tuple[float, float, float, float] = US_BBOX) -> tuple[float, float] | None:
    """Degrees or Web Mercator metres -> (lon, lat) inside ``bbox``; None for anything that does not land there
    (State Plane metres, junk)."""
    lon, lat = num(x), num(y)
    if lon is None or lat is None or not (math.isfinite(lon) and math.isfinite(lat)) or (lon == 0 and lat == 0):
        return None
    if abs(lon) > 180 and abs(lat) > 90:  # Web Mercator metres
        if abs(lon) > WEB_MERCATOR_MAX or abs(lat) > WEB_MERCATOR_MAX:
            return None
        lon = lon / 6378137.0 * 180 / math.pi
        lat = math.degrees(2 * math.atan(math.exp(lat / 6378137.0)) - math.pi / 2)
    if not _inside(bbox, lon, lat) and _inside(bbox, lat, lon):  # latitude and longitude swapped
        lon, lat = lat, lon
    return (round(lon, 6), round(lat, 6)) if _inside(bbox, lon, lat) else None


def _excluded(status_exclude: Any) -> set[str]:
    if status_exclude is None:
        status_exclude = ["ARCHIVED"]
    elif isinstance(status_exclude, str):  # a single status written as a YAML string
        status_exclude = [status_exclude]
    elif not isinstance(status_exclude, (list, tuple, set)):
        status_exclude = []
    return {str(s).strip().upper() for s in status_exclude if str(s).strip()}


def parse_rows(
    payload: Any, utility: str, *, fields: dict[str, Any] | None = None, status_exclude: Any = None,
    outage_points: bool = True, customers_served: int | None = None, link: str | None = None,
    tz: ZoneInfo | None = None, now: datetime | None = None, max_points: int = 5000, iso_local: bool = False,
    bbox: Any = None,
) -> list[Event]:
    """Rows -> the utility total plus one ``outage_point_event`` per located outage (rows sharing an id are one
    outage)."""
    f = _fields(fields)
    excluded = _excluded(status_exclude)
    box = _bbox(bbox)
    now = now or datetime.now(timezone.utc)
    latest: datetime | None = None
    outages: dict[str, dict[str, Any]] = {}  # event id (or row number) -> customers and rows
    for i, row in enumerate(rows_from(payload)):
        try:
            status = clean_text(_get(row, f["status"]))
            if status and status.upper() in excluded:
                continue
            out = safe_int(_get(row, f["customers"]))
            if not out or out <= 0:
                continue
            rid = clean_text(_get(row, f["id"]))
            updated = _time_of(row, f["updated"], tz, now, iso_local=iso_local)
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        if updated and (latest is None or updated > latest):
            latest = updated
        o = outages.setdefault(f"id:{rid}" if rid else f"row:{i}", {"id": rid, "out": 0, "rows": []})
        o["out"] += out
        o["rows"].append(row)
    total = sum(o["out"] for o in outages.values())
    events = [utility_total_event(utility, total, customers_served, len(outages), updated=latest, link=link)]
    if not outage_points:
        return events
    points: dict[str, Event] = {}
    for o in outages.values():
        try:
            located = next(((row, pos) for row in o["rows"]
                            if (pos := _lonlat(_get(row, f["lon"]), _get(row, f["lat"]), box))), None)
            if located is None:
                continue
            row, (lon, lat) = located
            oid = o["id"] or f"pt-{lat:.4f},{lon:.4f}"
            base, k = oid, 2
            while oid in points:  # two id-less rows at the same place
                oid, k = f"{base}#{k}", k + 1
            status = clean_text(_get(row, f["status"]))
            started = _time_of(row, f["started"], tz, now, iso_local=iso_local, not_after=now + timedelta(hours=1))
            etr_raw = _get(row, f["etr"])
            etr: datetime | str | None = _time_of(row, f["etr"], tz, now, iso_local=iso_local)
            if etr is not None and started is not None and etr < started:
                etr = None  # a placeholder ("JAN-01 12:00 AM")
            elif etr is None and isinstance(etr_raw, str) and etr_raw[:1].isalpha() and "Date(" not in etr_raw:
                etr = clean_text(etr_raw)  # "Assessing" and the like
            ev = outage_point_event(
                utility, oid, lon, lat, o["out"],
                cause=_pretty(_get(row, f["cause"])),
                etr=etr,
                started=started,
                updated=_time_of(row, f["updated"], tz, now, iso_local=iso_local),
                crew_status=_pretty(status),
                link=link,
            )
        except (TypeError, ValueError, AttributeError, OverflowError):
            continue
        area = clean_text(_get(row, f["area"]))
        if area:
            ev.description = f"Area: {_pretty(area)}"
            ev.metrics["area"] = area
        if len(o["rows"]) > 1:
            ev.metrics["rows"] = len(o["rows"])
        points[oid] = ev
    pts = list(points.values())
    if len(pts) > max_points:
        pts = sorted(pts, key=lambda e: -e.metrics["customers_out"])[:max_points]
    return events + pts


def decode_d(payload: Any) -> Any:
    """The decoded ``d`` member (or the payload itself when it has none); raises SourceError for a response that
    is not a row list, including ASP.NET error objects (``{"d": "{\"Message\": ...}"}``)."""
    if isinstance(payload, dict) and "d" not in payload:
        raise SourceError('unexpected response (no "d" member)')
    data = payload.get("d") if isinstance(payload, dict) else payload
    if isinstance(data, str) and data.strip():
        try:
            data = json.loads(data)
        except ValueError as exc:
            raise SourceError('"d" is not valid JSON') from exc
    if isinstance(data, dict) and not any(isinstance(v, list) for v in data.values()):
        msg = clean_text(data.get("Message") or data.get("message") or data.get("error"))
        raise SourceError("error response" + (f": {msg[:200]}" if msg else ' (no row list in "d")'))
    if not isinstance(data, (list, dict)) and data not in ("", None):
        raise SourceError('unexpected response (no row list in "d")')
    return data


@register
class DStringEventOutages(Source):
    type = "dstring_events"
    default_name = "Utility outages"
    category = Category.power
    default_interval = 300
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        payload = await self.get_json(self.option_url(), headers=self.options.get("headers") or {})
        decode_d(payload)
        return parse_rows(
            payload, self.name,
            fields=self.options.get("fields"),
            status_exclude=self.options.get("status_exclude"),
            outage_points=bool(self.options.get("outage_points", True)),
            customers_served=safe_int(self.options.get("customers_served")),
            link=self.options.get("link"),
            tz=zone(self.options.get("timezone"), self.cfg.states),
            max_points=safe_int(self.options.get("max_points")) or 5000,
            iso_local=bool(self.options.get("iso_local", False)),
            bbox=self.options.get("bbox"),
        )
