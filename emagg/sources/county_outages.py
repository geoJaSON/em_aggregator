"""County-level outage tables (customers out / served per county), drawn as county outlines.

Covers utilities and aggregators that publish per-county numbers: FPL's county file, Entergy's county
endpoint, ORNL's ODIN county dataset. Counties are matched by FIPS when the feed has it, else by name within
the configured (or per-record) state. Example::

    - id: fpl_counties
      type: county_outages
      name: FPL
      states: [FL]
      url: https://www.fplmaps.com/customer/outage/CountyOutages.json
      records: outages
      county_field: County Name
      out_field: Customers Out
      served_field: Customers Served
"""

from __future__ import annotations

from typing import Any

from emagg import regions
from emagg.models import Category, Event
from emagg.sources.base import Source, SourceError, register
from emagg.sources.kubra import county_outage_event
from emagg.sources.mapped import dig
from emagg.util import clean_text, to_int


def parse_county_table(records: list[Any], utility: str, options: dict[str, Any], states: list[str]) -> list[Event]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}  # (fips, utility) -> aggregated row
    utility_field = options.get("utility_field")
    for rec in records:
        if not isinstance(rec, dict):
            continue
        out = to_int(rec.get(options.get("out_field", "customers_out")))
        if not out or out <= 0:
            continue
        county = None
        fips = rec.get(options["fips_field"]) if options.get("fips_field") else None
        if fips:
            county = regions.county_by_fips(str(fips).strip())
        if county is None and options.get("county_field"):
            st_raw = clean_text(rec.get(options["state_field"])) if options.get("state_field") else None
            st = None
            if st_raw:
                st = st_raw.upper() if len(st_raw) == 2 else regions.state_code_for_name(st_raw)
            county = regions.resolve_county(clean_text(rec.get(options["county_field"])), [st] if st else states)
        if county is None:
            continue
        util = (clean_text(rec.get(utility_field)) if utility_field else None) or utility
        served = to_int(rec.get(options["served_field"])) if options.get("served_field") else None
        row = rows.setdefault((county["fips"], util), {
            "county": county, "utility": util, "out": 0, "served": 0, "served_known": True, "etr": None, "updated": None,
        })
        row["out"] += out
        row["served"] += served or 0
        row["served_known"] &= served is not None
        if options.get("etr_field"):
            row["etr"] = row["etr"] or rec.get(options["etr_field"])
        if options.get("updated_field"):
            row["updated"] = row["updated"] or rec.get(options["updated_field"])
    events = []
    for (fips, util), r in rows.items():
        ev = county_outage_event(
            util, r["county"]["state"], r["county"], r["out"], r["served"] if r["served_known"] else None,
            etr=r["etr"], updated=r["updated"], link=options.get("link"),
        )
        if utility_field:  # aggregators (ODIN) list several utilities per county
            ev.id = f"county-{fips}-{util}"
        events.append(ev)
    return events


@register
class CountyOutages(Source):
    type = "county_outages"
    default_name = "County outages"
    category = Category.power
    default_interval = 600
    required_options = ("url",)

    async def fetch(self) -> list[Event]:
        payload = await self.get_json(self.option_url(), headers=self.options.get("headers") or {},
                                      params=self.options.get("params") or None)
        records = dig(payload, self.options.get("records"))
        if not isinstance(records, list):
            raise SourceError(f"no list at '{self.options.get('records') or '(top level)'}' in response")
        return parse_county_table(records, self.name, self.options, self.cfg.states)
