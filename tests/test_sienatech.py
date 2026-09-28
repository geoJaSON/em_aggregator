"""Siena Technologies WebMaps co-op outage feed (cache.sienatech.com .../webmaps/data/<CODE>/OUTAGE)."""

import asyncio
import glob
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

import emagg.sources.sienatech  # noqa: F401  (registers the adapter)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.geo import encode_polyline
from emagg.models import Category, Severity
from emagg.sources.kubra import county_severity, customers_severity
from emagg.sources import REGISTRY, SourceContext
from emagg.sources.base import SourceError
from emagg.sources.sienatech import (
    county_report,
    etr_value,
    is_county_table,
    local_time,
    lonlat,
    match_county,
    parse_outage_data,
    zone_for,
)
from emagg.store import Store

PENDING = Path(catalog.CATALOG_DIR) / "power_sienatech.yaml"
SOUTHEAST = {"TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC"}
UTC = timezone.utc


def by_id(events):
    return {e.id: e for e in events}


# --- the OUTAGE document -------------------------------------------------------------------------------------------


def test_parse_fixture_total_and_counties(fixture, now):
    events = parse_outage_data(fixture("sienatech_ssemc_outage.json"), "Snapping Shoals EMC", ["GA"],
                               link="https://ssemc.example/", tz=zone_for(["GA"]))
    ev = by_id(events)
    assert {e.metrics["utility"] for e in events} == {"Snapping Shoals EMC"}
    assert all(e.category == Category.power for e in events)

    total = ev["total"]
    assert total.metrics["customers_out"] == 1874 and total.metrics["customers_served"] == 111265
    assert total.metrics["outages"] == 6 and total.states == ["GA"]
    assert total.severity == Severity.moderate  # 1.7% of customers
    assert total.url == "https://ssemc.example/"

    # Only the "County" report becomes counties; zero-out counties, District and Zip tables are not used.
    counties = {e.fips: e for e in events if e.metrics["kind"] == "county_outage"}
    assert set(counties) == {"13151", "13217", "13247", "13035"}  # Henry, Newton, Rockdale, Butts
    henry = counties["13151"]
    assert henry.id == "county-13151" and henry.states == ["GA"]
    assert henry.metrics["customers_out"] == 1210 and henry.metrics["customers_served"] == 45812
    assert henry.metrics["outages"] == 3 and henry.geometry["type"] in ("Polygon", "MultiPolygon")
    assert henry.metrics["etr"] == "2026-09-27T21:00:00+00:00"  # ORDS ISO time, 3 h after NOW
    assert counties["13035"].metrics["etr"] is None
    assert sum(c.metrics["customers_out"] for c in counties.values()) == total.metrics["customers_out"]

    # The outage list carries no positions: nothing is drawn as a point, and nothing is invented.
    assert not [e for e in events if e.metrics["kind"] == "outage"]
    assert set(by_id(parse_outage_data(fixture("sienatech_ssemc_outage.json"), "SSEMC", ["GA"]))) == set(ev)


def test_county_report_selection():
    reports = [None, "x", {"name": "District", "polygons": []}, {"name": "Zip"}, {"name": "Counties", "polygons": []}]
    assert county_report(reports)["name"] == "Counties"
    assert county_report([{"name": "Parish"}])["name"] == "Parish"
    assert county_report([{"name": "Substation"}, {"name": "District"}, {"name": "Zip"}]) is None
    assert county_report([{"name": "Service Area"}, {"name": "Member Area"}], "member area")["name"] == "Member Area"
    assert county_report([{"name": "County"}], "Township") is None
    for name in ("County", "Counties", "PARISH", "Parishes", "Borough", "COUNTY_REPORT", "CountyOutages",
                 "outageCounty", "By County"):
        assert is_county_table(name), name
    for name in ("Country Club District", "Discount Zone", "Substation", "Zip", "District", None, "", 5):
        assert not is_county_table(name), name


def test_district_only_tenant_gets_no_county_events():
    payload = {"reportData": {"summary": {"affected": 40, "accounts": 1000, "outages": 2},
                              "reports": [{"name": "Substation", "polygons": [{"name": "Henry", "affected": 40}]}]}}
    events = parse_outage_data(payload, "U", ["GA"])
    assert [e.id for e in events] == ["total"]  # a substation named "Henry" is not Henry County
    forced = parse_outage_data(payload, "U", ["GA"], county_report_name="Substation")
    assert "county-13151" in by_id(forced)  # unless the operator says that table is counties


def test_match_county_names_and_ambiguity():
    assert match_county("HENRY COUNTY", ["GA"])["fips"] == "13151"
    assert match_county("DeKalb", ["GA"])["fips"] == "13089"
    assert match_county("13151", ["GA"])["fips"] == "13151"
    assert match_county("13151", ["TX"]) is None  # a FIPS code outside the utility's states is not its county
    assert match_county("48201", ["GA"]) is None and match_county("48201", [])["state"] == "TX"
    assert match_county("Middlesex", ["NH", "MA"])["fips"] == "25017"
    assert match_county("Hillsborough", ["NH", "MA"])["fips"] == "33011"
    assert match_county("Hillsborough", ["NH", "FL"]) is None  # in both of the utility's states: skipped
    assert match_county("Hillsborough, FL", ["NH", "FL"])["fips"] == "12057"
    assert match_county("Rockingham (NH)", ["NH", "MA"])["fips"] == "33015"
    assert match_county("St Tammany Parish", ["LA"])["fips"] == "22103"
    assert match_county("", ["GA"]) is None and match_county(None, ["GA"]) is None
    assert match_county("30253", ["GA"]) is None and match_county("Nowhere", ["GA"]) is None


def test_multi_state_utility_counties():
    payload = {"reportData": {"summary": {"affected": 30, "accounts": 110000},
                              "reports": [{"name": "County", "polygons": [
                                  {"name": "Middlesex", "affected": 12, "accounts": 30000},
                                  {"name": "Rockingham", "affected": 18, "accounts": 50000},
                                  {"name": "Worcester", "affected": 5}]}]}}
    ev = by_id(parse_outage_data(payload, "Unitil", ["NH", "MA"]))
    assert ev["county-25017"].states == ["MA"] and ev["county-33015"].states == ["NH"]
    assert ev["county-25027"].metrics["customers_served"] is None  # Worcester, MA
    assert ev["total"].states == ["NH", "MA"]


def test_bad_records_skipped_and_fallbacks():
    payload = {
        "reportData": {"reports": [None, {"name": "County", "polygons": [
            None, "x", {"name": "Henry", "affected": "many"}, {"name": "Nowhere", "affected": 5},
            {"name": "Newton", "affected": "7", "accounts": 0}, {"name": "Newton", "affected": 3},
            {"affected": 9}]}]},
        "outageData": {"outages": [None, {"id": 1, "customersAffected": 4}]},
    }
    ev = by_id(parse_outage_data(payload, "U", ["GA"]))
    # No summary: the total is the county report's sum (unmatched names included), outages from the list.
    assert ev["total"].metrics["customers_out"] == 24 and ev["total"].metrics["outages"] == 1
    assert ev["total"].metrics["customers_served"] is None
    newton = ev["county-13217"]
    assert newton.metrics["customers_out"] == 10 and newton.metrics["customers_served"] is None
    assert "county-13151" not in ev

    only_outages = {"outageData": {"outages": [{"customersAffected": 5}, {"customersAffected": "2"}, {}]}}
    assert by_id(parse_outage_data(only_outages, "U", ["GA"]))["total"].metrics["customers_out"] == 7
    for junk in (None, [], "x", {}, {"reportData": "x", "outageData": [1]}, {"reportData": {"reports": {"a": 1}}}):
        events = parse_outage_data(junk, "U", ["GA"])
        assert [e.id for e in events] == ["total"] and events[0].metrics["customers_out"] == 0
    served = parse_outage_data({"reportData": {"summary": {"affected": 50, "accounts": 100}}}, "U", ["GA"],
                               customers_served=1000)
    assert served[0].metrics["percent_out"] == 5.0


def test_duplicates_are_added_up_before_the_event_is_built(now):
    payload = {"reportData": {"summary": {"affected": 1100, "accounts": 2000},
                              "reports": [{"name": "County", "polygons": [
                                  {"name": "Henry", "affected": 10, "accounts": 100, "outages": 1,
                                   "etor": "2026-09-27T20:00:00Z"},
                                  {"name": "HENRY COUNTY", "affected": 1000, "accounts": 900, "outages": 2,
                                   "etor": "2026-09-27T22:00:00Z"},
                                  {"name": "Newton", "affected": 40, "accounts": 400},
                                  {"name": "Newton County", "affected": 50}]}]},
               "outageData": {"outages": [
                   {"id": 7, "customersAffected": 5, "lat": 33.45, "lon": -84.15, "cause": "Tree"},
                   {"id": 7, "customersAffected": 1000, "lat": 33.46, "lon": -84.16}]}}
    ev = by_id(parse_outage_data(payload, "U", ["GA"]))
    henry = ev["county-13151"]
    assert henry.metrics["customers_out"] == 1010 and henry.metrics["customers_served"] == 1000
    assert henry.metrics["percent_out"] == 101.0 and henry.metrics["outages"] == 3
    assert henry.title == "U: 1,010 out in Henry, GA (101%)" and henry.severity == county_severity(101.0, 1010)
    assert henry.metrics["etr"] == "2026-09-27T22:00:00+00:00"  # the later estimate
    newton = ev["county-13217"]  # one row without a customer count: no percentage rather than a wrong one
    assert newton.metrics["customers_out"] == 90 and newton.metrics["customers_served"] is None
    assert newton.metrics["percent_out"] is None and newton.title == "U: 90 out in Newton, GA"
    point = ev["7"]
    assert point.metrics["customers_out"] == 1005 and point.title == "1,005 customers out — Tree"
    assert point.severity == customers_severity(1005) and point.geometry["coordinates"] == [-84.15, 33.45]


def test_non_finite_numbers_do_not_raise():
    for bad in ("NaN", "inf", "-Infinity", "1e400", float("nan"), float("inf")):
        payload = {"reportData": {"summary": {"affected": bad, "accounts": bad, "outages": bad},
                                  "reports": [{"name": "County", "polygons": [
                                      {"name": "Henry", "affected": bad, "accounts": bad},
                                      {"name": "Newton", "affected": 5, "accounts": bad, "outages": bad}]}]},
                   "outageData": {"outages": [{"id": 1, "customersAffected": bad, "lat": 33.4, "lon": -84.1},
                                              {"id": 2, "customersAffected": 3, "lat": bad, "lon": -84.1}]}}
        ev = by_id(parse_outage_data(payload, "U", ["GA"]))
        assert ev["total"].metrics["customers_out"] == 5  # falls back to the county report's usable rows
        assert ev["total"].metrics["customers_served"] is None and ev["total"].metrics["outages"] == 2
        assert set(ev) == {"total", "county-13217"}
        assert ev["county-13217"].metrics["customers_served"] is None and "outages" not in ev["county-13217"].metrics


# --- positions and times ------------------------------------------------------------------------------------------


def test_lonlat_spellings():
    want = (-84.15, 33.45)
    for rec in (
        {"lat": 33.45, "lon": -84.15},
        {"Latitude": "33.45", "Longitude": "-84.15"},
        {"lat": 33.45, "lng": -84.15},
        {"x": -84.15, "y": 33.45},
        {"lat": -84.15, "lon": 33.45},  # swapped: US longitudes are negative
        {"position": "33.45,-84.15"},  # the older Siena feed's "lat,lon" string
        {"location": {"lat": 33.45, "lng": -84.15}},
        {"geometry": {"type": "Point", "coordinates": [-84.15, 33.45]}},
        {"coordinates": [-84.15, 33.45]},
        {"g": encode_polyline([(33.45, -84.15), (33.46, -84.14)])},  # Siena line-layer encoding
        {"gps_lat": 33.45, "gps_lng": -84.15},
    ):
        assert lonlat(rec) == pytest.approx(want), rec
    for rec in (None, "x", {}, {"lat": 0, "lon": 0}, {"lat": "abc", "lon": -84}, {"lat": 33.4},
                {"x": 1234567, "y": 3456789}, {"position": "somewhere"}, {"longestDuration": 45, "lat": 33},
                {"g": "!!"}, {"lat": True, "lon": -84}, {"geometry": {"type": "Polygon", "coordinates": [[1, 2]]}}):
        assert lonlat(rec) is None, rec


def test_outage_points_when_positions_present(now):
    payload = {"reportData": {"summary": {"affected": 1500, "accounts": 50000, "outages": 4}},
               "outageData": {"outages": [
                   {"id": 11, "county": "Henry", "customersAffected": 900, "lat": 33.45, "lon": -84.15,
                    "etor": "2026-09-27T21:00:00Z", "outageStart": "2026-09-27 12:30:00", "cause": "Tree"},
                   {"id": 12, "customersAffected": 500, "position": "33.60,-83.86", "etor": "Assessing"},
                   {"customersAffected": 60, "latitude": 33.30, "longitude": -84.00, "outageStart": "2026-09-27T16:00:00Z"},
                   {"id": 14, "customersAffected": 40},  # no position: counted, not drawn
                   {"id": 15, "customersAffected": 0, "lat": 33.4, "lon": -84.1},
               ]}}
    tz = zone_for(["GA"])
    events = parse_outage_data(payload, "SSEMC", ["GA"], tz=tz)
    points = [e for e in events if e.metrics["kind"] == "outage"]
    assert [p.id for p in points][:2] == ["11", "12"] and len(points) == 3
    first = points[0]
    assert first.metrics["customers_out"] == 900 and first.metrics["cause"] == "Tree" and first.metrics["county"] == "Henry"
    assert first.starts_at == datetime(2026, 9, 27, 16, 30, tzinfo=UTC)  # naive local (EDT) -> UTC
    assert first.metrics["etr"] == "2026-09-27T21:00:00+00:00"
    assert regions.locate(*first.geometry["coordinates"])[1]["fips"] == "13151"
    assert points[1].metrics["etr"] == "Assessing"
    hashed = points[2]
    assert hashed.id.startswith("pt-") and hashed.id == [e for e in parse_outage_data(payload, "SSEMC", ["GA"])
                                                        if e.metrics["kind"] == "outage"][2].id  # stable
    top1 = parse_outage_data(payload, "SSEMC", ["GA"], max_points=1, counties=False)
    assert [e.id for e in top1] == ["total", "11"]
    assert [e.id for e in parse_outage_data(payload, "SSEMC", ["GA"], outage_points=False)] == ["total"]


def test_times_and_zones():
    ny, chi = zone_for(["GA"]), zone_for(["TX"], "America/Chicago")
    assert str(ny) == "America/New_York" and str(chi) == "America/Chicago"
    assert zone_for(["TX"]) is None and zone_for(["GA", "AL"]) is None and zone_for([]) is None
    assert str(zone_for(["NH", "MA"])) == "America/New_York" and zone_for(["GA"], "Not/AZone") is None
    assert local_time("2026-09-27T16:00:00Z", None) == datetime(2026, 9, 27, 16, tzinfo=UTC)
    assert local_time("2026-09-27T11:00:00-05:00", None) == datetime(2026, 9, 27, 16, tzinfo=UTC)
    assert local_time("2026-09-27 11:00:00", chi) == datetime(2026, 9, 27, 16, tzinfo=UTC)
    assert local_time("09/27/2026 11:00 AM", chi) == datetime(2026, 9, 27, 16, tzinfo=UTC)
    assert local_time("2026-09-27 11:00:00", None) is None  # no zone: not guessed
    assert local_time(1790524800000, None) == datetime(2026, 9, 27, 16, tzinfo=UTC)
    for bad in (None, "", True, "0000-00-00 00:00:00", "soon", "13/45/2026 99:99"):
        assert local_time(bad, ny) is None
    assert etr_value("Assessing", ny) == "Assessing"
    assert etr_value("0000-00-00 00:00:00", ny) is None and etr_value("NULL", ny) is None
    assert etr_value("2026-09-27 11:00:00", None) is None  # a time we cannot place is dropped, not shown raw


# --- adapter --------------------------------------------------------------------------------------------------------


def run_fetch(options, handler, states=("GA",)):
    cfg = SourceConfig.model_validate({"id": "t", "type": "sienatech", "name": "Snapping Shoals EMC",
                                       "states": list(states), **options})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = REGISTRY["sienatech"](cfg, SourceContext(http, AreaConfig(), Store()))
            assert src.interval >= 300 and src.config_error() is None
            return await src.fetch()

    return asyncio.run(go())


def test_fetch(fixture):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert request.method == "GET"
        return httpx.Response(200, json=fixture("sienatech_ssemc_outage.json"))

    events = run_fetch({"code": "SSEMC", "link": "https://ssemc.example/"}, handler)
    assert seen == ["https://cache.sienatech.com/apex/siena_ords/webmaps/data/SSEMC/OUTAGE"]
    kinds = [e.metrics["kind"] for e in events]
    assert kinds.count("utility_total") == 1 and kinds.count("county_outage") == 4
    assert events[0].url == "https://ssemc.example/"
    run_fetch({"code": "SSEMC", "base_url": "https://mirror.example/data/"},
              lambda r: httpx.Response(200, json={"reportData": {}}) if r.url.path == "/data/SSEMC/OUTAGE"
              else httpx.Response(404))


@pytest.mark.parametrize("response, message", [
    (httpx.Response(420), "rate limited"),
    (httpx.Response(500), "HTTP 500"),
    (httpx.Response(200, text="<html>"), "invalid JSON"),
    (httpx.Response(200, json=[1, 2]), "unexpected"),
    (httpx.Response(200, json={"error": "nope"}), "unexpected"),
])
def test_fetch_bad_responses_raise(response, message):
    with pytest.raises(SourceError, match=message):
        run_fetch({"code": "SSEMC"}, lambda request: response)


def test_missing_code_is_a_config_error():
    cfg = SourceConfig.model_validate({"id": "t", "type": "sienatech"})
    src = REGISTRY["sienatech"](cfg, SourceContext(None, AreaConfig(), Store()))
    assert src.config_error() == "not configured: set code"


# --- catalog ----------------------------------------------------------------------------------------------------------


def test_pending_catalog_entries_are_valid():
    entries = yaml.safe_load(PENDING.read_text())
    assert isinstance(entries, list) and len(entries) >= 12
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "power_sienatech.yaml"}
    known_states = regions.state_codes()
    seen, codes = set(), set()
    for e in entries:
        assert e["id"].startswith("sienatech_") and e["id"] not in seen and e["id"] not in existing, e["id"]
        seen.add(e["id"])
        assert e["type"] == "sienatech" and e["type"] in REGISTRY
        cfg = SourceConfig.model_validate(_interpolate(e))
        assert cfg.name and cfg.states and all(len(s) == 2 and s.isupper() and s in known_states for s in cfg.states)
        code = cfg.options["code"]
        assert code == code.upper() and code not in codes
        codes.add(code)
        assert cfg.meta["confidence"] in ("high", "medium", "low") and cfg.meta["evidence"]
        if cfg.meta["confidence"] == "high":
            assert cfg.meta["evidence"].count("github.com/") >= 2
        if "link" in cfg.options:
            assert cfg.options["link"].startswith(("https://", "http://"))
        if "timezone" in cfg.options:
            assert zone_for(cfg.states, cfg.options["timezone"]) is not None
        src = REGISTRY["sienatech"](cfg, SourceContext(None, AreaConfig(), Store()))
        assert src.config_error() is None and src.interval >= 300
    assert {"SSEMC", "MVEC", "DEC", "UNITIL", "BREMCO", "PRECO", "HOMER", "CFEMC", "COAST", "BEMC", "JOEMC",
            "CORE"} <= codes  # every tenant in lukesteve03's sienatech_sources.json
    southeast = [e for e in entries if set(e["states"]) & SOUTHEAST and e.get("enabled", True)]
    assert len(southeast) >= 8


def _norm(name):
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def test_enabled_entries_do_not_duplicate_other_catalogs():
    """A utility enabled in two catalogs (two platforms) would be counted twice in the Power and state totals."""
    mine = [e for e in yaml.safe_load(PENDING.read_text()) if e.get("enabled", True)]
    others = []
    for path in sorted(glob.glob(str(Path(catalog.CATALOG_DIR) / "*.yaml*"))):
        if Path(path).name != PENDING.name:
            others += [o for o in yaml.safe_load(Path(path).read_text()) or [] if isinstance(o, dict)]
    for e in mine:
        for o in others:
            if o.get("enabled", True) and set(o.get("states") or []) & set(e["states"]):
                assert _norm(o.get("name")) != _norm(e["name"]), (e["id"], o.get("id"))
    demco = next(e for e in yaml.safe_load(PENDING.read_text()) if e["id"] == "sienatech_demco_la")
    assert demco["enabled"] is False  # covered by wov_dixie_la (Milsoft WOV)
