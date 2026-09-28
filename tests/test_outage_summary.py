"""Milsoft Web Outage Viewer adapter (emagg/sources/outage_summary.py) and its pending catalog.

The osj_* fixtures are synthetic East Texas numbers in the recorded shapes: the outage records use exactly the
17 fields of the real record below; the summary keys come from codebooker's Otter Tail and Keys parsers; the
boundaries keys from scottarver's Beauregard Electric types. The fixture's layer name "County", its nameField
"NAME" and the summary's updateTime format ("...Z") are guesses, not taken from a recorded payload.
"""

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

import emagg.sources.outage_summary  # noqa: F401  (registers milsoft_wov)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.models import Severity
from emagg.sources import REGISTRY, SourceContext, SourceError
from emagg.sources.outage_summary import (
    county_candidates,
    layer_kind,
    parse_boundaries,
    parse_outages,
    parse_wov,
    select_county_layer,
    summary_fields,
)
from emagg.store import Store

PENDING = Path(catalog.__file__).parent / "power_outage_summary.yaml"

# A real outages.json record, verbatim (pdhung3012/PowerOutageTracker-UI user_input/1.json, 2025; the repo
# stored it as a Python repr, converted back to JSON here). The point is in Walthall County, MS.
REAL_OUTAGE = {
    "outageRecID": "2025-03-07-0445", "outageName": "TRF_1335752010",
    "outagePoint": {"lat": 31.28250642247155, "lng": -90.251913646075},
    "outageStartTime": "2025-03-07T13:13:02-06:00", "estimatedTimeOfRestoral": None, "outageEndTime": None,
    "verified": False, "cause": None, "code": None, "crewAssigned": False, "customersOutInitially": 1,
    "customersOutNow": 1, "customersRestored": 0, "streetsAffected": None, "isPlanned": False,
    "outageModifiedTime": "2025-03-07T13:19:14.16-06:00", "outageWorkStatus": "",
}


def by_id(events):
    return {e.id: e for e in events}


def run(cfg: SourceConfig, handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = REGISTRY[cfg.type](cfg, SourceContext(http, AreaConfig(), Store()))
            assert src.config_error() is None
            return await src.fetch()

    return asyncio.run(go())


def fixture_server(fixture, seen=None, overrides=None):
    files = {
        "/data/outageSummary.json": fixture("osj_summary.json"),
        "/data/outages.json": fixture("osj_outages.json"),
        "/data/boundaries.json": fixture("osj_boundaries.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if overrides and path in overrides:
            return overrides[path]
        if path in files:
            return httpx.Response(200, json=files[path])
        return httpx.Response(404, text="not found")

    return handler


# --- pure parsing ----------------------------------------------------------------------------------------


def test_real_outage_record():
    [ev] = parse_outages([REAL_OUTAGE], "Magnolia EPA")
    assert ev.id == "2025-03-07-0445"
    assert ev.geometry == {"type": "Point", "coordinates": [-90.251914, 31.282506]}
    assert ev.starts_at.isoformat() == "2025-03-07T19:13:02+00:00"
    assert ev.updated_at.isoformat() == "2025-03-07T19:19:14+00:00"  # "13:19:14.16-06:00"
    m = ev.metrics
    assert m["kind"] == "outage" and m["utility"] == "Magnolia EPA" and m["customers_out"] == 1
    assert m["etr"] is None and m["cause"] is None and m["crew_status"] is None and m["verified"] is False
    assert ev.title == "1 customer out" and ev.severity == Severity.minor
    assert regions.locate(-90.251913646075, 31.28250642247155)[0] == "MS"


def test_outages_fixture(fixture, now):
    events = by_id(parse_outages(fixture("osj_outages.json"), "Demo Co-op", link="https://outage.example.coop/"))
    # restored (0 out) and planned outages are skipped
    assert set(events) == {"2026-09-27-0012", "2026-09-27-0013", "2026-09-27-0015", "2026-09-27-0018", "2026-09-27-0021"}
    big = events["2026-09-27-0018"]
    assert big.severity == Severity.severe and big.title == "5,600 customers out — Equipment damage"
    assert big.metrics["crew_status"] == "Assessing Damage" and big.metrics["etr"].startswith("2026-09-28T12:00")
    assert big.description == "Streets affected: SH 87, SH 184, FM 83, FM 944, CR 3121 and 2 more"
    assert big.url == "https://outage.example.coop/"
    crew = events["2026-09-27-0012"]
    assert crew.metrics["crew_status"] == "Crew On Site" and crew.description == "Streets affected: FM 1747, CR 42"
    assert events["2026-09-27-0013"].metrics["crew_status"] is None  # crewAssigned false, empty work status
    planned = by_id(parse_outages(fixture("osj_outages.json"), "Demo Co-op", include_planned=True))
    p = planned["2026-09-27-P001"]
    assert p.metrics["planned"] is True and p.title.startswith("Planned outage: 12 customers out")
    assert "2026-09-27-0009" not in planned  # restored stays out


def test_outages_bad_records_never_raise():
    rows = [
        None, "x", 5, {}, {"customersOutNow": 3},  # no point
        {"customersOutNow": 3, "outagePoint": "31,-90"},
        {"customersOutNow": 3, "outagePoint": {"lat": None, "lng": -90}},
        {"customersOutNow": 3, "outagePoint": {"lat": 0, "lng": 0}},
        {"customersOutNow": 3, "outagePoint": {"lat": 131.2, "lng": -90.2}},
        {"customersOutNow": "n/a", "outagePoint": {"lat": 31.2, "lng": -90.2}},
        {"customersOutNow": "12", "outagePoint": {"lat": "31.2", "lng": "-90.2"}, "outageRecId": 77,
         "outageStartTime": "garbage", "streetsAffected": "Main St", "crewAssigned": "true"},
        {"customersOutNow": 4, "outagePoint": {"lat": 31.3, "lng": -90.3}},  # no id: position id
        {"customersOutNow": 9, "outagePoint": {"lat": 31.3, "lng": -90.3}, "outageRecID": "77"},  # duplicate id
    ]
    events = by_id(parse_outages(rows, "U"))
    assert set(events) == {"77", "pt-31.3000,-90.3000"}
    ok = events["77"]
    assert ok.metrics["customers_out"] == 12 and ok.starts_at is None and ok.description == "Streets affected: Main St"
    assert ok.metrics["crew_status"] == "Crew assigned"
    assert parse_outages({"outages": rows}, "U") and parse_outages("nope", "U") == [] and parse_outages(None, "U") == []
    assert len(parse_outages(rows, "U", max_points=1)) == 1


def test_summary_fields(fixture, now):
    f = summary_fields(fixture("osj_summary.json"))
    assert f["out"] == 8951 and f["served"] == 37900 and f["updated"] == "2026-09-27T17:58:00Z" and f["outages"] is None
    assert summary_fields({"summary": {"customersOut": "1,204", "customersServed": 9000}})["out"] == 1204
    assert summary_fields([1, 2]) == {"out": None, "served": None, "updated": None, "outages": None}


def test_non_finite_numbers_and_extreme_times():
    # Python's json accepts NaN and Infinity; they must not fail the poll
    payload = json.loads('{"customersOutNow": NaN, "customersServed": Infinity, "updateTime": "9999-12-31T23:59:59-06:00"}')
    assert summary_fields(payload) == {"out": None, "served": None, "updated": None, "outages": None}
    assert summary_fields({"customersOutNow": "NaN", "customersServed": "-Infinity"})["out"] is None
    rec = {**REAL_OUTAGE, "outageModifiedTime": "9999-12-31T23:59:59-06:00", "estimatedTimeOfRestoral": "0001-01-01T00:00:00+05:00"}
    [ev] = parse_outages([rec, {**REAL_OUTAGE, "outageRecID": "nan", "customersOutNow": float("nan")}], "U")
    assert ev.id == "2025-03-07-0445" and ev.updated_at is None and ev.metrics["etr"] is None
    assert ev.starts_at.isoformat() == "2025-03-07T19:13:02+00:00"
    layer = [{"name": "County", "boundaries": [{"name": "Sabine", "customersOutNow": float("inf")}, {"name": "Jasper", "customersOutNow": 4, "customersServed": float("nan")}]}]
    [county] = parse_boundaries(layer, "U", ["TX"])
    assert county.id == "county-48241" and county.metrics["customers_served"] is None
    total = parse_wov(payload, [REAL_OUTAGE], layer, "U", ["TX"], options={"max_points": None, "include_planned": "false"})[0]
    assert total.metrics["customers_out"] == 4 and total.updated_at is None  # from the county layer (the MS point is dropped)
    for bad in ("abc", 0, -5, None):
        assert len(parse_wov({}, [REAL_OUTAGE], None, "U", ["MS"], options={"max_points": bad})) == 2


def test_points_outside_the_utility_states_are_dropped():
    rows = [
        {**REAL_OUTAGE, "outageRecID": "ms"},  # Walthall County, MS
        {**REAL_OUTAGE, "outageRecID": "la", "outagePoint": {"lat": 30.45, "lng": -91.15}},  # Baton Rouge, LA
        {**REAL_OUTAGE, "outageRecID": "tx", "outagePoint": {"lat": 31.35, "lng": -94.1}},  # Sabine County, TX
        {**REAL_OUTAGE, "outageRecID": "gulf", "outagePoint": {"lat": 28.5, "lng": -90.0}},  # offshore: kept
    ]
    ids = [e.id for e in parse_wov({"customersOutNow": 4}, rows, None, "Magnolia EPA", ["MS"])[1:]]
    assert ids == ["ms", "gulf"]
    assert [e.id for e in parse_wov({}, rows, None, "U", ["MS", "TX"])[1:]] == ["ms", "tx", "gulf"]
    assert len(parse_wov({}, rows, None, "U", [])) == 5  # no states: nothing dropped


def test_boundaries_fixture_counties(fixture):
    events = by_id(parse_boundaries(fixture("osj_boundaries.json"), "Demo Co-op", ["TX"], updated="2026-09-27T17:58:00Z"))
    assert set(events) == {"county-48241", "county-48351", "county-48403", "county-48457"}  # San Augustine has 0 out
    sabine = events["county-48403"]
    assert sabine.metrics["kind"] == "county_outage" and sabine.metrics["utility"] == "Demo Co-op"
    assert sabine.metrics["customers_out"] == 5600 and sabine.metrics["customers_served"] == 7400
    assert sabine.severity == Severity.extreme and sabine.states == ["TX"] and sabine.fips == "48403"
    assert sabine.title == "Demo Co-op: 5,600 out in Sabine, TX (76%)"
    assert sabine.geometry["type"] in ("Polygon", "MultiPolygon")
    assert events["county-48241"].severity == Severity.moderate  # Jasper 2,490 / 14,200 = 17.5%
    assert sabine.updated_at.isoformat() == "2026-09-27T17:58:00+00:00"


def test_boundary_layer_selection():
    county_rows = [{"name": "Allen", "customersOutNow": 30, "customersServed": 4100},
                   {"name": "Beauregard Parish", "customersOutNow": 5, "customersServed": 20000}]
    zip_layer = {"name": "Zip Code", "nameField": "ZCTA5CE10",
                 "boundaries": [{"name": "70634", "customersOutNow": 35, "customersServed": 24100}]}
    district_layer = {"name": "Districts", "nameField": "NAME",
                      "boundaries": [{"name": "Allen", "customersOutNow": 35, "customersServed": 24100}]}
    counties = {"name": "Parishes", "nameField": "NAME", "boundaries": county_rows}
    assert layer_kind(zip_layer) == "other" and layer_kind(district_layer) == "other" and layer_kind(counties) == "county"
    assert layer_kind({"name": "Boundaries", "nameField": "COUNTY_NAM"}) == "county"
    assert layer_kind({"name": "Boundaries", "nameField": "NAME"}) == "unknown"

    # zip and district layers never become counties, and only one layer is counted
    events = by_id(parse_boundaries([zip_layer, district_layer, counties], "BECI", ["LA"]))
    assert set(events) == {"county-22003", "county-22011"}
    assert events["county-22003"].metrics["customers_out"] == 30
    assert parse_boundaries([zip_layer, district_layer], "BECI", ["LA"]) == []

    # an unlabelled layer is used when its rows are mostly county names in the utility's states
    unlabelled = {"name": "Summary", "nameField": "NAME", "boundaries": county_rows}
    assert select_county_layer([zip_layer, unlabelled], ["LA"]) is unlabelled
    assert select_county_layer([zip_layer, unlabelled], ["TX"]) is None  # not TX counties
    assert select_county_layer([unlabelled], []) is None

    # forced by name (case-insensitive); unknown names give nothing
    assert select_county_layer([zip_layer, district_layer], ["LA"], layer="districts") is district_layer
    assert select_county_layer([zip_layer], ["LA"], layer="nope") is None

    # single layer object or wrapped list, rows labelled via nameField, duplicates added up
    single = {"name": "County", "nameField": "CNTY", "boundaries": [
        {"CNTY": "Allen", "customersOutNow": 3, "customersServed": 100},
        {"name": "ALLEN PARISH", "customersOutNow": 2, "customersServed": 50},
        {"name": "Nowhere", "customersOutNow": 9}, None, "x",
        {"name": "Allen", "customersOutNow": "lots"},
    ]}
    for payload in (single, {"boundaries": [single]}):
        [ev] = parse_boundaries(payload, "BECI", ["LA"])
        assert ev.metrics["customers_out"] == 5 and ev.metrics["customers_served"] == 150
    assert parse_boundaries("junk", "U", ["LA"]) == [] and parse_boundaries(None, "U", ["LA"]) == []
    assert parse_boundaries([{"name": "County", "boundaries": "x"}], "U", ["LA"]) == []


def test_county_name_variants_and_ambiguity():
    assert [c["fips"] for c in county_candidates("Bryan Co", ["GA"])] == ["13029"]
    assert [c["fips"] for c in county_candidates("Bryan Co.", ["GA"])] == ["13029"]
    assert [c["fips"] for c in county_candidates("Cherokee, NC", ["GA", "NC"])] == ["37039"]
    assert [c["fips"] for c in county_candidates("Cherokee (NC)", ["GA", "NC"])] == ["37039"]
    assert [c["fips"] for c in county_candidates("Cherokee County, North Carolina", ["GA", "NC"])] == ["37039"]
    assert [c["fips"] for c in county_candidates("DE WITT", ["TX"])] == ["48123"]
    assert sorted(c["fips"] for c in county_candidates("Clay", ["GA", "NC"])) == ["13061", "37043"]
    # a trailing all-caps CO is the county abbreviation as often as Colorado
    assert [c["fips"] for c in county_candidates("DUNDY CO", ["CO", "NE"])] == ["31057"]
    assert [c["fips"] for c in county_candidates("Dundy Co", ["CO", "NE"])] == ["31057"]
    assert sorted(c["fips"] for c in county_candidates("WASHINGTON CO", ["CO", "NE"])) == ["08121", "31177"]
    assert [c["fips"] for c in county_candidates("WASHINGTON CO", ["CO"])] == ["08121"]
    # short forms: Jefferson Davis Parish (LA) and County (MS); Jeff Davis County, TX is its real name
    assert [c["fips"] for c in county_candidates("JEFF DAVIS", ["LA"])] == ["22053"]
    assert [c["fips"] for c in county_candidates("Jeff Davis Parish", ["LA"])] == ["22053"]
    assert [c["fips"] for c in county_candidates("Jeff Davis", ["MS"])] == ["28065"]
    assert [c["fips"] for c in county_candidates("Jeff Davis", ["TX"])] == ["48243"]
    assert [c["fips"] for c in county_candidates("LA SALLE", ["LA"])] == ["22059"]

    # Clay exists in GA and NC: resolved by where the outage points are, else skipped
    layer = [{"name": "County", "boundaries": [{"name": "Clay", "customersOutNow": 40}, {"name": "Fannin", "customersOutNow": 7}]}]
    assert set(by_id(parse_boundaries(layer, "BRMEMC", ["GA", "NC"]))) == {"county-13111"}
    assert set(by_id(parse_boundaries(layer, "BRMEMC", ["GA", "NC"], point_fips={"37043"}))) == {"county-13111", "county-37043"}
    point_in_clay_nc = [{"outageRecID": "a", "customersOutNow": 40, "outagePoint": {"lat": 35.05, "lng": -83.75}}]
    assert regions.locate(-83.75, 35.05)[1]["fips"] == "37043"
    events = by_id(parse_wov({"customersOutNow": 47}, point_in_clay_nc, layer, "BRMEMC", ["GA", "NC"]))
    assert "county-37043" in events and "county-13061" not in events


def test_boundaries_one_group_per_parish():
    # scottarver keys Beauregard Electric's boundaries.json groups by name, calls them parishes and reads only
    # boundaries[0]: one single-row group per parish. Every parish counts, not just the first group.
    groups = [
        {"name": "Allen", "nameField": "NAME", "boundaries": [{"name": "Allen", "customersAffected": 5, "customersOutNow": 5, "customersServed": 4100}]},
        {"name": "Beauregard", "nameField": "NAME", "boundaries": [{"name": "Beauregard", "customersAffected": 50, "customersOutNow": 50, "customersServed": 20000}]},
        {"name": "Calcasieu Parish", "nameField": "NAME", "boundaries": [{"name": "Calcasieu", "customersAffected": 7, "customersOutNow": 7, "customersServed": 3000}]},
        {"name": "Jeff Davis", "nameField": "NAME", "boundaries": [{"customersAffected": 0, "customersOutNow": 2, "customersServed": 900}]},
    ]
    events = by_id(parse_boundaries(groups, "BECI", ["LA"]))
    assert {k: e.metrics["customers_out"] for k, e in events.items()} == {
        "county-22003": 5, "county-22011": 50, "county-22019": 7, "county-22053": 2}
    assert events["county-22011"].metrics["customers_served"] == 20000
    # layers of one area (a County and a Zip layer with one row each) are not merged
    one_area = [{"name": "County", "boundaries": [{"name": "Allen", "customersOutNow": 5}]},
                {"name": "Zip Code", "boundaries": [{"name": "70634", "customersOutNow": 5}]}]
    assert [e.id for e in parse_boundaries(one_area, "BECI", ["LA"])] == ["county-22003"]
    same_county_twice = [{"name": "Allen", "boundaries": [{"name": "Allen", "customersOutNow": 5}]},
                         {"name": "Allen Parish", "boundaries": [{"name": "Allen", "customersOutNow": 5}]}]
    assert select_county_layer(same_county_twice, ["LA"])["name"] == "Allen Parish"  # plain county layer rule


def test_county_layer_preference():
    districts = {"name": "County Commission District", "nameField": "DISTRICT",
                 "boundaries": [{"name": "District 1", "customersOutNow": 9}, {"name": "District 2", "customersOutNow": 4}]}
    counties = {"name": "County", "nameField": "NAME",
                "boundaries": [{"name": "Bryan", "customersOutNow": 9}, {"name": "Liberty", "customersOutNow": 4}]}
    assert layer_kind(districts) == "other" and layer_kind({"name": "Ward County"}) == "other"
    assert layer_kind({"name": "Howard County"}) == "county"  # "ward" only as a word
    assert select_county_layer([districts, counties], ["GA"]) is counties
    # of two county-named layers the one whose rows are county names wins; else the first
    county_names_odd = {"name": "Counties (service areas)", "boundaries": [{"name": "North", "customersOutNow": 1}, {"name": "South", "customersOutNow": 1}]}
    assert select_county_layer([county_names_odd, counties], ["GA"]) is counties
    assert select_county_layer([county_names_odd, counties], []) is county_names_odd


def test_parse_wov_combined(fixture):
    events = parse_wov(fixture("osj_summary.json"), fixture("osj_outages.json"), fixture("osj_boundaries.json"),
                       "Demo Co-op", ["TX"], link="https://outage.example.coop/")
    kinds = [e.metrics["kind"] for e in events]
    assert kinds == ["utility_total"] + ["county_outage"] * 4 + ["outage"] * 5
    total = events[0]
    assert total.id == "total" and total.metrics["customers_out"] == 8951 and total.metrics["customers_served"] == 37900
    assert total.metrics["outages"] == 5 and total.severity == Severity.extreme  # 23.6% out
    assert total.title == "Demo Co-op: 8,951 customers without power (23.6%)"
    assert {e.metrics["utility"] for e in events} == {"Demo Co-op"}
    assert len({e.id for e in events}) == len(events)
    # the county layer and the points agree with the summary total
    assert sum(e.metrics["customers_out"] for e in events if e.metrics["kind"] == "county_outage") == 8951
    assert sum(e.metrics["customers_out"] for e in events if e.metrics["kind"] == "outage") == 8951

    # no usable summary: county layer, then points, give the total; the served override wins
    assert parse_wov(None, fixture("osj_outages.json"), fixture("osj_boundaries.json"), "D", ["TX"])[0].metrics["customers_out"] == 8951
    t = parse_wov([], fixture("osj_outages.json"), None, "D", ["TX"], options={"customers_served": "50,000"})[0]
    assert t.metrics["customers_out"] == 8951 and t.metrics["customers_served"] == 50000
    # outages not fetched: the count comes from the summary if it has one
    only = parse_wov({"customersOutNow": 0, "customersServed": 900, "totalOutages": 0}, None, None, "D", ["TX"])
    assert len(only) == 1 and only[0].metrics["outages"] == 0 and only[0].severity == Severity.info


# --- fetch -----------------------------------------------------------------------------------------------


def cfg(**options):
    return SourceConfig.model_validate({"id": "wov_demo", "type": "milsoft_wov", "name": "Demo Co-op", "states": ["TX"],
                                        "url": "https://outage.example.coop:7576", **options})


def test_fetch(fixture):
    seen = []
    events = run(cfg(), fixture_server(fixture, seen))
    assert sorted(str(r.url) for r in seen) == [
        "https://outage.example.coop:7576/data/boundaries.json?v=2",
        "https://outage.example.coop:7576/data/outageSummary.json?v=2",
        "https://outage.example.coop:7576/data/outages.json?v=2",
    ]
    assert seen[0].headers["referer"] == "https://outage.example.coop:7576/"
    kinds = [e.metrics["kind"] for e in events]
    assert kinds.count("utility_total") == 1 and kinds.count("county_outage") == 4 and kinds.count("outage") == 5
    assert events[0].url == "https://outage.example.coop:7576/"


def test_fetch_options(fixture):
    seen = []
    events = run(cfg(params={}, county_reports=False, outage_points=False, link="https://coop.example/outages"),
                 fixture_server(fixture, seen))
    assert [str(r.url) for r in seen] == ["https://outage.example.coop:7576/data/outageSummary.json"]
    assert len(events) == 1 and events[0].url == "https://coop.example/outages"
    assert events[0].metrics["outages"] is None


def test_fetch_optional_files_missing(fixture):
    # outages.json and boundaries.json that do not exist (404/410) are skipped: the total still comes through
    events = run(cfg(), fixture_server(fixture, overrides={"/data/outages.json": httpx.Response(404)}))
    kinds = [e.metrics["kind"] for e in events]
    assert kinds == ["utility_total", "county_outage", "county_outage", "county_outage", "county_outage"]
    events = run(cfg(), fixture_server(fixture, overrides={"/data/boundaries.json": httpx.Response(410)}))
    assert "county_outage" not in [e.metrics["kind"] for e in events]
    # an HTML page in place of boundaries.json means the viewer has no Summary tab
    events = run(cfg(), fixture_server(fixture, overrides={"/data/boundaries.json": httpx.Response(200, text="<html/>")}))
    assert "county_outage" not in [e.metrics["kind"] for e in events] and len(events) == 6


def test_fetch_transient_failures_keep_previous_events(fixture):
    # a 403 (firewall, rate limit) or an HTML page in place of outages.json fails the poll, so the scheduler
    # keeps the previous points instead of clearing every outage and re-creating them next poll
    with pytest.raises(SourceError, match="outages.json: HTTP 403"):
        run(cfg(), fixture_server(fixture, overrides={"/data/outages.json": httpx.Response(403)}))
    with pytest.raises(SourceError, match="outage_points: false"):
        run(cfg(), fixture_server(fixture, overrides={"/data/outages.json": httpx.Response(200, text="<html>viewer</html>")}))
    with pytest.raises(SourceError, match="boundaries.json: HTTP 403"):
        run(cfg(), fixture_server(fixture, overrides={"/data/boundaries.json": httpx.Response(403)}))


def test_fetch_errors(fixture):
    with pytest.raises(SourceError, match="outageSummary.json: HTTP 500"):
        run(cfg(), fixture_server(fixture, overrides={"/data/outageSummary.json": httpx.Response(500)}))
    with pytest.raises(SourceError, match="invalid JSON"):
        run(cfg(), fixture_server(fixture, overrides={"/data/outageSummary.json": httpx.Response(200, text="<html/>")}))
    with pytest.raises(SourceError, match="not an object"):
        run(cfg(), fixture_server(fixture, overrides={"/data/outageSummary.json": httpx.Response(200, json=[])}))
    # a server error on an optional file fails the poll, so the previous points are kept rather than cleared
    with pytest.raises(SourceError, match="outages.json: HTTP 502"):
        run(cfg(), fixture_server(fixture, overrides={"/data/outages.json": httpx.Response(502)}))

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(SourceError, match="refused"):
        run(cfg(), down)


def test_registration():
    cls = REGISTRY["milsoft_wov"]
    assert cls.default_interval >= 300 and cls.required_options == ("url",)
    src = cls(SourceConfig(id="x", type="milsoft_wov"), SourceContext(None, AreaConfig(), None))
    assert src.config_error() == "not configured: set url"


# --- catalog ---------------------------------------------------------------------------------------------


def pending_entries():
    return yaml.safe_load(PENDING.read_text())


def test_pending_catalog_entries_are_valid():
    entries = pending_entries()
    assert len(entries) >= 57
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "power_outage_summary.yaml"}
    valid_states = regions.state_codes()
    seen = set()
    for e in entries:
        sid = e["id"]
        assert sid not in seen, f"duplicate id {sid}"
        seen.add(sid)
        assert sid not in existing, f"{sid} collides with an existing catalog id"
        assert sid.startswith("wov_")
        assert e["type"] in REGISTRY and e["type"] == "milsoft_wov"
        sc = SourceConfig.model_validate(_interpolate(e))
        assert sc.states and all(len(st) == 2 and st.isupper() and st in valid_states for st in sc.states), sid
        assert sc.meta["confidence"] in ("high", "medium", "low") and sc.meta["evidence"] and sc.meta.get("notes")
        url = sc.options["url"]
        assert url.startswith(("https://", "http://")) and url.endswith("/") and "/data/" not in url, sid
        assert set(sc.options) <= {"url", "params", "link", "boundary_layer", "county_reports", "outage_points"}
        if sc.meta["confidence"] == "high":
            assert ";" in sc.meta["evidence"], f"{sid}: high confidence needs two sources"
        if not sc.enabled:
            assert sc.meta["confidence"] == "low"
        src = REGISTRY[sc.type](sc, SourceContext(None, AreaConfig(), None))
        assert src.config_error() is None
        assert src.data_url("summary") == url + "data/outageSummary.json"


def test_pending_catalog_southeast_coverage():
    entries = pending_entries()
    southeast = {"TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC"}
    by_state = {st: [e["id"] for e in entries if st in e["states"]] for st in southeast}
    assert all(by_state[st] for st in southeast), by_state
    assert {"wov_bec_la", "wov_dixie_la", "wov_ngemc_ga", "wov_bbec_tx", "wov_keys_energy_fl", "wov_bcec_tx",
            "wov_midsouth_ec_tx"} <= {e["id"] for e in entries}


_GENERIC_WORDS = re.compile(
    r"\b(electric(al)?|co ?op(erative)?|corp(oration)?|inc|assn|association|membership|emc|ec|rec|remc|power|light|"
    r"utilities|utility|energy|and|the|of|company|co|services?|system|department|dept|public|rural|municipal)\b"
)


def _utility_key(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", str(name).lower().replace("&", " and ").replace("-", " "))
    return " ".join(_GENERIC_WORDS.sub(" ", text).split())


def test_no_enabled_duplicate_of_another_catalog():
    # Two enabled sources for one utility would count it twice in state and national power totals.
    others = []
    for path in sorted(PENDING.parent.glob("*.yaml*")):
        if path == PENDING or not path.name.endswith((".yaml", ".yaml")):
            continue
        try:
            entries = yaml.safe_load(path.read_text()) or []
        except yaml.YAMLError:
            continue  # another group's file mid-edit; its own test reports it
        others += [(path.name, e) for e in entries if isinstance(e, dict) and e.get("enabled", True) is not False]
    clashes = []
    for e in pending_entries():
        key = _utility_key(e["name"])
        if not key or e.get("enabled", True) is False:
            continue
        for fname, o in others:
            if _utility_key(o.get("name", "")) == key and set(e["states"]) & set(o.get("states") or []):
                clashes.append(f"{e['id']} = {o.get('id')} ({fname})")
    assert not clashes, clashes
    by_id_entries = {e["id"]: e for e in pending_entries()}
    for sid, other in (("wov_nueces_ec_tx", "nisc_nueceselectric_tx"), ("wov_wood_county_ec_tx", "nisc_wcec_tx")):
        assert by_id_entries[sid]["enabled"] is False and other in by_id_entries[sid]["meta"]["notes"]


def test_fixtures_are_json():
    data = Path(catalog.__file__).parent.parent / "demo_data"
    for name in ("osj_summary.json", "osj_outages.json", "osj_boundaries.json"):
        json.loads((data / name).read_text())
