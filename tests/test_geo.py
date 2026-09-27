from emagg import geo


def test_polyline_round_trip():
    pts = [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    encoded = geo.encode_polyline(pts)
    assert encoded == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"  # Google's documented example
    assert geo.decode_polyline(encoded) == pts


def test_quadkey_round_trip():
    x, y = geo.lonlat_to_tile(-95.37, 29.76, 7)
    qk = geo.tile_to_quadkey(x, y, 7)
    assert len(qk) == 7
    assert geo.quadkey_to_tile(qk) == (x, y, 7)
    bb = geo.tile_bbox(x, y, 7)
    assert bb[0] <= -95.37 <= bb[2] and bb[1] <= 29.76 <= bb[3]
    assert qk in geo.quadkeys_for_bbox((-95.4, 29.7, -95.3, 29.8), 7)


def test_bbox_and_intersection():
    poly = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 3], [0, 0]]]}
    assert geo.bbox_of(poly) == (0, 0, 2, 3)
    assert geo.bboxes_intersect((0, 0, 2, 3), (1, 1, 5, 5))
    assert not geo.bboxes_intersect((0, 0, 2, 3), (3, 3, 5, 5))
    assert geo.representative_point(poly) == (1, 1.5)
    assert geo.bbox_of(None) is None


def test_simplify_keeps_valid_rings():
    ring = [[0, 0], [1, 0.00001], [2, 0], [2, 2], [0, 2], [0, 0]]
    out = geo.simplify_geometry({"type": "Polygon", "coordinates": [ring]}, tolerance=0.01)
    coords = out["coordinates"][0]
    assert coords[0] == coords[-1] and len(coords) == 5  # the near-collinear point is dropped


def test_merge_polygons():
    a = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    b = {"type": "MultiPolygon", "coordinates": [[[[5, 5], [6, 5], [6, 6], [5, 5]]]]}
    merged = geo.merge_polygons([a, None, b])
    assert merged["type"] == "MultiPolygon" and len(merged["coordinates"]) == 2
    assert geo.merge_polygons([a])["type"] == "Polygon"
    assert geo.merge_polygons([]) is None
