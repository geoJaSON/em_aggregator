"""Roll active events up into per-category headline numbers for the dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from emagg.models import SEVERITY_ORDER, Category, utcnow
from emagg.store import ts

LABELS = {
    Category.weather: "Weather",
    Category.flood: "Flooding",
    Category.power: "Power",
    Category.comms: "Comms",
    Category.roads: "Roads",
    Category.transport: "Transport",
    Category.fire: "Wildfire",
    Category.seismic: "Quakes",
    Category.tropical: "Tropical",
    Category.shelter: "Shelters",
    Category.other: "Other",
}


def utility_customers_out(events: list[dict[str, Any]], state: str | None = None) -> dict[str, int]:
    """Customers out per utility, using the best figure each utility publishes: its total (only when it can be
    attributed to the requested state), else its county report, else its outage points."""
    figures: dict[str, dict[str, int]] = {}
    for e in events:
        m = e["metrics"]
        kind, util, n = m.get("kind"), m.get("utility"), m.get("customers_out") or 0
        if kind not in ("utility_total", "county_outage", "outage", "outage_cluster") or not util:
            continue
        f = figures.setdefault(util, {})
        if kind == "utility_total":
            if state is None or e["states"] == [state]:
                f["total"] = n
        elif kind == "county_outage":
            f["county"] = f.get("county", 0) + n
        else:
            f["points"] = f.get("points", 0) + n
    return {u: f.get("total", f.get("county", f.get("points", 0))) for u, f in figures.items()}


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def build_summary(events: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    recent = ts(now - timedelta(hours=1))
    cats: dict[str, dict[str, Any]] = {}
    for c in Category:
        cats[c.value] = {
            "category": c.value,
            "label": LABELS[c],
            "count": 0,
            "new_last_hour": 0,
            "max_severity": None,
            "by_severity": {s.value: 0 for s in SEVERITY_ORDER},
            "headline": None,
        }
    by_cat: dict[str, list[dict[str, Any]]] = {c.value: [] for c in Category}
    for e in events:
        c = cats[e["category"]]
        c["count"] += 1
        c["by_severity"][e["severity"]] += 1
        if c["max_severity"] is None or e["severity_rank"] > SEVERITY_ORDER.index(c["max_severity"]):
            c["max_severity"] = SEVERITY_ORDER[e["severity_rank"]]
        if not e["baseline"] and e["first_seen"] >= recent:
            c["new_last_hour"] += 1
        by_cat[e["category"]].append(e)
    for c in cats.values():
        if c["max_severity"] is not None:
            c["max_severity"] = c["max_severity"].value
        c["headline"] = _headline(c["category"], by_cat[c["category"]])
    return {"generated_at": ts(now), "total": len(events), "categories": list(cats.values())}


def _headline(category: str, events: list[dict[str, Any]]) -> str | None:
    if not events:
        return None
    m = [e["metrics"] for e in events]
    if category == "power":
        per_utility = utility_customers_out(events)
        if per_utility:
            n = len(per_utility)
            return f"{sum(per_utility.values()):,} customers out across {n} {'utility' if n == 1 else 'utilities'}"
        return _plural(len(events), "report")
    if category == "flood":
        gauges = [x for x in m if x.get("kind") == "river_gauge"]
        flooding = [x for x in gauges if x.get("observed_category") in ("minor", "moderate", "major")]
        major = [x for x in gauges if x.get("observed_category") == "major"]
        parts = []
        if gauges:
            parts.append(f"{_plural(len(flooding), 'gauge')} in flood" + (f" ({len(major)} major)" if major else ""))
        other = len(events) - len(gauges)
        if other:
            parts.append(_plural(other, "alert/report"))
        return "; ".join(parts)
    if category == "weather":
        warnings = sum(1 for e in events if e["title"].endswith("Warning"))
        return f"{_plural(len(events), 'alert')}" + (f", {_plural(warnings, 'warning')}" if warnings else "")
    if category == "roads":
        closures = sum(1 for e in events if e["severity_rank"] >= 3)
        return f"{_plural(closures, 'closure')}, {_plural(len(events) - closures, 'other incident')}"
    if category == "fire":
        acres = sum(x.get("acres") or 0 for x in m)
        return f"{_plural(len(events), 'fire')}, {acres:,.0f} acres"
    if category == "seismic":
        mags = [x.get("magnitude") for x in m if x.get("magnitude") is not None]
        return f"{_plural(len(events), 'quake')}" + (f", largest M{max(mags):.1f}" if mags else "")
    if category == "tropical":
        return "; ".join(e["title"] for e in events[:3])
    if category == "transport":
        closed = sum(1 for x in m if x.get("kind") == "airport_closure")
        stops = sum(1 for x in m if x.get("kind") == "ground_stop")
        return f"{_plural(closed, 'airport closure')}, {_plural(stops, 'ground stop')}"
    if category == "shelter":
        pop = sum(x.get("population") or 0 for x in m)
        return f"{_plural(len(events), 'open shelter')}" + (f", {pop:,} occupants" if pop else "")
    return _plural(len(events), "report")


def build_state_rollup(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-state counts for a national overview. Multi-state events count once in each state."""
    from emagg.regions import state_bbox, state_name

    rows: dict[str, dict[str, Any]] = {}
    for e in events:
        for st in e["states"] or ["??"]:
            r = rows.setdefault(st, {
                "state": st,
                "name": state_name(st) or ("Unassigned / offshore" if st == "??" else st),
                "count": 0,
                "by_severity": {s.value: 0 for s in SEVERITY_ORDER},
                "by_category": {},
                "customers_out": 0,
                "bbox": state_bbox(st),
            })
            r["count"] += 1
            r["by_severity"][e["severity"]] += 1
            r["by_category"][e["category"]] = r["by_category"].get(e["category"], 0) + 1
            r.setdefault("_events", []).append(e)
    out = []
    for code, r in rows.items():
        evs = r.pop("_events", [])
        r["customers_out"] = sum(utility_customers_out(evs, state=code).values())
        out.append(r)
    out.sort(key=lambda r: (-r["by_severity"]["extreme"], -r["by_severity"]["severe"], -r["count"]))
    return out
