"""Generic GeoJSON and ArcGIS Feature Service sources with a small field-mapping language.

These cover the long tail: county road-closure layers, shelter lists, a utility's ESRI outage layer,
a state DOT's GeoJSON. Example (config.yaml)::

    - id: county_closures
      type: arcgis
      name: County road closures
      url: https://services.arcgis.com/.../FeatureServer/0
      where: "STATUS = 'Closed'"
      category: roads
      id_field: OBJECTID
      title: "Road closed: {ROAD_NAME}"
      description: "{REASON}"
      severity: {field: STATUS, map: {Closed: severe, Restricted: moderate}, default: minor}
      updated_field: LAST_EDITED_DATE
      metrics: [DETOUR, AGENCY]
"""

from __future__ import annotations

from typing import Any

from emagg.config import AreaConfig
from emagg.geo import simplify_geometry
from emagg.models import Category, Event, Severity
from emagg.sources.base import Source, SourceError, register
from emagg.util import clean_text, num, parse_time, render_template


class FieldMapping:
    def __init__(self, options: dict[str, Any], default_category: Category = Category.other):
        self.category = Category(options.get("category", default_category.value))
        self.id_field = options.get("id_field")
        self.title = options.get("title", "{name}")
        self.description = options.get("description")
        self.area = options.get("area")
        self.url = options.get("link") or options.get("url_template")
        self.severity = options.get("severity", "minor")
        self.starts_field = options.get("starts_field")
        self.updated_field = options.get("updated_field")
        self.expires_field = options.get("expires_field")
        metrics = options.get("metrics") or []
        self.metrics = metrics if isinstance(metrics, dict) else {m: m for m in metrics}
        self.filter = options.get("filter") or {}
        self.simplify = float(options.get("simplify", 0.0005))

    def _severity(self, props: dict[str, Any]) -> Severity:
        rule = self.severity
        if isinstance(rule, str):
            return Severity(rule)
        value = props.get(rule.get("field"))
        if "map" in rule and value is not None:
            mapped = rule["map"].get(str(value))
            if mapped:
                return Severity(mapped)
        if "thresholds" in rule:
            n = num(value)
            if n is not None:
                for floor, sev in sorted(rule["thresholds"], key=lambda t: -float(t[0])):
                    if n >= float(floor):
                        return Severity(sev)
        return Severity(rule.get("default", "minor"))

    def _included(self, props: dict[str, Any]) -> bool:
        for key, wanted in self.filter.items():
            allowed = wanted if isinstance(wanted, list) else [wanted]
            if props.get(key) not in allowed:
                return False
        return True

    def to_event(self, feature: dict[str, Any], index: int) -> Event | None:
        props = feature.get("properties") or {}
        if not self._included(props):
            return None
        raw_id = props.get(self.id_field) if self.id_field else feature.get("id")
        eid = str(raw_id) if raw_id is not None else f"idx-{index}"
        return Event(
            id=eid,
            category=self.category,
            title=clean_text(render_template(self.title, props)) or eid,
            severity=self._severity(props),
            description=clean_text(render_template(self.description, props)) if self.description else None,
            area=clean_text(render_template(self.area, props)) if self.area else None,
            geometry=simplify_geometry(feature.get("geometry"), tolerance=self.simplify),
            starts_at=parse_time(props.get(self.starts_field)) if self.starts_field else None,
            updated_at=parse_time(props.get(self.updated_field)) if self.updated_field else None,
            expires_at=parse_time(props.get(self.expires_field)) if self.expires_field else None,
            url=clean_text(render_template(self.url, props)) if self.url else None,
            metrics={name: props.get(field) for name, field in self.metrics.items()},
        )


async def query_arcgis(
    source: Source,
    layer_url: str,
    *,
    where: str = "1=1",
    area: AreaConfig | None = None,
    out_fields: str = "*",
    page_size: int = 1000,
    max_pages: int = 20,
    extra_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Query an ArcGIS FeatureServer/MapServer layer as GeoJSON, following pagination."""
    url = layer_url.rstrip("/") + "/query"
    params: dict[str, Any] = {
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "f": "geojson",
        "resultRecordCount": page_size,
    }
    if area is not None and area.bbox:
        params.update(
            {
                "geometry": ",".join(str(v) for v in area.bbox),
                "geometryType": "esriGeometryEnvelope",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
            }
        )
    params.update(extra_params or {})
    features: list[dict[str, Any]] = []
    for page in range(max_pages):
        params["resultOffset"] = page * page_size
        data = await source.get_json(url, params=params)
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise SourceError(f"ArcGIS error {err.get('code')}: {err.get('message')}")
        batch = data.get("features") or []
        features.extend(batch)
        exceeded = data.get("exceededTransferLimit") or (data.get("properties") or {}).get("exceededTransferLimit")
        if not exceeded or not batch:
            break
    return features


@register
class GeoJSONFeed(Source):
    type = "geojson"
    default_name = "GeoJSON feed"
    category = Category.other
    default_interval = 300
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        headers = self.options.get("headers") or {}
        payload = await self.get_json(self.options["url"], headers=headers)
        features = payload.get("features", []) if isinstance(payload, dict) else []
        mapping = FieldMapping(self.options)
        return [e for i, f in enumerate(features) if (e := mapping.to_event(f, i))]


@register
class ArcGISLayer(Source):
    type = "arcgis"
    default_name = "ArcGIS layer"
    category = Category.other
    default_interval = 300
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        features = await query_arcgis(
            self,
            self.options["url"],
            where=self.options.get("where", "1=1"),
            area=None if self.ignore_area else self.ctx.area,
        )
        mapping = FieldMapping(self.options)
        return [e for i, f in enumerate(features) if (e := mapping.to_event(f, i))]
