import re
from collections import defaultdict
from urllib.parse import urlsplit

import pytest

from emagg import catalog, regions
from emagg.config import CatalogConfig, Config, SourceConfig, _interpolate
from emagg.sources import REGISTRY


@pytest.fixture
def fake_entries(monkeypatch):
    entries = [
        {"id": "national_feed", "type": "usgs_earthquakes", "meta": {}},
        {"id": "la_power", "type": "kubra", "states": ["LA"], "instance_id": "i", "view_id": "v", "meta": {"confidence": "medium"}},
        {"id": "fl_511", "type": "ibi511", "states": ["FL"], "base_url": "https://fl511.example", "api_key": "${FL511_TEST_KEY}",
         "meta": {"signup": "https://example/signup"}},
        {"id": "off_by_default", "type": "wzdx", "states": ["LA"], "url": "https://x", "enabled": False, "meta": {}},
    ]
    monkeypatch.setattr(catalog, "load_entries", lambda: [dict(e) for e in entries])


def ids(cfg: Config) -> list[str]:
    return [s.id for s in cfg.sources]


def test_catalog_selects_by_state(fake_entries):
    assert ids(Config.model_validate({"area": {"states": ["LA"]}})) == ["national_feed", "la_power", "off_by_default"]
    assert ids(Config.model_validate({"area": {"preset": "national"}})) == ["national_feed", "la_power", "fl_511", "off_by_default"]
    assert ids(Config.model_validate({"area": {"states": ["LA"]}, "catalog": {"states": ["FL"]}})) == ["national_feed", "fl_511"]


def test_catalog_overrides_and_filters(fake_entries, monkeypatch):
    cfg = Config.model_validate({
        "area": {"preset": "national"},
        "catalog": {"exclude": ["fl_511"], "types": ["kubra", "wzdx"], "enable": ["off_by_default"]},
        "sources": [{"id": "la_power", "type": "kubra", "name": "Mine", "instance_id": "a", "view_id": "b"}],
    })
    assert ids(cfg) == ["la_power", "off_by_default"]
    assert cfg.sources[0].name == "Mine"  # user definition wins
    assert cfg.sources[1].enabled is True
    assert ids(Config.model_validate({"catalog": {"enabled": False}})) == []

    monkeypatch.setenv("FL511_TEST_KEY", "k123")
    fl = next(s for s in Config.model_validate({"area": {"states": ["FL"]}}).sources if s.id == "fl_511")
    assert fl.options["api_key"] == "k123" and fl.meta["signup"] == "https://example/signup"
    assert "meta" not in fl.options


def test_real_catalog_entries_are_valid():
    """Every shipped catalog entry parses, names a known adapter and has a unique id."""
    entries = catalog.load_entries()
    assert entries, "catalog is empty"
    seen = set()
    for e in entries:
        assert e["id"] not in seen, f"duplicate catalog id {e['id']}"
        seen.add(e["id"])
        assert e["type"] in REGISTRY, f"{e['id']}: unknown type {e['type']}"
        cfg = SourceConfig.model_validate(_interpolate(e))
        assert cfg.meta.get("catalog_file")
        for st in cfg.states:
            assert len(st) == 2 and st.isupper()
            assert st in regions.state_codes(), f"{e['id']}: unknown state {st}"
        assert len(set(cfg.states)) == len(cfg.states), f"{e['id']}: state listed twice"
        assert cfg.meta.get("confidence") in (None, "high", "medium", "low"), e["id"]
    cfg = Config.model_validate({"area": {"preset": "national"}})
    assert len(cfg.sources) == len(entries)
    assert CatalogConfig().enabled


# --- Feed identity ------------------------------------------------------------------------------------------
# Two enabled entries that poll the same feed report the same customers/closures twice (national totals double,
# and a multi-state view split into per-state entries puts the whole total in each state). The identity is
# whatever the adapter builds its requests from.
_ID_KEYS = {
    "kubra": ("api_base", "instance_id", "view_id"),
    "nisc_hosted": ("base_url", "tenant"),
    "outageentry": ("base_url", "client"),
    "pacificorp": ("site", "state"),
    "ibi511": ("base_url",),
}
# Generic URL adapters (arcgis, geojson, json, wzdx, county_outages, osi_pop, wec_outages, ...): the URL plus the
# options that select a different slice of the same response.
_URL_SLICE_KEYS = ("where", "filter", "exclude", "records", "params")


def _norm(v):
    if isinstance(v, str):
        return v.strip().rstrip("/").lower()
    if isinstance(v, dict):
        return tuple(sorted((str(k), _norm(x)) for k, x in v.items()))
    if isinstance(v, list):
        return tuple(_norm(x) for x in v)
    return v


def _norm_url(url: str) -> str:
    parts = urlsplit(str(url).strip())
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


def feed_identity(entry: dict) -> tuple:
    t = entry["type"]
    if t in _ID_KEYS:
        return (t,) + tuple(_norm(entry.get(k)) for k in _ID_KEYS[t])
    if t == "sienatech":  # a full `url` overrides base_url + code
        return (t, _norm_url(entry["url"])) if entry.get("url") else (t, _norm(entry.get("base_url")), _norm(entry["code"]))
    if t == "milsoft_wov":  # the adapter drops the query string and trailing slash of `url`
        return (t, _norm_url(str(entry["url"]).split("?", 1)[0]), _norm(entry.get("params") or {}))
    if entry.get("url"):
        where = entry.get("where") or ("1=1" if t == "arcgis" else None)
        return ("url", _norm_url(entry["url"]), _norm(where)) + tuple(_norm(entry.get(k)) for k in _URL_SLICE_KEYS[1:])
    # Built-in national feeds with no URL option: one per type.
    return (t,) + tuple(sorted((k, repr(_norm(v))) for k, v in entry.items() if k not in ("id", "name", "meta", "enabled", "interval", "states")))


def _duplicates(entries: list[dict]) -> dict[tuple, list[str]]:
    by_identity: dict[tuple, list[str]] = defaultdict(list)
    for e in entries:
        if e.get("enabled", True):
            by_identity[feed_identity(e)].append(e["id"])
    return {k: v for k, v in by_identity.items() if len(v) > 1}


def test_enabled_catalog_entries_have_unique_feed_identity():
    """No two enabled entries poll the same feed (e.g. Ameren IL and MO on one KUBRA view)."""
    assert _duplicates(catalog.load_entries()) == {}


def test_feed_identity_catches_split_views_and_ignores_disabled_copies():
    base = {"type": "kubra", "instance_id": "I", "view_id": "V", "meta": {}}
    entries = [
        {**base, "id": "ameren_il", "states": ["IL"]},
        {**base, "id": "ameren_mo", "states": ["MO"], "view_id": "v "},
        {**base, "id": "other_view", "view_id": "W"},
        {"id": "a", "type": "nisc_hosted", "tenant": "SamHouston"},
        {"id": "b", "type": "nisc_hosted", "tenant": "samhouston", "enabled": False},
        {"id": "c", "type": "milsoft_wov", "url": "https://outage.example.coop/?v=2"},
        {"id": "d", "type": "milsoft_wov", "url": "https://outage.example.coop:443"},
        {"id": "e", "type": "arcgis", "url": "https://x/FeatureServer/0"},
        {"id": "f", "type": "json", "url": "https://x/FeatureServer/0/", "where": "1=1"},
        {"id": "g", "type": "arcgis", "url": "https://x/FeatureServer/0", "where": "STATE='LA'"},
        {"id": "h", "type": "sienatech", "code": "SSEMC"},
        {"id": "i", "type": "sienatech", "code": "ssemc "},
        {"id": "j", "type": "outageentry", "client": "walton"},
        {"id": "k", "type": "outageentry", "client": "greys"},
        {"id": "n1", "type": "nws_alerts"},
        {"id": "n2", "type": "nws_alerts"},
    ]
    dups = sorted(sorted(v) for v in _duplicates(entries).values())
    assert dups == [["ameren_il", "ameren_mo"], ["c", "d"], ["e", "f"], ["h", "i"], ["n1", "n2"]]


def _utility_key(name: str) -> str:
    name = re.sub(r"\(.*?\)", " ", name.lower())
    name = re.sub(r"\b(co-?op|cooperative|corporation|corp|company|co|inc)\b\.?", " ", name)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name).split())


# Same utility on purpose: FPL's county table and its outage points complement each other (one utility total).
_SAME_UTILITY_OK = {frozenset({"fpl_counties", "fpl_points"})}


def test_enabled_power_entries_do_not_repeat_a_utility_in_the_same_state():
    """The same utility on two platforms (say a co-op on NISC and on Milsoft) must not both be on by default."""
    entries = [e for e in catalog.load_entries() if e.get("enabled", True) and e["meta"]["catalog_file"].startswith("power")]
    clashes = []
    for i, a in enumerate(entries):
        ua = _utility_key((a.get("constants") or {}).get("utility") or a.get("name") or a["id"])
        for b in entries[i + 1:]:
            ub = _utility_key((b.get("constants") or {}).get("utility") or b.get("name") or b["id"])
            shared = set(a.get("states") or []) & set(b.get("states") or [])
            if ua == ub and shared and frozenset({a["id"], b["id"]}) not in _SAME_UTILITY_OK:
                clashes.append((a["id"], b["id"]))
    assert clashes == []


def test_power_titles_format_customer_counts():
    """Mapped power feeds format customer counts with thousands separators ("12,500 customers out")."""
    for e in catalog.load_entries():
        if e["meta"]["catalog_file"].startswith("power") and "customers out" in str(e.get("title", "")):
            assert re.match(r"^\{\w+:,\} customers out", e["title"]), f"{e['id']}: {e['title']}"
