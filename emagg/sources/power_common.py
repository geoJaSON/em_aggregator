"""Shared building blocks for power-outage adapters, so every utility rolls up the same way.

Conventions every power adapter follows (the Power tile and the States tab depend on them):

* one ``utility_total_event`` per utility (id ``"total"``) when the feed publishes a total,
* ``county_outage_event`` per county when it publishes county numbers,
* ``outage_point_event`` per outage/cluster when it publishes locations,
* ``metrics.utility`` is the same display name on all of a utility's events.
"""

from __future__ import annotations

from typing import Any

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.sources.kubra import county_outage_event, county_severity, customers_severity
from emagg.util import clean_text, parse_time

__all__ = [
    "county_outage_event",
    "county_severity",
    "customers_severity",
    "outage_point_event",
    "total_severity",
    "utility_total_event",
]


def total_severity(out: int, pct: float | None) -> Severity:
    """Utility-wide severity: by share of customers when known, else by count."""
    if pct is not None:
        if pct >= 20:
            return Severity.extreme
        if pct >= 5:
            return Severity.severe
        if pct >= 1:
            return Severity.moderate
        return Severity.minor if out else Severity.info
    if out >= 50000:
        return Severity.extreme
    if out >= 10000:
        return Severity.severe
    if out >= 1000:
        return Severity.moderate
    return Severity.minor if out else Severity.info


def utility_total_event(
    utility: str,
    customers_out: int,
    customers_served: int | None = None,
    outages: int | None = None,
    updated: Any = None,
    link: str | None = None,
    states: list[str] | None = None,
    percent_out: float | None = None,
) -> Event:
    out = int(customers_out or 0)
    served = int(customers_served) if customers_served else None
    pct = (out / served * 100.0) if served else percent_out
    title = f"{utility}: {out:,} customers without power"
    if pct is not None and out:
        title += f" ({pct:.1f}%)"
    return Event(
        id="total",
        category=Category.power,
        title=title,
        severity=total_severity(out, pct),
        description=f"{outages:,} active outages." if outages is not None else None,
        area=utility,
        geometry=None,
        updated_at=parse_time(updated),
        url=link,
        states=list(states or []),
        metrics={
            "kind": "utility_total",
            "utility": utility,
            "customers_out": out,
            "customers_served": served,
            "percent_out": round(pct, 3) if pct is not None else None,
            "outages": outages,
        },
    )


def outage_point_event(
    utility: str,
    outage_id: str,
    lon: float,
    lat: float,
    customers_out: int,
    *,
    cluster: bool = False,
    outages: int = 1,
    cause: Any = None,
    etr: Any = None,
    started: Any = None,
    updated: Any = None,
    crew_status: Any = None,
    link: str | None = None,
) -> Event:
    n = int(customers_out or 0)
    cause_txt = clean_text(cause)
    if cluster:
        title = f"{n:,} customers out ({outages} outages)"
    else:
        title = f"{n:,} customer{'s' if n != 1 else ''} out" + (f" — {cause_txt}" if cause_txt else "")
    etr_t = parse_time(etr)
    return Event(
        id=str(outage_id),
        category=Category.power,
        title=title,
        severity=customers_severity(n) if n else Severity.minor,
        area=utility,
        geometry=point(lon, lat),
        starts_at=parse_time(started),
        updated_at=parse_time(updated),
        url=link,
        metrics={
            "kind": "outage_cluster" if cluster else "outage",
            "utility": utility,
            "customers_out": n,
            "outages": outages,
            "etr": etr_t.isoformat() if etr_t else (clean_text(etr) if etr else None),
            "cause": cause_txt,
            "crew_status": clean_text(crew_status),
        },
    )
