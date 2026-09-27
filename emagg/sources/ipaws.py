"""FEMA IPAWS public alert feed: CAP alerts from every alerting authority, not just NWS.

This is where county/state emergency managers' public warnings appear: evacuation orders, shelter-in-place,
civil emergencies, 911 outages, boil-water notices. Public REST feed, no key; FEMA asks for polling no more
often than every 2 minutes. NWS alerts also flow through IPAWS and are skipped by default (the nws_alerts
source already has them, with better geometry).

The feed returns alerts *sent since* a timestamp, so the adapter keeps the active set between polls.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any

from emagg import regions
from emagg.geo import merge_polygons, simplify_geometry
from emagg.models import Category, Event, Severity, utcnow
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text, parse_time

FEED = "https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE/rest/{feed}/recent/{since}"

CAP_SEVERITY = {"extreme": Severity.extreme, "severe": Severity.severe, "moderate": Severity.moderate, "minor": Severity.minor}

# SAME event codes used by non-weather authorities -> (category, minimum severity). None = skip by default.
SAME_EVENTS: dict[str, tuple[Category, Severity] | None] = {
    "EVI": (Category.other, Severity.extreme),  # Evacuation Immediate
    "SPW": (Category.other, Severity.severe),  # Shelter in Place Warning
    "CEM": (Category.other, Severity.severe),  # Civil Emergency Message
    "CDW": (Category.other, Severity.severe),  # Civil Danger Warning
    "LAE": (Category.other, Severity.moderate),  # Local Area Emergency
    "LEW": (Category.other, Severity.severe),  # Law Enforcement Warning
    "HMW": (Category.other, Severity.severe),  # Hazardous Materials Warning
    "NUW": (Category.other, Severity.extreme),  # Nuclear Power Plant Warning
    "RHW": (Category.other, Severity.extreme),  # Radiological Hazard Warning
    "TOE": (Category.comms, Severity.severe),  # 911 Telephone Outage Emergency
    "FRW": (Category.fire, Severity.severe),  # Fire Warning
    "EQW": (Category.seismic, Severity.severe),  # Earthquake Warning
    "TSW": (Category.flood, Severity.extreme),  # Tsunami Warning
    "DBW": (Category.flood, Severity.extreme),  # Dam Break Warning (not an NWS product in all areas)
    "CAE": None,  # Child Abduction Emergency (AMBER)
    "BLU": None,  # Blue Alert
    "ADR": None,  # Administrative Message
    "DMO": None,  # Practice/Demo
    "RWT": None,  # Required Weekly Test
    "RMT": None,  # Required Monthly Test
    "NPT": None,  # National Periodic Test
}

CAP_CATEGORY = {
    "Fire": Category.fire,
    "Met": Category.weather,
    "Transport": Category.transport,
    "Geo": Category.seismic,
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _local(c.tag) == name]


def _text(el: ET.Element, name: str) -> str | None:
    for c in el:
        if _local(c.tag) == name:
            return clean_text(c.text)
    return None


def _pairs(el: ET.Element, name: str) -> list[tuple[str, str]]:
    out = []
    for c in _kids(el, name):
        k, v = _text(c, "valueName"), _text(c, "value")
        if k and v:
            out.append((k, v))
    return out


def _cap_polygon(text: str) -> dict[str, Any] | None:
    ring = []
    for pair in text.split():
        try:
            lat, lon = (float(x) for x in pair.split(","))
        except ValueError:
            return None
        ring.append([round(lon, 5), round(lat, 5)])
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]} if len(ring) >= 4 else None


def _cap_circle(text: str) -> dict[str, Any] | None:
    try:
        center, radius = text.split()
        lat, lon = (float(x) for x in center.split(","))
        r_km = float(radius)
    except ValueError:
        return None
    if r_km <= 0:
        return None
    ring = []
    for i in range(25):
        a = 2 * math.pi * (i % 24) / 24
        dlat = r_km / 111.32 * math.cos(a)
        dlon = r_km / (111.32 * max(math.cos(math.radians(lat)), 0.01)) * math.sin(a)
        ring.append([round(lon + dlon, 5), round(lat + dlat, 5)])
    return {"type": "Polygon", "coordinates": [ring]}


def _is_nws(sender: str | None, sender_name: str | None) -> bool:
    s = (sender or "").lower()
    return s.endswith("@noaa.gov") or s.startswith("w-nws") or (sender_name or "").upper().startswith("NWS ")


def parse_cap_feed(
    xml_text: str, include_nws: bool = False, now: datetime | None = None
) -> tuple[list[Event], set[str]]:
    """Return (events, identifiers that were cancelled or superseded)."""
    now = now or utcnow()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise SourceError(f"invalid CAP XML: {exc}") from exc
    alerts = [root] if _local(root.tag) == "alert" else [el for el in root.iter() if _local(el.tag) == "alert"]
    events: list[Event] = []
    removed: set[str] = set()
    fips_to_postal = {s["fips"]: s["code"] for s in regions._data()["states"]}
    for al in alerts:
        ident = _text(al, "identifier")
        if not ident or (_text(al, "status") or "").lower() != "actual":
            continue
        refs = [r.split(",")[1] for r in (_text(al, "references") or "").split() if r.count(",") >= 2]
        msg_type = (_text(al, "msgType") or "").lower()
        if msg_type == "cancel":
            removed.update(refs)
            continue
        if (_text(al, "scope") or "public").lower() != "public":
            continue
        infos = _kids(al, "info")
        if not infos:
            continue
        info = next((i for i in infos if (_text(i, "language") or "en-US").lower().startswith("en")), infos[0])
        sender, sender_name = _text(al, "sender"), _text(info, "senderName")
        if not include_nws and _is_nws(sender, sender_name):
            continue
        expires = parse_time(_text(info, "expires"))
        if expires and expires < now:
            removed.add(ident)
            continue
        codes = dict(_pairs(info, "eventCode"))
        same = (codes.get("SAME") or "").upper()
        rule = SAME_EVENTS.get(same, "unknown")
        if rule is None:
            continue
        cap_cat = next((c.text for c in _kids(info, "category") if c.text), "Other")
        category = rule[0] if isinstance(rule, tuple) else CAP_CATEGORY.get(cap_cat, Category.other)
        event_name = _text(info, "event") or "Public alert"
        if category == Category.weather and any(w in event_name.lower() for w in ("flood", "surge", "dam")):
            category = Category.flood
        severity = CAP_SEVERITY.get((_text(info, "severity") or "").lower(), Severity.info)
        if isinstance(rule, tuple) and rule[1].rank > severity.rank:
            severity = rule[1]

        geoms, states, area_desc = [], set(), []
        for area in _kids(info, "area"):
            if _text(area, "areaDesc"):
                area_desc.append(_text(area, "areaDesc"))
            shapes = [g for p in _kids(area, "polygon") if p.text and (g := _cap_polygon(p.text))]
            shapes += [g for c in _kids(area, "circle") if c.text and (g := _cap_circle(c.text))]
            for name, value in _pairs(area, "geocode"):
                if name.upper() not in ("SAME", "FIPS6") or len(value) != 6 or not value.isdigit():
                    continue
                st = fips_to_postal.get(value[1:3])
                if st:
                    states.add(st)
                if not shapes and value[3:] != "000":
                    county = regions.county_by_fips(value[1:])
                    if county:
                        geoms.append(regions.county_geometry(county))
            geoms = shapes + geoms if shapes else geoms
        geometry = merge_polygons(geoms)
        if geometry:
            geometry = simplify_geometry(geometry, tolerance=0.002)
        parts = [_text(info, "headline"), _text(info, "description"), _text(info, "instruction")]
        events.append(
            Event(
                id=ident,
                category=category,
                title=event_name + (f" — {sender_name}" if sender_name else ""),
                severity=severity,
                description="\n\n".join(p for p in parts if p) or None,
                area="; ".join(area_desc) or None,
                geometry=geometry,
                starts_at=parse_time(_text(info, "onset") or _text(info, "effective") or _text(al, "sent")),
                updated_at=parse_time(_text(al, "sent")),
                expires_at=expires,
                url=_text(info, "web") if (_text(info, "web") or "").startswith("http") else None,
                states=sorted(states),
                metrics={
                    "kind": "ipaws_alert",
                    "same_code": same or None,
                    "sender": sender_name or sender,
                    "urgency": _text(info, "urgency"),
                    "certainty": _text(info, "certainty"),
                    "cap_category": cap_cat,
                },
                supersedes=refs,
            )
        )
        removed.update(refs)
    return events, removed


@register
class IPAWSAlerts(Source):
    type = "ipaws"
    default_name = "Public alerts (FEMA IPAWS)"
    category = Category.other
    default_interval = 120  # FEMA: no more often than every 2 minutes

    def __init__(self, cfg, ctx):
        super().__init__(cfg, ctx)
        self._active: dict[str, Event] = {}
        self._since: datetime | None = None

    async def fetch(self) -> list[Event]:
        now = utcnow()
        since = self._since or now - timedelta(hours=float(self.options.get("lookback_hours", 24)))
        url = FEED.format(feed=self.options.get("feed", "public"), since=since.strftime("%Y-%m-%dT%H:%M:%SZ"))
        resp = await self.ctx.http.get(url, headers={"Accept": "application/xml"})
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from IPAWS feed")
        events, removed = parse_cap_feed(resp.text, bool(self.options.get("include_nws", False)), now)
        for rid in removed:
            self._active.pop(rid, None)
        for e in events:
            self._active[e.id] = e
        self._active = {k: e for k, e in self._active.items() if not e.expires_at or e.expires_at > now}
        self._since = now - timedelta(minutes=5)  # overlap so nothing slips between polls
        return list(self._active.values())
