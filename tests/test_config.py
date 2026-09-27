from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from emagg.config import Config, load_config
from emagg.scheduler import build_sources
from emagg.sources import REGISTRY, SourceContext
from emagg.store import Store

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_example_config_is_valid(monkeypatch):
    monkeypatch.setenv("WAZE_FEED_URL", "https://example.invalid/feed")
    cfg = load_config(EXAMPLE)
    assert cfg.area.states == ["VA"]
    assert all(s.type in REGISTRY for s in cfg.sources)
    waze = next(s for s in cfg.sources if s.id == "waze")
    assert waze.options["url"] == "https://example.invalid/feed"
    # Every example source (even disabled ones) can be instantiated without config errors.
    ctx = SourceContext(httpx.AsyncClient(), cfg.area, Store())
    for s in cfg.sources:
        s.enabled = True
    sources, problems = build_sources(cfg.sources, ctx)
    assert set(problems) == {"state_511"}  # api_key comes from an env var that is not set here


def test_env_default_and_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  title: ${NOPE:-Fallback}\nsources: []\n")
    assert load_config(p).app.title == "Fallback"
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")


def test_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None)
    assert [s.type for s in cfg.sources] == ["nws_alerts", "nwps_gauges", "usgs_earthquakes", "nifc_wildfires", "nhc_storms"]


def test_validation_errors():
    with pytest.raises(ValidationError):
        Config.model_validate({"sources": [{"id": "a", "type": "waze"}, {"id": "a", "type": "wzdx"}]})
    with pytest.raises(ValidationError):
        Config.model_validate({"sources": [{"id": "field_reports", "type": "waze"}]})
    with pytest.raises(ValidationError):
        Config.model_validate({"area": {"bbox": [10, 10, 5, 20]}})
