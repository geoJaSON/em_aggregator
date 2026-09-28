"""NISC hosted co-op outage maps (outagemap-data.cloud.coop): coordinate decode, regions, planned outages, catalog.

Fixtures: nisc_mohaveelectric_* is a real trimmed capture (vmanam1/az-power-outage-archive tests/test_coop.py).
nisc_samhouston_* is SYNTHETIC: no recorded summary.json with ``regionDataSets`` exists in any evidence repo, so its
region tables follow the shape documented in lukesteve03/OpenSourcePowerOutageScraper cloud_coop_base.py ("Counties"
/ "Zip" datasets, a region's id is its label), with made-up numbers, a configurationId borrowed from that docstring,
and an extent chosen so the points land in the counties the table names. Replace it with a live capture once
outagemap-data.cloud.coop is reachable (or confirm with ``emagg poll``).
"""

import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest
import yaml

import emagg.sources.nisc_hosted  # noqa: F401  (registers the adapter)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.models import Category, Severity
from emagg.sources import REGISTRY, SourceContext
from emagg.sources.base import SourceError
from emagg.sources.nisc_hosted import (
    county_dataset,
    extent_bbox,
    extent_from_config,
    is_planned,
    match_county,
    mercator_to_lonlat,
    parse_summary,
    xy_to_lonlat,
)
from emagg.store import Store

PENDING = Path(catalog.CATALOG_DIR) / "power_nisc_hosted.yaml"
R = 6378137.0


def by_id(events):
    return {e.id: e for e in events}


def forward(lon, lat):
    """lon/lat -> Web Mercator metres (test-side reference implementation)."""
    return math.radians(lon) * R, R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


# --- coordinate math --------------------------------------------------------------------------------------------


def test_real_capture_decodes_to_utility_map_markers(fixture):
    """Real Mohave Electric capture (vmanam1/az-power-outage-archive): offsets from boundaryExtent's SW corner."""
    extent = extent_from_config(fixture("nisc_mohaveelectric_config.json"))
    assert extent == [-12762208.0, 4084789.0, -12586187.0, 4255993.0]
    lon, lat = xy_to_lonlat(85852, 138987, extent)
    assert lat == pytest.approx(35.43955, abs=1e-4) and lon == pytest.approx(-113.87364, abs=1e-4)
    lon2, lat2 = xy_to_lonlat(3535, 95257, extent)
    assert lat2 == pytest.approx(35.11886, abs=1e-4) and lon2 == pytest.approx(-114.61311, abs=1e-4)
    for x, y in ((lon, lat), (lon2, lat2)):  # Kingman and Fort Mohave: Mohave County, AZ
        state, county = regions.locate(x, y)
        assert state == "AZ" and county["fips"] == "04015"


def test_offsets_are_plain_metres_from_a_fixed_origin():
    """Two hand-calibrated Central Lincoln PUD outages (the-roseburg-community/utility-outages) back-solve to the
    same origin within ~1 km, so x/y are unscaled Web Mercator metres from one corner."""
    cal = [(21181, 43585, 43.47878393579475, -124.22129431060301), (35957, 192616, 44.44270935878297, -124.08033686199173)]
    origins = [(forward(lon, lat)[0] - x, forward(lon, lat)[1] - y) for x, y, lat, lon in cal]
    assert abs(origins[0][0] - origins[1][0]) < 1500 and abs(origins[0][1] - origins[1][1]) < 1500
    extent = [origins[0][0], origins[0][1], origins[0][0] + 200000, origins[0][1] + 250000]
    lon, lat = xy_to_lonlat(cal[1][0], cal[1][1], extent)
    assert lat == pytest.approx(cal[1][2], abs=0.01) and lon == pytest.approx(cal[1][3], abs=0.02)
    assert mercator_to_lonlat(*forward(-95.37, 29.76)) == pytest.approx((-95.37, 29.76))


def test_extent_sources_and_guards():
    both = {"mapSettings": {"boundaryExtent": [1, 2, 3, 4], "fullExtent": [9, 9, 9, 9]}}
    assert extent_from_config(both) == [1, 2, 3, 4]
    assert extent_from_config({"mapSettings": {"fullExtent": [-1e7, 3e6, -9e6, 4e6]}}) == [-1e7, 3e6, -9e6, 4e6]
    assert extent_from_config({"mapSettings": {"fullExtent": {"xmin": -1e7, "ymin": 3e6, "xmax": -9e6, "ymax": 4e6}}})[:2] == [-1e7, 3e6]
    for bad in (None, [], {}, {"mapSettings": {}}, {"mapSettings": {"boundaryExtent": ["a", "b"]}},
                {"mapSettings": {"boundaryExtent": [0, 0, 0, 0]}}, {"mapSettings": {"boundaryExtent": [5, 5, 1, 1]}},
                {"mapSettings": {"boundaryExtent": [1, 2, "inf", 4]}}, {"mapSettings": {"boundaryExtent": [1, 2, 3]}},
                {"mapSettings": {"boundaryExtent": [-1e7, 3e6, 9e307, 4e6]}}):
        assert extent_from_config(bad) is None, bad
    # a degenerate boundaryExtent falls through to fullExtent
    assert extent_from_config({"mapSettings": {"boundaryExtent": [0, 0, 0, 0], "fullExtent": [1, 2, 3, 4]}}) == [1, 2, 3, 4]
    extent = [-12762208.0, 4084789.0, -12586187.0, 4255993.0]
    assert xy_to_lonlat(10, 10, None) is None  # offsets need the extent
    assert xy_to_lonlat(10, 10, [0, 0, 0, 0]) is None  # ... a real one, not a point in the Gulf of Guinea
    assert xy_to_lonlat(1e308, 1e308, None) is None and xy_to_lonlat(float("inf"), 1, extent) is None
    assert xy_to_lonlat("x", 1, extent) is None and xy_to_lonlat(None, 1, extent) is None
    assert xy_to_lonlat(1_000_000, 10, extent) is None  # offset far outside the utility's own map
    assert xy_to_lonlat(5_000_000, 10, extent) is None  # absolute metres far outside it
    lon, lat = xy_to_lonlat(-12676356, 4223776, extent)  # already-absolute metres are accepted as such
    assert lat == pytest.approx(35.4396, abs=1e-3) and lon == pytest.approx(-113.8736, abs=1e-3)
    bb = extent_bbox(extent)
    assert bb[0] < -113.87 < bb[2] and bb[1] < 35.44 < bb[3]


# --- summary parsing ------------------------------------------------------------------------------------------


def test_parse_summary_demo(fixture):
    """Synthetic Sam Houston fixture (see module docstring)."""
    summary = fixture("nisc_samhouston_summary.json")
    extent = extent_from_config(fixture("nisc_samhouston_config.json"))
    events = parse_summary(summary, "Sam Houston EC", ["TX"], extent, link="https://samhouston.outagemap.coop/")
    ev = by_id(events)
    assert {e.metrics["utility"] for e in events} == {"Sam Houston EC"}
    assert all(e.category == Category.power for e in events)

    total = ev["total"]
    assert total.metrics["customers_out"] == 8776  # every outage counts, including the one flagged planned
    assert total.metrics["customers_served"] == 143810 and total.metrics["outages"] == 9
    assert total.metrics["planned_outages"] == 1 and total.metrics["planned_customers_out"] == 23
    assert "Includes 1 outage flagged planned (23 customers)" in total.description and total.states == ["TX"]
    assert total.severity == Severity.severe  # 6.1% out
    assert total.updated_at is not None

    # County dataset -> county events (zero-out counties and the ZIP dataset are not used).
    counties = {e.fips: e for e in events if e.metrics["kind"] == "county_outage"}
    assert set(counties) == {"48199", "48291", "48339", "48373", "48407", "48457", "48471"}
    liberty = counties["48291"]
    assert liberty.id == "county-48291" and liberty.metrics["customers_out"] == 6875
    assert liberty.metrics["customers_served"] == 21450 and liberty.severity == Severity.severe
    assert liberty.geometry["type"] in ("Polygon", "MultiPolygon") and liberty.url.endswith("outagemap.coop/")

    # Outage points keep the feed's ids and land in the county the region table puts them in.
    points = {e.id: e for e in events if e.metrics["kind"] == "outage"}
    assert len(points) == 9
    dayton = points["1840271"]
    assert dayton.metrics["customers_out"] == 3875 and dayton.metrics["cause"] == "Weather"
    assert dayton.description.startswith("Multiple poles down")
    assert regions.locate(*dayton.geometry["coordinates"])[1]["fips"] == "48291"
    assert points["1840231"].metrics["etr"] and points["1840231"].metrics["crew_status"] == "Crew assigned"
    assert points["1840231"].starts_at is not None
    planned = points["1839990"]
    assert planned.metrics["planned"] is True and planned.severity == Severity.minor  # labelled, not downgraded
    assert planned.title.startswith("Planned outage: 23 customers out")
    assert points["1840271"].metrics["planned"] is False and not points["1840271"].title.startswith("Planned")
    assert regions.locate(*planned.geometry["coordinates"])[1]["fips"] == "48457"  # Tyler County

    # Stable ids across polls.
    again = parse_summary(summary, "Sam Houston EC", ["TX"], extent)
    assert set(by_id(again)) == set(ev)


def test_planned_excluded_on_request_and_options(fixture):
    summary = fixture("nisc_samhouston_summary.json")
    extent = extent_from_config(fixture("nisc_samhouston_config.json"))
    ev = by_id(parse_summary(summary, "SHEC", ["TX"], extent, include_planned=False))
    assert ev["total"].metrics["customers_out"] == 8753 and ev["total"].metrics["outages"] == 8
    assert ev["total"].metrics["planned_customers_out"] == 23
    assert "Excludes 1 outage flagged planned (23 customers)" in ev["total"].description
    assert ev["1839990"].metrics["planned"] is True  # still drawn
    only_total = parse_summary(summary, "SHEC", ["TX"], extent, counties=False, outage_points=False)
    assert [e.id for e in only_total] == ["total"]
    top2 = parse_summary(summary, "SHEC", ["TX"], extent, counties=False, max_points=2)
    assert [e.id for e in top2] == ["total", "1840271", "1840231"]  # the largest outages are kept
    no_extent = parse_summary(summary, "SHEC", ["TX"], None)
    assert not [e for e in no_extent if e.metrics["kind"] == "outage"]
    assert len([e for e in no_extent if e.metrics["kind"] == "county_outage"]) == 7  # counties need no extent
    forced = parse_summary(summary, "SHEC", ["TX"], extent, county_dataset_id="Zip", outage_points=False)
    assert [e.id for e in forced] == ["total"]  # ZIP labels are never read as counties


def test_real_capture_totals(fixture):
    events = parse_summary(fixture("nisc_mohaveelectric_summary.json"), "Mohave Electric", ["AZ"],
                           extent_from_config(fixture("nisc_mohaveelectric_config.json")))
    ev = by_id(events)
    # 93 out, as vmanam1/az-power-outage-archive's own test asserts for this capture (planned:true counts too).
    assert ev["total"].metrics["customers_out"] == 93 and ev["total"].metrics["planned_customers_out"] == 92
    assert ev["total"].metrics["outages"] == 2
    assert ev["349902"].metrics["cause"] == "Weather" and ev["349902"].metrics["planned"] is True
    assert ev["349902"].severity == Severity.minor and ev["349902"].title.startswith("Planned outage: 92 customers")
    assert ev["349870"].metrics["etr"] is None
    assert ev["total"].updated_at.year == 2026


def test_bad_records_skipped_and_region_fallback():
    extent = [-12762208.0, 4084789.0, -12586187.0, 4255993.0]
    summary = {
        "totalServed": "1,000",
        "outages": [None, "x", {"id": "a", "nbrOut": "many", "x": 1, "y": 1}, {"id": "b", "nbrOut": 5},
                    {"id": "c", "nbrOut": 3, "x": 85852, "y": 138987}, {"nbrOut": 2, "x": 85900, "y": 139000},
                    {"id": "d", "nbrOut": 4, "x": 1, "y": 1, "lifeCycleStatus": "Planned"}],
        "regionDataSets": [None, {"id": "Counties", "regions": [None, {"id": "Nowhere", "numberOut": 5},
                                                               {"id": "MOHAVE COUNTY", "numberOut": "7", "numberServed": 0}]}],
    }
    ev = by_id(parse_summary(summary, "U", ["AZ"], extent))
    assert ev["total"].metrics["customers_out"] == 14 and ev["total"].metrics["customers_served"] == 1000
    assert ev["total"].metrics["planned_outages"] == 1  # lifeCycleStatus "Planned" (CLPUD style)
    assert "c" in ev and "b" not in ev and "a" not in ev
    assert any(k.startswith("xy-") for k in ev)  # no id: a stable hash of position and start time
    assert ev["county-04015"].metrics["customers_out"] == 7 and ev["county-04015"].metrics["customers_served"] is None

    # A tenant without an outage list: the total comes from the region tables.
    only_regions = {"totalServed": 500, "regionDataSets": [
        {"id": "Zip", "regions": [{"id": "86401", "numberOut": 40}, {"id": "86442", "numberOut": 2}]},
        {"id": "BD", "description": "Board District", "regions": [{"id": "1", "numberOut": 42}]}]}
    total = by_id(parse_summary(only_regions, "U", ["AZ"]))["total"]
    assert total.metrics["customers_out"] == 42 and total.metrics["outages"] is None
    assert parse_summary({}, "U", ["AZ"])[0].metrics["customers_out"] == 0


def test_region_labels_and_ambiguous_counties():
    assert county_dataset({"regionDataSets": [{"id": "Zip", "description": "Zip Code"}, {"id": "Parishes"}]})["id"] == "Parishes"
    assert county_dataset({"regionDataSets": [{"id": "BD", "description": "Board District"}]}) is None
    assert county_dataset({"regionDataSets": [{"id": "Country"}]}) is None
    assert match_county("MOHAVE COUNTY", ["AZ"])["fips"] == "04015"
    assert match_county("04015", ["AZ"])["fips"] == "04015" and match_county("04015", [])["fips"] == "04015"
    assert match_county("Washington, AR", ["AR", "OK"])["fips"] == "05143"
    assert match_county("Washington (OK)", ["AR", "OK"])["fips"] == "40147"
    assert match_county("San Jacinto", ["TX"])["fips"] == "48407"
    assert match_county("Lafayette Parish", ["LA"])["fips"] == "22055"
    assert match_county("", ["TX"]) is None and match_county("77327", ["TX"]) is None
    # Same name in two of the utility's states: resolved by its outage points, then its map extent, else skipped.
    assert match_county("Washington", ["AR", "OK"]) is None
    assert match_county("Washington", ["AR", "OK"], points=[(-94.16, 36.06)])["fips"] == "05143"
    x0, y0 = forward(-94.6, 35.7)
    x1, y1 = forward(-93.9, 36.3)
    assert match_county("Washington", ["AR", "OK"], service_bbox=extent_bbox([x0, y0, x1, y1]))["fips"] == "05143"


def test_county_labels_stay_in_the_utilitys_states():
    """"Co" is a county abbreviation, never Colorado; labels and FIPS codes outside the co-op's states are skipped."""
    assert match_county("Jackson Co", ["TX"])["fips"] == "48239"
    assert match_county("JACKSON CO.", ["TX"])["fips"] == "48239"
    assert match_county("Liberty Co", ["TX"])["fips"] == "48291"
    assert match_county("Polk Cnty", ["TX"])["fips"] == "48373"
    assert match_county("Washington Co., AR", ["AR", "OK"])["fips"] == "05143"
    assert match_county("Washington Co", ["AR", "OK"]) is None  # ambiguous and no points: skipped
    assert match_county("Washington Co", ["AR", "OK"], points=[(-94.16, 36.06)])["fips"] == "05143"
    assert match_county("Jackson, CO", ["CO"])["fips"] == "08057"
    assert match_county("Mohave AZ", ["TX"]) is None
    assert match_county("Mohave, AZ", ["TX"]) is None
    assert match_county("48201", ["AZ"]) is None
    assert match_county("Harris, TX", ["TX", "LA"])["fips"] == "48201"


def test_planned_status_text():
    assert is_planned({"planned": True})
    assert is_planned({"lifeCycleStatus": "Planned"}) and is_planned({"lifeCycleStatus": "PLANNED OUTAGE"})
    for status in ("Unplanned", "Not Planned", "not planned", "Non-Planned", "Active", ""):
        assert not is_planned({"planned": False, "lifeCycleStatus": status}), status
    extent = [-12762208.0, 4084789.0, -12586187.0, 4255993.0]
    s = {"totalServed": 1000, "outages": [{"id": "1", "nbrOut": 50, "x": 85852, "y": 138987, "planned": False,
                                           "lifeCycleStatus": "Unplanned"}]}
    ev = by_id(parse_summary(s, "U", ["AZ"], extent, include_planned=False))
    assert ev["total"].metrics["customers_out"] == 50 and ev["total"].metrics["planned_outages"] == 0
    assert "planned" not in (ev["total"].description or "") and not ev["1"].title.startswith("Planned")


def test_flagged_planned_outage_matches_its_county_row():
    """A feed where every outage carries planned:true (as archived Trico snapshots do for 'Pending Investigation'
    outages) must not report 0 next to a county event counting the same customers."""
    extent = [-12762208.0, 4084789.0, -12586187.0, 4255993.0]
    s = {"totalServed": 100, "outages": [{"id": "p", "nbrOut": 40, "planned": True, "cause": "Pending Investigation",
                                          "x": 85852, "y": 138987}],
         "regionDataSets": [{"id": "Counties", "regions": [{"id": "Mohave", "numberOut": 40, "numberServed": 100}]}]}
    ev = by_id(parse_summary(s, "U", ["AZ"], extent))
    assert ev["total"].metrics["customers_out"] == 40 == ev["county-04015"].metrics["customers_out"]
    assert ev["total"].severity == Severity.extreme and ev["p"].severity == Severity.minor


def test_bad_numbers_and_duplicates_never_crash():
    extent = [-12762208.0, 4084789.0, -12586187.0, 4255993.0]
    raw = ('{"totalServed": NaN, "lastUpdate": "garbage", "outages": ['
           '{"id": "n", "nbrOut": NaN, "x": 85852, "y": 138987}, {"id": "i", "nbrOut": Infinity, "x": 1, "y": 1},'
           '{"id": "s", "nbrOut": "nan"}, {"id": "neg", "nbrOut": -4, "x": 1, "y": 1},'
           '{"id": "a", "nbrOut": 3, "x": 85852, "y": 138987, "estimateTime": -1, "timeOff": 0},'
           '{"id": "a", "nbrOut": 4, "x": 85852, "y": 138987, "planned": true},'
           '{"id": "total", "nbrOut": 2, "x": 3535, "y": 95257}, {"id": "big", "nbrOut": 1, "x": 1e308, "y": 1e308}],'
           '"regionDataSets": [{"id": "Counties", "regions": ['
           '{"id": "Mohave", "numberOut": 5, "numberServed": 100}, {"id": "Mohave County", "numberOut": 6, '
           '"numberServed": 200}, {"id": "Mohave", "numberOut": NaN}]}]}')
    summary = json.loads(raw)
    events = parse_summary(summary, "U", ["AZ"], extent, max_points="abc")
    ev = by_id(events)
    total = ev["total"]
    assert total.metrics["customers_out"] == 10 and total.metrics["customers_served"] is None
    assert total.updated_at is None
    a = ev["a"]  # duplicate id: counts added up and the title/severity follow the sum
    assert a.metrics["customers_out"] == 7 and a.title == "Planned outage: 7 customers out"
    assert a.metrics["etr"] is None and a.starts_at is None  # -1 / 0 sentinels
    assert ev["outage-total"].metrics["customers_out"] == 2  # never shadows the utility total
    assert [e.id for e in events].count("total") == 1
    county = ev["county-04015"]  # listed twice: out and served both added up
    assert county.metrics["customers_out"] == 11 and county.metrics["customers_served"] == 300
    assert "11 out" in county.title and county.metrics["percent_out"] == pytest.approx(3.67, abs=0.01)
    assert len(parse_summary(summary, "U", ["AZ"], extent, counties=False, max_points=1)) == 2


# --- adapter ------------------------------------------------------------------------------------------------------


def run_fetch(options, handler, states=("TX",), store=None):
    cfg = SourceConfig.model_validate({"id": "t", "type": "nisc_hosted", "name": "Sam Houston EC", "states": list(states), **options})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = REGISTRY["nisc_hosted"](cfg, SourceContext(http, AreaConfig(), store or Store()))
            assert src.interval >= 300 and src.config_error() is None
            return await src.fetch()

    return asyncio.run(go())


def test_fetch_caches_extent_per_configuration(fixture):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[-1]
        assert request.url.host == "outagemap-data.cloud.coop"
        assert request.url.path.startswith("/samhouston/Hosted_Outage_Map/")
        return httpx.Response(200, json=fixture(f"nisc_samhouston_{name}"))

    store = Store()
    events = run_fetch({"tenant": "samhouston"}, handler, store=store)
    assert seen == ["/samhouston/Hosted_Outage_Map/summary.json", "/samhouston/Hosted_Outage_Map/config.json"]
    kinds = [e.metrics["kind"] for e in events]
    assert kinds.count("utility_total") == 1 and kinds.count("county_outage") == 7 and kinds.count("outage") == 9
    seen.clear()
    run_fetch({"tenant": "samhouston"}, handler, store=store)
    assert seen == ["/samhouston/Hosted_Outage_Map/summary.json"]  # extent cached for this configurationId


def test_fetch_without_config_still_reports_totals(fixture):
    def handler(request):
        if request.url.path.endswith("config.json"):
            return httpx.Response(404)
        return httpx.Response(200, json=fixture("nisc_samhouston_summary.json"))

    kinds = [e.metrics["kind"] for e in run_fetch({"tenant": "samhouston", "base_url": "https://mirror.example/"}, handler)]
    assert kinds.count("utility_total") == 1 and kinds.count("county_outage") == 7 and "outage" not in kinds


def test_fetch_survives_config_network_error(fixture):
    def handler(request):
        if request.url.path.endswith("config.json"):
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json=fixture("nisc_samhouston_summary.json"))

    events = run_fetch({"tenant": "samhouston"}, handler)
    kinds = [e.metrics["kind"] for e in events]
    assert kinds.count("utility_total") == 1 and kinds.count("county_outage") == 7 and "outage" not in kinds
    assert events[0].metrics["customers_out"] == 8776


def test_fetch_options(fixture):
    def handler(request):
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=fixture(f"nisc_samhouston_{name}"))

    events = run_fetch({"tenant": "samhouston", "include_planned": False, "max_points": "3", "counties": False,
                        "customers_served": "150,000"}, handler)
    assert events[0].metrics["customers_out"] == 8753 and events[0].metrics["customers_served"] == 150000
    assert [e.metrics["kind"] for e in events] == ["utility_total", "outage", "outage", "outage"]
    events = run_fetch({"tenant": "samhouston", "max_points": "lots", "customers_served": "NaN"}, handler)
    assert [e.metrics["kind"] for e in events].count("outage") == 9
    assert events[0].metrics["customers_served"] == 143810


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="<html>"), httpx.Response(200, json=[1, 2]),
                                      httpx.Response(200, json={"error": "nope"})])
def test_fetch_bad_responses_raise(response):
    with pytest.raises(SourceError):
        run_fetch({"tenant": "samhouston"}, lambda request: response)


def test_missing_tenant_is_a_config_error():
    cfg = SourceConfig.model_validate({"id": "t", "type": "nisc_hosted"})
    src = REGISTRY["nisc_hosted"](cfg, SourceContext(None, AreaConfig(), Store()))
    assert src.config_error() == "not configured: set tenant"


# --- catalog ------------------------------------------------------------------------------------------------------


def test_pending_catalog_entries_are_valid():
    entries = yaml.safe_load(PENDING.read_text())
    assert isinstance(entries, list) and len(entries) >= 60
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "power_nisc_hosted.yaml"}
    known_states = regions.state_codes()
    seen, tenants = set(), set()
    for e in entries:
        assert e["id"].startswith("nisc_") and e["id"] not in seen and e["id"] not in existing, e["id"]
        seen.add(e["id"])
        assert e["type"] == "nisc_hosted" and e["type"] in REGISTRY
        cfg = SourceConfig.model_validate(_interpolate(e))
        assert cfg.states and all(len(s) == 2 and s.isupper() and s in known_states for s in cfg.states), e["id"]
        assert cfg.options["tenant"] and cfg.options["tenant"] not in tenants
        tenants.add(cfg.options["tenant"])
        assert cfg.name and cfg.meta["confidence"] in ("high", "medium", "low") and cfg.meta["evidence"]
        if cfg.meta["confidence"] == "high":
            assert cfg.meta["evidence"].count("github.com/") >= 2
        if "link" in cfg.options:
            assert cfg.options["link"].startswith("https://")
        assert REGISTRY["nisc_hosted"](cfg, SourceContext(None, AreaConfig(), Store())).config_error() is None
    southeast = [e for e in entries if set(e["states"]) & {"TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC"}]
    assert len(southeast) >= 35
