import asyncio
from datetime import timedelta

import httpx

from emagg.config import AreaConfig, SourceConfig
from emagg.demo import KUBRA_INSTANCE, KUBRA_OUTAGES, KUBRA_VIEW, DemoTransport
from emagg.geo import encode_polyline
from emagg.models import Category, Severity
from emagg.sources import SourceContext
from emagg.sources.ibi511 import parse_511
from emagg.sources.kubra import KubraOutages, parse_summary, parse_tile
from emagg.sources.mapped import FieldMapping
from emagg.sources.nhc_storms import parse_storms
from emagg.sources.nifc_wildfires import parse_fires
from emagg.sources.nws_alerts import NWSAlerts, parse_alerts
from emagg.sources.nwps_gauges import parse_gauges
from emagg.sources.usgs_earthquakes import parse_quakes
from emagg.sources.waze import parse_waze
from emagg.sources.wzdx import parse_wzdx
from emagg.store import Store


def by_id(events):
    return {e.id: e for e in events}


# --- NWS -------------------------------------------------------------------------------------------


def test_nws_drops_superseded_and_cancelled(fixture):
    events = by_id(parse_alerts(fixture("nws_alerts.json")))
    p = "urn:oid:2.49.0.1.840.0.demo."
    assert set(events) == {p + "hu.1", p + "ss.1", p + "ffw.2", p + "fl.1", p + "tor.1"}
    assert events[p + "ffw.2"].supersedes == [p + "ffw.1"]


def test_nws_normalization(fixture, now):
    events = by_id(parse_alerts(fixture("nws_alerts.json")))
    p = "urn:oid:2.49.0.1.840.0.demo."
    ffw, tor, hu = events[p + "ffw.2"], events[p + "tor.1"], events[p + "hu.1"]
    assert ffw.category == Category.flood and ffw.severity == Severity.severe
    assert tor.category == Category.weather and tor.severity == Severity.extreme
    assert tor.expires_at == now + timedelta(minutes=22)
    assert hu.geometry is None and len(hu.metrics["affected_zones"]) == 2
    assert "Turn around" in ffw.description


def test_nws_exclude_events(fixture):
    events = parse_alerts(fixture("nws_alerts.json"), exclude_events=["tornado warning"])
    assert all(e.title != "Tornado Warning" for e in events)


def test_nws_zone_geometry_is_resolved_and_cached():
    store = Store()

    async def run():
        async with httpx.AsyncClient(transport=DemoTransport()) as http:
            src = NWSAlerts(SourceConfig(id="nws", type="nws_alerts"), SourceContext(http, AreaConfig(states=["TX"]), store))
            return await src.fetch()

    events = by_id(asyncio.run(run()))
    hu = events["urn:oid:2.49.0.1.840.0.demo.hu.1"]
    assert hu.geometry["type"] == "MultiPolygon" and len(hu.geometry["coordinates"]) == 2
    assert "affected_zones" not in hu.metrics
    assert store.kv_get("nwszone:https://api.weather.gov/zones/county/TXC201")["type"] == "Polygon"


# --- NWPS gauges -----------------------------------------------------------------------------------


def test_gauges_filter_to_flooding(fixture):
    events = by_id(parse_gauges(fixture("nwps_gauges.json")))
    assert set(events) == {"BBPT2", "CYPT2", "BRMT2", "GRNT2", "RSHT2", "SJRT2"}
    assert events["BRMT2"].severity == Severity.extreme
    assert events["GRNT2"].severity == Severity.minor  # action stage
    # observed moderate, forecast major -> severity follows the worse of the two
    assert events["BBPT2"].severity == Severity.extreme
    assert events["BBPT2"].title == "Moderate flooding: Buffalo Bayou at Piney Point (forecast major)"
    assert events["SJRT2"].title.startswith("Forecast minor flooding")
    assert events["BRMT2"].metrics["forecast_stage"] is None  # -999 means missing
    assert events["BRMT2"].url == "https://water.noaa.gov/gauges/brmt2"


def test_gauges_options(fixture):
    assert "GRNT2" not in by_id(parse_gauges(fixture("nwps_gauges.json"), min_category="minor"))
    assert "SJRT2" not in by_id(parse_gauges(fixture("nwps_gauges.json"), include_forecast=False))


# --- USGS, NIFC, NHC -------------------------------------------------------------------------------


def test_quakes(fixture):
    events = by_id(parse_quakes(fixture("usgs_earthquakes.json")))
    assert events["demo2026abcd"].severity == Severity.minor
    assert events["demo2026efgh"].severity == Severity.severe  # M5.1
    assert events["demo2026abcd"].geometry == {"type": "Point", "coordinates": [-95.1, 29.75]}
    assert events["demo2026abcd"].metrics["depth_km"] == 5.0
    assert parse_quakes(fixture("usgs_earthquakes.json"), min_magnitude=3) == [events["demo2026efgh"]]


def test_quake_pager_override():
    payload = {"features": [{"id": "x", "properties": {"mag": 4.2, "alert": "orange"}, "geometry": {"coordinates": [0, 0, 1]}}]}
    assert parse_quakes(payload)[0].severity == Severity.extreme


def test_fires(fixture):
    features = fixture("nifc_incidents.json")["features"]
    events = by_id(parse_fires(features))
    assert set(events) == {"D3M0F1RE-0000-0000-0000-000000000001", "D3M0F1RE-0000-0000-0000-000000000002"}
    bear = events["D3M0F1RE-0000-0000-0000-000000000001"]
    assert bear.title == "Bear Creek Fire (250 ac, 60% contained)"
    assert bear.area == "Montgomery County, TX"
    assert bear.severity == Severity.moderate
    assert events["D3M0F1RE-0000-0000-0000-000000000002"].severity == Severity.extreme


def test_storms(fixture):
    (storm,) = parse_storms(fixture("nhc_current_storms.json"))
    assert storm.title == "Hurricane Demo (Cat 3, 121 mph)"
    assert storm.severity == Severity.extreme
    assert storm.geometry["coordinates"] == [-94.7, 29.0]


def test_storm_position_from_text_fields():
    payload = {"activeStorms": [{"id": "ep01", "name": "X", "classification": "TS", "intensity": "45", "latitude": "15.2N", "longitude": "105.1W"}]}
    (storm,) = parse_storms(payload)
    assert storm.geometry["coordinates"] == [-105.1, 15.2]
    assert storm.severity == Severity.moderate


# --- Roads -----------------------------------------------------------------------------------------


def test_waze(fixture):
    events = by_id(parse_waze(fixture("waze_feed.json")))
    assert "demo-waze-006" not in events  # pothole not in default types
    flood = events["demo-waze-001"]
    assert flood.category == Category.flood and flood.severity == Severity.severe
    assert flood.title == "Flooded road: Allen Pkwy"
    assert events["demo-waze-004"].title == "Traffic signal out: Westheimer Rd"
    assert not any(e.startswith("jam-") for e in events)
    assert "jam-9001" in by_id(parse_waze(fixture("waze_feed.json"), include_jams=True))
    assert "demo-waze-001" not in by_id(parse_waze(fixture("waze_feed.json"), min_reliability=8))


def test_wzdx_v4(fixture, now):
    events = by_id(parse_wzdx(fixture("wzdx_feed.json"), now=now))
    assert set(events) == {"demo-wz-1"}  # lane closure excluded, future closure excluded
    wz = events["demo-wz-1"]
    assert wz.title == "Road closed: SH 288 southbound"
    assert wz.geometry["type"] == "LineString"
    assert set(by_id(parse_wzdx(fixture("wzdx_feed.json"), closures_only=False, now=now))) == {"demo-wz-1", "demo-wz-2"}


def test_wzdx_v3_flat(now):
    payload = {"features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-90, 40]},
                             "properties": {"road_event_id": "r1", "road_name": "US 36", "vehicle_impact": "all-lanes-closed",
                                            "start_date": "2026-09-01T00:00:00Z", "end_date": "2026-12-01T00:00:00Z"}}]}
    (e,) = parse_wzdx(payload, now=now)
    assert e.id == "r1" and e.title == "Road closed: US 36"


def test_511(fixture):
    events = by_id(parse_511(fixture("ibi511_events.json")))
    assert set(events) == {"demo-511-1", "demo-511-2"}
    closure = events["demo-511-1"]
    assert closure.severity == Severity.severe and closure.geometry["type"] == "LineString"
    assert closure.title == "Road closed: I-610 West Loop Northbound"
    assert events["demo-511-2"].severity == Severity.moderate
    assert set(by_id(parse_511(fixture("ibi511_events.json"), closures_only=True))) == {"demo-511-1"}


def test_511_case_insensitive_keys():
    (e,) = parse_511({"events": [{"id": 7, "latitude": 40.1, "longitude": -75.2, "eventType": "closures",
                                  "roadwayName": "Main", "isFullClosure": "true"}]})
    assert e.id == "7" and e.severity == Severity.severe


# --- Power -----------------------------------------------------------------------------------------


def test_kubra_summary():
    summary = {"summaryFileData": {"date_generated": "2026-09-27T17:00:00Z",
                                   "totals": [{"total_cust_a": {"val": 54000}, "total_cust_s": 900000, "total_outages": 412}]}}
    e = parse_summary(summary, "Acme Power")
    assert e.title == "Acme Power: 54,000 customers without power (6.0%)"
    assert e.severity == Severity.severe
    assert e.metrics["customers_out"] == 54000 and e.metrics["outages"] == 412
    zero = parse_summary({"summaryFileData": {"totals": [{"total_cust_a": {"val": 0}, "total_cust_s": 900000}]}}, "Acme")
    assert zero.severity == Severity.info


def test_kubra_tile_items():
    data = {"file_data": [
        {"id": "a", "desc": {"cluster": True, "n_out": 3, "cust_a": {"val": 1500}}, "geom": {"p": [encode_polyline([(30.0, -90.0)])]}},
        {"id": "b", "desc": {"cluster": False, "n_out": 1, "cust_a": {"val": 12}, "inc_id": "INC9", "etr": "ETR-NULL",
                             "cause": {"EN-US": "Tree"}}, "geom": {"p": [encode_polyline([(30.1, -90.1)])]}},
    ]}
    cluster, outage = parse_tile(data, "0231", "Acme")
    assert cluster.metrics["kind"] == "outage_cluster" and cluster.severity == Severity.moderate
    assert outage.id == "INC9" and outage.title == "12 customers out — Tree"
    assert outage.metrics["etr"] is None
    assert outage.geometry == {"type": "Point", "coordinates": [-90.1, 30.1]}


def _kubra_fetch(**options):
    store = Store()

    async def run():
        async with httpx.AsyncClient(transport=DemoTransport()) as http:
            cfg = SourceConfig(id="k", type="kubra", name="Acme", instance_id=KUBRA_INSTANCE, view_id=KUBRA_VIEW, **options)
            return await KubraOutages(cfg, SourceContext(http, AreaConfig(), store)).fetch()

    return asyncio.run(run())


def test_kubra_descends_clusters_to_outages():
    events = _kubra_fetch()
    assert events[0].id == "total"
    outages = [e for e in events if e.metrics["kind"] == "outage"]
    assert len(outages) == len(KUBRA_OUTAGES)
    assert sum(e.metrics["customers_out"] for e in outages) == sum(o[2] for o in KUBRA_OUTAGES)


def test_kubra_request_budget_keeps_clusters():
    events = _kubra_fetch(max_tile_requests=5)
    points = [e for e in events if e.id != "total"]
    assert any(e.metrics["kind"] == "outage_cluster" for e in points)
    # Nothing is lost: clusters that could not be expanded still carry their customer counts.
    assert sum(e.metrics["customers_out"] for e in points) == sum(o[2] for o in KUBRA_OUTAGES)


# --- Generic mapped feeds --------------------------------------------------------------------------


def test_field_mapping():
    mapping = FieldMapping({
        "category": "roads",
        "id_field": "OBJECTID",
        "title": "Road closed: {ROAD}",
        "severity": {"field": "STATUS", "map": {"Closed": "severe"}, "default": "minor"},
        "updated_field": "EDITED",
        "metrics": ["DETOUR"],
        "filter": {"ACTIVE": ["Y"]},
    })
    feature = {"geometry": {"type": "Point", "coordinates": [-80, 35]},
               "properties": {"OBJECTID": 4, "ROAD": "Elm St", "STATUS": "Closed", "EDITED": 1790000000000, "DETOUR": "Oak", "ACTIVE": "Y"}}
    e = mapping.to_event(feature, 0)
    assert (e.id, e.title, e.severity, e.category) == ("4", "Road closed: Elm St", Severity.severe, Category.roads)
    assert e.updated_at is not None and e.metrics == {"DETOUR": "Oak"}
    assert mapping.to_event({**feature, "properties": {**feature["properties"], "ACTIVE": "N"}}, 0) is None


def test_field_mapping_thresholds():
    mapping = FieldMapping({"severity": {"field": "OUT", "thresholds": [[100, "moderate"], [1000, "severe"]], "default": "minor"}})
    sev = lambda n: mapping.to_event({"properties": {"OUT": n}}, 0).severity  # noqa: E731
    assert (sev(5), sev(150), sev(5000)) == (Severity.minor, Severity.moderate, Severity.severe)


def test_511_legacy_dates_and_corrupt_polyline():
    from emagg.geo import encode_polyline

    good = encode_polyline([(35.0, -80.0), (35.01, -80.01)])
    bad = encode_polyline([(35.0, -80.0), (35.01, -80.01), (35.02, -215.0)])  # decodes out of range
    items = [
        {"ID": "a", "Latitude": 35.0, "Longitude": -80.0, "EventType": "closures", "RoadwayName": "US-176",
         "LastUpdated": "29/07/2026 11:43:34", "MapEncodedPolyline": bad},
        {"ID": "b", "Latitude": 0, "Longitude": 0, "EventType": "closures"},
        {"ID": "c", "Latitude": 35.0, "Longitude": -80.0, "EventType": "closures", "EncodedPolyline": good,
         "StartDate": -62135596800},
    ]
    events = {e.id: e for e in parse_511(items)}
    assert set(events) == {"a", "c"}  # (0,0) dropped
    assert events["a"].updated_at.isoformat() == "2026-07-29T11:43:34+00:00"
    assert len(events["a"].geometry["coordinates"]) == 2  # stopped before the corrupt vertex
    assert events["c"].starts_at is None  # .NET empty date


def test_wzdx_impact_from_lanes(now):
    from emagg.sources.wzdx import impact_from_lanes

    assert impact_from_lanes([{"type": "general", "status": "closed"}, {"type": "general", "status": "closed"},
                              {"type": "shoulder", "status": "open"}]) == "all-lanes-closed"
    assert impact_from_lanes([{"type": "general", "status": "closed"}]) == "some-lanes-closed"
    assert impact_from_lanes([{"type": "general", "status": "open"}]) is None
    feature = {"type": "Feature", "id": "k1", "geometry": {"type": "LineString", "coordinates": [[-85, 38], [-85.1, 38.1]]},
               "properties": {"core_details": {"event_type": "work-zone", "road_names": ["I-65"]}, "vehicle_impact": "unknown",
                              "start_date": "2026-09-01T00:00:00Z", "end_date": "2026-12-01T00:00:00Z",
                              "lanes": [{"order": 1, "type": "general", "status": "closed"}, {"order": 2, "type": "general", "status": "closed"}]}}
    (e,) = parse_wzdx({"features": [feature]}, now=now)
    assert e.title == "Road closed: I-65" and e.severity == Severity.severe
