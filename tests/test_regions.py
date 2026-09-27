import pytest

from emagg import regions
from emagg.config import AreaConfig, SourceConfig
from emagg.geo import point
from emagg.models import Category, Event
from emagg.scheduler import attribute, in_area
from emagg.store import Store
from emagg.summary import build_state_rollup


@pytest.mark.parametrize(
    "lon,lat,state,county",
    [
        (-95.37, 29.76, "TX", "Harris"),
        (-90.07, 29.95, "LA", "Orleans"),
        (-75.55, 35.25, "NC", "Dare"),  # Outer Banks
        (-94.79, 29.31, "TX", "Galveston"),  # Galveston Island
        (-81.80, 24.55, "FL", "Monroe"),  # Key West
        (-66.10, 18.47, "PR", None),  # San Juan waterfront: snapped to shore
        (-94.0, 27.0, None, None),  # open Gulf
    ],
)
def test_locate(lon, lat, state, county):
    s, c = regions.locate(lon, lat)
    assert s == state
    if county:
        assert c["name"] == county


def test_county_lookup_by_name():
    assert regions.find_county("LA", "Orleans Parish")["fips"] == "22071"
    assert regions.find_county("FL", "Saint Johns County")["fips"] == "12109"
    assert regions.find_county("fl", "Miami-Dade")["fips"] == "12086"
    assert regions.county_geometry(regions.county_by_fips("12086"))["type"] in ("Polygon", "MultiPolygon")


def ev(eid, geometry=None, states=(), **kw):
    return Event(id=eid, category=Category.power, title=eid, geometry=geometry, states=list(states), **kw)


def test_attribute():
    e = ev("pt", point(-90.07, 29.95))
    attribute(e, [])
    assert e.states == ["LA"] and e.fips == "22071"

    line = ev("line", {"type": "LineString", "coordinates": [[-94.5, 30.0], [-93.0, 30.2]]})
    attribute(line, [])
    assert line.states == ["TX", "LA"]

    marine = ev("marine", states=["GM", "TX"])  # marine zone prefixes are not states
    attribute(marine, [])
    assert marine.states == ["TX"]

    total = ev("total")  # no geometry: falls back to the source's configured states
    attribute(total, ["MS"])
    assert total.states == ["MS"]


def test_state_area_filter():
    gulf = AreaConfig(preset="gulf_southeast")
    assert gulf.states == ["TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC"] and gulf.bbox
    assert in_area(ev("a", states=["FL"]), gulf)
    assert not in_area(ev("b", states=["AR"]), gulf)
    assert in_area(ev("c", states=["AR", "LA"]), gulf)
    offshore = ev("d", point(-88.0, 27.0))
    assert in_area(offshore, gulf)  # no state, inside bbox
    assert not in_area(ev("e", point(-120.0, 35.0)), gulf)
    # Explicit values override the preset
    assert AreaConfig(preset="gulf_southeast", states=["FL"]).states == ["FL"]
    with pytest.raises(ValueError):
        AreaConfig(preset="atlantis")


def test_store_state_filter(now):
    store = Store()
    store.replace_source_events("s", [ev("a", states=["FL"]), ev("b", states=["TX", "LA"]), ev("c")], now)
    assert {e["id"] for e in store.query_events(states=["LA"], now=now)} == {"b"}
    assert {e["id"] for e in store.query_events(states=["FL", "TX"], now=now)} == {"a", "b"}
    assert store.get_event("s", "b")["states"] == ["TX", "LA"]


def test_state_rollup():
    def row(states, kind, utility, customers, sev="minor"):
        return {"states": states, "severity": sev, "category": "power",
                "metrics": {"kind": kind, "utility": utility, "customers_out": customers}}

    events = [
        row(["LA"], "utility_total", "Acme LA", 1000),
        row(["LA"], "outage", "Acme LA", 400),  # covered by Acme LA's total
        row(["LA", "MS"], "utility_total", "Multi", 5000),  # can't be split by state
        row(["LA"], "outage", "Multi", 700),
        row(["MS"], "outage", "Multi", 300),
        {"states": [], "severity": "severe", "category": "tropical", "metrics": {}},
    ]
    by_state = {r["state"]: r for r in build_state_rollup(events)}
    assert by_state["LA"]["customers_out"] == 1700
    assert by_state["MS"]["customers_out"] == 300
    assert by_state["??"]["by_severity"]["severe"] == 1


def test_source_states_field():
    cfg = SourceConfig(id="x", type="kubra", states=["LA"], instance_id="i", view_id="v")
    assert cfg.states == ["LA"] and "states" not in cfg.options


def test_power_headline_counts_county_only_utilities():
    from emagg.summary import build_summary

    def ev(kind, util, n, states):
        return {"category": "power", "severity": "minor", "severity_rank": 1, "states": states, "baseline": True,
                "first_seen": "2026-09-27T00:00:00+00:00", "title": "", "metrics": {"kind": kind, "utility": util, "customers_out": n}}

    events = [ev("utility_total", "Kubra Co", 1000, ["GA"]), ev("county_outage", "FPL", 300, ["FL"]),
              ev("county_outage", "FPL", 200, ["FL"]), ev("outage", "Cleco", 50, ["LA"])]
    power = next(c for c in build_summary(events)["categories"] if c["category"] == "power")
    assert power["headline"] == "1,550 customers out across 3 utilities"
