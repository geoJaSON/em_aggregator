# EM Aggregator

A near-real-time situational-awareness board for emergency management. It polls public and partner
data feeds (weather warnings, river gauges, power outages, road closures, wildfires, earthquakes, tropical
storms), normalizes everything into one event model with a common severity scale, tracks what is **new,
escalating, or cleared**, and shows it on a single map and list. Operators can add **field reports** for
things no feed covers (cell service down, a road blocked by debris).

```
 feeds ──► source adapters ──► normalized events ──► SQLite (lifecycle + history) ──► API + live dashboard
 (NWS, NWPS, Kubra,   (fetch + parse,      (category, severity,     (first seen, escalated,     (map, summary tiles,
  Waze, WZDx, ...)     one per feed)        geometry, metrics)        cleared, source health)     changes, sources)
```

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# 1) See it working with no network or setup: simulated hurricane scenario near Houston
emagg serve --demo            # open http://127.0.0.1:8000

# 2) Real data: the built-in free sources work with no configuration at all (whole US)
emagg poll                    # poll every source once and print what came back
emagg serve

# 3) Your area and your feeds
cp config.example.yaml config.yaml   # set area.bbox / area.states, app.user_agent, enable sources
emagg poll                           # check each feed before relying on it
emagg serve --host 0.0.0.0 --port 8000
```

Demo mode runs the real adapters, store and UI against sample payloads in each feed's real format. It is
labelled DEMO throughout; none of it reflects real conditions.

## What data exists (and what doesn't)

The honest picture for the four things EM usually asks for first:

| Need | Source | Access | In this app |
|---|---|---|---|
| **Weather warnings** | NWS alerts (api.weather.gov) | Free, no key | `nws_alerts` (built in) |
| **Flooding** | NOAA NWPS river gauges: observed/forecast flood category | Free, no key | `nwps_gauges` (built in) |
| | NWS flood warnings, Waze "flooded road" reports | See rows above/below | via `nws_alerts`, `waze` |
| **Power outages** | Utility outage maps on KUBRA Storm Center (many large US utilities) | Public JSON, per utility | `kubra`: utility totals + outage points |
| | Utility maps published as ArcGIS layers | Public | `arcgis` (generic mapping) |
| | DOE/ORNL **EAGLE-I**: county-level, ~15 min, nationwide | Account for government EM/energy partners | *not yet — adapter once we can see its API* |
| | PowerOutage.us | Commercial API | *not yet* |
| **Cell / comms outages** | FCC **DIRS**: county-level % cell sites out, only when activated for a disaster | Public reports; FCC also offers qualifying agencies direct read-only access to NORS/DIRS filings | *field reports for now* |
| | Carrier outage maps, Downdetector | No public API / commercial | — |
| **Blocked roads** | **Waze for Cities**: crowd-reported closures, flooding, debris, signal outages (~2 min) | Free for government partners | `waze` |
| | State/local DOT **WZDx** feeds (closures and work zones) | Mostly public | `wzdx` |
| | State **511** systems on the IBI/Iteris platform (NY, GA, AZ, WI, …) | Free developer key | `ibi511` (experimental) |
| | County/city road-closure layers (often ArcGIS) | Usually public | `arcgis` |
| Wildfire | NIFC WFIGS current incidents | Free | `nifc_wildfires` (built in) |
| Earthquakes | USGS real-time feeds | Free | `usgs_earthquakes` (built in) |
| Tropical | NHC active storms | Free | `nhc_storms` (built in) |

Takeaways:

* **Weather, river flooding, fire, quakes, tropical**: fully available now, nationwide, no keys.
* **Power**: good coverage *if your utilities' outage maps run on Storm Center* (check below). For a
  single nationwide county-level feed, request EAGLE-I access; that is the best next adapter to add.
* **Roads**: the richest near-real-time source for an agency is **Waze for Cities**. Sign up; it's free
  for government. DOT WZDx/511 feeds add official closures.
* **Cell service**: there is no free real-time public feed. Options, best first: apply to the FCC for
  NORS/DIRS information sharing for your agency; ingest DIRS county data during activations; use
  **field reports** in this app (from carrier liaisons, ESF-2, field teams).

### Verification status

Every adapter is written against the feed's published format and tested against sample payloads in that
format (`emagg/demo_data/`, `tests/`). The environment this was built in had no internet access, so **no
adapter has been exercised against the live endpoints yet**. Run `emagg poll` first: it prints what each
feed returned or exactly why it failed. The Kubra request flow matches the open-source `kubra` scraper.
The `ibi511` field names come from platform documentation and are the least certain.

## Configuring feeds

See `config.example.yaml` (every option is commented). Highlights:

* **Area**: `area.bbox` drops events entirely outside it; `area.states` filters NWS alerts server-side.
* **Kubra utility**: open the utility's outage map, open browser dev tools → Network, find the request
  `kubra.io/stormcenter/api/v1/stormcenters/<instance_id>/views/<view_id>/currentState`, and copy the two IDs.
* **Secrets**: `${ENV_VAR}` works anywhere in the YAML, e.g. `url: ${WAZE_FEED_URL}`.
* **Any ArcGIS/GeoJSON layer**: `type: arcgis` / `type: geojson` with a field mapping (`title` template,
  `severity` from a field via `map` or numeric `thresholds`, `filter`, `metrics`). No code needed.

## How it works

**Normalized event.** Every adapter emits `Event`s: `category` (weather, flood, power, comms, roads, fire,
seismic, tropical, other), `severity`, `title`, `area`, GeoJSON `geometry`, times, source link, and
source-specific `metrics` (stage in ft, customers out, acres, magnitude, …).

**One severity scale** so a list can be sorted across feeds (colors follow NWS flood-category convention):

| Severity | NWS alert | River gauge | Power (utility total) | Power (single outage) | Road |
|---|---|---|---|---|---|
| extreme | Extreme | Major flooding | ≥ 20% customers out | — | — |
| severe | Severe | Moderate flooding | ≥ 5% | ≥ 5,000 customers | Full closure, flooded road |
| moderate | Moderate | Minor flooding | ≥ 1% | ≥ 1,000 | Major crash, signal out |
| minor | Minor | Action stage | < 1% | < 1,000 | Debris, lane closure |

Single outages are capped at *severe* so they never outrank life-safety warnings. Thresholds live in each
adapter and are easy to tune.

**Change tracking.** Each poll replaces a source's current set. The store records when every event was
first seen, when its severity changed, and when it cleared or expired. Updated NWS alerts inherit the
original first-seen time instead of looking new. The first successful load of a source is a *baseline*:
it is shown but not announced as new, so a restart does not flood the **Changes** tab.

**Source health.** Each feed polls on its own interval with exponential backoff on failure. The
**Sources** tab shows ok / stale / failing / not configured, so a quiet map is never mistaken for an
all-clear.

**Live updates.** The server pushes a Server-Sent Event after each poll that changed something; the
dashboard refetches. It also refetches every 60 s as a fallback.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/events?status=active\|ended\|all&category=…&source=…&min_severity=…` | GeoJSON FeatureCollection. Works directly as a layer in QGIS/ArcGIS |
| GET | `/api/events/{source}/{id}` | One event |
| GET | `/api/summary` | Per-category counts and headline numbers |
| GET | `/api/timeline?hours=6` | New / escalated / eased / cleared / expired |
| GET | `/api/sources` | Configuration and health of every feed |
| GET | `/api/stream` | Server-Sent Events |
| POST | `/api/reports` | Field report `{category, title, severity, lat, lon, description?, expires_hours?, reporter?}` |
| POST | `/api/reports/{id}/resolve` | Close a field report |
| POST | `/api/sources/{id}/refresh` | Poll a source now |

Interactive docs at `/docs`.

## Adding an adapter

1. Create `emagg/sources/<name>.py`: a pure `parse_…(payload) -> list[Event]` plus a small `Source`
   subclass decorated with `@register` whose `fetch()` downloads and calls the parser.
2. Import it in `emagg/sources/__init__.py`.
3. Add a sample payload to `emagg/demo_data/` and a test in `tests/test_sources.py`.

## Deployment notes

* One process, SQLite, no external services. Put it behind your usual reverse proxy with authentication:
  the dashboard has no login of its own. Set `app.write_token` so only people with the token can add or
  resolve field reports or force refreshes.
* Map tiles come from CARTO/OpenStreetMap/Esri; the map library is bundled, so the UI itself needs no CDN.
* Be a good API citizen: keep default poll intervals and set a real contact in `app.user_agent`
  (api.weather.gov requires it).

## Next steps

* EAGLE-I adapter (county-level power, nationwide) once access is in place.
* FCC DIRS ingestion during activations; NORS/DIRS sharing if the agency is approved.
* NWS Local Storm Reports (spotter reports: trees/wires down, flooding), CO-OPS coastal water levels,
  FEMA declarations, shelter status.
* Alerting: push/email/Teams when something *new* crosses a severity threshold in the area.
* Per-county rollups and a printable situation report for briefings.

## Development

```bash
pip install -e ".[dev]"
pytest
emagg sources      # list adapter types
```
