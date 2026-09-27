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


def _coerce(value: Any) -> Any:
    """Numeric strings like "1,234" become numbers so metrics sum and sort properly."""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if len(text) > 1 and text.startswith("0") and not text.startswith("0."):
            return value  # codes like FIPS "007" keep their leading zeros
        n = num(text)
        if n is not None and text.replace(",", "").replace(".", "", 1).lstrip("-").isdigit():
            return int(n) if n.is_integer() else n
    return value


def flatten(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """{"a": {"b": 1}} -> {"a_b": 1}, so nested JSON fields work in templates and field options."""
    out: dict[str, Any] = {}
    for k, v in record.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "_"))
        else:
            out[key] = v
    return out


def dig(data: Any, path: str | None) -> Any:
    """Follow a dotted path ("data.items") into parsed JSON; empty path returns the data itself."""
    for part in [p for p in (path or "").split(".") if p]:
        data = data.get(part) if isinstance(data, dict) else None
    return data


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
        # Fixed metrics added to every event, e.g. {kind: outage, utility: Duke Energy} so power points roll up.
        self.constants = dict(options.get("constants") or {})
        self.filter = options.get("filter") or {}
        self.exclude = options.get("exclude") or {}  # {field: [values]} records to drop
        self.time_metrics = list(options.get("time_metrics") or [])  # metrics holding timestamps (epoch/ISO)
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
        if "contains" in rule and value is not None:
            text = str(value).lower()
            for needle, sev in rule["contains"].items():
                if str(needle).lower() in text:
                    return Severity(sev)
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
        for key, unwanted in self.exclude.items():
            if props.get(key) in (unwanted if isinstance(unwanted, list) else [unwanted]):
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
            metrics=self._metrics(props),
        )

    def _metrics(self, props: dict[str, Any]) -> dict[str, Any]:
        out = {**self.constants, **{name: _coerce(props.get(field)) for name, field in self.metrics.items()}}
        for name in self.time_metrics:
            t = parse_time(out.get(name))
            out[name] = t.isoformat() if t else None
        return out


def esri_to_geojson(feature: dict[str, Any]) -> dict[str, Any]:
    """Convert an Esri JSON feature ({attributes, geometry}) queried with outSR=4326 to a GeoJSON feature."""
    g = feature.get("geometry") or {}
    geometry = None
    if "x" in g and "y" in g and g["x"] is not None and g["y"] is not None:
        geometry = {"type": "Point", "coordinates": [g["x"], g["y"]]}
    elif g.get("points"):
        geometry = {"type": "MultiPoint", "coordinates": g["points"]}
    elif g.get("paths"):
        paths = g["paths"]
        geometry = {"type": "LineString", "coordinates": paths[0]} if len(paths) == 1 else {"type": "MultiLineString", "coordinates": paths}
    elif g.get("rings"):
        geometry = {"type": "Polygon", "coordinates": g["rings"]}
    attrs = feature.get("attributes") or {}
    oid = next((attrs[k] for k in ("OBJECTID", "objectid", "FID", "ObjectID") if k in attrs), None)
    return {"type": "Feature", "id": oid, "geometry": geometry, "properties": attrs}


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
    """Query an ArcGIS FeatureServer/MapServer layer, following pagination. Asks for GeoJSON; servers that
    reject that format (older ArcGIS Server, common for utility outage maps) are re-queried as Esri JSON."""
    url = layer_url.rstrip("/") + "/query"
    params: dict[str, Any] = {
        "where": where,
        "outFields": out_fields,
        "outSR": 4326,
        "f": "geojson",
        "returnGeometry": "true",
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
    page = 0
    while page < max_pages:
        params["resultOffset"] = page * page_size
        try:
            data = await source.get_json(url, params=params)
        except SourceError:
            if params["f"] == "geojson" and page == 0:
                params["f"] = "json"  # some servers answer f=geojson with an HTTP error
                continue
            raise
        if isinstance(data, dict) and data.get("error"):
            if params["f"] == "geojson" and page == 0:
                params["f"] = "json"
                continue
            err = data["error"]
            raise SourceError(f"ArcGIS error {err.get('code')}: {err.get('message')}")
        batch = data.get("features") or []
        if params["f"] == "json" or (batch and "attributes" in batch[0] and "properties" not in batch[0]):
            batch = [esri_to_geojson(f) for f in batch]
        features.extend(batch)
        exceeded = data.get("exceededTransferLimit") or (data.get("properties") or {}).get("exceededTransferLimit")
        if not exceeded or not batch:
            break
        page += 1
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
        payload = await self.get_json(self.option_url(), headers=headers)
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
            self.option_url(),
            where=self.options.get("where", "1=1"),
            area=None if self.ignore_area else self.ctx.area,
        )
        mapping = FieldMapping(self.options)
        return [e for i, f in enumerate(features) if (e := mapping.to_event(f, i))]


@register
class JSONRecords(Source):
    """Any JSON API that returns a list of records with coordinates (utility outage lists, incident feeds).

    Example (FPL outage points)::

        - id: fpl_points
          type: json
          url: https://www.fplmaps.com/customer/outage/StormFeedRestoration.json
          records: outages            # dotted path to the list ("" = top level)
          lat: lat
          lon: lng                    # nested fields are flattened with "_", e.g. startLocation_latitude
          category: power
          id_field: ticketNum
          title: "{customersAffected} customers out — {Cause}"
          severity: {field: customersAffected, thresholds: [[5000, severe], [1000, moderate]], default: minor}
          metrics: {customers_out: customersAffected, etr: etr}
          constants: {kind: outage, utility: FPL}
    """

    type = "json"
    default_name = "JSON feed"
    category = Category.other
    default_interval = 300
    required_options = ("url", "lat", "lon")

    async def fetch(self) -> list[Event]:
        method = str(self.options.get("method", "GET")).upper()
        kwargs: dict[str, Any] = {"headers": self.options.get("headers") or {}}
        if self.options.get("params"):
            kwargs["params"] = self.options["params"]
        if method == "POST":
            kwargs["json"] = self.options.get("body") or {}
            resp = await self.ctx.http.post(self.option_url(), **kwargs)
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} from {self.option_url().split('?')[0]}")
            payload = resp.json()
        else:
            payload = await self.get_json(self.option_url(), **kwargs)
        records = dig(payload, self.options.get("records"))
        if not isinstance(records, list):
            raise SourceError(f"no list at '{self.options.get('records') or '(top level)'}' in response")
        return parse_json_records(records, self.options)


def parse_json_records(records: list[Any], options: dict[str, Any]) -> list[Event]:
    mapping = FieldMapping(options)
    lat_key, lon_key = options["lat"], options["lon"]
    events = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        props = flatten(rec)
        lat, lon = num(props.get(lat_key)), num(props.get(lon_key))
        if lat is None or lon is None or (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            geometry = None
        else:
            geometry = {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]}
        feature = {"id": props.get("id"), "properties": props, "geometry": geometry}
        ev = mapping.to_event(feature, i)
        if ev is not None and (geometry is not None or options.get("keep_unlocated")):
            events.append(ev)
    return events
