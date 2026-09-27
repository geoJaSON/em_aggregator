"""Adapters added for national coverage: comms, coastal flooding, storm reports, NHC GIS, FEMA, power, generic."""

import asyncio

import httpx

from emagg.config import AreaConfig, SourceConfig
from emagg.demo import DemoTransport, load_fixture, load_text_fixture
from emagg.models import Category, Severity
from emagg.sources import REGISTRY, SourceContext
from emagg.sources.coops import classify, station_event, thresholds
from emagg.sources.county_outages import parse_county_table
from emagg.sources.faa_nas import _minutes, parse_nas
from emagg.sources.fema_declarations import parse_declarations
from emagg.sources.iem_lsr import parse_lsr
from emagg.sources.ioda import parse_ioda
from emagg.sources.ipaws import parse_cap_feed
from emagg.sources.kubra import parse_county_report, parse_summary
from emagg.sources.mapped import FieldMapping, flatten, parse_json_records
from emagg.sources.nhc_gis import parse_nhc_layer
from emagg.store import Store
from emagg.util import parse_time


def by_id(events):
    return {e.id: e for e in events}


def run_source(cfg: SourceConfig, area: AreaConfig | None = None):
    async def go():
        async with httpx.AsyncClient(transport=DemoTransport()) as http:
            src = REGISTRY[cfg.type](cfg, SourceContext(http, area or AreaConfig(), Store()))
            return await src.fetch()

    return asyncio.run(go())


# --- comms / infrastructure -------------------------------------------------------------------------


def test_ioda(fixture):
    events = by_id(parse_ioda(fixture("ioda_alerts.json")))
    assert set(events) == {"region-TX"}  # Florida recovered; country "Georgia" is not a US state
    tx = events["region-TX"]
    assert tx.severity == Severity.severe and tx.states == ["TX"] and tx.category == Category.comms
    assert tx.metrics["max_drop_pct"] == 22 and tx.url.endswith("/region/4435")


def test_faa(now):
    events = by_id(parse_nas(load_text_fixture("faa_status.xml", now)))
    assert events["airport_closure-GLS"].severity == Severity.severe
    assert events["airport_closure-GLS"].geometry["type"] == "Point"
    assert events["ground_stop-HOU"].title.startswith("Ground stop: William P Hobby")
    assert events["ground_delay-IAH"].metrics["avg_delay_min"] == 84
    assert events["delay-MSY-departure"].severity == Severity.info
    assert "delay-MSY-departure" not in by_id(parse_nas(load_text_fixture("faa_status.xml", now), include_delays=False))
    assert _minutes("45 minutes") == 45 and _minutes("2 hours") == 120 and _minutes(None) is None


def test_ipaws(now):
    events, removed = parse_cap_feed(load_text_fixture("ipaws_public.xml", now), now=now)
    events = by_id(events)
    assert set(events) == {"DEMO-GALVESTON-EVI-1", "DEMO-HARRIS-TOE-1"}  # NWS duplicate and AMBER skipped
    evac = events["DEMO-GALVESTON-EVI-1"]
    assert evac.severity == Severity.extreme and evac.states == ["TX"]
    assert evac.geometry["type"] == "MultiPolygon"  # two CAP polygons; county outline not needed
    toe = events["DEMO-HARRIS-TOE-1"]
    assert toe.category == Category.comms and toe.geometry  # drawn from the SAME county code (Harris)
    with_nws, _ = parse_cap_feed(load_text_fixture("ipaws_public.xml", now), include_nws=True, now=now)
    assert "DEMO-NWS-DUP" in by_id(with_nws)


def test_ipaws_cancel_and_update(now):
    xml = """<alerts><alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>B</identifier><sender>x@y.gov</sender>
      <sent>2026-09-27T17:00:00Z</sent><status>Actual</status><msgType>Cancel</msgType><scope>Public</scope>
      <references>x@y.gov,A,2026-09-27T16:00:00Z</references><info><event>Evacuation Immediate</event></info></alert>
      <alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>D</identifier><sender>x@y.gov</sender>
      <sent>2026-09-27T17:00:00Z</sent><status>Actual</status><msgType>Update</msgType><scope>Public</scope>
      <references>x@y.gov,C,2026-09-27T16:00:00Z</references><info><event>Shelter in Place Warning</event>
      <eventCode><valueName>SAME</valueName><value>SPW</value></eventCode>
      <area><areaDesc>Somewhere</areaDesc><circle>29.7,-95.3 5</circle></area></info></alert></alerts>"""
    events, removed = parse_cap_feed(xml, now=now)
    assert removed == {"A", "C"}
    (spw,) = events
    assert spw.supersedes == ["C"] and spw.severity == Severity.severe and spw.geometry["type"] == "Polygon"


# --- coastal / storm reports / NHC / FEMA -------------------------------------------------------------


def test_coops_thresholds_and_classes():
    flood = load_fixture("coops_floodlevels.json")
    assert thresholds(flood["8771450"])[1] == "NWS"
    assert thresholds(flood["8771341"])[1] == "NOS"  # NWS values missing -> NOS
    assert thresholds(flood["8772447"]) is None
    t, _ = thresholds(flood["8771450"])
    assert classify(7.2, t, 0.5) == ("major", Severity.extreme)
    assert classify(4.5, t, 0.5) == ("near", Severity.minor)
    assert classify(3.0, t, 0.5) is None


def test_coops_fetch_filters_area_and_rising_surge():
    events = by_id(run_source(SourceConfig(id="c", type="coops_water_levels"), AreaConfig(states=["TX"])))
    assert set(events) == {"8771450", "8771341"}
    pier = events["8771450"]
    assert pier.title == "Moderate coastal flooding: Galveston Pier 21" and pier.metrics["ft_above_minor"] > 1
    assert pier.updated_at is not None and pier.states == ["TX"]


def test_coops_station_event_without_data():
    assert station_event({"id": "1", "lat": 0, "lng": 0}, {"data": []}, {"nws_minor": 1}) is None


def test_lsr(fixture):
    events = parse_lsr(fixture("iem_lsr.json"))
    titles = {e.title: e for e in events}
    assert len(events) == 4  # snow report not in the type table
    surge = titles["Storm surge (6.5 FT): Surfside Beach, TX"]
    assert surge.category == Category.flood and surge.severity == Severity.severe
    gust = titles["Thunderstorm wind gust (82 MPH): Kemah, TX"]
    assert gust.severity == Severity.moderate  # >= 75 mph
    assert len(parse_lsr(fixture("iem_lsr.json"), include_all=True)) == 5


def test_nhc_gis(fixture):
    cone = parse_nhc_layer("cone", fixture("nhc_gis_cone.json")["features"])[0]
    assert cone.id == "cone-AT9" and cone.title == "Forecast cone: Hurricane Demo (advisory 18)"
    ww = by_id(parse_nhc_layer("watch_warning", fixture("nhc_gis_ww.json")["features"]))
    assert ww["ww-AT9-HWR-11"].severity == Severity.extreme
    assert ww["ww-AT9-TWR-12"].title == "Tropical Storm Warning (coast): Hurricane Demo"


def test_fema_declarations(fixture):
    (d,) = parse_declarations(fixture("fema_declarations.json"))
    assert d.id == "EM-3999-TX" and d.metrics["counties"] == 4 and d.metrics["programs"] == ["PA"]
    assert d.geometry["type"] == "MultiPolygon" and d.url.endswith("/3999")
    assert parse_declarations(fixture("fema_declarations.json"), states=["FL"]) == []


def test_compact_timestamps():
    assert parse_time("202609271200").isoformat() == "2026-09-27T12:00:00+00:00"
    assert parse_time("1790000000000").year == 2026  # epoch ms still works


# --- power ----------------------------------------------------------------------------------------------


def test_kubra_multi_state_totals():
    summary = {"summaryFileData": {"totals": [
        {"total_cust_a": {"val": 100}, "total_cust_s": 1000, "total_outages": 3},
        {"total_cust_a": {"val": 50}, "total_cust_s": 500, "total_outages": 2}]}}
    e = parse_summary(summary, "Two State Power")
    assert e.metrics["customers_out"] == 150 and e.metrics["customers_served"] == 1500 and e.metrics["outages"] == 5


def test_kubra_county_report_nested_states():
    report = {"file_data": {"areas": [
        {"key": "state", "name": "Louisiana", "areas": [
            {"key": "county", "name": "Caddo", "cust_a": {"val": 1200}, "cust_s": 50000},
            {"key": "county", "name": "Bossier", "cust_a": {"val": 0}, "cust_s": 30000}]},
        {"key": "state", "name": "Texas", "areas": [{"key": "county", "name": "Harrison", "cust_a": {"val": 300}, "cust_s": 20000}]},
    ]}}
    events = by_id(parse_county_report(report, "SWEPCO", ["LA", "AR", "TX"]))
    assert set(events) == {"county-22017", "county-48203"}
    caddo = events["county-22017"]
    assert caddo.states == ["LA"] and caddo.fips == "22017" and caddo.metrics["percent_out"] == 2.4
    assert caddo.geometry["type"] in ("Polygon", "MultiPolygon")


def test_kubra_demo_county_reports():
    cfg = SourceConfig(id="k", type="kubra", name="Acme", states=["TX"], instance_id="demo-instance",
                       view_id="demo-view", outage_points=False)
    events = run_source(cfg)
    kinds = [e.metrics["kind"] for e in events]
    assert kinds.count("utility_total") == 1 and kinds.count("county_outage") == 5 and "outage" not in kinds


def test_county_table_by_name_and_fips():
    fpl = [{"County Name": "Miami-Dade", "Customers Out": "12,345", "Customers Served": "1,200,000"},
           {"County Name": "Broward", "Customers Out": "0", "Customers Served": "950,000"},
           {"County Name": "Not A County", "Customers Out": "5", "Customers Served": "10"}]
    (e,) = parse_county_table(fpl, "FPL", {"county_field": "County Name", "out_field": "Customers Out",
                                            "served_field": "Customers Served"}, ["FL"])
    assert e.id == "county-12086" and e.metrics["customers_out"] == 12345 and e.severity == Severity.minor
    odin = [{"communitydescriptor": "22071", "metersaffected": 900, "name": "Utility A"},
            {"communitydescriptor": "22071", "metersaffected": 100, "name": "Utility B"}]
    events = parse_county_table(odin, "ODIN", {"fips_field": "communitydescriptor", "out_field": "metersaffected",
                                               "utility_field": "name"}, [])
    assert {e.id for e in events} == {"county-22071-Utility A", "county-22071-Utility B"}
    entergy = [{"county": "Orleans", "customersAffected": 40000, "customersServed": 80000, "state": "L"}]
    (e,) = parse_county_table(entergy, "Entergy", {"county_field": "county", "out_field": "customersAffected",
                                                   "served_field": "customersServed"}, ["LA"])
    assert e.severity == Severity.extreme and e.area == "Orleans, LA"  # 50% out


def test_json_records_datacapable_and_cleco():
    events = parse_json_records(
        [{"id": 1, "numPeople": 2500, "latitude": 29.7, "longitude": -95.4, "type": "OUTAGE", "cause": "Weather", "etrTime": 1790000000000},
         {"id": 2, "numPeople": 10, "latitude": 29.8, "longitude": -95.3, "type": "PLANNED_OUTAGE"},
         {"id": 3, "numPeople": 5, "latitude": 0, "longitude": 0}],
        {"lat": "latitude", "lon": "longitude", "category": "power", "id_field": "id",
         "title": "{numPeople} customers out — {cause}", "exclude": {"type": "PLANNED_OUTAGE"},
         "severity": {"field": "numPeople", "thresholds": [[5000, "severe"], [1000, "moderate"]], "default": "minor"},
         "metrics": {"customers_out": "numPeople", "etr": "etrTime"}, "time_metrics": ["etr"],
         "constants": {"kind": "outage", "utility": "CenterPoint"}},
    )
    (e,) = events  # planned outage excluded, (0,0) dropped
    assert e.severity == Severity.moderate and e.metrics["kind"] == "outage" and e.metrics["etr"].startswith("2026")


def test_flatten_and_string_numbers():
    assert flatten({"a": {"b": 1, "c": {"d": 2}}, "e": 3}) == {"a_b": 1, "a_c_d": 2, "e": 3}
    m = FieldMapping({"metrics": {"out": "Customers Out", "name": "County"}})
    e = m.to_event({"properties": {"Customers Out": "1,234", "County": "007"}}, 0)
    assert e.metrics == {"out": 1234, "name": "007"}


def test_arcgis_falls_back_to_esri_json():
    import emagg.sources.mapped  # noqa: F401

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["f"])
        if request.url.params["f"] == "geojson":
            return httpx.Response(200, json={"error": {"code": 400, "message": "Invalid or missing input parameters."}})
        return httpx.Response(200, json={"features": [
            {"attributes": {"OBJECTID": 7, "customers": 42, "county": "Lubbock"}, "geometry": {"x": -101.85, "y": 33.58}},
            {"attributes": {"OBJECTID": 8, "customers": 5}, "geometry": {"rings": [[[-101, 33], [-101.1, 33], [-101.1, 33.1], [-101, 33]]]}},
        ]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            cfg = SourceConfig(id="x", type="arcgis", url="https://example.gov/arcgis/rest/services/Outages/MapServer/3",
                               category="power", title="{customers} customers out", constants={"kind": "outage", "utility": "Xcel"},
                               metrics={"customers_out": "customers"})
            return await REGISTRY["arcgis"](cfg, SourceContext(http, AreaConfig(), Store())).fetch()

    events = by_id(asyncio.run(go()))
    assert calls == ["geojson", "json"]
    assert events["7"].geometry == {"type": "Point", "coordinates": [-101.85, 33.58]}
    assert events["7"].metrics == {"kind": "outage", "utility": "Xcel", "customers_out": 42}
    assert events["8"].geometry["type"] == "Polygon"
