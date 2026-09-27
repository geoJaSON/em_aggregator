"""The normalized event model every source adapter produces."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Category(str, Enum):
    weather = "weather"
    flood = "flood"
    power = "power"
    comms = "comms"
    roads = "roads"
    fire = "fire"
    seismic = "seismic"
    tropical = "tropical"
    other = "other"


class Severity(str, Enum):
    info = "info"
    minor = "minor"
    moderate = "moderate"
    severe = "severe"
    extreme = "extreme"

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.index(self)

    @classmethod
    def from_rank(cls, rank: int) -> "Severity":
        return SEVERITY_ORDER[max(0, min(rank, len(SEVERITY_ORDER) - 1))]


SEVERITY_ORDER = [Severity.info, Severity.minor, Severity.moderate, Severity.severe, Severity.extreme]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class Event(BaseModel):
    """One thing an emergency manager may care about: an alert, a gauge in flood, an outage, a closure."""

    id: str
    category: Category
    title: str
    severity: Severity = Severity.info
    source: str = ""
    description: str | None = None
    area: str | None = None
    geometry: dict[str, Any] | None = None
    starts_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    url: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    # IDs (within the same source) of earlier versions this event replaces, e.g. an NWS alert update.
    # Lets the store keep the original first-seen time instead of treating each update as brand new.
    supersedes: list[str] = Field(default_factory=list)

    @field_validator("starts_at", "updated_at", "expires_at")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
