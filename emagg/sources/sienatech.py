"""Power outages from co-op outage maps built on Siena Technologies' WebMaps (``<code>.maps.sienatech.com``).

Each utility ("code", e.g. ``SSEMC``) has one public JSON document behind its outage map::

    GET https://cache.sienatech.com/apex/siena_ords/webmaps/data/<CODE>/OUTAGE

    {"reportData": {
        "summary": {"affected": 1874, "accounts": 111265, "outages": 6},
        "reports": [{"name": "County", "polygons": [
                        {"name": "Henry", "affected": 1210, "accounts": 45812, "outages": 3,
                         "etor": "...", "outageStart": "...", "percentAffected": 2.64}, ...]},
                    {"name": "District", "polygons": [...]},
                    {"name": "Zip", "polygons": [...]}]},
     "outageData": {"outages": [{"id": 2609271, "county": "Henry", "customersAffected": 980,
                                 "etor": "...", "outageStart": "..."}, ...]}}

**Reports are area tables.** Each ``reports[]`` entry is one way of cutting the service territory, named by its
area type; its ``polygons`` are the areas of that type. Siena maps publish County, District and Zip tables (the
2017 JEMC map had exactly those three: simonw/disaster-data ``irma-2017-archive/jemc-outages.json``), and the
working collector (lukesteve03/OpenSourcePowerOutageScraper ``sienatech_base.py``) reads the report named
"County" as counties. Only a county/parish report becomes county events; district, substation, ZIP or
service-area tables are not counties and are never matched to one.

**Totals** come from ``reportData.summary`` (``affected`` = customers out, ``accounts`` = customers served,
``outages`` = active outages), falling back to the county report, then the outage list.

**Outage points.** ``outageData.outages[]`` has the outage id, county, customers and times. None of the code
that reads this feed uses a position, so the field names are unverified: an outage is drawn as a point only when
it carries a plausible US position under a common spelling (``lat``/``lon``, ``latitude``/``longitude``,
``x``/``y`` in degrees, a ``"lat,lon"`` ``position`` string as in the older Siena feed, a GeoJSON point, or an
encoded polyline ``g`` as in Siena's line layer). Outages without one still count in the total and county.

**Times** (``etor``, ``outageStart``) are unverified: no recorded payload was available. A timestamp with an offset
or ``Z`` is taken at its word; a naive one is read in the ``timezone`` option (default: the zone of the utility's
states when they share one) and dropped when no zone is known. Assumption to check against the map UI once the
host is reachable: ORDS serialises Oracle DATE columns with a ``Z`` even when they hold local time, which would
shift these times by the UTC offset. If a live ETR proves to be local time, add an option to read ``Z`` times
as local; nothing here guesses.

Siena answers HTTP 420 when it throttles a client; keep the interval generous (default 10 minutes). Example::

    - id: sienatech_ssemc_ga
      type: sienatech
      name: Snapping Shoals EMC
      states: [GA]
      code: SSEMC
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from emagg import regions
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.local_time import etr_value, local_time, lonlat, safe_int, zone_for  # noqa: F401  (re-exported)
from emagg.sources.power_common import county_outage_event, outage_point_event, utility_total_event
from emagg.util import clean_text, get_ci

BASE = "https://cache.sienatech.com/apex/siena_ords/webmaps/data"

_COUNTY_WORDS = {"county", "counties", "parish", "parishes", "borough", "boroughs"}
_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")  # "COUNTY_REPORT", "CountyOutages", "outageCounty"
_STATE_SUFFIX = re.compile(r"^(?P<name>.+?)\s*(?:,\s*|\(\s*)(?P<st>[A-Za-z]{2})\s*\)?$")


# --- counties ---------------------------------------------------------------------------------------------------


def is_county_table(name: Any) -> bool:
    """A report named for counties ("County", "Counties", "Parish", "COUNTY_REPORT", "CountyOutages")."""
    return any(w.lower() in _COUNTY_WORDS for w in _WORDS.findall(str(name or "")))


def county_report(reports: list[Any], name: str | None = None) -> dict[str, Any] | None:
    """The report holding counties: ``name`` when configured, else the first named County/Counties/Parish."""
    reps = [r for r in reports or [] if isinstance(r, dict)]
    if name:
        want = str(name).strip().lower()
        return next((r for r in reps if str(r.get("name") or "").strip().lower() == want), None)
    return next((r for r in reps if is_county_table(r.get("name"))), None)


def match_county(label: Any, states: list[str]) -> dict[str, Any] | None:
    """County record for a polygon name ("Henry", "HENRY COUNTY", "Middlesex, MA", "13151") within the
    utility's states. A name found in more than one of them is skipped rather than guessed; a FIPS code
    outside them (say a ZIP code in a table configured as counties) is not a county of this utility."""
    text = clean_text(label)
    if not text:
        return None
    if text.isdigit():
        c = regions.county_by_fips(text) if len(text) == 5 else None
        return c if c and (not states or c["state"] in states) else None
    m = _STATE_SUFFIX.match(text)
    if m and m.group("st").upper() in regions.state_codes():
        return regions.find_county(m.group("st").upper(), m.group("name"))
    found = {c["fips"]: c for st in states or [] if st and (c := regions.find_county(st, text))}
    return next(iter(found.values())) if len(found) == 1 else None


def later_etr(a: datetime | str | None, b: datetime | str | None) -> datetime | str | None:
    """Of two restoration estimates, the later time; a time beats text, and text beats nothing."""
    if isinstance(a, datetime) and isinstance(b, datetime):
        return max(a, b)
    if isinstance(b, datetime):
        return b
    return a if a is not None else b


def parse_counties(
    report: dict[str, Any] | None, utility: str, states: list[str], *, updated: Any = None,
    link: str | None = None, tz: ZoneInfo | None = None,
) -> list[Event]:
    """One event per county of the county report. A county listed more than once ("Henry" and "HENRY COUNTY")
    is added up first, so its title, percentage and severity describe the sum."""
    rows: dict[str, dict[str, Any]] = {}
    for poly in (report or {}).get("polygons") or []:
        if not isinstance(poly, dict):
            continue
        out = safe_int(poly.get("affected")) or 0
        if out <= 0:
            continue
        county = match_county(poly.get("name"), states)
        if county is None:
            continue
        row = rows.setdefault(county["fips"], {"county": county, "out": 0, "served": 0, "outages": None, "etr": None})
        row["out"] += out
        served = safe_int(poly.get("accounts"))
        # Served is only meaningful when every row that adds customers out also gives its customer count.
        row["served"] = row["served"] + served if row["served"] is not None and served and served > 0 else None
        n_out = safe_int(get_ci(poly, "outages", "outageCount"))
        if n_out is not None and n_out >= 0:
            row["outages"] = (row["outages"] or 0) + n_out
        row["etr"] = later_etr(row["etr"], etr_value(poly.get("etor"), tz))
    events = []
    for row in rows.values():
        etr = row["etr"]
        ev = county_outage_event(
            utility, row["county"]["state"], row["county"], row["out"], row["served"] or None,
            etr=etr.isoformat() if isinstance(etr, datetime) else etr, updated=updated, link=link,
        )
        if row["outages"] is not None:
            ev.metrics["outages"] = row["outages"]
        events.append(ev)
    return events


# --- outages and the whole document -----------------------------------------------------------------------------


def _outage_id(o: dict[str, Any], pos: tuple[float, float]) -> str:
    raw = get_ci(o, "id", "outageId", "outage_id", "incidentId")
    if raw not in (None, ""):
        return str(raw)
    key = f"{pos[0]:.5f},{pos[1]:.5f}:{get_ci(o, 'outageStart')}"
    return "pt-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def parse_outages(
    outages: list[Any], utility: str, *, link: str | None = None, tz: ZoneInfo | None = None,
    updated: Any = None, max_points: int = 500,
) -> list[Event]:
    """One point per located outage (the largest ``max_points``). An outage id listed twice is added up first
    (position, times and cause from its first record), so the point's title and severity describe the sum."""
    rows: dict[str, list[Any]] = {}
    for o in outages or []:
        if not isinstance(o, dict):
            continue
        n = safe_int(get_ci(o, "customersAffected", "affected")) or 0
        pos = lonlat(o) if n > 0 else None
        if pos is None:
            continue
        key = _outage_id(o, pos)
        if key in rows:
            rows[key][2] += n
        else:
            rows[key] = [o, pos, n]
    events = []
    for key, (o, pos, n) in rows.items():
        ev = outage_point_event(
            utility, key, pos[0], pos[1], n,
            cause=get_ci(o, "cause", "outageCause"), etr=etr_value(get_ci(o, "etor"), tz),
            started=local_time(get_ci(o, "outageStart"), tz), updated=updated, link=link,
        )
        county = clean_text(get_ci(o, "county"))
        if county:
            ev.metrics["county"] = county
        events.append(ev)
    ranked = sorted(events, key=lambda e: -e.metrics["customers_out"])
    return ranked[: max(0, int(max_points))]


def _updated(payload: dict[str, Any], tz: ZoneInfo | None) -> datetime | None:
    rd = payload.get("reportData") if isinstance(payload.get("reportData"), dict) else {}
    for part in (rd.get("summary"), rd, payload):
        if isinstance(part, dict):
            raw = get_ci(part, "lastUpdated", "lastUpdate", "updated", "updateTime", "asOf", "timestamp")
            if raw is not None and (t := local_time(raw, tz)):
                return t
    return None


def parse_outage_data(
    payload: Any,
    utility: str,
    states: list[str] | None = None,
    *,
    link: str | None = None,
    customers_served: int | None = None,
    county_report_name: str | None = None,
    counties: bool = True,
    outage_points: bool = True,
    max_points: int = 500,
    tz: ZoneInfo | None = None,
) -> list[Event]:
    """The OUTAGE document to a utility total, county events (county report only) and located outages."""
    states = list(states or [])
    payload = payload if isinstance(payload, dict) else {}
    rd = payload.get("reportData") if isinstance(payload.get("reportData"), dict) else {}
    summary = rd.get("summary") if isinstance(rd.get("summary"), dict) else {}
    reports = rd.get("reports") if isinstance(rd.get("reports"), list) else []
    od = payload.get("outageData") if isinstance(payload.get("outageData"), dict) else {}
    raw_outages = od.get("outages")
    outages = [o for o in raw_outages if isinstance(o, dict)] if isinstance(raw_outages, list) else []
    report = county_report(reports, county_report_name)
    updated = _updated(payload, tz)

    out = safe_int(summary.get("affected"))
    if out is None and report is not None:
        out = sum(max(0, safe_int(p.get("affected")) or 0) for p in report.get("polygons") or [] if isinstance(p, dict))
    if out is None:
        out = sum(max(0, safe_int(get_ci(o, "customersAffected", "affected")) or 0) for o in outages)
    served = customers_served or safe_int(summary.get("accounts"))
    n_outages = safe_int(summary.get("outages"))
    if n_outages is not None and n_outages < 0:
        n_outages = None
    if n_outages is None and outages:
        n_outages = len(outages)
    events = [utility_total_event(
        utility, max(0, out), served if served and served > 0 else None, n_outages,
        updated=updated, link=link, states=states,
    )]
    if counties:
        events.extend(parse_counties(report, utility, states, updated=updated, link=link, tz=tz))
    if outage_points:
        events.extend(parse_outages(outages, utility, link=link, tz=tz, updated=updated, max_points=max_points))
    return events


@register
class SienatechOutages(Source):
    type = "sienatech"
    default_name = "Co-op outages (Sienatech)"
    category = Category.power
    default_interval = 600
    required_options = ("code",)

    def url(self) -> str:
        if self.options.get("url"):
            return str(self.options["url"])
        base = str(self.options.get("base_url") or BASE).rstrip("/")
        code = quote(str(self.options["code"]).strip(), safe="")
        return f"{base}/{code}/{quote(str(self.options.get('layer') or 'OUTAGE'), safe='')}"

    async def fetch(self) -> list[Event]:
        resp = await self.ctx.http.get(self.url(), headers=self.options.get("headers") or {})
        if resp.status_code in (420, 429):
            raise SourceError(f"HTTP {resp.status_code} from Sienatech: rate limited, lengthen the interval")
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from Sienatech ({self.options['code']})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceError("invalid JSON from Sienatech") from exc
        parts = [payload.get(k) for k in ("reportData", "outageData")] if isinstance(payload, dict) else []
        if not any(isinstance(p, dict) for p in parts):
            raise SourceError("unexpected Sienatech response (no reportData/outageData)")
        return parse_outage_data(
            payload, self.name, self.cfg.states,
            link=self.options.get("link"),
            customers_served=safe_int(self.options.get("customers_served")),
            county_report_name=self.options.get("county_report"),
            counties=bool(self.options.get("counties", True)),
            outage_points=bool(self.options.get("outage_points", True)),
            max_points=int(self.options.get("max_points", 500)),
            tz=zone_for(self.cfg.states, self.options.get("timezone")),
        )
