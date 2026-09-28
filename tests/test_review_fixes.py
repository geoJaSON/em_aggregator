"""Regression tests for the cross-cutting integration review (multi-state totals, truncation, scale, counties)."""

import asyncio

from emagg import regions
from emagg.api import _limit_events
from emagg.cli import _endpoint
from emagg.config import AreaConfig, SourceConfig
from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.scheduler import Notifier, Scheduler, tidy_power_events
from emagg.sources.county_outages import parse_county_table
from emagg.sources.kubra import parse_county_report
from emagg.store import Store
from emagg.summary import build_state_rollup, build_summary
from emagg.util import render_template


def row(kind, util, n, states, sev="minor", category="power", uid=None):
    return {"uid": uid or f"{util}-{kind}-{n}-{states}", "category": category, "severity": sev,
            "severity_rank": ["info", "minor", "moderate", "severe", "extreme"].index(sev), "states": states,
            "baseline": True, "first_seen": "2026-09-27T00:00:00+00:00", "title": "",
            "metrics": {"kind": kind, "utility": util, "customers_out": n}}


def power(summary):
    return next(c for c in summary["categories"] if c["category"] == "power")


def test_multistate_total_does_not_leak_into_other_states():
    # We Energies-like: a WI+MI total, with county figures only in WI.
    events = [row("utility_total", "We", 301_084, ["WI", "MI"], sev="extreme"),
              row("county_outage", "We", 300_000, ["WI"], sev="extreme"), row("county_outage", "We", 1_084, ["WI"])]
    mi_view = [e for e in events if "MI" in e["states"]]
    mi = power(build_summary(mi_view, states=["MI"]))
    assert mi["count"] == 0 and mi["max_severity"] is None
    assert mi["headline"] == "No customers out reported"
    wi = power(build_summary([e for e in events if "WI" in e["states"]], states=["WI"]))
    assert wi["headline"] == "301,084 customers out across 1 utility" and wi["count"] == 2
    national = power(build_summary(events))
    assert national["headline"] == "301,084 customers out across 1 utility" and national["count"] == 3
    rollup = {r["state"]: r for r in build_state_rollup(events)}
    assert "MI" not in rollup  # nothing attributable to Michigan
    assert rollup["WI"]["by_severity"]["extreme"] == 1 and rollup["WI"]["customers_out"] == 301_084


def test_zero_totals_and_out_of_territory_points_are_dropped():
    def ev(eid, kind, n, lon=-90.0, lat=30.0):
        return Event(id=eid, category=Category.power, title=eid, geometry=None if kind == "utility_total" else point(lon, lat),
                     states=["LA"] if kind != "utility_total" or eid != "far" else [], metrics={"kind": kind, "utility": "U", "customers_out": n})

    far = Event(id="far", category=Category.power, title="far", geometry=point(-120, 37), states=["CA"],
                metrics={"kind": "outage", "utility": "U", "customers_out": 9})
    kept = tidy_power_events([ev("total", "utility_total", 0), ev("a", "outage", 5), far], ["LA"], 1000)
    assert [e.id for e in kept] == ["a"]
    many = [ev(f"p{i}", "outage", i) for i in range(10)]
    assert [e.id for e in tidy_power_events(many, ["LA"], 3)] == ["p9", "p8", "p7"]


def test_events_limit_never_drops_non_bulk_categories():
    rows = [row("outage", "TECO", n, ["FL"], sev="severe", uid=f"o{n}") for n in range(10)]
    rows.append(row("shelter", None, 0, ["FL"], sev="info", category="shelter", uid="shelter-1"))
    kept, omitted = _limit_events(rows, 5)
    ids = {r["uid"] for r in kept}
    assert "shelter-1" in ids and len(kept) == 5
    assert {"o9", "o8", "o7", "o6"} <= ids  # largest outages kept
    assert omitted == {"outage": 6}
    assert _limit_events(rows, 100) == (rows, {})


def test_sse_published_only_on_change_or_health_change():
    class Src:
        def __init__(self, fail=False):
            self.id, self.interval, self.options, self.ignore_area = "s", 60, {}, True
            self.cfg = SourceConfig(id="s", type="json")
            self.fail = fail

        async def fetch(self):
            if self.fail:
                raise RuntimeError("down")
            return [Event(id="x", category=Category.roads, title="x", severity=Severity.minor)]

    notifier, src = Notifier(), Src()
    sched = Scheduler([src], Store(), notifier, AreaConfig())

    async def run():
        q = notifier.subscribe()
        await sched.poll(src)  # new event + first health report
        await sched.poll(src)  # nothing changed
        src.fail = True
        await sched.poll(src)  # health changed
        await sched.poll(src)  # still failing: silent
        return [q.get_nowait() for _ in range(q.qsize())]

    msgs = asyncio.run(run())
    assert [(m["ok"], m["health_changed"]) for m in msgs] == [(True, True), (False, True)]


def test_startup_spread_for_many_sources():
    class S:
        def __init__(self, i):
            self.id, self.interval = f"src{i}", 300

    sched = Scheduler.__new__(Scheduler)
    sched.sources = {f"src{i}": S(i) for i in range(50)}
    delays = [sched._startup_delay(s) for s in sched.sources.values()]
    assert max(delays) > 60 and min(delays) >= 0 and len({round(d) for d in delays}) > 20
    assert sched._startup_delay(S(1)) == sched._startup_delay(S(1))  # stable per source


def test_resolve_county_refuses_ambiguous_names():
    assert regions.resolve_county("Washington", ["TX", "OK"]) is None  # exists in both states
    assert regions.resolve_county("Washington", ["TX"])["fips"] == "48477"
    assert regions.resolve_county("48477", ["TX"])["name"] == "Washington"
    assert regions.resolve_county("48477", ["OK"]) is None  # FIPS outside the utility's states
    assert regions.resolve_county("", ["TX"]) is None


def test_county_rows_are_aggregated_per_fips():
    recs = [{"County": "Harris", "Out": "100", "Served": "1000"}, {"County": "Harris", "Out": "50", "Served": "500"},
            {"County": "Washington", "Out": "7", "Served": "70"}]
    events = parse_county_table(recs, "Acme", {"county_field": "County", "out_field": "Out", "served_field": "Served"}, ["TX", "OK"])
    (harris,) = events  # Washington is ambiguous across TX/OK and skipped
    assert harris.metrics["customers_out"] == 150 and harris.metrics["customers_served"] == 1500
    assert harris.metrics["percent_out"] == 10.0 and harris.title == "Acme: 150 out in Harris, TX (10%)"
    report = {"file_data": {"areas": [{"key": "county", "name": "Harris", "cust_a": {"val": 5}, "cust_s": 10},
                                      {"key": "county", "name": "Harris", "cust_a": {"val": 5}, "cust_s": 10}]}}
    (e,) = parse_county_report(report, "Acme", ["TX"])
    assert e.metrics["customers_out"] == 10 and e.metrics["customers_served"] == 20


def test_templates_format_numeric_strings_and_drop_dangling_separators():
    assert render_template("{n:,} customers out — {cause}", {"n": "12345", "cause": None}) == "12,345 customers out"
    assert render_template("{n:,} out", {"n": "unknown"}) == "unknown out"


def test_host_endpoint_labels():
    assert _endpoint("https://api.weather.gov/alerts") == "api.weather.gov"
    assert _endpoint("http://outage.example-coop.org:8008/data") == "http://outage.example-coop.org:8008"
    assert _endpoint("https://oms.coop.org:8443/x") == "oms.coop.org:8443"
    assert _endpoint("https://waze.demo.invalid/feed") is None
