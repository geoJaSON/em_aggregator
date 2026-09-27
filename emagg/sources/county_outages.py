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
    events: dict[str, Event] = {}
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
            name = clean_text(rec.get(options["county_field"]))
            st_raw = clean_text(rec.get(options["state_field"])) if options.get("state_field") else None
            st = None
            if st_raw:
                st = st_raw.upper() if len(st_raw) == 2 else regions.state_code_for_name(st_raw)
            for cand in ([st] if st else states):
                if name and cand and (county := regions.find_county(cand, name)):
                    break
        if county is None:
            continue
        util = clean_text(rec.get(utility_field)) if utility_field else None
        util = util or utility
        served = to_int(rec.get(options["served_field"])) if options.get("served_field") else None
        ev = county_outage_event(
            util, county["state"], county, out, served,
            etr=rec.get(options["etr_field"]) if options.get("etr_field") else None,
            updated=rec.get(options["updated_field"]) if options.get("updated_field") else None,
            link=options.get("link"),
        )
        if utility_field:  # aggregators (ODIN) list several utilities per county
            ev.id = f"county-{county['fips']}-{util}"
        prev = events.get(ev.id)
        if prev:  # same county listed twice: add up
            prev.metrics["customers_out"] += out
            continue
        events[ev.id] = ev
    return list(events.values())


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
