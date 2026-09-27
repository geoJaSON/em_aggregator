"""NHC forecast cones, forecast tracks and coastal watch/warning segments for every active storm. No key.

Uses NOAA's NHC_tropical_weather_summary ArcGIS service, where each product for all active storms lives in a
single layer (7 = cone, 6 = forecast track, 8 = watch/warning lines), returned as GeoJSON.
"""

from __future__ import annotations

from typing import Any

from emagg.geo import simplify_geometry
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, register
from emagg.sources.mapped import query_arcgis
from emagg.sources.nhc_storms import CLASSIFICATIONS
from emagg.util import clean_text

SERVICE = "https://mapservices.weather.noaa.gov/tropical/rest/services/tropical/NHC_tropical_weather_summary/MapServer"
LAYERS = {"cone": 7, "track": 6, "watch_warning": 8}

WW = {
    "HWR": ("Hurricane Warning", Severity.extreme),
    "HWA": ("Hurricane Watch", Severity.severe),
    "TWR": ("Tropical Storm Warning", Severity.severe),
    "TWA": ("Tropical Storm Watch", Severity.moderate),
}


def _storm_severity(stormtype: str) -> Severity:
    t = (stormtype or "").upper()
    if t in ("HU", "MH", "TY"):
        return Severity.severe
    if t in ("TS", "STS"):
        return Severity.moderate
    return Severity.minor


def _storm_label(p: dict[str, Any]) -> str:
    name = clean_text(p.get("stormname")) or "Unnamed storm"
    kind = CLASSIFICATIONS.get(str(p.get("stormtype") or "").upper())
    # The cone layer's stormname is bare ("Fausto"); points layers carry "Tropical Storm Fausto".
    return f"{kind} {name}" if kind and not name.lower().startswith(kind.lower().split()[0]) else name


def parse_nhc_layer(product: str, features: list[dict[str, Any]]) -> list[Event]:
    events = []
    for i, f in enumerate(features):
        p = f.get("properties") or {}
        geom = simplify_geometry(f.get("geometry"), tolerance=0.01, ndigits=4)
        if not geom:
            continue
        storm = _storm_label(p)
        key = p.get("binnumber") or p.get("stormnum") or storm
        adv = clean_text(p.get("advisnum"))
        suffix = f" (advisory {adv})" if adv else ""
        if product == "cone":
            eid, title, sev = f"cone-{key}", f"Forecast cone: {storm}{suffix}", _storm_severity(p.get("stormtype"))
        elif product == "track":
            eid, title, sev = f"track-{key}", f"Forecast track: {storm}{suffix}", _storm_severity(p.get("stormtype"))
        else:
            label, sev = WW.get(str(p.get("tcww") or "").upper(), ("Tropical watch/warning", Severity.moderate))
            eid, title = f"ww-{key}-{p.get('tcww')}-{f.get('id', i)}", f"{label} (coast): {storm}"
        events.append(
            Event(
                id=eid,
                category=Category.tropical,
                title=title,
                severity=sev,
                description=clean_text(p.get("advdate")),
                area=storm,
                geometry=geom,
                url="https://www.nhc.noaa.gov/",
                metrics={
                    "kind": f"nhc_{product}",
                    "storm": storm,
                    "advisory": adv,
                    "basin": p.get("basin"),
                    "watch_warning": p.get("tcww"),
                },
            )
        )
    return events


@register
class NHCForecastGIS(Source):
    type = "nhc_gis"
    default_name = "Hurricane cones & coastal warnings (NHC)"
    category = Category.tropical
    default_interval = 600

    async def fetch(self) -> list[Event]:
        events: list[Event] = []
        for product in self.options.get("products", ["cone", "track", "watch_warning"]):
            features = await query_arcgis(self, f"{SERVICE}/{LAYERS[product]}", area=None)
            events.extend(parse_nhc_layer(product, features))
        return events
