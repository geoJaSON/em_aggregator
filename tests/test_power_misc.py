"""PacifiCorp, OSI POP, WEC OutageEventJSON and {"d": ...} outage adapters, and their pending catalog.

Fixtures: misc_pacificorp_* are real recorded files (richardsondev/rmp-outages, 2026-09-27; that repo stores
map+list merged without last_upd, split back here); misc_pop_outages.json is a real Black Hills PopOutage
snapshot (aachokey/denver-outages, 2025-12-16); misc_wec_events.json is a real We Energies OutageEventJSON list
rebuilt from tmoody1973/mke-power-outage-tracker's recorded history (2026-08-05). misc_pop_summary.json and
misc_nwe_outages.json follow the field names read by lukesteve03/OpenSourcePowerOutageScraper and
codebooker/AmericaMap; their values are illustrative (no recorded payload was found).
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

import emagg.sources.dstring_events  # noqa: F401  (registers dstring_events)
import emagg.sources.osi_pop  # noqa: F401  (registers osi_pop)
import emagg.sources.pacificorp  # noqa: F401  (registers pacificorp)
import emagg.sources.wec_outages  # noqa: F401  (registers wec_outages)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.models import Severity
from emagg.scheduler import attribute
from emagg.sources import REGISTRY, SourceContext, SourceError
from emagg.sources import dstring_events as dse
from emagg.sources import osi_pop, pacificorp, wec_outages
from emagg.sources.local_time import parse_any, parse_local, safe_int, zone
from emagg.store import Store
from emagg.summary import utility_customers_out

PENDING = Path(catalog.__file__).parent / "power_misc.yaml"
MOUNTAIN, CENTRAL = zone("America/Denver"), zone("America/Chicago")


def by_id(events):
    return {e.id: e for e in events}


def kinds(events):
    out: dict[str, int] = {}
    for e in events:
        out[e.metrics["kind"]] = out.get(e.metrics["kind"], 0) + 1
    return out


def run(cfg: dict, handler, states=None):
    sc = SourceConfig.model_validate(cfg)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = REGISTRY[sc.type](sc, SourceContext(http, AreaConfig(), Store()))
            assert src.config_error() is None
            assert src.interval >= 300
            return await src.fetch()

    return asyncio.run(go())


def server(files: dict, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        body = files.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, text=body if isinstance(body, str) else json.dumps(body))

    return handler


# --- local times ------------------------------------------------------------------------------------------


def test_parse_local_formats_and_year(now):
    assert parse_local("02:04 PM on 09/27", ["%I:%M %p on %m/%d"], MOUNTAIN, now).isoformat() == "2026-09-27T20:04:00+00:00"
    assert parse_local("Sep 27, 3:56 p.m.", ["%b %d, %I:%M %p"], CENTRAL, now).isoformat() == "2026-09-27T20:56:00+00:00"
    assert parse_local("Jan 5, 12:10 a.m.", ["%b %d, %I:%M %p"], CENTRAL, now).year == 2026  # not 100 days ahead
    assert parse_local("Oct 2, 12:10 a.m.", ["%b %d, %I:%M %p"], CENTRAL, now).year == 2026  # an ETR days ahead
    new_year = datetime(2027, 1, 1, 6, 0, tzinfo=timezone.utc)
    assert parse_local("11:50 PM on 12/31", ["%I:%M %p on %m/%d"], MOUNTAIN, new_year).isoformat() == "2027-01-01T06:50:00+00:00"
    assert parse_local("Dec 30, noon", ["%b %d, %I:%M %p"], CENTRAL, new_year).year == 2026
    assert parse_local("Feb 29, 1:00 a.m.", ["%b %d, %I:%M %p"], CENTRAL, datetime(2028, 3, 1, tzinfo=timezone.utc)).year == 2028
    for bad in (None, "", "Assessing", "Not yet determined", 12, "13:99 PM on 09/27"):
        assert parse_local(bad, ["%I:%M %p on %m/%d"], MOUNTAIN, now) is None


def test_parse_any(now):
    assert parse_any(1790521920000, [], None, now).isoformat() == "2026-09-27T15:12:00+00:00"
    assert parse_any("/Date(1790521920000)/", [], None, now).isoformat() == "2026-09-27T15:12:00+00:00"
    assert parse_any("/Date(1790521920000-0600)/", [], None, now).isoformat() == "2026-09-27T15:12:00+00:00"
    assert parse_any("2026-09-27T09:12:00", [], MOUNTAIN, now).isoformat() == "2026-09-27T15:12:00+00:00"  # naive = local
    assert parse_any("2026-09-27T09:12:00Z", [], MOUNTAIN, now).isoformat() == "2026-09-27T09:12:00+00:00"
    assert parse_any("09/27/2026 3:12:00 PM", ["%m/%d/%Y %I:%M:%S %p"], MOUNTAIN, now).isoformat() == "2026-09-27T21:12:00+00:00"
    assert parse_any(631152000000, [], None, now) is None  # 1990-01-01: InService "no ETR" placeholder
    assert parse_any("garbage", ["%m/%d/%Y"], MOUNTAIN, now) is None and parse_any("", [], None) is None
    assert zone(None, ["UT"]).key == "America/Denver" and zone("Not/AZone", ["OR"]).key == "America/Los_Angeles"
    assert zone(None, []) is None and zone(None, ["XX"]) is None
    # zone-less machine formats as UTC (a raw .NET DateTime); display strings stay local
    assert parse_any("2026-09-27T15:12:00", [], MOUNTAIN, now, naive_utc=True).isoformat() == "2026-09-27T15:12:00+00:00"
    assert parse_any("09/27/2026 9:12 AM", ["%m/%d/%Y %I:%M %p"], MOUNTAIN, now, naive_utc=True).hour == 15
    # Intergraph-style stamps: a zone code is explicit; without one the stamp is naive
    assert parse_any("20260927091200MD", [], None, now).isoformat() == "2026-09-27T15:12:00+00:00"
    assert parse_any("20260927101200CD", [], MOUNTAIN, now).isoformat() == "2026-09-27T15:12:00+00:00"
    assert parse_any("20260927091200", [], MOUNTAIN, now).hour == 15
    assert parse_any("20260927151200", [], MOUNTAIN, now, naive_utc=True).hour == 15
    assert parse_any("20261399999999MD", [], MOUNTAIN, now) is None and parse_any(True, [], None) is None
    assert parse_any(float("nan"), [], MOUNTAIN, now) is None and parse_any(float("inf"), [], MOUNTAIN, now) is None
    assert [safe_int(v) for v in (float("nan"), float("inf"), "NaN", "1,234", 2.6, None, "x")] == [None, None, None, 1234, 3, None, None]


# --- PacifiCorp --------------------------------------------------------------------------------------------


def test_pacificorp_utah_fixture(fixture, now):
    events = pacificorp.parse_pacificorp(
        fixture("misc_pacificorp_mapUT.json"), fixture("misc_pacificorp_listUT.json"), "Rocky Mountain Power (UT)", "UT",
        tz=zone(None, ["UT"]), now=now, link="https://www.rockymountainpower.net/outages-safety.html",
    )
    assert kinds(events) == {"utility_total": 1, "county_outage": 6, "outage": 8}
    assert all(e.metrics["utility"] == "Rocky Mountain Power (UT)" for e in events)
    ev = by_id(events)
    total = ev["total"]
    assert total.metrics["customers_out"] == 27 and total.metrics["outages"] == 8 and total.states == ["UT"]
    assert total.metrics["customers_served"] is None and total.severity == Severity.minor
    salt_lake = ev["county-49035"]
    assert salt_lake.metrics["customers_out"] == 20 and salt_lake.metrics["outages"] == 2 and salt_lake.fips == "49035"
    assert salt_lake.title == "Rocky Mountain Power (UT): 20 out in Salt Lake, UT" and salt_lake.states == ["UT"]
    assert ev["county-49049"].metrics["customers_out"] == 3  # Utah County: 2 planned + 1 unplanned
    big = ev["40.745,-111.863@20260927T2212Z"]  # "04:12 PM on 09/27" MDT
    assert big.title == "19 customers out — Emergency Repair" and big.metrics["crew_status"] == "Crews Notified"
    assert big.starts_at.isoformat() == "2026-09-27T22:12:00+00:00"
    assert big.metrics["etr"] == "2026-09-27T23:30:00+00:00"  # "Before 05:30 PM on 09/27"
    assert big.description == "ZIP 84102" and big.geometry == {"type": "Point", "coordinates": [-111.863, 40.745]}
    assert big.url == "https://www.rockymountainpower.net/outages-safety.html"
    assessing = ev["38.917,-111.933@20260927T1619Z"]
    assert assessing.metrics["etr"] == "Assessing"
    planned = ev["40.423,-111.951@20260927T1726Z"]
    assert planned.metrics["planned"] is True and planned.title.startswith("Planned outage: 2 customers out")
    attribute(big, ["UT"])
    assert big.states == ["UT"] and big.fips == "49035"


def test_pacificorp_wyoming_cluster_and_unplanned_only(fixture, now):
    wy_map, wy_list = fixture("misc_pacificorp_mapWY.json"), fixture("misc_pacificorp_listWY.json")
    events = by_id(pacificorp.parse_pacificorp(wy_map, wy_list, "Rocky Mountain Power (WY)", "WY", tz=MOUNTAIN, now=now))
    cluster = events["41.331,-105.583@20260927T1425Z"]
    assert cluster.metrics["kind"] == "outage_cluster" and cluster.metrics["outages"] == 2
    assert cluster.title == "45 customers out (2 outages)"
    assert events["total"].metrics["customers_out"] == 46 and events["total"].metrics["outages"] == 3
    assert events["county-56001"].metrics["customers_out"] == 45  # Albany

    ut_map, ut_list = fixture("misc_pacificorp_mapUT.json"), fixture("misc_pacificorp_listUT.json")
    unplanned = by_id(pacificorp.parse_pacificorp(ut_map, ut_list, "RMP", "UT", include_planned=False, tz=MOUNTAIN, now=now))
    assert unplanned["total"].metrics["customers_out"] == 24 and unplanned["total"].metrics["outages"] == 6
    assert "county-49011" not in unplanned  # Davis: planned only
    assert unplanned["county-49049"].metrics["customers_out"] == 1
    assert not any(e.metrics.get("planned") for e in unplanned.values())
    # only the map file: planned points are subtracted from the total instead
    map_only = by_id(pacificorp.parse_pacificorp(ut_map, None, "RMP", "UT", include_planned=False, tz=MOUNTAIN, now=now))
    assert map_only["total"].metrics["customers_out"] == 24 and kinds(map_only.values()) == {"utility_total": 1, "outage": 6}
    list_only = pacificorp.parse_pacificorp(None, ut_list, "RMP", "UT", tz=MOUNTAIN, now=now)
    assert kinds(list_only) == {"utility_total": 1, "county_outage": 6}


def test_pacificorp_idaho_quiet_and_bad_records(now):
    quiet = pacificorp.parse_pacificorp({"count": 0, "totalState": 0, "outages": []},
                                        {"count": 0, "totalState": 0, "zips": [], "counties": []}, "RMP (ID)", "ID")
    assert len(quiet) == 1 and quiet[0].metrics["customers_out"] == 0 and quiet[0].severity == Severity.info
    rows = [
        None, "x", {}, {"custOut": 5}, {"custOut": 5, "latitude": "n/a", "longitude": -111.9},
        {"custOut": 0, "latitude": 40.7, "longitude": -111.9}, {"custOut": 5, "latitude": 0, "longitude": 0},
        {"custOut": 5, "latitude": 140.7, "longitude": -111.9},
        {"custOut": "12", "latitude": "40.7", "longitude": "-111.9", "reported": "bad", "etr": None, "outCount": "x"},
        {"custOut": 3, "latitude": 40.7, "longitude": -111.9, "reported": "bad"},  # same place, no time: suffixed id
    ]
    pts = by_id(pacificorp.parse_points({"outages": rows}, "RMP", tz=MOUNTAIN, now=now))
    assert set(pts) == {"40.700,-111.900", "40.700,-111.900#2"}
    assert pts["40.700,-111.900"].metrics["customers_out"] == 12 and pts["40.700,-111.900"].starts_at is None
    assert pacificorp.parse_points([1, 2], "RMP") == [] and pacificorp.parse_points({"outages": "x"}, "RMP") == []
    counties = [None, {"countyName": "Nowhere", "custOutUnplan": 4}, {"countyName": "Salt Lake", "custOutUnplan": "7"},
                {"countyName": "salt lake", "custOutPlan": 2, "outCountPlan": 1}, {"countyName": None, "custOutUnplan": 9}]
    [sl] = pacificorp.parse_counties({"counties": counties}, "RMP", "UT")
    assert sl.metrics["customers_out"] == 9 and sl.metrics["outages"] == 1
    assert pacificorp.parse_counties({"counties": counties}, "RMP", "") == []
    assert pacificorp.parse_pacificorp({"outages": []}, {"counties": []}, "RMP", "UT") == []  # no totalState
    assert pacificorp.loads_lenient('{"totalState": 5}{"totalState": 6}') == {"totalState": 5}
    assert pacificorp.loads_lenient("<html>") is None
    assert pacificorp.loads_lenient('\ufeff{"totalState": 5}') == {"totalState": 5}  # UTF-8 byte-order mark
    total = pacificorp.parse_total({"totalState": 5, "last_upd": "09/27/2026 11:40 AM"}, None, "RMP", "UT", tz=MOUNTAIN, now=now)
    assert total.updated_at.isoformat() == "2026-09-27T17:40:00+00:00"
    # a county listed twice: one event whose title and severity use the sum
    [dup] = pacificorp.parse_counties({"counties": [{"countyName": "Salt Lake", "custOutUnplan": 400},
                                                    {"countyName": "Salt Lake", "custOutUnplan": 700, "outCountUnplan": 2}]},
                                      "RMP", "UT")
    assert dup.metrics["customers_out"] == 1100 and dup.metrics["outages"] == 2 and "1,100 out" in dup.title


def test_pacificorp_nan_never_raises(now):
    nan = float("nan")
    # Python's json accepts NaN / Infinity; a bad number skips that record (or reads as 0), never the whole poll
    map_p = json.loads('{"totalState": NaN, "count": Infinity, "outages": [{"custOut": NaN, "latitude": 40.7, "longitude": -111.9},'
                       ' {"custOut": 4, "outCount": NaN, "latitude": 40.8, "longitude": -111.8, "icon": "planned"}]}')
    list_p = {"totalState": 9, "count": 2, "counties": [{"countyName": "Salt Lake", "custOutUnplan": nan, "custOutPlan": 4},
                                                         {"countyName": "Utah", "custOutUnplan": 5, "outCountUnplan": nan}]}
    ev = by_id(pacificorp.parse_pacificorp(map_p, list_p, "RMP", "UT", tz=MOUNTAIN, now=now))
    assert ev["total"].metrics["customers_out"] == 9 and ev["county-49035"].metrics["customers_out"] == 4
    assert ev["county-49049"].metrics["outages"] == 0 and kinds(ev.values())["outage"] == 1
    # a NaN totalState alone is no total (the fetch reports "no totalState"); nothing raises
    assert pacificorp.parse_pacificorp(map_p, None, "RMP", "UT", include_planned=False, tz=MOUNTAIN, now=now) == []
    head = pacificorp.parse_total({"totalState": 7, "count": float("inf")}, None, "RMP", "UT")
    assert head.metrics["customers_out"] == 7 and head.metrics["outages"] is None


def test_pacificorp_fetch(fixture):
    seen: list = []
    files = {
        "/etc/pcorp/datafiles/outagemap/mapUT.json": fixture("misc_pacificorp_mapUT.json"),
        "/etc/pcorp/datafiles/outagemap/listUT.json": fixture("misc_pacificorp_listUT.json"),
    }
    cfg = {"id": "rmp", "type": "pacificorp", "name": "Rocky Mountain Power (UT)", "states": ["UT"],
           "site": "https://www.rockymountainpower.net/", "state": "ut"}
    events = run(cfg, server(files, seen))
    assert kinds(events) == {"utility_total": 1, "county_outage": 6, "outage": 8}
    assert {r.url.host for r in seen} == {"www.rockymountainpower.net"} and len(seen) == 2
    # concatenated objects in the map file, list file missing: still a total and points
    files["/etc/pcorp/datafiles/outagemap/mapUT.json"] = json.dumps(fixture("misc_pacificorp_mapUT.json")) + '{"x": 1}'
    del files["/etc/pcorp/datafiles/outagemap/listUT.json"]
    assert kinds(run(cfg, server(files))) == {"utility_total": 1, "outage": 8}
    # both gone -> error
    with pytest.raises(SourceError):
        run(cfg, server({}))
    with pytest.raises(SourceError):
        run(cfg, server({"/etc/pcorp/datafiles/outagemap/mapUT.json": "<html>",
                         "/etc/pcorp/datafiles/outagemap/listUT.json": "[]"}))
    only_list = run({**cfg, "outage_points": False}, server({"/etc/pcorp/datafiles/outagemap/listUT.json":
                                                             fixture("misc_pacificorp_listUT.json")}))
    assert kinds(only_list) == {"utility_total": 1, "county_outage": 6}
    # both off: list<ST>.json is still read for the total, but no county outlines are drawn
    seen = []
    total_only = run({**cfg, "outage_points": False, "county_reports": False}, server(
        {"/etc/pcorp/datafiles/outagemap/listUT.json": fixture("misc_pacificorp_listUT.json")}, seen))
    assert kinds(total_only) == {"utility_total": 1} and total_only[0].metrics["customers_out"] == 27
    assert [r.url.path for r in seen] == ["/etc/pcorp/datafiles/outagemap/listUT.json"]
    # a byte-order mark in front of the JSON
    bom = run(cfg, server({"/etc/pcorp/datafiles/outagemap/mapUT.json": "\ufeff" + json.dumps(fixture("misc_pacificorp_mapUT.json")),
                           "/etc/pcorp/datafiles/outagemap/listUT.json": fixture("misc_pacificorp_listUT.json")}))
    assert kinds(bom) == {"utility_total": 1, "county_outage": 6, "outage": 8}
    urls = {"map_url": "https://m.example/m.json", "list_url": "https://m.example/l.json", "county_reports": False}
    seen = []
    run({**cfg, **urls}, server({"/m.json": fixture("misc_pacificorp_mapUT.json")}, seen))
    assert [str(r.url) for r in seen] == ["https://m.example/m.json"]


# --- OSI POP -----------------------------------------------------------------------------------------------


def test_pop_outages_real_records():
    events = by_id(osi_pop.parse_outages(json.loads((Path(catalog.__file__).parent.parent / "demo_data" /
                                                     "misc_pop_outages.json").read_text()), "Black Hills Energy"))
    assert set(events) == {"6941adafa9d2537c6f5c71cc", "6941e01ca9d2537c6f5c884c"}
    ch = events["6941e01ca9d2537c6f5c884c"]
    assert ch.geometry == {"type": "Point", "coordinates": [-104.823244, 41.103275]}
    assert ch.starts_at.isoformat() == "2025-12-16T22:41:27+00:00" and ch.metrics["etr"] == "2025-12-17T00:31:32+00:00"
    assert ch.metrics["crew_status"] == "Predicted" and ch.description == "Area: Cheyenne South"
    assert ch.metrics["customers_out"] == 1 and ch.title == "1 customer out" and ch.metrics["utility"] == "Black Hills Energy"
    attribute(ch, ["CO", "MT", "SD", "WY"])
    assert ch.states == ["WY"] and ch.fips == "56021"
    assert regions.locate(-104.6867147, 38.32580245)[0] == "CO"


def test_pop_restored_pending_calls_is_not_out():
    # the next real snapshot (aachokey/denver-outages commit 21569695, 2025-12-16 23:09Z): the Pueblo outage is
    # "Restored Pending Calls" (power back, the utility is confirming) while Cheyenne is still out
    snapshot = json.loads((Path(catalog.__file__).parent.parent / "demo_data" / "misc_pop_outages.json").read_text())
    snapshot[0]["fieldValues"]["energizationStatus"] = "Restored Pending Calls"
    assert set(by_id(osi_pop.parse_outages(snapshot, "BHE"))) == {"6941e01ca9d2537c6f5c884c"}
    assert [out for _, _, out in osi_pop.active_outages(snapshot)] == [1]
    cfg = {"id": "bhe", "type": "osi_pop", "name": "BHE", "states": ["CO", "WY"], "url": "https://bhe.example"}
    events = by_id(run(cfg, server({"/POP/model/PopOutage": snapshot})))  # no summary: total from active outages
    assert events["total"].metrics["customers_out"] == 1 and events["total"].metrics["outages"] == 1


def test_pop_summary_groups(fixture):
    states = ["CO", "MT", "SD", "WY"]
    events = by_id(osi_pop.parse_summary(fixture("misc_pop_summary.json"), "Black Hills Energy", states, outages=2))
    total = events["total"]
    # County grouping only (Town rows would double-count; Gas rows are another service)
    # the grouping lists a county with no one out (Pennington), so it is the full service area
    assert total.metrics["customers_out"] == 5 and total.metrics["customers_served"] == 141320
    assert total.metrics["outages"] == 2
    assert set(events) == {"total", "county-08101", "county-56021"}  # Custer is in CO, MT and SD: ambiguous
    # an outage point in Custer County, SD settles it
    custer_sd = by_id(osi_pop.parse_summary(fixture("misc_pop_summary.json"), "BHE", states,
                                            points=[(-104.82, 41.10), (-103.60, 43.77)]))
    assert set(custer_sd) == {"total", "county-08101", "county-56021", "county-46033"}
    assert custer_sd["county-46033"].metrics["customers_out"] == 3 and custer_sd["county-46033"].states == ["SD"]
    # points in two of the Custers: still ambiguous
    two = osi_pop.parse_summary(fixture("misc_pop_summary.json"), "BHE", states, points=[(-103.60, 43.77), (-105.37, 38.10)])
    assert "county-46033" not in by_id(two) and "county-08027" not in by_id(two)
    configured = osi_pop.parse_summary(fixture("misc_pop_summary.json"), "BHE", states, customers_served=225000)
    assert configured[0].metrics["customers_served"] == 225000  # the configured figure wins
    pueblo = events["county-08101"]
    assert pueblo.metrics["customers_served"] == 45210 and pueblo.states == ["CO"]
    town = osi_pop.parse_summary(fixture("misc_pop_summary.json"), "BHE", states, summary_field="town")
    assert town[0].metrics["customers_out"] == 2 and len(town) == 3  # total from towns, counties still drawn
    gas = osi_pop.parse_summary(fixture("misc_pop_summary.json"), "BHE", states, service_type="Gas", county_reports=False)
    assert len(gas) == 1 and gas[0].metrics["customers_out"] == 40
    assert osi_pop.parse_summary(fixture("misc_pop_summary.json"), "BHE", states, summary_field="Zip") == []
    one_state = by_id(osi_pop.parse_summary(
        [{"summaryField": "Area", "summaryFieldValue": "Custer, SD", "affectedCount": 4, "totalCount": 900},
         {"summaryField": "Area", "summaryFieldValue": "Lincoln (NE)", "affectedCount": "2", "totalCount": None},
         {"summaryField": "Area", "summaryFieldValue": "Nowhere", "affectedCount": 9}],
        "BHE", states, county_field="area", customers_served=5000))
    assert set(one_state) == {"total", "county-46033", "county-31111"}
    assert one_state["total"].metrics["customers_out"] == 15 and one_state["total"].metrics["customers_served"] == 5000
    assert one_state["county-31111"].metrics["customers_served"] is None
    # only areas with outages listed and nothing configured: no customers-served figure (it would be partial)
    partial = osi_pop.parse_summary([{"summaryField": "County", "summaryFieldValue": "Pueblo", "affectedCount": 500,
                                      "totalCount": 5000}], "BHE", states)
    assert partial[0].metrics["customers_served"] is None and partial[0].metrics["percent_out"] is None
    assert partial[1].metrics["customers_served"] == 5000  # the county's own figure still holds


def test_pop_bad_records_never_raise():
    rows = [None, 5, {}, {"id": "a", "currentAffected": 3}, {"id": "b", "currentAffected": 0, "lat": 40, "lon": -104},
            {"id": "c", "currentAffected": None, "fieldValues": {"currentAffected": "7"}, "lat": "40.1", "lon": "-104.2"},
            {"id": "d", "currentAffected": 2, "lat": None, "lon": None,
             "geoJSONPolygon": {"type": "Polygon", "coordinates": [[[-104, 40], [-104.1, 40], [-104.1, 40.1], [-104, 40]]]}},
            {"id": "e", "currentAffected": 2, "lat": 91, "lon": -104, "geoJSONPolygon": "POLYGON(...)"},
            {"id": "f", "serviceType": "Gas", "currentAffected": 2, "lat": 40, "lon": -104},
            {"id": "c", "currentAffected": 1, "lat": 40, "lon": -104},  # duplicate id
            {"currentAffected": 1, "lat": 40.5, "lon": -104.5, "fieldValues": "x"}]
    rows += [{"id": "g", "currentAffected": 3, "geoJSONPolygon": {"type": "Polygon", "coordinates": [["x"]]}},
             {"id": "h", "currentAffected": 3, "geoJSONPolygon": {"type": "Polygon", "coordinates": [[[-104, "y"], "zz", [[[[[[[[1]]]]]]]]]]}},
             {"id": "i", "currentAffected": float("nan"), "lat": 40, "lon": -104},
             {"id": "j", "currentAffected": 2, "lat": float("nan"), "lon": -104, "geoJSONPolygon":
              {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[-105, 39], [-105.2, 39.2], [float("inf"), 0]]]}}}]
    events = by_id(osi_pop.parse_outages(rows, "BHE"))
    assert set(events) == {"c", "d", "j", "pt-40.5000,-104.5000"}
    assert events["c"].metrics["customers_out"] == 7
    assert events["j"].geometry["coordinates"] == [-105.1, 39.1]
    assert osi_pop.parse_outages({"outages": rows}, "BHE") and osi_pop.parse_outages("x", "BHE") == []
    assert len(osi_pop.parse_outages(rows, "BHE", max_points=1)) == 1
    assert osi_pop.parse_summary("x", "BHE", ["CO"]) == [] and osi_pop.parse_summary([None, 3], "BHE", ["CO"]) == []
    nan_rows = [{"summaryField": "County", "summaryFieldValue": "Pueblo", "affectedCount": float("nan"), "totalCount": float("inf")},
                {"summaryField": "County", "summaryFieldValue": "Laramie", "affectedCount": 2, "totalCount": "NaN"}]
    nan_ev = osi_pop.parse_summary(nan_rows, "BHE", ["CO", "WY"])
    assert nan_ev[0].metrics["customers_out"] == 2 and [e.id for e in nan_ev] == ["total", "county-56021"]


def test_pop_fetch(fixture):
    seen: list = []
    files = {"/POP/model/PopOutageSummary": fixture("misc_pop_summary.json"), "/POP/model/PopOutage": fixture("misc_pop_outages.json")}
    cfg = {"id": "bhe", "type": "osi_pop", "name": "Black Hills Energy", "states": ["CO", "MT", "SD", "WY"],
           "url": "https://www.blackhillsenergy.com/"}
    events = run(cfg, server(files, seen))
    assert kinds(events) == {"utility_total": 1, "county_outage": 2, "outage": 2}
    assert by_id(events)["total"].metrics["outages"] == 2
    assert {str(r.url) for r in seen} == {
        "https://www.blackhillsenergy.com/POP/model/PopOutageSummary?ServiceType=Electric",
        "https://www.blackhillsenergy.com/POP/model/PopOutage?ServiceType=Electric",
    }
    # summary down: total from the outage records
    no_summary = by_id(run(cfg, server({"/POP/model/PopOutage": fixture("misc_pop_outages.json")})))
    assert no_summary["total"].metrics["customers_out"] == 2 and kinds(no_summary.values()) == {"utility_total": 1, "outage": 2}
    empty = run({**cfg, "customers_served": 220000}, server({"/POP/model/PopOutageSummary": [], "/POP/model/PopOutage": []}))
    assert len(empty) == 1 and empty[0].metrics["customers_out"] == 0 and empty[0].metrics["customers_served"] == 220000
    with pytest.raises(SourceError):
        run(cfg, server({}))
    with pytest.raises(SourceError):
        run({**cfg, "outage_points": False}, server({"/POP/model/PopOutage": fixture("misc_pop_outages.json")}))
    with pytest.raises(SourceError):
        run(cfg, server({"/POP/model/PopOutageSummary": [], "/POP/model/PopOutage": "5"}))
    # JSON error objects are errors, not "no outages"
    err = {"error": "Service unavailable"}
    with pytest.raises(SourceError):
        run(cfg, server({"/POP/model/PopOutageSummary": err, "/POP/model/PopOutage": err}))
    with pytest.raises(SourceError):
        run({**cfg, "outage_points": False}, server({"/POP/model/PopOutageSummary": err}))
    with pytest.raises(SourceError):
        run(cfg, server({"/POP/model/PopOutageSummary": fixture("misc_pop_summary.json"), "/POP/model/PopOutage": err}))
    # an error object for the summary alone: total from the outage records
    no_sum = run(cfg, server({"/POP/model/PopOutageSummary": err, "/POP/model/PopOutage": fixture("misc_pop_outages.json")}))
    assert no_sum[0].metrics["customers_out"] == 2 and kinds(no_sum) == {"utility_total": 1, "outage": 2}
    wrapped = run(cfg, server({"/POP/model/PopOutageSummary": {"data": fixture("misc_pop_summary.json")},
                               "/POP/model/PopOutage": {"outages": fixture("misc_pop_outages.json")}}))
    assert kinds(wrapped) == {"utility_total": 1, "county_outage": 2, "outage": 2}
    seen = []
    summary_only = run({**cfg, "outage_points": False, "service_type": "Gas"},
                       server({"/POP/model/PopOutageSummary": fixture("misc_pop_summary.json")}, seen))
    assert len(seen) == 1 and seen[0].url.params["ServiceType"] == "Gas" and summary_only[0].metrics["customers_out"] == 40


# --- WEC OutageEventJSON -------------------------------------------------------------------------------------


def test_wec_fixture(fixture, now):
    events = wec_outages.parse_wec(fixture("misc_wec_events.json"), "We Energies", ["WI", "MI"],
                                   customers_served=1260000, tz=CENTRAL, now=now)
    assert kinds(events) == {"utility_total": 1, "county_outage": 6, "outage": 8}
    assert all(e.metrics["utility"] == "We Energies" for e in events)
    ev = by_id(events)
    total = ev["total"]
    assert total.metrics["customers_out"] == 1084 and total.metrics["outages"] == 8
    assert total.metrics["customers_served"] == 1260000 and total.metrics["percent_out"] == pytest.approx(0.086, abs=0.001)
    assert total.updated_at.isoformat() == "2026-08-05T20:50:00+00:00"  # "Aug 5, 3:50 p.m." CDT
    waukesha = ev["county-55133"]
    assert waukesha.metrics["customers_out"] == 1071 and waukesha.metrics["customers_served"] == 203383
    assert waukesha.metrics["outages"] == 1 and waukesha.severity == Severity.minor
    assert ev["county-55101"].metrics["customers_out"] == 3 and ev["county-55101"].metrics["outages"] == 3  # Racine
    vilas = ev["county-55125"]  # region "upper peninsula mi", but Vilas County is in Wisconsin
    assert vilas.states == ["WI"] and vilas.metrics["customers_out"] == 2
    big = ev["43.0078,-88.3832@20260805T1944Z"]
    assert big.metrics["customers_out"] == 1072 and big.severity == Severity.moderate
    assert big.description == "Dousman, Oconomowoc, Waukesha, Racine and 2 more"
    assert big.metrics["etr"] == "2026-08-05T23:00:00+00:00" and big.metrics["crew_status"] == "Assigned"
    assert big.starts_at.isoformat() == "2026-08-05T19:44:00+00:00"
    eagle = ev["45.9626,-89.1708@20260805T1602Z"]
    assert eagle.metrics["etr"].startswith("Reassessing – Outage may require multiple crews")
    assert eagle.description == "Eagle River"


def test_wec_county_state_resolution():
    iron_mi = {"AffectedCusts": 5, "County": "iron", "CountyCusts": 4000, "Region": "upper peninsula mi"}
    iron_wi = {"AffectedCusts": 5, "County": "iron", "CountyCusts": 3000, "Region": "northern wi"}
    iron_none = {"AffectedCusts": 5, "County": "iron", "Region": "north"}
    assert wec_outages.slice_county(iron_mi, ["WI", "MI"])["fips"] == "26071"
    assert wec_outages.slice_county(iron_wi, ["WI", "MI"])["fips"] == "55051"
    assert wec_outages.slice_county(iron_none, ["WI", "MI"]) is None
    assert wec_outages.slice_county(iron_none, ["WI", "MI"], (-88.64, 46.09))["fips"] == "26071"  # Iron River, MI
    assert wec_outages.slice_county(iron_mi, [])["fips"] == "26071"
    assert wec_outages.slice_county({"County": None}, ["WI"]) is None
    # the feed's spelling of Manitowoc (94 slices in the mke tracker's history)
    manitowic = {"AffectedCusts": 3, "County": "manitowic", "CountyCusts": 41000, "Region": "fox valley wi"}
    assert wec_outages.slice_county(manitowic, ["WI", "MI"])["fips"] == "55071"
    # an unknown name is placed by the outage's position only when it is the outage's one county
    typo = {"AffectedCusts": 3, "County": "sheboygen", "Region": "southeast wi"}
    sheboygan = (-87.75, 43.75)
    assert wec_outages.slice_county(typo, ["WI", "MI"], sheboygan) is None
    assert wec_outages.slice_county(typo, ["WI", "MI"], sheboygan, sole_county=True)["fips"] == "55117"
    assert wec_outages.slice_county(typo, ["MI"], sheboygan, sole_county=True) is None  # not in the utility's states
    rec = {"Latitude": 43.75, "Longitude": -87.75, "Slices": [typo, {**typo, "AffectedCusts": 2}]}
    [county] = wec_outages.parse_counties([rec], "WE", ["WI"])
    assert county.fips == "55117" and county.metrics["customers_out"] == 5
    mixed = {"Latitude": 43.75, "Longitude": -87.75, "Slices": [typo, {"AffectedCusts": 2, "County": "manitowic"}]}
    assert [e.fips for e in wec_outages.parse_counties([mixed], "WE", ["WI"])] == ["55071"]  # typo dropped


def test_wec_bad_records_never_raise(now):
    rows = [None, 3, {}, {"Slices": "x"}, {"Latitude": 43, "Longitude": -88, "Slices": [None, {"AffectedCusts": "n/a"}]},
            {"Latitude": "x", "Longitude": -88, "Slices": [{"AffectedCusts": 4, "County": "milwaukee"}]},
            {"Latitude": 43.1, "Longitude": -88.1, "OffTime": "Sep 27, 1:00 p.m.", "ETR": None, "Slices": [{"AffectedCusts": 2}]},
            {"Latitude": 43.1, "Longitude": -88.1, "OffTime": "Sep 27, 1:00 p.m.", "Slices": [{"AffectedCusts": 1, "City": "x"}]},
            {"Latitude": 43.1, "Longitude": -88.1, "OffTime": "garbage", "Slices": [{"AffectedCusts": 1}]}]
    events = wec_outages.parse_wec(rows, "WE", ["WI"], tz=CENTRAL, now=now)
    ev = by_id(events)
    assert ev["total"].metrics["customers_out"] == 8 and ev["total"].metrics["outages"] == 7
    assert set(ev) == {"total", "county-55079", "43.1000,-88.1000@20260927T1800Z", "43.1000,-88.1000@20260927T1800Z#2",
                       "43.1000,-88.1000"}
    assert ev["county-55079"].metrics["customers_served"] is None  # no CountyCusts
    assert wec_outages.parse_wec("x", "WE", ["WI"])[0].metrics["customers_out"] == 0
    nan_rows = json.loads('[{"Latitude": 43.1, "Longitude": -88.1, "Slices": [{"AffectedCusts": NaN, "County": "milwaukee"},'
                          ' {"AffectedCusts": 4, "County": "milwaukee", "CountyCusts": Infinity}]}, {"Latitude": NaN, "Longitude": -88,'
                          ' "Slices": [{"AffectedCusts": 2}]}]')
    nan_ev = by_id(wec_outages.parse_wec(nan_rows, "WE", ["WI"], tz=CENTRAL, now=now))
    assert nan_ev["total"].metrics["customers_out"] == 6 and nan_ev["county-55079"].metrics["customers_out"] == 4
    assert nan_ev["county-55079"].metrics["customers_served"] is None and kinds(nan_ev.values())["outage"] == 1
    assert len(wec_outages.parse_points(rows, "WE", tz=CENTRAL, now=now, max_points=1)) == 1


def test_wec_fetch(fixture):
    seen: list = []
    cfg = {"id": "we", "type": "wec_outages", "name": "We Energies", "states": ["WI", "MI"],
           "url": "https://www.we-energies.com/outagesummary/view/OutageEventJSON", "customers_served": "1,100,000",
           "county_reports": False}
    events = run(cfg, server({"/outagesummary/view/OutageEventJSON": fixture("misc_wec_events.json")}, seen))
    assert kinds(events) == {"utility_total": 1, "outage": 8} and len(seen) == 1
    assert events[0].metrics["customers_served"] == 1100000
    with pytest.raises(SourceError):
        run(cfg, server({"/outagesummary/view/OutageEventJSON": {"error": "x"}}))
    with pytest.raises(SourceError):
        run(cfg, server({"/outagesummary/view/OutageEventJSON": "<html>"}))
    assert len(run(cfg, server({"/outagesummary/view/OutageEventJSON": []}))) == 1


# --- {"d": ...} rows (NorthWestern Energy) ------------------------------------------------------------------


def test_dstring_fixture(fixture, now):
    events = dse.parse_rows(fixture("misc_nwe_outages.json"), "NorthWestern Energy", tz=zone(None, ["MT"]), now=now,
                            link="https://www.northwesternenergy.com/outages/outage-map")
    ev = by_id(events)
    assert set(ev) == {"total", "2026092700041", "2026092700057", "2026092700063"}  # ARCHIVED row dropped
    total = ev["total"]
    assert total.metrics["customers_out"] == 219 and total.metrics["outages"] == 3 and total.states == []
    butte = ev["2026092700041"]
    assert butte.title == "212 customers out — Wind" and butte.metrics["crew_status"] == "Assigned"
    assert butte.starts_at.isoformat() == "2026-09-27T15:12:00+00:00" and butte.metrics["etr"] == "2026-09-27T20:00:00+00:00"
    assert butte.description == "Area: Butte" and butte.url == "https://www.northwesternenergy.com/outages/outage-map"
    aberdeen = ev["2026092700057"]
    assert aberdeen.metrics["etr"] is None and aberdeen.metrics["cause"] is None  # 1990-01-01 placeholder ETR
    attribute(aberdeen, ["MT", "SD", "WY"])
    assert aberdeen.states == ["SD"]
    attribute(total, ["MT", "SD", "WY"])
    assert total.states == ["MT", "SD", "WY"]
    # a multi-state total never counts for one state; the points do
    for e in events:
        attribute(e, ["MT", "SD", "WY"])
    rows = [e.model_dump() for e in events]
    assert utility_customers_out([r for r in rows if "SD" in r["states"]], "SD") == {"NorthWestern Energy": 6}
    assert utility_customers_out(rows) == {"NorthWestern Energy": 219}


def test_dstring_shapes_and_fields(now):
    rows = [{"ID": 7, "CUST": "30", "LON": -12530000.0, "LAT": 5780000.0, "STAT": "open", "OFF": "09/27/2026 9:30 AM"},
            {"ID": 8, "CUST": 4, "LON": None, "LAT": None, "STAT": "OPEN"},
            {"ID": 9, "CUST": 4, "LON": 1e9, "LAT": 1e9, "STAT": "closed"},
            {"ID": 10, "CUST": 2, "LON": -112.5, "LAT": 46.0, "STAT": "Closed"}]
    fields = {"customers": "CUST", "lon": "LON", "lat": "LAT", "id": "ID", "status": "STAT", "started": ["OFF"]}
    for payload in ({"d": json.dumps(rows)}, {"d": rows}, rows, {"d": json.dumps({"outages": rows})}):
        ev = by_id(dse.parse_rows(payload, "U", fields=fields, status_exclude=["closed"], tz=MOUNTAIN, now=now))
        assert set(ev) == {"total", "7"}
        assert ev["total"].metrics["customers_out"] == 34 and ev["total"].metrics["outages"] == 2
        lon, lat = ev["7"].geometry["coordinates"]
        assert lon == pytest.approx(-112.5589, abs=1e-4) and lat == pytest.approx(45.9978, abs=1e-4)  # Web Mercator, Butte
        assert ev["7"].starts_at.isoformat() == "2026-09-27T15:30:00+00:00"
    for bad in ({"d": "not json"}, {"d": 5}, "x", None, {"x": []}, {"d": [None, 1, {"NUM_CUST": "x"}]}):
        [total] = dse.parse_rows(bad, "U")
        assert total.metrics["customers_out"] == 0
    etrs = [{"EVENTID": "a", "NUM_CUST": 1, "XCOORD": -112.5, "YCOORD": 46.0, "OFF_DTS": "09/27/2026 9:30 AM",
             "EST_REP_TIME": "JAN-01 12:00 AM"},
            {"EVENTID": "b", "NUM_CUST": 1, "XCOORD": -112.5, "YCOORD": 46.0, "OFF_DTS": "bad", "LOCAL_OFF_DTS": "SEP-27 9:30 AM",
             "EST_REP_TIME": "Assessing"},
            {"EVENTID": "c", "NUM_CUST": 1, "XCOORD": -112.5, "YCOORD": 46.0, "EST_REP_TIME": "/Date(1790539200000)/"},
            {"EVENTID": "d", "NUM_CUST": 1, "XCOORD": -112.5, "YCOORD": 46.0, "EST_REP_TIME": "/Date(631152000000)/"}]
    ev = by_id(dse.parse_rows({"d": json.dumps(etrs)}, "U", tz=MOUNTAIN, now=now))
    assert ev["a"].metrics["etr"] is None  # before the start: placeholder
    assert ev["b"].starts_at.isoformat() == "2026-09-27T15:30:00+00:00" and ev["b"].metrics["etr"] == "Assessing"
    assert ev["c"].metrics["etr"] == "2026-09-27T20:00:00+00:00" and ev["c"].starts_at is None
    assert ev["d"].metrics["etr"] is None
    only_total = dse.parse_rows({"d": rows}, "U", fields=fields, outage_points=False, status_exclude=[])
    assert len(only_total) == 1 and only_total[0].metrics["customers_out"] == 40
    capped = dse.parse_rows({"d": rows}, "U", fields=fields, status_exclude=[], max_points=1)
    assert [e.id for e in capped] == ["total", "7"]


def test_dstring_local_columns_first_and_raw_iso_is_utc(now):
    pt = {"NUM_CUST": 5, "XCOORD": -112.5, "YCOORD": 46.0}
    rows = [
        # both columns: LOCAL_* first, as codebooker/AmericaMap reads them
        {**pt, "EVENTID": "a", "LOCAL_OFF_DTS": "SEP-27 9:12 AM", "OFF_DTS": "2026-09-27T15:12:00",
         "LOCAL_ERT": "SEP-27 2:00 PM", "EST_REP_TIME": "2026-09-27T20:00:00"},
        # raw columns only: a zone-less ISO time is UTC (what .NET writes for a raw DateTime)
        {**pt, "EVENTID": "b", "OFF_DTS": "2026-09-27T15:12:00", "EST_REP_TIME": "2026-09-27T20:00:00"},
        # LOCAL_* unreadable: the raw column
        {**pt, "EVENTID": "c", "LOCAL_OFF_DTS": "n/a", "OFF_DTS": "2026-09-27T15:12:00Z", "LOCAL_ERT": "Assessing"},
        # a start more than an hour ahead is wrong: the next column (an Intergraph stamp with its zone code)
        {**pt, "EVENTID": "d", "LOCAL_OFF_DTS": "SEP-27 3:00 PM", "OFF_DTS": "20260927091200MD"},
        {**pt, "EVENTID": "e", "OFF_DTS": "2026-09-27T09:12:00"},
    ]
    ev = by_id(dse.parse_rows({"d": json.dumps(rows)}, "NWE", tz=MOUNTAIN, now=now))
    for oid in "abcd":
        assert ev[oid].starts_at.isoformat() == "2026-09-27T15:12:00+00:00", oid
    assert ev["a"].metrics["etr"] == ev["b"].metrics["etr"] == "2026-09-27T20:00:00+00:00"
    assert ev["c"].metrics["etr"] == "Assessing"
    assert ev["e"].starts_at.isoformat() == "2026-09-27T09:12:00+00:00"
    local = by_id(dse.parse_rows({"d": rows}, "NWE", tz=MOUNTAIN, now=now, iso_local=True))
    assert local["e"].starts_at.isoformat() == "2026-09-27T15:12:00+00:00"  # iso_local: true
    assert local["b"].starts_at is None  # 15:12 MDT would be in the future


def test_dstring_coordinates_duplicates_and_bad_numbers(now):
    rows = [
        {"EVENTID": "swapped", "NUM_CUST": 3, "XCOORD": 46.0, "YCOORD": -112.5},
        {"EVENTID": "stateplane", "NUM_CUST": 4, "XCOORD": 400000.0, "YCOORD": 250000.0},  # Montana State Plane m
        {"EVENTID": "mixed", "NUM_CUST": 1, "XCOORD": -12530000.0, "YCOORD": 46.0},
        {"EVENTID": "guam", "NUM_CUST": 2, "XCOORD": 144.8, "YCOORD": 13.4},
        {"EVENTID": "dup", "NUM_CUST": 3, "XCOORD": None, "YCOORD": None, "EVENT_STATUS": "ASSIGNED"},
        {"EVENTID": "dup", "NUM_CUST": 4, "XCOORD": -111.04, "YCOORD": 45.68, "EVENT_STATUS": "ASSIGNED"},
        {"EVENTID": "nan", "NUM_CUST": float("nan"), "XCOORD": -112.5, "YCOORD": 46.0},
        {"EVENTID": "inf", "NUM_CUST": 2, "XCOORD": float("inf"), "YCOORD": 46.0},
        {"NUM_CUST": 1, "XCOORD": -112.0, "YCOORD": 46.5}, {"NUM_CUST": 2, "XCOORD": -112.0, "YCOORD": 46.5},
        {"EVENTID": "old", "NUM_CUST": 9, "XCOORD": -112.0, "YCOORD": 46.5, "EVENT_STATUS": "archived"},
    ]
    ev = by_id(dse.parse_rows({"d": rows}, "NWE", tz=MOUNTAIN, now=now, status_exclude="ARCHIVED"))
    assert ev["swapped"].geometry["coordinates"] == [-112.5, 46.0]
    assert not {"stateplane", "mixed", "guam", "nan", "inf", "old"} & set(ev)  # not drawn (no null-island points)
    assert ev["dup"].metrics["customers_out"] == 7 and ev["dup"].metrics["rows"] == 2 and ev["dup"].title == "7 customers out"
    assert set(ev) == {"total", "swapped", "dup", "pt-46.5000,-112.0000", "pt-46.5000,-112.0000#2"}
    total = ev["total"]
    # every kept row counts, drawn or not: swapped 3, stateplane 4, mixed 1, guam 2, dup 3+4, inf 2, two id-less 1+2
    assert total.metrics["customers_out"] == 22 and total.metrics["outages"] == 8
    guam = by_id(dse.parse_rows({"d": rows}, "NWE", now=now, bbox=[140, 10, 150, 20]))
    assert "guam" in guam and "swapped" not in guam and "old" not in guam  # default status_exclude: ARCHIVED
    assert dse._lonlat(-12530000.0, 5780000.0) == pytest.approx((-112.5589, 45.9978), abs=1e-4)  # Web Mercator: Butte
    assert dse._bbox("x") == dse._bbox([1, 2, 3]) == dse._bbox([5, 0, 1, 1]) == dse.US_BBOX
    assert dse._excluded(5) == set() and dse._excluded([]) == set()


def test_dstring_fetch(fixture):
    cfg = {"id": "nwe", "type": "dstring_events", "name": "NorthWestern Energy", "states": ["MT", "SD", "WY"],
           "url": "https://www.northwesternenergy.com/get-outage-map-data", "customers_served": 380000}
    events = run(cfg, server({"/get-outage-map-data": fixture("misc_nwe_outages.json")}))
    assert kinds(events) == {"utility_total": 1, "outage": 3}
    assert events[0].metrics["customers_served"] == 380000
    assert len(run(cfg, server({"/get-outage-map-data": {"d": "[]"}}))) == 1
    asp_error = {"d": json.dumps({"Message": "There was an error processing the request.", "StackTrace": "",
                                  "ExceptionType": ""})}
    for bad in ({"d": "<html>"}, {"x": 1}, {"d": 5}, asp_error, {"d": {"Message": "x"}}, {"d": "{}"}):
        with pytest.raises(SourceError):
            run(cfg, server({"/get-outage-map-data": bad}))
    with pytest.raises(SourceError, match="error processing"):
        run(cfg, server({"/get-outage-map-data": asp_error}))
    assert len(run(cfg, server({"/get-outage-map-data": {"d": json.dumps({"outages": []})}}))) == 1
    kept = run({**cfg, "status_exclude": "UNASSIGNED"}, server({"/get-outage-map-data": fixture("misc_nwe_outages.json")}))
    assert {e.id for e in kept} == {"total", "2026092700041", "2026092700063", "2026092600991"}  # a string option
    with pytest.raises(SourceError):
        run(cfg, server({}))


# --- pending catalog ------------------------------------------------------------------------------------------


def pending_entries():
    return yaml.safe_load(PENDING.read_text())


def test_pending_catalog_entries_are_valid():
    entries = pending_entries()
    assert len(entries) == 12
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "power_misc.yaml"}
    for other in PENDING.parent.glob("*.yaml"):
        if other != PENDING:
            existing |= {e["id"] for e in (yaml.safe_load(other.read_text()) or [])}
    prefixes = {"pacificorp": "pacificorp_", "osi_pop": "osi_pop_", "wec_outages": "wec_", "dstring_events": "dstring_"}
    valid_states = regions.state_codes()
    seen = set()
    for e in entries:
        sid = e["id"]
        assert sid not in seen, f"duplicate id {sid}"
        seen.add(sid)
        assert sid not in existing, f"{sid} collides with an existing catalog id"
        assert e["type"] in REGISTRY and sid.startswith(prefixes[e["type"]]), sid
        sc = SourceConfig.model_validate(_interpolate(e))
        assert sc.states and all(len(st) == 2 and st.isupper() and st in valid_states for st in sc.states), sid
        assert sc.meta["confidence"] in ("high", "medium", "low") and sc.meta["evidence"], sid
        if sc.meta["confidence"] == "high":
            assert sc.meta["evidence"].count("github.com/") >= 2, sid
        src = REGISTRY[sc.type](sc, SourceContext(None, AreaConfig(), Store()))
        assert src.config_error() is None and src.interval >= 300, sid
        for key in ("url", "site", "link"):
            if key in sc.options:
                assert str(sc.options[key]).startswith("https://"), sid
        if sc.type == "pacificorp":
            assert sc.states == [sc.options["state"]] and sc.name.endswith(f"({sc.options['state']})")
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names))  # one display name per utility feed (metrics.utility)
