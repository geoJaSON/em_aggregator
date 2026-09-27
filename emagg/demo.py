"""Offline demo: serves sample payloads (in each upstream feed's real format) through the real adapters.

``emagg serve --demo`` swaps the HTTP transport for ``DemoTransport`` so the whole pipeline — fetch,
parse, area filtering, storage, change tracking, API and dashboard — runs with no network access.
All demo content is fictional and labelled DEMO in the UI. Scenario: a hurricane landfall near Houston.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from emagg.config import AppConfig, AreaConfig, Config, SourceConfig
from emagg.geo import encode_polyline, point, quadkey_to_tile, tile_bbox
from emagg.models import Category, Event, Severity, utcnow
from emagg.store import Store

DATA = Path(__file__).parent / "demo_data"
_PLACEHOLDER = re.compile(r"^\{\{(?:(ms|s):)?now(?:([+-]\d+)([smhd]))?\}\}$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def _resolve(value: Any, now: datetime) -> Any:
    if isinstance(value, str):
        m = _PLACEHOLDER.match(value)
        if not m:
            return value
        kind, amount, unit = m.groups()
        t = now + (timedelta(**{_UNITS[unit]: int(amount)}) if amount else timedelta())
        if kind == "ms":
            return int(t.timestamp() * 1000)
        if kind == "s":
            return int(t.timestamp())
        return t.isoformat().replace("+00:00", "Z")
    if isinstance(value, list):
        return [_resolve(v, now) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, now) for k, v in value.items()}
    return value


def load_fixture(name: str, now: datetime | None = None) -> Any:
    """Load a demo payload with ``{{now±N[smhd]}}`` placeholders resolved relative to ``now``."""
    return _resolve(json.loads((DATA / name).read_text()), now or utcnow())


# --- simulated Kubra Storm Center -------------------------------------------------------------------

KUBRA_INSTANCE, KUBRA_VIEW = "demo-instance", "demo-view"
KUBRA_OUTAGES = [
    # lat, lon, customers, cause
    (29.760, -95.370, 2400, "Weather"),
    (29.752, -95.362, 380, "Tree contact"),
    (29.771, -95.391, 1250, "Weather"),
    (29.690, -95.450, 5600, "Equipment damage"),
    (29.702, -95.462, 140, None),
    (29.820, -95.520, 860, "Weather"),
    (29.905, -95.300, 3100, "Flooded substation"),
    (29.560, -95.100, 7200, "Weather"),
    (29.545, -95.090, 410, "Tree contact"),
    (29.400, -94.950, 9800, "Storm surge"),
    (29.300, -94.800, 12600, "Storm surge"),
    (30.050, -95.420, 230, None),
    (29.640, -95.580, 55, "Under investigation"),
    (29.980, -95.700, 17, "Under investigation"),
]
KUBRA_SERVICE_AREA = [(29.0, -96.1), (30.4, -96.1), (30.4, -94.4), (29.0, -94.4), (29.0, -96.1)]


def _kubra_tile(quadkey: str, tick: int) -> dict[str, Any] | None:
    bb = tile_bbox(*quadkey_to_tile(quadkey))
    inside = [(i, o) for i, o in enumerate(KUBRA_OUTAGES) if bb[0] <= o[1] < bb[2] and bb[1] <= o[0] < bb[3]]
    if not inside:
        return None
    grow = 1 + 0.08 * (tick % 5)  # numbers drift between polls so the demo shows updates
    hour = utcnow().replace(minute=0, second=0)
    if len(inside) > 1 and len(quadkey) < 11:
        lat = sum(o[0] for _, o in inside) / len(inside)
        lon = sum(o[1] for _, o in inside) / len(inside)
        customers = int(sum(o[2] for _, o in inside) * grow)
        items = [{"id": f"c-{quadkey}", "desc": {"cluster": True, "n_out": len(inside), "cust_a": {"val": customers}},
                  "geom": {"p": [encode_polyline([(lat, lon)])]}}]
    else:
        items = [
            {
                "id": f"o-{i}",
                "desc": {
                    "cluster": False,
                    "n_out": 1,
                    "cust_a": {"val": int(o[2] * grow)},
                    "inc_id": f"INC{i + 1:04d}",
                    "cause": {"EN-US": o[3]} if o[3] else None,
                    "crew_status": "Crew assigned" if o[2] > 1000 else "Pending",
                    "etr": "ETR-NULL" if o[2] > 5000 else (hour + timedelta(hours=6)).isoformat(),
                    "start_time": (hour - timedelta(hours=3)).isoformat(),
                },
                "geom": {"p": [encode_polyline([(o[0], o[1])])]},
            }
            for i, o in inside
        ]
    return {"file_title": "demo", "file_data": items}


# --- the fake transport ---------------------------------------------------------------------------------


class DemoTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.ticks: dict[str, int] = {}

    def _tick(self, key: str) -> int:
        self.ticks[key] = self.ticks.get(key, 0) + 1
        return self.ticks[key]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = self.route(request.url.host, request.url.path)
        if body is None:
            return httpx.Response(404, json={"error": "not found (demo)"})
        return httpx.Response(200, json=body)

    def route(self, host: str, path: str) -> Any:
        now = utcnow()
        if host == "api.weather.gov":
            if path == "/alerts/active":
                return load_fixture("nws_alerts.json", now)
            m = re.match(r"^/zones/\w+/(\w+)$", path)
            if m and (DATA / f"nws_zone_{m.group(1)}.json").exists():
                return load_fixture(f"nws_zone_{m.group(1)}.json", now)
            return None
        if host == "api.water.noaa.gov" and path == "/nwps/v1/gauges":
            data = load_fixture("nwps_gauges.json", now)
            if self._tick("gauges") >= 2:  # Cypress Creek rises from minor to moderate after the first poll
                g = next(g for g in data["gauges"] if g["lid"] == "CYPT2")
                g["status"]["observed"].update(floodCategory="moderate", primary=159.8)
            return data
        if host == "earthquake.usgs.gov":
            return load_fixture("usgs_earthquakes.json", now)
        if host == "services3.arcgis.com" and path.endswith("/query"):
            return load_fixture("nifc_incidents.json", now)
        if host == "www.nhc.noaa.gov":
            return load_fixture("nhc_current_storms.json", now)
        if host == "waze.demo.invalid":
            data = load_fixture("waze_feed.json", now)
            if self._tick("waze") >= 2:  # a new flooded-road report arrives
                data["alerts"].append({
                    "uuid": "demo-waze-007", "type": "WEATHERHAZARD", "subtype": "HAZARD_WEATHER_FLOOD",
                    "street": "Westpark Tollway", "city": "Houston, TX", "location": {"x": -95.4820, "y": 29.7180},
                    "pubMillis": int((now - timedelta(minutes=1)).timestamp() * 1000), "reliability": 6,
                    "reportDescription": "Underpass flooded, vehicle stranded",
                })
            return data
        if host == "wzdx.demo.invalid":
            return load_fixture("wzdx_feed.json", now)
        if host == "511.demo.invalid" and path == "/api/getevents":
            return load_fixture("ibi511_events.json", now)
        if host == "kubra.io":
            return self._kubra(path, now)
        return None

    def _kubra(self, path: str, now: datetime) -> Any:
        base = f"/stormcenter/api/v1/stormcenters/{KUBRA_INSTANCE}/views/{KUBRA_VIEW}"
        if path == f"{base}/currentState":
            return {
                "stormcenterDeploymentId": "demo-deployment",
                "data": {"interval_generation_data": "data/demo-interval",
                         "cluster_interval_generation_data": "cluster-data/demo-cluster/{qkh}"},
                "datastatic": {"demo-regions-key": "regions/demo-regions"},
            }
        if path == f"{base}/configuration/demo-deployment":
            return {"config": {"layers": {"data": {"interval_generation_data": [
                {"type": "THEMATIC_LAYER", "id": "thematic-1"}, {"type": "CLUSTER_LAYER", "id": "cluster-2"}]}}}}
        if path == "/data/demo-interval/public/summary-1/data.json":
            tick = self._tick("kubra-summary")
            out = int(sum(o[2] for o in KUBRA_OUTAGES) * (1 + 0.08 * ((tick - 1) % 5)) * 4.1)
            return {"summaryFileData": {"date_generated": now.isoformat(), "totals": [
                {"total_cust_a": {"val": out}, "total_cust_s": 2700000, "total_outages": 1834 + tick * 7,
                 "total_percent_cust_a": {"val": round(out / 27000, 2)}}]}}
        if path == "/regions/demo-regions/demo-regions-key/serviceareas.json":
            return {"file_data": [{"geom": {"a": [encode_polyline(KUBRA_SERVICE_AREA)]}}]}
        m = re.match(r"^/cluster-data/demo-cluster/\d+/public/cluster-2/([0-3]+)\.json$", path)
        if m:
            return _kubra_tile(m.group(1), self.ticks.get("kubra-summary", 1) - 1)
        return None


def demo_config(db_path: str = ":memory:") -> Config:
    fast = {"interval": 45}
    return Config(
        app=AppConfig(title="EM Aggregator — DEMO", db_path=db_path, user_agent="em-aggregator-demo"),
        area=AreaConfig(name="Houston / Galveston (demo)", bbox=(-96.2, 28.8, -94.3, 30.6), states=["TX"]),
        sources=[
            SourceConfig(id="nws_alerts", type="nws_alerts", **fast),
            SourceConfig(id="river_gauges", type="nwps_gauges", **fast),
            SourceConfig(id="earthquakes", type="usgs_earthquakes", **fast),
            SourceConfig(id="wildfires", type="nifc_wildfires", **fast),
            SourceConfig(id="tropical", type="nhc_storms", **fast),
            SourceConfig(id="gulf_power", type="kubra", name="Gulf Coast Power (demo)",
                         instance_id=KUBRA_INSTANCE, view_id=KUBRA_VIEW, **fast),
            SourceConfig(id="waze", type="waze", url="https://waze.demo.invalid/feed", **fast),
            SourceConfig(id="state_511", type="ibi511", name="State 511 (demo)",
                         base_url="https://511.demo.invalid", api_key="demo", **fast),
            SourceConfig(id="work_zones", type="wzdx", name="DOT work zones (demo)", url="https://wzdx.demo.invalid/feed", **fast),
        ],
    )


def seed_field_reports(store: Store) -> None:
    now = utcnow()
    reports = [
        ("No cell service (multiple carriers) — Kemah", Category.comms, Severity.severe, 29.543, -95.020,
         "DEMO: Field team reports no signal on any carrier along Hwy 146 in Kemah since ~0400."),
        ("Cell site on generator, degraded data — Pasadena", Category.comms, Severity.moderate, 29.690, -95.200,
         "DEMO: Carrier liaison reports site on backup power; voice OK, data degraded."),
        ("Power lines down across road — Pearland", Category.roads, Severity.severe, 29.563, -95.286,
         "DEMO: Energized lines down on Broadway St. Utility notified; road blocked both directions."),
    ]
    for i, (title, cat, sev, lat, lon, desc) in enumerate(reports):
        store.add_event(
            Event(
                id=f"demo-report-{i + 1}",
                source="field_reports",
                category=cat,
                title=title,
                severity=sev,
                description=desc,
                geometry=point(lon, lat),
                starts_at=now - timedelta(minutes=35 * (i + 1)),
                updated_at=now - timedelta(minutes=35 * (i + 1)),
                expires_at=now + timedelta(hours=12),
                metrics={"kind": "field_report", "reporter": "EOC demo"},
            ),
            now - timedelta(minutes=35 * (i + 1)),
        )
