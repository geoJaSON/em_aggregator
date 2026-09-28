"""Power outages from utilities on CFA Software's OutageEntry map (``www.outageentry.com/Outage/outage.php?Client=X``).

The map loads its outage markers with a form POST (the generic ``json`` adapter only GETs)::

    POST https://www.outageentry.com/Outage/ajax/ajaxShellOut.php
    Origin: https://www.outageentry.com
    Referer: https://www.outageentry.com/Outage/outage.php?Client=<CLIENT>&serviceIndex=1&openingPage=
    X-Requested-With: XMLHttpRequest

    action=get&client=<CLIENT>&target=cfa_device_markers&serviceIndex=1&port=&includePrecictions=
    &includeIndividual=true&includeComments=false&devicesToPolygonize=[]&dataUrl=null

    -> {"0": {"markers": [{"consumers_affected": "1184", "estimated_restore_time": "2026-09-27 16:30:00",
                           "start_date": "2026-09-27 11:52:00", ...}, ...]}}

(the form, including the map's own ``includePrecictions`` spelling, is exactly what the working collector in
lukesteve03/OpenSourcePowerOutageScraper ``outageentry_base.py`` sends). Each marker is one out device (or, with
``includeIndividual``, one individually reported outage) and ``consumers_affected`` its customers out; the
utility total is their sum, as in that collector. The feed publishes no customers-served figure, so the catalog
sets ``customers_served`` per utility (that repo's ``missing_customers_served.json``) for the percentage.

**Positions and ids.** Only the three fields above are confirmed by working code. A marker becomes a point when it
carries a plausible US position under a common spelling (``lat``/``lng``, ``latitude``/``longitude``, ...; see
``emagg.sources.sienatech.lonlat``); its id is the marker's own id field when it has one, else a stable hash of its
position and start time. Markers without a position still count in the total. There is no county table.

**Quiet days.** ``{"0": {}}``, ``{"0": {"markers": []}}``, ``{"0": []}``, ``{"0": null}``, ``{}`` and ``[]`` all mean no
outages (the collector reads ``raw.get("0") or {}``); they must not be errors, or the scheduler would keep showing
the last poll's outages after power is back. Non-JSON, a scalar, or a block with keys but no ``markers`` (an
``error`` block, say) is an error.

**Times** are assumed to be MySQL-style local times (unverified: no recorded payload was available); they are read
in the ``timezone`` option (default: the zone of the utility's states when they share one). A zero date
(``0000-00-00 00:00:00``) or blank ETR means none. Example::

    - id: outageentry_greys_ga
      type: outageentry
      name: GreyStone Power
      states: [GA]
      client: GREYS
      customers_served: 155949
"""

from __future__ import annotations

import hashlib
from typing import Any
from zoneinfo import ZoneInfo

from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.power_common import outage_point_event, utility_total_event
from emagg.sources.local_time import etr_value, local_time, lonlat, safe_int, zone_for
from emagg.util import clean_text, get_ci

BASE = "https://www.outageentry.com/Outage/"
AJAX = "ajax/ajaxShellOut.php"
ORIGIN = "https://www.outageentry.com"


def request_form(client: str, service_index: int = 1) -> dict[str, str]:
    """The marker request the map itself sends."""
    return {
        "action": "get",
        "client": client,
        "target": "cfa_device_markers",
        "serviceIndex": str(service_index),
        "port": "",
        "includePrecictions": "",  # sic: the map's spelling
        "includeIndividual": "true",
        "includeComments": "false",
        "devicesToPolygonize": "[]",
        "dataUrl": "null",
    }


def map_url(client: str, service_index: int = 1, base: str = BASE) -> str:
    return f"{base}outage.php?Client={client}&serviceIndex={service_index}&openingPage="


def markers_block(payload: Any) -> dict[str, Any] | None:
    """The per-service block holding ``markers`` (``{"0": {...}}``; also a list or a bare block)."""
    if isinstance(payload, list):
        return next((b for b in payload if isinstance(b, dict) and "markers" in b), None)
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("0"), dict):
        return payload["0"]
    if "markers" in payload:
        return payload
    return next((b for b in payload.values() if isinstance(b, dict) and "markers" in b), None)


def is_quiet(payload: Any) -> bool:
    """An empty response that means "no outages" rather than an error: ``[]``, ``{}`` or an empty ``"0"`` block
    (``{}``, ``[]`` or null). A block with markers in it, even none, is read normally."""
    if payload == [] or payload == {}:
        return True
    if not isinstance(payload, dict) or "0" not in payload or any(k in payload for k in ("error", "errors")):
        return False
    return payload["0"] is None or payload["0"] == [] or payload["0"] == {}


def markers(payload: Any) -> list[dict[str, Any]]:
    block = markers_block(payload) or {}
    items = block.get("markers")
    if isinstance(items, dict):  # a PHP associative array keyed by marker id
        items = list(items.values())
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _marker_id(m: dict[str, Any], pos: tuple[float, float]) -> str:
    raw = get_ci(m, "id", "outage_id", "outageId", "device_id", "deviceId", "marker_id", "event_id", "incident_id")
    if raw not in (None, ""):
        return str(raw)
    key = f"{pos[0]:.5f},{pos[1]:.5f}:{m.get('start_date')}"
    return "oe-" + hashlib.sha1(key.encode()).hexdigest()[:12]


def parse_markers(
    payload: Any,
    utility: str,
    states: list[str] | None = None,
    *,
    link: str | None = None,
    customers_served: int | None = None,
    outage_points: bool = True,
    max_points: int = 500,
    tz: ZoneInfo | None = None,
) -> list[Event]:
    """Marker response to a utility total and one point per located marker (the largest ``max_points``). A marker
    id listed twice is added up first (position, times and cause from its first record), so the point's title and
    severity describe the sum."""
    items = markers(payload)
    counts = [(m, max(0, safe_int(m.get("consumers_affected")) or 0)) for m in items]
    active = [(m, n) for m, n in counts if n > 0]
    total = utility_total_event(
        utility, sum(n for _, n in active), customers_served if customers_served and customers_served > 0 else None,
        len(active), link=link, states=list(states or []),
    )
    events = [total]
    if not outage_points:
        return events
    rows: dict[str, list[Any]] = {}
    for m, n in active:
        pos = lonlat(m)
        if pos is None:
            continue
        key = _marker_id(m, pos)
        if key in rows:  # one device listed twice: add up
            rows[key][2] += n
        else:
            rows[key] = [m, pos, n]
    points = []
    for key, (m, pos, n) in rows.items():
        ev = outage_point_event(
            utility, key, pos[0], pos[1], n,
            cause=get_ci(m, "cause", "outage_cause", "reason"),
            etr=etr_value(m.get("estimated_restore_time"), tz),
            started=local_time(m.get("start_date"), tz),
            link=link,
        )
        device = clean_text(get_ci(m, "device_name", "device", "name"))
        if device:
            ev.metrics["device"] = device
        points.append(ev)
    events.extend(sorted(points, key=lambda e: -e.metrics["customers_out"])[: max(0, int(max_points))])
    return events


@register
class OutageEntryOutages(Source):
    type = "outageentry"
    default_name = "Utility outages (OutageEntry)"
    category = Category.power
    default_interval = 300
    required_options = ("client",)

    async def fetch(self) -> list[Event]:
        client = str(self.options["client"]).strip()
        index = safe_int(self.options.get("service_index", 1))
        index = 1 if index is None else index
        base = str(self.options.get("base_url") or BASE).rstrip("/") + "/"
        headers = {
            "Origin": ORIGIN,
            "Referer": map_url(client, index, base),
            "X-Requested-With": "XMLHttpRequest",
            **(self.options.get("headers") or {}),
        }
        resp = await self.ctx.http.post(base + AJAX, data=request_form(client, index), headers=headers)
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from OutageEntry ({client})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceError(f"invalid JSON from OutageEntry ({client}: unknown client?)") from exc
        if not is_quiet(payload):
            block = markers_block(payload)
            if block is None or "markers" not in block:
                shown = payload if block is None else block
                keys = ", ".join(sorted(map(str, shown))[:5]) if isinstance(shown, dict) else type(shown).__name__
                raise SourceError(f"unexpected OutageEntry response for {client} (no markers; got {keys or 'empty'})")
        return parse_markers(
            payload, self.name, self.cfg.states,
            link=self.options.get("link") or map_url(client, index, base),
            customers_served=safe_int(self.options.get("customers_served")),
            outage_points=bool(self.options.get("outage_points", True)),
            max_points=int(self.options.get("max_points", 500)),
            tz=zone_for(self.cfg.states, self.options.get("timezone")),
        )
