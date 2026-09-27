"""Configuration: area of interest, app settings and the list of source instances.

Config is YAML. Any string may reference environment variables as ``${NAME}`` or ``${NAME:-default}``
so API keys and partner feed URLs can stay out of the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


class AppConfig(BaseModel):
    title: str = "EM Aggregator"
    # api.weather.gov rejects requests without an identifying User-Agent; include a contact address.
    user_agent: str = "em-aggregator/0.1 (set app.user_agent in config.yaml)"
    db_path: str = "data/emagg.sqlite"
    retention_hours: int = 72
    # If set, POST endpoints (field reports, manual refresh) require header X-EMAgg-Token with this value.
    write_token: str | None = None
    request_timeout: float = 30.0


class AreaConfig(BaseModel):
    name: str = "United States"
    # [min_lon, min_lat, max_lon, max_lat]. Events whose geometry falls entirely outside are dropped.
    bbox: tuple[float, float, float, float] | None = None
    # Two-letter state codes. Used by sources that filter server-side by state (e.g. NWS alerts).
    states: list[str] = Field(default_factory=list)

    @field_validator("states")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s and s.strip()]

    @field_validator("bbox")
    @classmethod
    def _check_bbox(cls, v):
        if v is not None and (v[0] >= v[2] or v[1] >= v[3]):
            raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
        return v


class SourceConfig(BaseModel):
    """One source instance. Unknown keys are passed to the adapter as options."""

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    name: str | None = None
    enabled: bool = True
    interval: int | None = None  # seconds between polls
    # Keep events outside the area bbox (e.g. hurricanes still far offshore).
    ignore_area: bool | None = None

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"id": "nws_alerts", "type": "nws_alerts"},
    {"id": "river_gauges", "type": "nwps_gauges"},
    {"id": "earthquakes", "type": "usgs_earthquakes"},
    {"id": "wildfires", "type": "nifc_wildfires"},
    {"id": "tropical", "type": "nhc_storms"},
]


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    area: AreaConfig = Field(default_factory=AreaConfig)
    sources: list[SourceConfig] = Field(default_factory=lambda: [SourceConfig(**s) for s in DEFAULT_SOURCES])

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, v: list[SourceConfig]) -> list[SourceConfig]:
        seen = set()
        for s in v:
            if s.id in seen:
                raise ValueError(f"duplicate source id: {s.id}")
            if s.id == "field_reports":
                raise ValueError("'field_reports' is reserved for manually entered reports")
            seen.add(s.id)
        return v


def load_config(path: str | os.PathLike | None) -> Config:
    """Load YAML config; with no path, look for ./config.yaml and fall back to built-in defaults."""
    candidate = Path(path) if path else Path("config.yaml")
    if not candidate.exists():
        if path:
            raise FileNotFoundError(f"config file not found: {candidate}")
        return Config()
    raw = yaml.safe_load(candidate.read_text()) or {}
    return Config.model_validate(_interpolate(raw))
