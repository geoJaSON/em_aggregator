"""OutageEntry (CFA Software) utility outage map: form POST to ajaxShellOut.php, marker parsing, catalog."""

import asyncio
import glob
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

import emagg.sources.outageentry  # noqa: F401  (registers the adapter)
from emagg import catalog, regions
from emagg.config import AreaConfig, SourceConfig, _interpolate
from emagg.models import Category, Severity
from emagg.sources.kubra import customers_severity
from emagg.sources import REGISTRY, SourceContext
from emagg.sources.base import SourceError
from emagg.sources.outageentry import is_quiet, markers, markers_block, parse_markers, request_form
from emagg.sources.sienatech import zone_for
from emagg.store import Store

PENDING = Path(catalog.CATALOG_DIR) / "power_outageentry.yaml"
SOUTHEAST = {"TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC"}
UTC = timezone.utc


def by_id(events):
    return {e.id: e for e in events}


# --- parsing ------------------------------------------------------------------------------------------------------


def test_parse_fixture_total(fixture):
    events = parse_markers(fixture("outageentry_greys_markers.json"), "GreyStone Power", ["GA"],
                           customers_served=155949, link="https://www.outageentry.com/Outage/outage.php?Client=GREYS")
    total = events[0]
    assert total.id == "total" and total.category == Category.power
    assert total.metrics["utility"] == "GreyStone Power" and total.states == ["GA"]
    assert total.metrics["customers_out"] == 1463  # sum of consumers_affected (the zero marker is not an outage)
    assert total.metrics["outages"] == 5 and total.metrics["customers_served"] == 155949
    assert total.metrics["percent_out"] == pytest.approx(0.938, abs=1e-3) and total.severity == Severity.minor
    assert total.url.endswith("Client=GREYS")
    assert len(events) == 1  # the confirmed marker fields carry no position: no points are invented
    no_served = parse_markers(fixture("outageentry_greys_markers.json"), "GreyStone Power", ["GA"])[0]
    assert no_served.metrics["percent_out"] is None and no_served.severity == Severity.moderate  # >= 1,000 out


def test_markers_with_positions():
    tz = zone_for(["GA"])
    payload = {"0": {"markers": [
        {"id": "D-17", "consumers_affected": "1184", "lat": "33.9526", "lng": "-84.5499",
         "estimated_restore_time": "2026-09-27 16:30:00", "start_date": "2026-09-27 11:52:00", "device_name": "Fuse 4411"},
        {"consumers_affected": "236", "latitude": 33.99, "longitude": -84.70, "estimated_restore_time": "0000-00-00 00:00:00",
         "start_date": "2026-09-27 13:05:00"},
        {"id": "D-17", "consumers_affected": 16, "lat": 33.9526, "lng": -84.5499},  # same device twice: added up
        {"consumers_affected": "41", "estimated_restore_time": "", "start_date": "2026-09-27 13:40:00"},  # no position
        {"consumers_affected": "3", "lat": 0, "lng": 0},  # null island: not drawn
        None, "x", {"consumers_affected": "lots", "lat": 33.9, "lng": -84.6},
    ]}}
    events = parse_markers(payload, "GreyStone Power", ["GA"], tz=tz)
    ev = by_id(events)
    assert ev["total"].metrics["customers_out"] == 1184 + 236 + 16 + 41 + 3 and ev["total"].metrics["outages"] == 5
    points = [e for e in events if e.metrics["kind"] == "outage"]
    assert len(points) == 2 and points[0].id == "D-17"
    fuse = points[0]
    assert fuse.metrics["customers_out"] == 1200 and fuse.metrics["device"] == "Fuse 4411"
    assert fuse.title == "1,200 customers out" and fuse.severity == customers_severity(1200)
    assert fuse.starts_at == datetime(2026, 9, 27, 15, 52, tzinfo=UTC)  # 11:52 EDT
    assert fuse.metrics["etr"] == "2026-09-27T20:30:00+00:00" and fuse.severity == Severity.moderate
    assert regions.locate(*fuse.geometry["coordinates"])[1]["fips"] == "13067"  # Cobb County
    hashed = points[1]
    assert hashed.id.startswith("oe-") and hashed.metrics["etr"] is None
    assert hashed.id == [e for e in parse_markers(payload, "G", ["GA"]) if e.metrics["kind"] == "outage"][1].id
    naive = [e for e in parse_markers(payload, "G", ["GA"], tz=None) if e.id == "D-17"][0]
    assert naive.starts_at is None and naive.metrics["etr"] is None  # local times with no zone are not guessed
    assert [e.id for e in parse_markers(payload, "G", ["GA"], max_points=1)] == ["total", "D-17"]
    assert [e.id for e in parse_markers(payload, "G", ["GA"], outage_points=False)] == ["total"]


def test_duplicate_markers_added_up_before_the_event_is_built():
    payload = {"0": {"markers": [{"id": "D1", "consumers_affected": "5", "lat": 33.95, "lng": -84.55},
                                 {"id": "D1", "consumers_affected": "1000", "lat": 33.96, "lng": -84.56}]}}
    point = [e for e in parse_markers(payload, "G", ["GA"]) if e.id == "D1"][0]
    assert point.metrics["customers_out"] == 1005 and point.title == "1,005 customers out"
    assert point.severity == customers_severity(1005) and point.geometry["coordinates"] == [-84.55, 33.95]


def test_non_finite_numbers_do_not_raise():
    for bad in ("NaN", "inf", "-Infinity", "1e400", float("nan"), float("inf")):
        payload = {"0": {"markers": [{"consumers_affected": bad, "lat": 33.9, "lng": -84.6},
                                     {"consumers_affected": "4", "lat": bad, "lng": -84.6}]}}
        events = parse_markers(payload, "G", ["GA"])
        assert [e.id for e in events] == ["total"] and events[0].metrics["customers_out"] == 4


def test_response_shapes():
    block = {"markers": [{"consumers_affected": 2}]}
    assert markers_block({"0": block}) is block
    assert markers_block([None, block]) is block
    assert markers_block(block) is block
    assert markers_block({"1": block, "status": "ok"}) is block
    assert markers_block({"0": {}}) == {} and markers({"0": {}}) == []
    assert markers({"0": {"markers": {"a": {"consumers_affected": 1}, "b": "x"}}}) == [{"consumers_affected": 1}]
    assert markers({"0": {"markers": None}}) == [] and markers({"0": {"markers": "x"}}) == []
    for junk in (None, "x", 5, {}, [], {"error": "bad client"}, {"0": []}, {"0": None}):
        assert markers_block(junk) is None
        assert parse_markers(junk, "U", ["GA"])[0].metrics["customers_out"] == 0
    for quiet in ({}, [], {"0": {}}, {"0": []}, {"0": None}, {"0": {}, "status": "ok"}):
        assert is_quiet(quiet), quiet
    for not_quiet in (None, "x", 5, {"error": "bad client"}, {"0": {}, "error": "x"}, {"1": {}}, {"0": "x"},
                      {"0": {"markers": [{"consumers_affected": 3}]}}, [{"markers": []}]):
        assert not is_quiet(not_quiet), not_quiet


# --- adapter --------------------------------------------------------------------------------------------------------


def run_fetch(options, handler, states=("GA",)):
    cfg = SourceConfig.model_validate({"id": "t", "type": "outageentry", "name": "GreyStone Power",
                                       "states": list(states), **options})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            src = REGISTRY["outageentry"](cfg, SourceContext(http, AreaConfig(), Store()))
            assert src.interval >= 300 and src.config_error() is None
            return await src.fetch()

    return asyncio.run(go())


def test_fetch_posts_the_maps_form(fixture):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=fixture("outageentry_greys_markers.json"))

    events = run_fetch({"client": "GREYS", "customers_served": 155949}, handler)
    req = seen[0]
    assert len(seen) == 1 and req.method == "POST"
    assert str(req.url) == "https://www.outageentry.com/Outage/ajax/ajaxShellOut.php"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    form = {k: v[0] for k, v in parse_qs(req.content.decode(), keep_blank_values=True).items()}
    assert form == {
        "action": "get", "client": "GREYS", "target": "cfa_device_markers", "serviceIndex": "1", "port": "",
        "includePrecictions": "", "includeIndividual": "true", "includeComments": "false",
        "devicesToPolygonize": "[]", "dataUrl": "null",
    }
    assert form == request_form("GREYS")
    assert req.headers["origin"] == "https://www.outageentry.com"
    assert req.headers["referer"] == "https://www.outageentry.com/Outage/outage.php?Client=GREYS&serviceIndex=1&openingPage="
    assert req.headers["x-requested-with"] == "XMLHttpRequest"
    total = events[0]
    assert total.metrics["customers_out"] == 1463 and total.metrics["customers_served"] == 155949
    assert total.url == "https://www.outageentry.com/Outage/outage.php?Client=GREYS&serviceIndex=1&openingPage="


def test_fetch_options_and_quiet_day():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"0": {}})

    events = run_fetch({"client": "ALCOA", "service_index": 0, "base_url": "https://www.outageentry.com/Outages",
                        "link": "https://alcoa.example/"}, handler, states=("TN",))
    assert str(seen[0].url) == "https://www.outageentry.com/Outages/ajax/ajaxShellOut.php"
    assert parse_qs(seen[0].content.decode())["serviceIndex"] == ["0"]
    assert [e.id for e in events] == ["total"] and events[0].metrics["customers_out"] == 0
    assert events[0].url == "https://alcoa.example/"


@pytest.mark.parametrize("payload", [{}, [], {"0": {}}, {"0": []}, {"0": None}, {"0": {"markers": []}},
                                     {"0": {"markers": None}}])
def test_fetch_empty_responses_are_a_quiet_day(payload):
    # Not errors: on an error the scheduler keeps the last poll's outages on the dashboard after power is back.
    events = run_fetch({"client": "GREYS"}, lambda request: httpx.Response(200, json=payload))
    assert [e.id for e in events] == ["total"] and events[0].metrics["customers_out"] == 0


@pytest.mark.parametrize("response, message", [
    (httpx.Response(500), "HTTP 500"),
    (httpx.Response(200, text="<html>login</html>"), "invalid JSON"),
    (httpx.Response(200, json="nope"), "no markers"),
    (httpx.Response(200, json=5), "no markers"),
    (httpx.Response(200, json={"error": "Invalid client"}), "no markers; got error"),
    (httpx.Response(200, json={"0": "x"}), "no markers"),
    (httpx.Response(200, json={"0": {}, "error": "x"}), "no markers; got empty"),
    (httpx.Response(200, json={"0": {"error": "Invalid client"}}), "error"),
])
def test_fetch_bad_responses_raise(response, message):
    with pytest.raises(SourceError, match=message):
        run_fetch({"client": "GREYS"}, lambda request: response)


def test_missing_client_is_a_config_error():
    cfg = SourceConfig.model_validate({"id": "t", "type": "outageentry"})
    src = REGISTRY["outageentry"](cfg, SourceContext(None, AreaConfig(), Store()))
    assert src.config_error() == "not configured: set client"


# --- catalog ----------------------------------------------------------------------------------------------------------


def test_pending_catalog_entries_are_valid():
    entries = yaml.safe_load(PENDING.read_text())
    assert isinstance(entries, list) and len(entries) >= 13
    # Ids from every other catalog file (uniqueness across all files is also checked in test_catalog.py).
    existing = {e["id"] for e in catalog.load_entries() if e["meta"]["catalog_file"] != "power_outageentry.yaml"}
    sienatech = Path(catalog.CATALOG_DIR) / "power_sienatech.yaml"
    existing |= {e["id"] for e in yaml.safe_load(sienatech.read_text())}
    known_states = regions.state_codes()
    seen, clients = set(), set()
    for e in entries:
        assert e["id"].startswith("outageentry_") and e["id"] not in seen and e["id"] not in existing, e["id"]
        seen.add(e["id"])
        assert e["type"] == "outageentry" and e["type"] in REGISTRY
        cfg = SourceConfig.model_validate(_interpolate(e))
        assert cfg.name and cfg.states and all(len(s) == 2 and s.isupper() and s in known_states for s in cfg.states)
        client = cfg.options["client"]
        assert client and client.lower() not in clients
        clients.add(client.lower())
        assert cfg.meta["confidence"] in ("high", "medium", "low") and cfg.meta["evidence"]
        if cfg.meta["confidence"] == "high":
            assert cfg.meta["evidence"].count("github.com/") >= 2
        if "customers_served" in cfg.options:
            assert int(cfg.options["customers_served"]) > 0
        # Every enabled entry can read its naive local times: one zone for its states, or an explicit one.
        if cfg.enabled:
            assert zone_for(cfg.states, cfg.options.get("timezone")) is not None, e["id"]
        src = REGISTRY["outageentry"](cfg, SourceContext(None, AreaConfig(), Store()))
        assert src.config_error() is None and src.interval >= 300
    assert {"tallahatchie", "svec", "greys", "nemepa", "alcorn_county", "plateau", "walton", "cfu", "cde", "tsemc",
            "glendale", "santa_clara", "cecar"} <= clients  # every tenant in lukesteve03's outageentry_sources.json
    southeast = [e for e in entries if set(e["states"]) & SOUTHEAST and e.get("enabled", True)]
    assert len(southeast) >= 6
    # Search-snippet-only tenants stay off until a live poll confirms them.
    for e in entries:
        if "github.com/" not in e["meta"]["evidence"]:
            assert e.get("enabled") is False, e["id"]


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
