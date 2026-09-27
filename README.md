# EM Aggregator

A near-real-time situational-awareness board for emergency management, national in scope. It polls public
data feeds, normalizes everything into one event model with a common severity scale, tags every event with its
state and county, tracks what is **new, escalating, or cleared**, and shows it on one map with state rollups.

It currently knows **118 public feeds** (58 for the Gulf & Southeast coast):

- Weather, flooding, tropical: NWS alerts, river gauges, NHC cones, coastal tide gauges, storm reports,
  SPC/WPC outlooks.
- Power outages for about 60 utilities.
- Road closures from 30 DOT feeds and state 511 systems.
- Internet outages, airport status, local public alerts (evacuations, 911 outages), FEMA declarations and
  open shelters.

Operators can add **field reports** for what no feed covers, such as cell service down or a road blocked by
debris.

```
 feeds ──► adapters ──► normalized events ──► state/county tagging ──► SQLite (lifecycle) ──► API + live dashboard
 (catalog + your        (fetch + parse,       (category, severity,     (Census boundaries)    (first seen, escalated,   (map, tiles, states,
  own config)            one per feed type)    geometry, metrics)                              cleared, source health)   changes, sources)
```

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

emagg serve --demo       # offline, simulated hurricane near Houston — http://127.0.0.1:8000
emagg serve              # live: every open catalog feed, nationwide, no configuration needed

cp config.example.yaml config.yaml   # pick a region preset (e.g. gulf_southeast), add keys, your own feeds
emagg poll                           # poll each configured feed once and show what came back / why it failed
emagg catalog --state FL             # list known feeds for a state
```

Demo mode runs the real adapters, store and UI against sample payloads in each feed's real format. It is
labelled DEMO throughout; none of it reflects real conditions.

## Coverage

The full list, with evidence and confidence for each entry, is in [docs/CATALOG.md](docs/CATALOG.md).

| Need | What's connected (no key unless noted) |
|---|---|
| **Weather** | NWS alerts; NWS Local Storm Reports (trees/wires down, damage, surge); SPC Day 1 severe outlook |
| **Flooding** | NOAA NWPS river gauges (observed + forecast flood category); NOAA CO-OPS tide gauges vs. NWS coastal flood thresholds; WPC excessive-rainfall outlook; flood reports from storm reports |
| **Tropical** | NHC active storms, forecast cones and tracks, coastal hurricane/tropical-storm watch and warning segments |
| **Power** | 52 utilities on KUBRA Storm Center: utility totals plus county breakdowns. Southeast: Georgia Power, Alabama Power, Mississippi Power, Dominion SC/NC, Santee Cooper, JEA, OUC, Lakeland, SECO, Cobb EMC, Fayetteville PWC, Oncor, AEP Texas, CPS, Austin Energy, TNMP, Pedernales, SWEPCO. Also Duke Energy (ArcGIS), FPL (county + points), Entergy LA/MS/TX/AR (county), CenterPoint, Cleco |
| **Roads** | 30 DOT WZDx closure feeds nationwide (LA, MS, FL, NC, Austin, and 20+ other states); NCDOT and FDOT statewide incident layers. With a developer key (free self-service for LA, GA and NC): 511LA, 511GA, DriveNC, 511NY, NVroads. On application (for EM agencies): DriveTexas. By agreement: FL511 |
| **Comms** | IODA state-level internet outages; IPAWS 911-outage alerts; field reports |
| **Public alerts** | FEMA IPAWS: non-NWS alerts from state/county authorities (evacuation orders, shelter-in-place, civil emergencies) |
| **Transport** | FAA airport closures, ground stops, ground delay programs |
| **Shelters / declarations** | FEMA National Shelter System open shelters; OpenFEMA disaster/emergency declarations (drawn as counties) |
| **Fire / seismic** | NIFC wildfire incidents; USGS earthquakes |

### Gaps and next connections

* **Cell service:** there is no free real-time public feed. FCC DIRS publishes county-level "% cell sites out"
  daily during activations, as documents (PDF/DOCX/TXT). A DIRS report parser is the next comms step. Carrier
  maps and Downdetector have no public API.
* **Power:** other connections found but not yet built:
  * TECO (Florida)
  * co-ops on NISC/cloud.coop, Sienatech, OutageEntry (dozens of TX/FL/GA/AL/LA co-ops)
  * Xcel/SPS and Tallahassee (ESRI)

  El Paso Electric has no usable feed. ORNL ODIN (national, county-level) is available but **off by
  default**, because it can double-count utilities already connected.
* **Roads:** Alabama (ALGO Traffic) is connected but **off by default**, since its API is undocumented with no
  stated terms. Found but not connected:
  * SCDOT/511SC: no developer API
  * Mississippi's MDOT alert map: internal only
  * Houston TranStar: RSS feed without coordinates
* **Deliberately excluded:**
  * Duke's key-protected API (the public ArcGIS layer is used instead)
  * a Georgia layer republished from PowerOutage.US, whose terms prohibit it
  * anything behind bot protection

### Verification status

Each adapter was written against its feed's documented format and checked field-by-field against working
open-source code or recorded API responses (evidence per entry in the catalog). It is then tested offline
against sample payloads in that format.

The build environment could reach only one feed live: Kentucky's WZDx feed. Parsing that feed exposed and
fixed a real gap: KYTC marks every work zone's impact "unknown" and describes closures per lane.

**Before relying on the others, run `emagg poll`**. It prints what each feed returned, or exactly why it failed.
Utility IDs and undocumented endpoints change; the Sources tab shows any feed that stops working.

## Configuring

See `config.example.yaml` (every option is commented). Highlights:

* **Region:** `area.preset: gulf_southeast` (also `national`, `conus`, `gulf_coast`, `southeast_atlantic`,
  `mid_atlantic`, `northeast`), or explicit `states`/`bbox`. Events tagged with other states are dropped.
* **Catalog:** feeds for the area's states are added automatically. `catalog.exclude`, `catalog.enable`,
  `catalog.types`, and same-id overrides in `sources:` adjust it.
* **Keys:** feeds needing a key stay "not configured", with a signup link in the Sources tab, until the
  environment variable is set (`LA511_KEY`, `GA511_KEY`, `DRIVENC_KEY`, `DRIVETEXAS_KEY`, …).
* **Your own feeds:** these need no code.
  * `arcgis`, `geojson` or `json` (records with lat/lon), with a field mapping for title, severity (`map`,
    `contains` or numeric `thresholds`), filter/exclude, metrics, constants.
  * `county_outages` for per-county tables.
  * `kubra` for any other Storm Center utility.

## How it works

**Normalized event.** Every adapter emits `Event`s with these fields:
- `category`: weather, flood, tropical, power, comms, roads, transport, fire, seismic, shelter, other
- `severity`
- `title`, `area`
- GeoJSON `geometry`
- `states` and county `fips`
- times and a source link
- source-specific `metrics`

**State and county tagging.** Feeds that know their state (NWS zone codes, gauges, utilities) say so.
Everything else is located with bundled, simplified Census boundaries, with a near-shore fallback so beach and
barrier-island points (Outer Banks, Galveston, Keys) land in the right county. County-keyed data (FEMA
declarations, utility county reports, IPAWS county codes) is drawn from the same boundaries.

**One severity scale** (colors follow the NWS flood-category convention):

| Severity | NWS alert | River / coastal gauge | Power (utility-wide) | Power (county) | Power (single outage) | Road |
|---|---|---|---|---|---|---|
| extreme | Extreme | Major flooding | ≥ 20% out | ≥ 50% out | — | — |
| severe | Severe | Moderate flooding | ≥ 5% | ≥ 20% | ≥ 5,000 customers | Full closure, flooded road |
| moderate | Moderate | Minor flooding | ≥ 1% | ≥ 5% | ≥ 1,000 | Major crash, signal out |
| minor | Minor | Action stage / near flood | < 1% | < 5% | < 1,000 | Debris, lane closure |

**Power totals** use the best figure each utility publishes: its total (when it can be attributed to one
state), else its county report, else its outage points. Multi-state utility totals are never assigned to one
state.

**Change tracking.** The store records when every event was first seen, when its severity changed, and when it
cleared or expired. A source's first load is a *baseline*: it is shown but not announced as new.

**Source health.** Each feed polls on its own interval with exponential backoff. The **Sources** tab shows
ok / stale / failing / not configured, grouped by category, so a quiet map is never mistaken for an all-clear.

**Dashboard.**
- Map clustering colored by the most severe item in each cluster.
- Category tiles, a state selector, and a **States** tab ranking states by severity and customers out.
- A **Changes** timeline and live updates over Server-Sent Events.
- Responses are gzip-compressed.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/events?status=&category=&source=&min_severity=&state=` | GeoJSON FeatureCollection; works directly as a layer in QGIS/ArcGIS |
| GET | `/api/summary?state=` | Per-category counts and headlines |
| GET | `/api/states` | Per-state rollup (severity counts, categories, customers out, bbox) |
| GET | `/api/timeline?hours=6&state=` | New / escalated / eased / cleared / expired |
| GET | `/api/sources` | Every feed: config, catalog evidence/confidence, health |
| GET | `/api/stream` | Server-Sent Events |
| POST | `/api/reports` | Field report `{category, title, severity, lat, lon, description?, expires_hours?, reporter?}` |
| POST | `/api/reports/{id}/resolve` | Close a field report |
| POST | `/api/sources/{id}/refresh` | Poll a source now |

Interactive docs at `/docs`.

## Adding an adapter or catalog entry

* **A new instance of a known feed type:** add an entry to `emagg/catalog/*.yaml`, with `states` and a `meta`
  block (evidence, confidence, signup), and run `pytest`. Every entry is validated.
* **A new feed type:** create `emagg/sources/<name>.py` with a pure `parse_…()` function and a `Source`
  subclass decorated with `@register`. Import it in `emagg/sources/__init__.py`, add a sample payload to
  `emagg/demo_data/`, and add a test.

## Deployment notes

* One process, SQLite, no external services. Put it behind your usual reverse proxy with authentication: the
  dashboard has no login of its own. Set `app.write_token` to protect field reports and manual refresh.
* The national default polls about 115 feeds, mostly every 2–10 minutes. That is modest per feed, but keep the
  default intervals and set a real contact in `app.user_agent` (api.weather.gov requires it). FEMA asks that
  IPAWS be polled no more often than every 2 minutes; the 511 APIs allow about 10 calls per minute per key.
* Map tiles come from CARTO/OpenStreetMap/Esri; the map libraries are bundled.
* Boundaries: simplified U.S. Census cartographic boundaries via `us-atlas` (rebuild with
  `scripts/build_boundaries.py`). Airport coordinates: `airportsdata` (MIT).

## Development

```bash
pip install -e ".[dev]"
pytest
emagg sources      # adapter types
emagg catalog      # known feeds
```
