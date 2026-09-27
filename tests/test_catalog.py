import pytest

from emagg import catalog
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
    cfg = Config.model_validate({"area": {"preset": "national"}})
    assert len(cfg.sources) == len(entries)
    assert CatalogConfig().enabled
