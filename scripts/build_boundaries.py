"""Build emagg/data/us_boundaries.json.gz from the us-atlas TopoJSON (U.S. Census cartographic boundaries).

    npm pack us-atlas@3 && tar xzf us-atlas-3.*.tgz
    python scripts/build_boundaries.py package/states-10m.json package/counties-10m.json

Output: simplified state and county polygons with FIPS codes, postal codes and names, used to tag events
with the state(s)/county they fall in and to draw county-keyed datasets.
"""

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from emagg.geo import simplify_line  # noqa: E402

FIPS_TO_POSTAL = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT", "10": "DE", "11": "DC",
    "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT",
    "31": "NE", "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "78": "VI",
}


def decode(topo, obj_name, tolerance):
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        arcs.append(pts)

    def ring(indices):
        out = []
        for i in indices:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(a[1:] if out else a)
        return out

    features = []
    for g in topo["objects"][obj_name]["geometries"]:
        polys = g["arcs"] if g["type"] == "MultiPolygon" else [g["arcs"]] if g["type"] == "Polygon" else []
        out_polys = []
        for poly in polys:
            rings = []
            for r in poly:
                pts = simplify_line(ring(r), tolerance)
                pts = [[round(x, 3), round(y, 3)] for x, y in pts]
                if len(pts) >= 4:
                    rings.append(pts)
            if rings:
                out_polys.append(rings)
        if out_polys:
            features.append((g["id"], g.get("properties", {}).get("name"), out_polys))
    return features


def bbox(polys):
    xs = [p[0] for poly in polys for p in poly[0]]
    ys = [p[1] for poly in polys for p in poly[0]]
    return [min(xs), min(ys), max(xs), max(ys)]


def main(states_path, counties_path):
    st = json.loads(Path(states_path).read_text())
    co = json.loads(Path(counties_path).read_text())
    states = []
    for fips, name, polys in decode(st, "states", 0.003):
        if fips in FIPS_TO_POSTAL:
            states.append({"code": FIPS_TO_POSTAL[fips], "fips": fips, "name": name, "bbox": bbox(polys), "polys": polys})
    counties = []
    for fips, name, polys in decode(co, "counties", 0.003):
        postal = FIPS_TO_POSTAL.get(fips[:2])
        if postal:
            counties.append({"fips": fips, "name": name, "state": postal, "bbox": bbox(polys), "polys": polys})
    out = Path(__file__).resolve().parent.parent / "emagg" / "data" / "us_boundaries.json.gz"
    payload = json.dumps({"source": "U.S. Census Bureau via us-atlas 3 (ISC)", "states": states, "counties": counties},
                         separators=(",", ":"))
    with gzip.open(out, "wt") as f:
        f.write(payload)
    print(f"{len(states)} states, {len(counties)} counties -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(*sys.argv[1:3])
