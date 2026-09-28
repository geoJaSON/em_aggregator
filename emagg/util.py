"""Parsing helpers shared by source adapters. Feeds are messy; these never raise on bad input."""

from __future__ import annotations

import math
import string
from datetime import datetime, timezone
from typing import Any


def parse_time(value: Any) -> datetime | None:
    """Accept ISO-8601 strings, epoch seconds or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and len(value.strip()) in (12, 14) and value.strip().isdigit() and value.strip()[:2] in ("19", "20"):
        # Compact UTC stamps like 202609271200 (SPC, IEM) are dates, not epoch milliseconds.
        try:
            fmt = "%Y%m%d%H%M%S" if len(value.strip()) == 14 else "%Y%m%d%H%M"
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        try:
            n = float(value)
        except ValueError:
            return None
        if n <= 0:
            return None
        if n > 1e11:  # milliseconds
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc).replace(microsecond=0)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    return None


def num(value: Any) -> float | None:
    """Coerce to float; also unwraps Kubra-style {"val": n} objects. NaN/Infinity (which json.loads accepts)
    count as missing, so they can never crash a parser."""
    if isinstance(value, dict):
        value = value.get("val", value.get("value"))
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def to_int(value: Any) -> int | None:
    n = num(value)
    return int(round(n)) if n is not None else None


def get_ci(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present key, matched case-insensitively."""
    if not mapping:
        return default
    lowered = {k.lower(): v for k, v in mapping.items()}
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
        v = lowered.get(key.lower())
        if v is not None:
            return v
    return default


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def render_template(template: str, values: dict[str, Any]) -> str:
    """str.format with missing keys rendered as empty strings."""
    try:
        return string.Formatter().vformat(template, (), _SafeDict({k: "" if v is None else v for k, v in values.items()}))
    except (ValueError, IndexError, AttributeError):
        return template


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
