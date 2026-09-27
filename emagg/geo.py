"""Small, dependency-free geometry helpers (GeoJSON in, GeoJSON out)."""

from __future__ import annotations

import math
from typing import Any, Iterable, Iterator

BBox = tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat


def iter_positions(geometry: dict[str, Any] | None) -> Iterator[tuple[float, float]]:
    if not geometry:
        return
    gtype = geometry.get("type")
    if gtype == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            yield from iter_positions(g)
        return
    yield from _walk(geometry.get("coordinates"))


def _walk(coords: Any) -> Iterator[tuple[float, float]]:
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        yield float(coords[0]), float(coords[1])
        return
    for c in coords:
        yield from _walk(c)


def bbox_of(geometry: dict[str, Any] | None) -> BBox | None:
    xs, ys = [], []
    for x, y in iter_positions(geometry):
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def bboxes_intersect(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def representative_point(geometry: dict[str, Any] | None) -> tuple[float, float] | None:
    """(lon, lat) for list zooming and sorting; the bbox centre for anything that is not a point."""
    if not geometry:
        return None
    if geometry.get("type") == "Point":
        c = geometry.get("coordinates") or []
        return (float(c[0]), float(c[1])) if len(c) >= 2 else None
    bb = bbox_of(geometry)
    if not bb:
        return None
    return (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2


def point(lon: float, lat: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]}


# --- simplification -------------------------------------------------------------------------------


def _perp_dist(p, a, b) -> float:
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def simplify_line(points: list, tolerance: float) -> list:
    """Iterative Douglas-Peucker."""
    if len(points) < 3 or tolerance <= 0:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        max_d, idx = 0.0, -1
        for i in range(start + 1, end):
            d = _perp_dist(points[i], points[start], points[end])
            if d > max_d:
                max_d, idx = d, i
        if idx != -1 and max_d > tolerance:
            keep[idx] = True
            stack.append((start, idx))
            stack.append((idx, end))
    return [p for p, k in zip(points, keep) if k]


def _simplify_ring(ring: list, tolerance: float) -> list | None:
    out = simplify_line(ring, tolerance)
    if len(out) < 4:
        # Too small to survive simplification; keep the original only if it was a valid ring.
        return ring if len(ring) >= 4 else None
    return out


def simplify_geometry(geometry: dict[str, Any] | None, tolerance: float = 0.002, ndigits: int = 5):
    """Simplify and round coordinates so large polygons (county/zone outlines) stay light for the browser."""
    if not geometry:
        return geometry
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")

    def rnd(p):
        return [round(p[0], ndigits), round(p[1], ndigits)]

    if gtype == "Point":
        return {"type": gtype, "coordinates": rnd(coords)}
    if gtype in ("LineString", "MultiPoint"):
        pts = [rnd(p) for p in coords]
        return {"type": gtype, "coordinates": simplify_line(pts, tolerance) if gtype == "LineString" else pts}
    if gtype == "MultiLineString":
        return {"type": gtype, "coordinates": [simplify_line([rnd(p) for p in line], tolerance) for line in coords]}
    if gtype == "Polygon":
        rings = [r for r in (_simplify_ring([rnd(p) for p in ring], tolerance) for ring in coords) if r]
        return {"type": gtype, "coordinates": rings} if rings else None
    if gtype == "MultiPolygon":
        polys = []
        for poly in coords:
            rings = [r for r in (_simplify_ring([rnd(p) for p in ring], tolerance) for ring in poly) if r]
            if rings:
                polys.append(rings)
        return {"type": gtype, "coordinates": polys} if polys else None
    if gtype == "GeometryCollection":
        geoms = [g for g in (simplify_geometry(g, tolerance, ndigits) for g in geometry.get("geometries") or []) if g]
        return {"type": gtype, "geometries": geoms}
    return geometry


def merge_polygons(geometries: Iterable[dict[str, Any] | None]) -> dict[str, Any] | None:
    """Combine Polygon/MultiPolygon geometries into a single MultiPolygon (no dissolve)."""
    polys: list = []
    for g in geometries:
        if not g:
            continue
        if g.get("type") == "Polygon":
            polys.append(g["coordinates"])
        elif g.get("type") == "MultiPolygon":
            polys.extend(g["coordinates"])
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


# --- Google encoded polylines (used by Kubra and some 511 systems) ---------------------------------


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Return a list of (lat, lon)."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10**precision
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def encode_polyline(points: Iterable[tuple[float, float]], precision: int = 5) -> str:
    """Encode (lat, lon) pairs. Used to build fixtures and tests."""
    factor = 10**precision
    out, prev_lat, prev_lon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for delta in (ilat - prev_lat, ilon - prev_lon):
            v = ~(delta << 1) if delta < 0 else delta << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lon = ilat, ilon
    return "".join(out)


# --- Web-mercator tiles / quadkeys (Kubra serves outage clusters by quadkey) ----------------------


def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_to_quadkey(x: int, y: int, zoom: int) -> str:
    digits = []
    for i in range(zoom, 0, -1):
        mask = 1 << (i - 1)
        d = (1 if x & mask else 0) + (2 if y & mask else 0)
        digits.append(str(d))
    return "".join(digits)


def quadkey_to_tile(quadkey: str) -> tuple[int, int, int]:
    x = y = 0
    zoom = len(quadkey)
    for i, ch in enumerate(quadkey):
        mask = 1 << (zoom - i - 1)
        d = int(ch)
        if d & 1:
            x |= mask
        if d & 2:
            y |= mask
    return x, y, zoom


def tile_bbox(x: int, y: int, zoom: int) -> BBox:
    n = 2**zoom

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))

    return x / n * 360.0 - 180.0, lat(y + 1), (x + 1) / n * 360.0 - 180.0, lat(y)


def quadkeys_for_bbox(bbox: BBox, zoom: int) -> list[str]:
    x0, y0 = lonlat_to_tile(bbox[0], bbox[3], zoom)
    x1, y1 = lonlat_to_tile(bbox[2], bbox[1], zoom)
    return [tile_to_quadkey(x, y, zoom) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
