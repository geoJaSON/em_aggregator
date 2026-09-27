"""Internet connectivity outages by US state from IODA (Georgia Tech Internet Outage Detection and Analysis).

Public API, no key. IODA watches BGP routing, active probing and network telescope signals per region. A
state-level drop is a useful proxy for widespread communications loss (power, fiber cuts, flooding) where
no carrier data is public. Alerts are point-in-time entries, so we look back a short window and take the
most recent entry per (state, signal): anything not back to "normal" is shown.
"""

from __future__ import annotations

import time
from typing import Any

from emagg import regions
from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.util import num, parse_time

API = "https://api.ioda.inetintel.cc.gatech.edu/v2/outages/alerts"

SIGNAL_NAMES = {
    "bgp": "BGP routing",
    "ping-slash24": "active probing",
    "merit-nt": "network telescope",
    "ucsd-nt": "network telescope",
    "gtr": "Google traffic",
    "gtr-norm": "Google traffic",
}


def parse_ioda(payload: dict[str, Any]) -> list[Event]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    names: dict[str, str] = {}
    for a in payload.get("data") or []:
        ent = a.get("entity") or {}
        if ent.get("type") != "region":
            continue
        attrs = ent.get("attrs") or {}
        if attrs.get("country_code") not in (None, "US"):
            continue
        code = regions.state_code_for_name(ent.get("name", ""))
        if not code:
            continue
        key = (code, a.get("datasource") or "?")
        if key not in latest or (a.get("time") or 0) > (latest[key].get("time") or 0):
            latest[key] = a
            names[code] = str(ent.get("code"))
    by_state: dict[str, list[dict[str, Any]]] = {}
    for (code, _), a in latest.items():
        if str(a.get("level", "")).lower() in ("critical", "warning"):
            by_state.setdefault(code, []).append(a)

    events = []
    for code, alerts in by_state.items():
        critical = [a for a in alerts if str(a.get("level")).lower() == "critical"]
        if len(critical) >= 2:
            sev = Severity.severe
        elif critical:
            sev = Severity.moderate
        else:
            sev = Severity.minor
        lines, drops = [], []
        for a in sorted(alerts, key=lambda x: x.get("datasource") or ""):
            value, normal = num(a.get("value")), num(a.get("historyValue"))
            drop = round((1 - value / normal) * 100) if value is not None and normal else None
            if drop is not None:
                drops.append(drop)
            label = SIGNAL_NAMES.get(a.get("datasource"), a.get("datasource"))
            lines.append(f"{label}: {a.get('level')}" + (f", {drop}% below normal" if drop is not None else ""))
        lp = regions.state_label_point(code)
        name = regions.state_name(code) or code
        events.append(
            Event(
                id=f"region-{code}",
                category=Category.comms,
                title=f"Internet connectivity drop: {name}",
                severity=sev,
                description="\n".join(lines) + "\n\nState-level signal; see IODA for affected networks and timing.",
                area=name,
                geometry=point(*lp) if lp else None,
                starts_at=parse_time(min(a.get("time") or 0 for a in alerts)),
                updated_at=parse_time(max(a.get("time") or 0 for a in alerts)),
                url=f"https://ioda.inetintel.cc.gatech.edu/region/{names[code]}",
                states=[code],
                metrics={
                    "kind": "internet_outage",
                    "signals": sorted(a.get("datasource") for a in alerts),
                    "critical_signals": len(critical),
                    "max_drop_pct": max(drops) if drops else None,
                },
            )
        )
    return events


@register
class IODAOutages(Source):
    type = "ioda"
    default_name = "Internet outages (IODA)"
    category = Category.comms
    default_interval = 300

    async def fetch(self) -> list[Event]:
        now = int(time.time())
        lookback = int(self.options.get("lookback_minutes", 90)) * 60
        payload = await self.get_json(
            API,
            params={
                "from": now - lookback,
                "until": now,
                "entityType": "region",
                "relatedTo": "country/US",
                "limit": 2000,
            },
        )
        return parse_ioda(payload)
