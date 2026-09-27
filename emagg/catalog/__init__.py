"""Catalog of known public feeds, enabled automatically for the configured area.

Each ``*.yaml`` file in this package is a list of source entries (same keys as ``sources:`` in config.yaml)
plus an optional ``meta`` block: where the endpoint/IDs were verified (``evidence``, ``evidence_year``),
``confidence`` (high/medium/low), ``signup`` (where to get a free key) and ``notes``.

Entries with no ``states`` are national and always included. State entries are included when they overlap
``catalog.states`` (default: ``area.states``; empty means every state).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from emagg.config import CatalogConfig, SourceConfig

CATALOG_DIR = Path(__file__).parent


def load_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(CATALOG_DIR.glob("*.yaml")):
        for entry in yaml.safe_load(path.read_text()) or []:
            entry = dict(entry)
            entry.setdefault("meta", {})["catalog_file"] = path.name
            entries.append(entry)
    return entries


def select(
    catalog: "CatalogConfig", area_states: list[str], existing_ids: set[str], interpolate
) -> list["SourceConfig"]:
    """Catalog entries that apply, as SourceConfigs (user-defined ids and exclusions win)."""
    from emagg.config import SourceConfig

    wanted_states = set(catalog.states or area_states)
    out = []
    for raw in load_entries():
        sid = raw["id"]
        if sid in existing_ids or sid in catalog.exclude:
            continue
        if catalog.types and raw["type"] not in catalog.types:
            continue
        states = set(raw.get("states") or [])
        if states and wanted_states and not (states & wanted_states):
            continue
        entry = interpolate(raw)
        if sid in catalog.enable:
            entry["enabled"] = True
        out.append(SourceConfig.model_validate(entry))
    return out
