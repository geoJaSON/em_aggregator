"""US states/counties: point-in-polygon attribution, county outlines by FIPS/name, and region presets.

Boundaries are simplified U.S. Census cartographic boundaries (via us-atlas), bundled in
``emagg/data/us_boundaries.json.gz`` and rebuilt with ``scripts/build_boundaries.py``.
"""

from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from emagg.geo import iter_positions, representative_point

DATA = Path(__file__).parent / "data" / "us_boundaries.json.gz"

# Named areas of interest. ``states`` drives both server-side filters (NWS) and event filtering.
REGIONS: dict[str, dict[str, Any]] = {
    "national": {"name": "United States", "states": [], "bbox": None},
    "conus": {"name": "Contiguous United States", "states": [], "bbox": (-125.0, 24.0, -66.5, 49.5)},
    "gulf_southeast": {
        "name": "Gulf & Southeast Coast",
        "states": ["TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC"],
        "bbox": (-106.7, 24.3, -75.3, 36.7),
    },
    "gulf_coast": {"name": "Gulf Coast", "states": ["TX", "LA", "MS", "AL", "FL"], "bbox": (-106.7, 24.3, -79.9, 36.6)},
    "southeast_atlantic": {"name": "Southeast Atlantic", "states": ["FL", "GA", "SC", "NC"], "bbox": (-87.7, 24.3, -75.3, 36.7)},
    "mid_atlantic": {"name": "Mid-Atlantic", "states": ["VA", "MD", "DE", "DC", "NJ", "PA", "WV"], "bbox": (-83.7, 36.5, -73.8, 42.3)},
    "northeast": {"name": "Northeast", "states": ["NY", "CT", "RI", "MA", "VT", "NH", "ME"], "bbox": (-79.8, 40.4, -66.9, 47.5)},
}


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    with gzip.open(DATA, "rt") as f:
        data = json.load(f)
    data["counties_by_state"] = {}
    for c in data["counties"]:
        data["counties_by_state"].setdefault(c["state"], []).append(c)
    data["county_by_fips"] = {c["fips"]: c for c in data["counties"]}
    data["state_by_code"] = {s["code"]: s for s in data["states"]}
    return data


def state_codes() -> set[str]:
    return set(_data()["state_by_code"])


def state_name(code: str) -> str | None:
    s = _data()["state_by_code"].get(code)
    return s["name"] if s else None


@lru_cache(maxsize=None)
def state_label_point(code: str) -> tuple[float, float] | None:
    """A (lon, lat) inside the state: vertex centroid of its largest ring, or the nearest county centre."""
    s = _data()["state_by_code"].get(code)
    if not s:
        return None
    ring = max((poly[0] for poly in s["polys"]), key=len)
    lon = sum(p[0] for p in ring) / len(ring)
    lat = sum(p[1] for p in ring) / len(ring)
    if _contains(s, lon, lat):
        return round(lon, 4), round(lat, 4)
    best = None
    for c in _data()["counties_by_state"].get(code, []):
        b = c["bbox"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        d = (cx - lon) ** 2 + (cy - lat) ** 2
        if _contains(c, cx, cy) and (best is None or d < best[0]):
            best = (d, cx, cy)
    return (round(best[1], 4), round(best[2], 4)) if best else None


@lru_cache(maxsize=1)
def _state_names() -> dict[str, str]:
    return {s["name"].lower(): s["code"] for s in _data()["states"]}


def state_code_for_name(name: str) -> str | None:
    return _state_names().get(str(name).strip().lower())


def state_bbox(code: str) -> list[float] | None:
    s = _data()["state_by_code"].get(code)
    return s["bbox"] if s else None


def _in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _contains(feature: dict[str, Any], x: float, y: float) -> bool:
    b = feature["bbox"]
    if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
        return False
    for poly in feature["polys"]:
        if _in_ring(x, y, poly[0]) and not any(_in_ring(x, y, hole) for hole in poly[1:]):
            return True
    return False


# Offsets (degrees) tried when a point lands just outside the simplified coastline (piers, beaches, bridges).
_NEAR_SHORE = [(dx * r, dy * r) for r in (0.03, 0.06) for dx, dy in
               ((1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7))]


def _locate_exact(lon: float, lat: float) -> tuple[str | None, dict[str, Any] | None]:
    data = _data()
    for s in data["states"]:
        if _contains(s, lon, lat):
            for c in data["counties_by_state"].get(s["code"], []):
                if _contains(c, lon, lat):
                    return s["code"], c
            return s["code"], None
    return None, None


def locate(lon: float, lat: float, near_shore: bool = True) -> tuple[str | None, dict[str, Any] | None]:
    """(state code, county record) containing the point, or (None, None) offshore/abroad.

    With ``near_shore``, a point within a few km of the (simplified) shoreline is snapped to the nearest land.
    """
    found = _locate_exact(lon, lat)
    if found[0] or not near_shore:
        return found
    for dx, dy in _NEAR_SHORE:
        found = _locate_exact(lon + dx, lat + dy)
        if found[0]:
            return found
    return None, None


def states_for_geometry(geometry: dict[str, Any] | None, max_samples: int = 40) -> list[str]:
    """States touched by a geometry (sampled vertices + centre). Empty when entirely offshore."""
    if not geometry:
        return []
    pts = list(iter_positions(geometry))
    if len(pts) > max_samples:
        step = len(pts) / max_samples
        pts = [pts[int(i * step)] for i in range(max_samples)]
    center = representative_point(geometry)
    if center:
        pts.append(center)
    found: list[str] = []
    for x, y in pts:
        code, _ = locate(x, y, near_shore=len(pts) == 1)
        if code and code not in found:
            found.append(code)
    return found


def _norm_county(name: str) -> str:
    name = name.lower().replace("saint ", "st. ").replace("st ", "st. ")
    name = re.sub(r"\b(county|parish|borough|census area|municipality|city and borough)\b", "", name)
    return re.sub(r"[^a-z.]", "", name)


@lru_cache(maxsize=1)
def _county_name_index() -> dict[tuple[str, str], dict[str, Any]]:
    return {(c["state"], _norm_county(c["name"])): c for c in _data()["counties"]}


def find_county(state: str, name: str) -> dict[str, Any] | None:
    return _county_name_index().get((state.upper(), _norm_county(name)))


def resolve_county(name: str | None, states: list[str] | None) -> dict[str, Any] | None:
    """The county called ``name`` within the given states, or a 5-digit FIPS code. None when unknown or ambiguous
    (the same name in two of the states, e.g. Washington County): a wrong county is worse than none."""
    text = (name or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) == 5:
        county = county_by_fips(text)
        return county if county and (not states or county["state"] in states) else None
    matches = {c["fips"]: c for st in states or [] if st and (c := find_county(st, text))}
    return next(iter(matches.values())) if len(matches) == 1 else None


def county_by_fips(fips: str) -> dict[str, Any] | None:
    return _data()["county_by_fips"].get(str(fips).zfill(5))


def county_geometry(county: dict[str, Any]) -> dict[str, Any]:
    polys = county["polys"]
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def valid_states(codes: Iterable[str]) -> list[str]:
    known = state_codes()
    out = []
    for c in codes:
        c = str(c).strip().upper()
        if c in known and c not in out:
            out.append(c)
    return out
