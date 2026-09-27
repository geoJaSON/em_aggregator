"""FEMA disaster and emergency declarations (OpenFEMA), drawn as the designated counties. No key.

Useful context during response: which counties are already under a Major Disaster or Emergency declaration
and which assistance programs are on.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emagg import regions
from emagg.geo import merge_polygons
from emagg.models import Category, Event, Severity, utcnow
from emagg.sources.base import Source, register
from emagg.util import clean_text, parse_time

API = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
TYPES = {"DR": ("Major Disaster", Severity.moderate), "EM": ("Emergency", Severity.minor),
         "FM": ("Fire Management", Severity.minor)}
PROGRAMS = {"ihProgramDeclared": "IHP", "iaProgramDeclared": "IA", "paProgramDeclared": "PA", "hmProgramDeclared": "HM"}


def parse_declarations(payload: dict[str, Any], states: list[str] | None = None) -> list[Event]:
    rows = payload.get("DisasterDeclarationsSummaries") or []
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if states and r.get("state") not in states:
            continue
        key = r.get("femaDeclarationString") or f"{r.get('declarationType')}-{r.get('disasterNumber')}-{r.get('state')}"
        groups.setdefault(key, []).append(r)
    events = []
    for key, rs in groups.items():
        first = rs[0]
        dtype = str(first.get("declarationType") or "")
        label, sev = TYPES.get(dtype, ("Declaration", Severity.minor))
        counties, statewide = [], False
        for r in rs:
            fips = f"{str(r.get('fipsStateCode') or '').zfill(2)}{str(r.get('fipsCountyCode') or '').zfill(3)}"
            if fips.endswith("000"):
                statewide = True
            elif (c := regions.county_by_fips(fips)) is not None:
                counties.append(c)
        programs = sorted({name for r in rs for field, name in PROGRAMS.items() if r.get(field)})
        title = clean_text(first.get("declarationTitle")) or clean_text(first.get("incidentType")) or "Declaration"
        areas = sorted({clean_text(r.get("designatedArea")) for r in rs if r.get("designatedArea")})
        events.append(
            Event(
                id=key,
                category=Category.fire if dtype == "FM" else Category.other,
                title=f"{label} declaration {key}: {title.title()}",
                severity=sev,
                description=(
                    f"Incident type: {first.get('incidentType')}. Programs: {', '.join(programs) or 'none listed'}.\n"
                    f"Designated: {'statewide; ' if statewide else ''}{'; '.join(areas[:40])}"
                    + (f" (+{len(areas) - 40} more)" if len(areas) > 40 else "")
                ),
                area=first.get("state"),
                geometry=merge_polygons(regions.county_geometry(c) for c in counties),
                starts_at=parse_time(first.get("incidentBeginDate")),
                updated_at=parse_time(max((r.get("declarationDate") or "") for r in rs) or None),
                url=f"https://www.fema.gov/disaster/{first.get('disasterNumber')}",
                states=[first["state"]] if first.get("state") else [],
                metrics={
                    "kind": "fema_declaration",
                    "declaration_type": dtype,
                    "incident_type": first.get("incidentType"),
                    "declared": first.get("declarationDate"),
                    "programs": programs,
                    "counties": len(counties),
                    "statewide": statewide,
                },
            )
        )
    return events


@register
class FEMADeclarations(Source):
    type = "fema_declarations"
    default_name = "FEMA declarations"
    category = Category.other
    default_interval = 1800

    async def fetch(self) -> list[Event]:
        since = (utcnow() - timedelta(days=int(self.options.get("days", 45)))).strftime("%Y-%m-%dT00:00:00.000Z")
        payload = await self.get_json(API, params={
            "$filter": f"declarationDate ge '{since}'",
            "$orderby": "declarationDate desc",
            "$top": 10000,
        })
        return parse_declarations(payload, self.cfg.states or self.ctx.area.states or None)
