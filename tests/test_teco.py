"""Tampa Electric (TECO) outage-tiles adapter: request body, recorded payloads, outlines, times, fetch, catalog."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

import emagg.sources.teco  # noqa: F401  (registers the adapter)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.geo import encode_polyline
from emagg.models import Category, Severity
from emagg.sources import REGISTRY, SourceContext
from emagg.sources.base import SourceError
from emagg.sources.teco import (
    FL_BBOX,
    center_of,
    local_time,
    outline,
    parse_outage_tiles,
    request_body,
    zone,
)
from emagg.store import Store

PENDING = Path(catalog.CATALOG_DIR) / "power_teco.yaml"
BASE = "https://outage-data-prod-hrcadje2h9aje9c9.a03.azurefd.net"
EASTERN = zone()


def by_id(events):
    return {e.id: e for e in events}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# --- request -----------------------------------------------------------------------------------------------------


def test_request_body_is_the_collectors_search():
    """Byte-for-byte the body codebooker/floridamap and codebooker/AmericaMap proxy.py POST (FL_BOUNDS)."""
    assert request_body() == {
        "size": 10000,
        "query": {
            "bool": {
                "must": {"match_all": {}},
                "filter": {
                    "geo_bounding_box": {
                        "polygonCenter": {
                            "top_left": {"lat": 31.1, "lon": -87.7},
                            "bottom_right": {"lat": 24.4, "lon": -79.9},
                        }
                    }
                },
            }
        },
        "sort": [{"updateTime": "asc"}, {"incidentId": "asc"}],
        "_source": [
            "updateTime", "status", "reason", "customerCount", "polygonCenter", "incidentId",
            "polygonPointsGoogle", "estimatedTimeOfRestoration",
        ],
    }
    assert FL_BBOX == (-87.7, 24.4, -79.9, 31.1)
    tampa = request_body((-84.70102730976562, 27.003667078761065, -79.99613229023437, 28.70343307240943), 50)
    box = tampa["query"]["bool"]["filter"]["geo_bounding_box"]["polygonCenter"]
    assert tampa["size"] == 50 and box["top_left"] == {"lat": 28.70343307240943, "lon": -84.70102730976562}


# --- recorded payloads -------------------------------------------------------------------------------------------


def test_recorded_2025_payload(fixture):
    """Complete response recorded 2025-07-01 23:27 UTC (simonw/scrape-florida-outages), no aggregation block."""
    events = parse_outage_tiles(fixture("teco_outage_tiles_20250701.json"))
    ev = by_id(events)
    assert list(ev) == ["total", "A202518238976", "A202518231109"]  # total first, then largest outage first
    assert {e.metrics["utility"] for e in events} == {"Tampa Electric"}
    assert all(e.category == Category.power for e in events)

    total = ev["total"]
    assert total.metrics["customers_out"] == 15 and total.metrics["outages"] == 2  # sum of records, hits.total
    assert total.states == ["FL"] and total.geometry is None and total.severity == Severity.minor
    # updateTime "2025-07-01T19:25:08" is Eastern (EDT): 23:25 UTC, two minutes before the capture.
    assert total.updated_at == utc(2025, 7, 1, 23, 25, 8)

    onsite = ev["A202518231109"]
    assert onsite.metrics["customers_out"] == 5 and onsite.metrics["kind"] == "outage"
    assert onsite.metrics["cause"] == "Under investigation"
    assert onsite.metrics["crew_status"] == "We're working onsite"
    assert onsite.metrics["etr"] == "2025-07-02T03:20:00+00:00"  # 23:20 EDT
    assert onsite.geometry == {"type": "Point", "coordinates": [-82.239146, 27.964647]}  # polygonCenter is [lon, lat]
    assert onsite.url == "https://outage.tecoenergy.com/" and onsite.severity == Severity.minor
    for e in events[1:]:
        assert regions.locate(*e.geometry["coordinates"])[1]["fips"] == "12057"  # Hillsborough County


def test_recorded_milton_payload_merges_repeated_incidents(fixture):
    """Hurricane Milton capture (2024-10-11 12:52 UTC), trimmed to 17 records; two incidents appear twice."""
    payload = fixture("teco_outage_tiles_20241011.json")
    assert len(payload["hits"]["hits"]) == 17
    events = parse_outage_tiles(payload, customers_served=849876)
    ev = by_id(events)
    assert len(events) == 16  # total + 15 distinct incidents

    total = ev["total"]
    assert total.metrics["customers_out"] == 21673 and total.metrics["outages"] == 17
    assert total.metrics["customers_served"] == 849876 and total.severity == Severity.moderate  # 2.55 %
    assert total.updated_at == utc(2024, 10, 11, 12, 50, 24)  # 08:50:24 EDT

    # A202428343144: two records (849 + 1,321) at the same centre -> one event; status/reason of the larger.
    merged = ev["A202428343144"]
    assert merged.metrics["customers_out"] == 2170 and merged.severity == Severity.moderate
    assert merged.metrics["crew_status"] == "We're on our way to investigate"
    other = ev["A202428352418"]  # 708 ("on our way") + 183 ("aware")
    assert other.metrics["customers_out"] == 891 and other.metrics["crew_status"] == "We're on our way to investigate"
    assert sum(e.metrics["customers_out"] for e in events[1:]) == total.metrics["customers_out"]

    assert ev["A202428519911"].metrics["etr"] == "2024-10-11T13:50:00+00:00"
    assert ev["A202428301846"].metrics["etr"] is None and ev["A202428301846"].metrics["customers_out"] == 4514
    assert ev["A202428300032"].metrics["cause"] == "Weather-related damage to equipment"
    assert regions.locate(*ev["A202428300032"].geometry["coordinates"])[1]["fips"] == "12105"  # Polk County

    # Without a served count the total falls back to count thresholds.
    assert by_id(parse_outage_tiles(payload))["total"].severity == Severity.severe
    # Stable ids across polls.
    assert set(by_id(parse_outage_tiles(payload, customers_served=849876))) == set(ev)


def test_current_endpoint_shape(fixture, now):
    """outage-tiles adds aggregations.customerCountSum and _tiles, and polygonPointsGoogle outlines."""
    payload = fixture("teco_outage_tiles.json")
    events = parse_outage_tiles(payload, "Tampa Electric", customers_served=849876)
    ev = by_id(events)
    total = ev["total"]
    assert total.metrics["customers_out"] == 14158 and total.metrics["outages"] == 11
    assert total.updated_at == now - timedelta(minutes=12)

    big = ev["A202428301846"]
    assert big.geometry["type"] == "Polygon" and len(big.geometry["coordinates"][0]) == 10  # 9 points, closed
    ring = big.geometry["coordinates"][0]
    assert ring[0] == ring[-1]
    assert all(abs(x + 82.4697) < 0.02 and abs(y - 28.0603) < 0.02 for x, y in ring)
    assert big.states == ["FL"] and big.fips == "12057"  # tagged from the centre (outlines are not points)
    assert big.metrics["etr"] == (now + timedelta(hours=9)).isoformat()
    assert big.updated_at == now - timedelta(minutes=12)

    merged = ev["A202428343144"]  # two records, two outlines
    assert merged.geometry["type"] == "MultiPolygon" and len(merged.geometry["coordinates"]) == 2
    assert merged.metrics["customers_out"] == 2170

    no_outline = ev["A202428487702"]  # polygonPointsGoogle: null -> the centre
    assert no_outline.geometry == {"type": "Point", "coordinates": [-82.57509, 28.15105]}
    assert no_outline.metrics["crew_status"] == "Additional support enroute"

    points_only = parse_outage_tiles(payload, polygons=False)
    assert all(e.geometry["type"] == "Point" for e in points_only[1:])
    assert [e.id for e in parse_outage_tiles(payload, outage_points=False)] == ["total"]
    assert [e.id for e in parse_outage_tiles(payload, max_points=2)] == ["total", "A202428301846", "A202428418364"]


def test_aggregation_counts_records_beyond_size():
    """customerCountSum covers every match, so a truncated hit list still gives the right total."""
    payload = {
        "hits": {"total": {"value": 12000, "relation": "eq"},
                 "hits": [{"_source": {"incidentId": "A1", "polygonCenter": [-82.46, 27.95], "customerCount": 7}}]},
        "aggregations": {"customerCountSum": {"value": 612345.0}},
    }
    total = by_id(parse_outage_tiles(payload))["total"]
    assert total.metrics["customers_out"] == 612345 and total.metrics["outages"] == 12000
    assert total.severity == Severity.extreme


def test_bad_records_are_skipped_not_raised():
    payload = {
        "hits": {
            "hits": [
                None, "x", 3, {"_source": None}, {"_source": "text"},
                {"_source": {"incidentId": "NOLOC", "customerCount": 40}},  # counted, not mapped
                {"_source": {"incidentId": "BAD", "polygonCenter": ["a", "b"], "customerCount": "12"}},
                {"_source": {"incidentId": "ZERO", "polygonCenter": [0, 0], "customerCount": 1}},
                {"_source": {"incidentId": "NEG", "polygonCenter": [-82.4, 27.9], "customerCount": -5}},
                {"_source": {"polygonCenter": {"lat": 27.95, "lon": -82.45}, "customerCount": "3",
                             "updateTime": "garbage", "estimatedTimeOfRestoration": "0001-01-01T00:00:00",
                             "polygonPointsGoogle": "not a polyline ~~~"}},
                {"_source": {"incidentId": "STR", "polygonCenter": "27.96,-82.47", "customerCount": None,
                             "polygonPointsGoogle": [{"lat": "x"}, 5, None]}},
            ]
        }
    }
    events = parse_outage_tiles(payload)
    ev = by_id(events)
    assert ev["total"].metrics["customers_out"] == 40 + 12 + 1 + 3
    assert ev["total"].metrics["outages"] == 6  # no hits.total: distinct incidents, mapped or not
    assert set(ev) == {"total", "NEG", "pt-27.9500,-82.4500", "STR"}
    assert ev["NEG"].metrics["customers_out"] == 0 and ev["NEG"].severity == Severity.minor
    assert ev["pt-27.9500,-82.4500"].metrics["etr"] is None and ev["pt-27.9500,-82.4500"].updated_at is None
    assert ev["STR"].geometry == {"type": "Point", "coordinates": [-82.47, 27.96]}
    for junk in (None, [], "x", {}, {"hits": []}, {"error": "x"}):
        assert parse_outage_tiles(junk) == []
    empty = parse_outage_tiles({"hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}})
    assert [e.id for e in empty] == ["total"] and empty[0].metrics["customers_out"] == 0
    assert empty[0].severity == Severity.info


# --- geometry and time helpers ------------------------------------------------------------------------------------


def test_center_formats():
    assert center_of([-82.46, 27.95]) == (-82.46, 27.95)
    assert center_of({"lat": 27.95, "lon": -82.46}) == (-82.46, 27.95)
    assert center_of("27.95,-82.46") == (-82.46, 27.95)
    for bad in (None, [], [1], ["x", 2], [0, 0], [-200, 27], "nope", 5):
        assert center_of(bad) is None


def test_outline_formats_and_guards():
    c = (-82.46, 27.95)
    square = [(27.951, -82.461), (27.951, -82.459), (27.949, -82.459), (27.949, -82.461)]  # (lat, lon)
    google = [{"lat": lat, "lng": lon} for lat, lon in square]
    expect = [[[-82.461, 27.951], [-82.459, 27.951], [-82.459, 27.949], [-82.461, 27.949], [-82.461, 27.951]]]
    assert outline(google, c) == expect
    assert outline([{"latitude": lat, "longitude": lon} for lat, lon in square], c) == expect
    assert outline(encode_polyline(square), c) == expect  # an encoded polyline
    assert outline([[lon, lat] for lat, lon in square], c) == expect  # GeoJSON order
    assert outline([[lat, lon] for lat, lon in square], c) == expect  # (lat, lon) pairs, told apart by the centre
    assert outline({"type": "Polygon", "coordinates": expect}, c) == expect
    assert outline([google, google[::-1]], c)[0] == expect[0] and len(outline([google, google], c)) == 2
    assert outline(google + [google[0]], c) == expect  # already closed: not closed twice
    assert outline(google, None) == expect

    far = [{"lat": lat + 1.0, "lng": lon} for lat, lon in square]  # a degree north of the centre
    huge = [{"lat": 27.95 + d, "lng": -82.46 + e} for d, e in ((0.4, 0.4), (0.4, -0.4), (-0.4, -0.4), (-0.4, 0.4))]
    for bad in (far, huge, google[:2], [google[0]] * 4, google[:3] + [{"lat": "x", "lng": 1}], "", "!!", None, {}, 7):
        assert outline(bad, c) == [], bad


def test_local_time():
    assert local_time("2025-07-01T19:25:08", EASTERN) == utc(2025, 7, 1, 23, 25, 8)  # EDT
    assert local_time("2026-01-15T12:00:00", EASTERN) == utc(2026, 1, 15, 17, 0)  # EST
    assert local_time("2025-07-18T00:10:34.610929", EASTERN) == utc(2025, 7, 18, 4, 10, 34)
    assert local_time("2026-09-27T17:30:00Z", EASTERN) == utc(2026, 9, 27, 17, 30)
    assert local_time("2026-09-27T13:30:00-04:00", EASTERN) == utc(2026, 9, 27, 17, 30)
    assert local_time(1751397908000, EASTERN) == utc(2025, 7, 1, 19, 25, 8)  # epoch ms are UTC
    assert local_time("2025-07-01T19:25:08", None) == utc(2025, 7, 1, 19, 25, 8)
    for none in (None, "", "  ", "0001-01-01T00:00:00", "soon", True):
        assert local_time(none, EASTERN) is None
    assert zone("Not/AZone") is None and zone("America/Chicago").key == "America/Chicago"


# --- fetch --------------------------------------------------------------------------------------------------------


def run_fetch(options, handler, states=("FL",)):
    async def go():
        cfg = SourceConfig.model_validate({"id": "teco_fl", "type": "teco", "states": list(states), **options})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = REGISTRY["teco"](cfg, SourceContext(http, AreaConfig(), Store()))
            assert src.interval >= 300 and src.config_error() is None
            return await src.fetch()

    return asyncio.run(go())


def test_fetch_config_then_tiles_with_session_cookie(fixture):
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        assert request.headers["origin"] == "https://outage.tecoenergy.com"
        assert request.headers["referer"] == "https://outage.tecoenergy.com/"
        if request.url.path == "/api/v1/config":
            return httpx.Response(200, json={"index": "geopoints-prod"},
                                  headers={"set-cookie": "MIC-X-API-V2=abc123; Path=/; Secure; HttpOnly"})
        assert request.url.path == "/api/v1/outage-tiles"
        assert request.headers["cookie"] == "MIC-X-API-V2=abc123"  # the cookie the config call set
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == request_body()
        return httpx.Response(200, json=fixture("teco_outage_tiles.json"))

    events = run_fetch({"customers_served": 849876}, handler)
    assert seen == [("GET", BASE + "/api/v1/config"), ("POST", BASE + "/api/v1/outage-tiles")]
    ev = by_id(events)
    assert ev["total"].metrics["customers_out"] == 14158 and ev["total"].metrics["customers_served"] == 849876
    assert ev["total"].metrics["percent_out"] == pytest.approx(1.666, abs=0.001)
    assert len(events) == 11 and {e.metrics["utility"] for e in events} == {"Tampa Electric"}


def test_fetch_options(fixture):
    bodies = []

    def handler(request):
        if request.method == "POST":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=fixture("teco_outage_tiles.json"))
        raise AssertionError("config call should be skipped")

    events = run_fetch({"name": "TECO", "config_path": "", "bbox": [-83.0, 27.0, -81.5, 28.6], "size": 99999,
                        "max_points": 3, "polygons": False, "link": "https://www.tampaelectric.com/poweroutages/",
                        "headers": {"X-Test": "1"}}, handler)
    box = bodies[0]["query"]["bool"]["filter"]["geo_bounding_box"]["polygonCenter"]
    assert box == {"top_left": {"lat": 28.6, "lon": -83.0}, "bottom_right": {"lat": 27.0, "lon": -81.5}}
    assert bodies[0]["size"] == 10000  # capped at Elasticsearch's result window
    assert [e.id for e in events][:1] == ["total"] and len(events) == 4
    assert {e.metrics["utility"] for e in events} == {"TECO"} and events[0].title.startswith("TECO: ")
    assert all(e.geometry["type"] == "Point" for e in events[1:])
    assert events[1].url == "https://www.tampaelectric.com/poweroutages/"
    assert events[0].metrics["customers_served"] is None  # none configured

    # A bad bbox falls back to Florida.
    run_fetch({"config_path": "", "bbox": [1, 2, 3]}, handler)
    box = bodies[-1]["query"]["bool"]["filter"]["geo_bounding_box"]["polygonCenter"]
    assert box["top_left"] == {"lat": 31.1, "lon": -87.7}


def test_fetch_survives_config_failure(fixture):
    def failing_config(status):
        def handler(request):
            if request.url.path.endswith("/config"):
                if status is None:
                    raise httpx.ConnectError("boom", request=request)
                return httpx.Response(status, text="nope")
            return httpx.Response(200, json=fixture("teco_outage_tiles_20250701.json"))

        return handler

    for status in (None, 404, 500):
        events = run_fetch({}, failing_config(status))
        assert by_id(events)["total"].metrics["customers_out"] == 15


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, text="forbidden"),
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"error": "bad query"}),
        httpx.Response(200, json={"hits": []}),
        httpx.Response(200, json={"hits": {"hits": {"not": "a list"}}}),
    ],
)
def test_fetch_bad_responses_raise(response):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        return response

    with pytest.raises(SourceError):
        run_fetch({}, handler)


# --- catalog ------------------------------------------------------------------------------------------------------


def test_pending_catalog_entries_are_valid():
    entries = yaml.safe_load(PENDING.read_text())
    assert isinstance(entries, list) and entries
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "power_teco.yaml"}
    known_states = regions.state_codes()
    seen = set()
    for e in entries:
        assert e["id"].startswith("teco") and e["id"] not in seen and e["id"] not in existing, e["id"]
        seen.add(e["id"])
        assert e["type"] == "teco" and e["type"] in REGISTRY
        cfg = SourceConfig.model_validate(_interpolate(e))
        assert cfg.states == ["FL"]
        assert all(len(s) == 2 and s.isupper() and s in known_states for s in cfg.states)
        assert cfg.name and cfg.meta["confidence"] in ("high", "medium", "low") and cfg.meta["evidence"]
        if cfg.meta["confidence"] == "high":
            assert cfg.meta["evidence"].count("github.com/") >= 2
        assert cfg.options["link"].startswith("https://")
        assert int(cfg.options["customers_served"]) > 800000
        src = REGISTRY["teco"](cfg, SourceContext(None, AreaConfig(), Store()))
        assert src.config_error() is None and src.interval >= 300 and src.category == Category.power
