import pytest
from fastapi.testclient import TestClient

from emagg.api import create_app
from emagg.config import Config, SourceConfig
from emagg.demo import DemoTransport, demo_config
from emagg.store import Store


@pytest.fixture
def client():
    config = demo_config()
    config.sources.append(SourceConfig(id="unconfigured_waze", type="waze"))
    config.sources.append(SourceConfig(id="off", type="usgs_earthquakes", enabled=False))
    app = create_app(config, demo=True, transport=DemoTransport(), store=Store(), start_scheduler=False)
    with TestClient(app) as c:
        yield c


def poll_all(client):
    for s in client.get("/api/sources").json()["sources"]:
        if s["health"] not in ("not_configured", "disabled") and s["id"] != "field_reports":
            r = client.post(f"/api/sources/{s['id']}/refresh")
            assert r.status_code == 200 and r.json()["ok"], r.text


def test_dashboard_and_config(client):
    assert "EM Aggregator" in client.get("/").text
    cfg = client.get("/api/config").json()
    assert cfg["demo"] is True and cfg["area"]["bbox"] == [-96.2, 28.8, -94.3, 30.6]


def test_events_summary_and_sources(client):
    health = {s["id"]: s["health"] for s in client.get("/api/sources").json()["sources"]}
    assert health["nws_alerts"] == "pending"
    assert health["unconfigured_waze"] == "not_configured" and health["off"] == "disabled"
    poll_all(client)
    health = {s["id"]: s["health"] for s in client.get("/api/sources").json()["sources"]}
    assert health["nws_alerts"] == "ok" and health["gulf_power"] == "ok"

    fc = client.get("/api/events").json()
    assert fc["type"] == "FeatureCollection"
    titles = [f["properties"]["title"] for f in fc["features"]]
    assert "Hurricane Demo (Cat 3, 121 mph)" in titles
    # Out-of-area items were dropped by the area filter.
    assert not any("Oklahoma" in t or "Ridge Fire" in t for t in titles)
    # Sorted most severe first.
    ranks = [f["properties"]["severity_rank"] for f in fc["features"]]
    assert ranks == sorted(ranks, reverse=True)

    power = client.get("/api/events", params={"category": "power"}).json()["features"]
    assert power and all(f["properties"]["category"] == "power" for f in power)

    cats = {c["category"]: c for c in client.get("/api/summary").json()["categories"]}
    assert "customers out across 1 utility" in cats["power"]["headline"]
    assert cats["flood"]["count"] > 0 and cats["tropical"]["max_severity"] == "extreme"


def test_timeline_reports_changes_after_baseline(client):
    poll_all(client)
    assert client.get("/api/timeline").json()["items"] == []
    poll_all(client)  # demo: a gauge rises and a new flooded-road report arrives
    items = {(i["id"], i["change"]) for i in client.get("/api/timeline").json()["items"]}
    assert ("CYPT2", "escalated") in items
    assert ("demo-waze-007", "new") in items


def test_field_reports(client):
    body = {"category": "comms", "title": "No cell service", "severity": "severe", "lat": 29.7, "lon": -95.3}
    r = client.post("/api/reports", json=body)
    assert r.status_code == 201
    rid = r.json()["properties"]["id"]
    comms = client.get("/api/events", params={"category": "comms"}).json()["features"]
    assert [f["properties"]["id"] for f in comms] == [rid]
    assert client.post(f"/api/reports/{rid}/resolve").status_code == 200
    assert client.get("/api/events", params={"category": "comms"}).json()["features"] == []
    assert client.post("/api/reports", json={**body, "lat": 200}).status_code == 422


def test_write_token():
    config = Config(sources=[])
    config.app.write_token = "s3cret"
    app = create_app(config, store=Store(), start_scheduler=False)
    body = {"category": "roads", "title": "Tree down", "lat": 29.7, "lon": -95.3}
    with TestClient(app) as c:
        assert c.post("/api/reports", json=body).status_code == 401
        assert c.post("/api/reports", json=body, headers={"X-EMAgg-Token": "wrong"}).status_code == 401
        assert c.post("/api/reports", json=body, headers={"X-EMAgg-Token": "s3cret"}).status_code == 201
        assert c.get("/api/events").status_code == 200  # reads stay open
