"""Helpers for small utility outage feeds: wall-clock times such as ``"02:04 PM on 09/27"`` or
``"Sep 27, 3:56 p.m."``, and customer counts that may be NaN.

Some outage maps publish the time the way their page shows it: in the utility's local time and often
without a year. ``parse_local`` reads such strings in a given time zone and picks the year that puts the
result closest to now. ISO-8601 strings with an offset and epoch numbers are left to
``emagg.util.parse_time``. Nothing here raises on bad input.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from emagg.geo import decode_polyline
from emagg.util import clean_text, get_ci, num, parse_time

# The zone that covers most of each state's population, for feeds that give local times only.
STATE_ZONES = {
    **dict.fromkeys(["CT", "DC", "DE", "FL", "GA", "IN", "KY", "MA", "MD", "ME", "MI", "NC", "NH", "NJ", "NY",
                     "OH", "PA", "RI", "SC", "VA", "VT", "WV"], "America/New_York"),
    **dict.fromkeys(["AL", "AR", "IA", "IL", "KS", "LA", "MN", "MO", "MS", "ND", "NE", "OK", "SD", "TN", "TX",
                     "WI"], "America/Chicago"),
    **dict.fromkeys(["CO", "ID", "MT", "NM", "UT", "WY"], "America/Denver"),
    "AZ": "America/Phoenix",
    **dict.fromkeys(["CA", "NV", "OR", "WA"], "America/Los_Angeles"),
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
}

_MS_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")  # ASP.NET JSON dates
_OFFSET = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")
# Intergraph-style date-time stamps, "YYYYMMDDHHMMSS" plus an optional US zone code ("20260927091200MD").
_DTS = re.compile(r"^((?:19|20)\d{12})([A-Z]{2})?$")
_DTS_ZONES = {"ES": -5, "ED": -4, "CS": -6, "CD": -5, "MS": -7, "MD": -6, "PS": -8, "PD": -7, "HS": -10}


def safe_int(value: Any) -> int | None:
    """``emagg.util.to_int`` that gives None for NaN and infinity (Python's json accepts both) instead of raising."""
    n = num(value)
    return int(round(n)) if n is not None and math.isfinite(n) else None


def zone(name: Any = None, states: Iterable[str] | None = None) -> ZoneInfo | None:
    """The configured time zone name, else the zone of the first state that has one."""
    keys = [str(name)] if name else []
    keys += [STATE_ZONES[s] for s in (states or []) if s in STATE_ZONES]
    for key in keys:
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def _normalize(text: str) -> str:
    text = " ".join(text.replace("\xa0", " ").split())
    text = re.sub(r"\b([ap])\.?\s?m\.?(?=\s|$|,)", lambda m: m.group(1).upper() + "M", text, flags=re.I)
    text = re.sub(r"\bnoon\b", "12:00 PM", text, flags=re.I)
    return re.sub(r"\bmidnight\b", "12:00 AM", text, flags=re.I)


def parse_local(
    value: Any, formats: Iterable[str], tz: ZoneInfo | None, now: datetime | None = None, max_ahead_days: int = 45
) -> datetime | None:
    """A local wall-clock string in one of ``formats`` (``strptime`` syntax, with or without ``%Y``) as UTC.

    Without a year, the latest year that puts the time no more than ``max_ahead_days`` after ``now`` wins (so
    "12/31" read on January 1st is last year, while an ETR a few days ahead stays ahead). Without a time zone,
    the text is read as UTC.
    """
    if value is None:
        return None
    text = _normalize(str(value))
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    ref_year = now.astimezone(tz).year if tz else now.year
    for fmt in formats:
        if "%Y" in fmt or "%y" in fmt:
            candidates = [(text, fmt)]
        else:
            candidates = [(f"{text} {y}", f"{fmt} %Y") for y in (ref_year - 1, ref_year, ref_year + 1)]
        found: list[datetime] = []
        for txt, f in candidates:
            try:
                naive = datetime.strptime(txt, f)
            except ValueError:
                continue
            found.append(naive.replace(tzinfo=tz or timezone.utc).astimezone(timezone.utc).replace(microsecond=0))
        if found:
            limit = now + timedelta(days=max_ahead_days)
            return max((dt for dt in found if dt <= limit), default=min(found))
    return None


def _as_utc(naive: datetime, tz: Any) -> datetime:
    return naive.replace(tzinfo=tz).astimezone(timezone.utc).replace(microsecond=0)


def parse_any(
    value: Any, formats: Iterable[str], tz: ZoneInfo | None, now: datetime | None = None, min_year: int = 2000,
    naive_utc: bool = False,
) -> datetime | None:
    """Epoch numbers, ``/Date(ms)/``, ISO-8601, Intergraph-style ``YYYYMMDDHHMMSS[zone]`` stamps or one of
    ``formats``.

    Machine-format times without a zone (ISO-8601 with no offset, stamps with no zone code) are read in ``tz``,
    or as UTC with ``naive_utc`` (what .NET serializers write for a raw database time). Display strings in
    ``formats`` are always read in ``tz``. Times before ``min_year`` are treated as missing: outage systems use
    dates like 1990-01-01 for "no ETR".
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    dt: datetime | None = None
    text = str(value).strip()
    naive_tz = timezone.utc if naive_utc or tz is None else tz
    m = _MS_DATE.match(text)
    dts = _DTS.match(text)
    try:
        if m:
            dt = parse_time(int(m.group(1)))
        elif dts:
            code = dts.group(2)
            zone_of = timezone(timedelta(hours=_DTS_ZONES[code])) if code in _DTS_ZONES else naive_tz
            dt = _as_utc(datetime.strptime(dts.group(1), "%Y%m%d%H%M%S"), zone_of)
        elif isinstance(value, (int, float)) or text.lstrip("-").isdigit():
            dt = parse_time(value)
        elif re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d", text) and not _OFFSET.search(text):
            dt = _as_utc(datetime.fromisoformat(text.replace(" ", "T", 1)), naive_tz)
        else:
            dt = parse_time(text) or parse_local(text, formats, tz, now)
    except (ValueError, OverflowError):
        return None
    if dt is None or dt.year < min_year:
        return None
    return dt


# Helpers shared by the Sienatech and OutageEntry co-op adapters (moved here from sienatech.py).

# --- times ------------------------------------------------------------------------------------------------------

# States that lie in a single time zone (split states such as TX, FL, TN, KY need an explicit ``timezone``).
_ZONES = {
    "America/New_York": "CT DC DE GA MA MD ME NC NH NJ NY OH PA RI SC VA VT WV",
    "America/Chicago": "AL AR IA IL LA MN MO MS OK WI",
    "America/Denver": "CO MT NM UT WY",
    "America/Phoenix": "AZ",
    "America/Los_Angeles": "CA NV WA",
    "America/Anchorage": "AK",
    "Pacific/Honolulu": "HI",
}
_TZ_BY_STATE = {st: zone for zone, codes in _ZONES.items() for st in codes.split()}
_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M")


def zone_for(states: list[str] | None, name: Any = None) -> ZoneInfo | None:
    """The configured zone, else the one zone all of the utility's states share (None when unknown)."""
    key = clean_text(name)
    if not key:
        zones = {_TZ_BY_STATE.get(str(s).upper()) for s in states or []}
        if len(zones) != 1 or None in zones:
            return None
        key = zones.pop()
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_time(value: Any, tz: ZoneInfo | None) -> datetime | None:
    """ISO-8601 (with or without offset), ``MM/DD/YYYY hh:mm AM`` or epoch seconds/ms, as UTC. A timestamp
    without an offset is read in ``tz``; with no zone known it is dropped rather than guessed."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        return parse_time(value)
    text = clean_text(value)
    if not text or text.startswith("0000-00-00"):
        return None
    dt = None
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        for fmt in _FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        if tz is None:
            return None
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def etr_value(value: Any, tz: ZoneInfo | None) -> datetime | str | None:
    """A restoration estimate as a time when it is one, else its text ("Assessing"), else None."""
    t = local_time(value, tz)
    if t is not None:
        return t
    text = clean_text(value)
    if not text or text.startswith("0000-00-00") or text.upper() in ("NULL", "NONE", "N/A"):
        return None
    if re.fullmatch(r"[\d\-/: .TZ+]+", text):  # a timestamp we could not place in time
        return None
    return text


# --- positions --------------------------------------------------------------------------------------------------

_US = (-180.0, 15.0, -60.0, 72.0)  # lon/lat box around every state (AK, HI, PR included)
_PAIRS = (("lon", "lat"), ("lng", "lat"), ("longitude", "latitude"), ("long", "lat"), ("x", "y"))
_WRAPPERS = ("position", "location", "latlng", "latLon", "point", "geometry", "geom", "coordinates", "coords")


def _pair(a: Any, b: Any) -> tuple[float, float] | None:
    """(lon, lat) from two numbers in either order; US longitudes are negative and latitudes positive."""
    x, y = num(a), num(b)
    if x is None or y is None:
        return None
    for lon, lat in ((x, y), (y, x)):
        if _US[0] <= lon <= _US[2] and _US[1] <= lat <= _US[3]:
            return round(lon, 6), round(lat, 6)
    return None


def lonlat(rec: Any, _depth: int = 0) -> tuple[float, float] | None:
    """A plausible US (lon, lat) from a record, under the common spellings (see module docstring)."""
    if not isinstance(rec, dict) or _depth > 2:
        return None
    for kx, ky in _PAIRS:
        x, y = get_ci(rec, kx), get_ci(rec, ky)
        if x is not None and y is not None and (p := _pair(x, y)):
            return p
    for key in _WRAPPERS:
        v = get_ci(rec, key)
        if isinstance(v, str):
            parts = [p for p in re.split(r"[,\s]+", v.strip()) if p]
            if len(parts) == 2 and (p := _pair(parts[1], parts[0])):
                return p
        elif isinstance(v, dict):
            coords = v.get("coordinates")
            if str(v.get("type", "")).lower() == "point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
                if p := _pair(coords[0], coords[1]):
                    return p
            elif p := lonlat(v, _depth + 1):
                return p
        elif isinstance(v, (list, tuple)) and len(v) >= 2 and not isinstance(v[0], (list, dict)):
            if p := _pair(v[0], v[1]):
                return p
    g = rec.get("g")
    if isinstance(g, str) and g:
        try:
            lat, lon = decode_polyline(g)[0]
        except (IndexError, ValueError, TypeError):
            pass
        else:
            if p := _pair(lon, lat):
                return p
    # Last resort: "gps_lat"/"gps_lng", "deviceLatitude"/"deviceLongitude" ...
    lat_k = next((k for k in rec if re.search(r"(lat|latitude)$", str(k), re.I)), None)
    lon_k = next((k for k in rec if re.search(r"(lon|lng|_long|longitude)$", str(k), re.I)), None)
    if lat_k and lon_k:
        return _pair(rec[lon_k], rec[lat_k])
    return None
