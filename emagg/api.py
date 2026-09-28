"""HTTP API and dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from emagg import __version__
from emagg.config import Config
from emagg.geo import point
from emagg.http import make_http_client
from emagg.models import SEVERITY_ORDER, Category, Event, Severity, utcnow
from emagg.regions import state_codes, state_name
from emagg.scheduler import Notifier, Scheduler, attribute, build_sources
from emagg.sources import REGISTRY, SourceContext
from emagg.store import Store, ts
from emagg.summary import LABELS, build_state_rollup, build_summary

log = logging.getLogger("emagg.api")
WEB = Path(__file__).parent / "web"
FIELD_REPORTS = "field_reports"


class ReportIn(BaseModel):
    category: Category
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    severity: Severity = Severity.moderate
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    expires_hours: float = Field(default=12, gt=0, le=720)
    reporter: str | None = Field(default=None, max_length=100)


def create_app(
    config: Config,
    *,
    demo: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    store: Store | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    store = store or Store(config.app.db_path)
    notifier = Notifier()
    state: dict[str, Any] = {"scheduler": None, "problems": {}, "sources": []}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = make_http_client(config, transport)
        ctx = SourceContext(http=http, area=config.area, store=store)
        sources, problems = build_sources(config.sources, ctx)
        for sid, why in problems.items():
            log.warning("source %s disabled: %s", sid, why)
        scheduler = Scheduler(sources, store, notifier, config.area, config.app.retention_hours)
        state.update(scheduler=scheduler, problems=problems, sources=sources)
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await http.aclose()

    app = FastAPI(title=config.app.title, version=__version__, lifespan=lifespan)
    # National event sets with county/zone outlines run to megabytes; they compress ~5-10x. (SSE is excluded.)
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.state.store = store
    app.state.notifier = notifier
    app.state.runtime = state

    def require_token(x_emagg_token: str | None = Header(default=None)) -> None:
        expected = config.app.write_token
        if expected and not (x_emagg_token and secrets.compare_digest(x_emagg_token, expected)):
            raise HTTPException(401, "missing or invalid X-EMAgg-Token header")

    # --- dashboard ----------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- read API ------------------------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "title": config.app.title,
            "version": __version__,
            "demo": demo,
            "area": {
                "name": config.area.name,
                "preset": config.area.preset,
                "bbox": config.area.bbox,
                "states": config.area.states,
            },
            "state_names": dict(sorted(((s, state_name(s)) for s in state_codes()), key=lambda kv: kv[1] or kv[0])),
            "categories": [{"id": c.value, "label": LABELS[c]} for c in Category],
            "severities": [s.value for s in SEVERITY_ORDER],
            "write_token_required": bool(config.app.write_token),
        }

    @app.get("/api/events")
    async def get_events(
        status: str = Query("active", pattern="^(active|ended|all)$"),
        category: list[Category] | None = Query(None),
        source: list[str] | None = Query(None),
        min_severity: Severity | None = None,
        state: list[str] | None = Query(None, description="two-letter state codes"),
        limit: int = Query(5000, ge=1, le=20000),
    ) -> dict[str, Any]:
        rows = store.query_events(
            status=status,
            categories=[c.value for c in category] if category else None,
            sources=source,
            min_severity=min_severity,
            states=state,
        )
        kept, omitted = _limit_events(rows, limit)
        return {
            "type": "FeatureCollection",
            "generated_at": ts(utcnow()),
            "total": len(rows),
            "truncated": len(kept) < len(rows),
            "omitted": omitted,
            "features": [_feature(r) for r in kept],
        }

    @app.get("/api/events/{source_id}/{event_id}")
    async def get_event(source_id: str, event_id: str) -> dict[str, Any]:
        row = store.get_event(source_id, event_id)
        if not row:
            raise HTTPException(404, "event not found")
        return _feature(row)

    @app.get("/api/summary")
    async def get_summary(state: list[str] | None = Query(None)) -> dict[str, Any]:
        # Customers are counted per state for a state filter or a regional area (multi-state utility totals
        # can't be split); nationally, utility totals are used as published.
        return build_summary(store.query_events(status="active", states=state), states=state or config.area.states)

    @app.get("/api/states")
    async def get_states() -> dict[str, Any]:
        """Active events rolled up by state: counts by severity and category, customers without power."""
        return {"states": build_state_rollup(store.query_events(status="active"))}

    @app.get("/api/timeline")
    async def get_timeline(
        hours: float = Query(6, gt=0, le=168),
        limit: int = Query(200, ge=1, le=1000),
        state: list[str] | None = Query(None),
    ):
        rows = store.timeline(utcnow() - timedelta(hours=hours), limit=limit, states=state)
        return {"items": [{k: v for k, v in r.items() if k != "geometry"} for r in rows]}

    @app.get("/api/sources")
    async def get_sources() -> dict[str, Any]:
        statuses = store.statuses()
        running = {s.id: s for s in state["sources"]}
        now = utcnow()
        out = []
        for cfg in config.sources:
            src = running.get(cfg.id)
            cls = REGISTRY.get(cfg.type)
            info: dict[str, Any] = (
                src.describe()
                if src
                else {
                    "id": cfg.id,
                    "name": cfg.name or (cls.default_name if cls else cfg.type),
                    "type": cfg.type,
                    "category": (cfg.options.get("category") or (cls.category.value if cls else "other")),
                    "interval": cfg.interval or (cls.default_interval if cls else None),
                    "note": cls.note if cls else None,
                }
            )
            info["states"] = cfg.states
            info["meta"] = {k: v for k, v in cfg.meta.items() if k in ("confidence", "notes", "signup", "evidence", "evidence_year")}
            info["catalog"] = bool(cfg.meta.get("catalog_file"))
            st = statuses.get(cfg.id, {})
            info.update({k: st.get(k) for k in ("last_attempt", "last_success", "last_error", "last_error_at", "consecutive_failures", "event_count", "duration_ms")})
            if not cfg.enabled:
                info["health"] = "disabled"
            elif cfg.id in state["problems"]:
                info["health"] = "not_configured"
                info["last_error"] = state["problems"][cfg.id]
            elif not st.get("last_attempt"):
                info["health"] = "pending"
            elif st.get("consecutive_failures"):
                info["health"] = "failing"
            elif st.get("last_success") and st["last_success"] < ts(now - timedelta(seconds=3 * info["interval"] + 60)):
                info["health"] = "stale"
            else:
                info["health"] = "ok"
            out.append(info)
        reports = store.query_events(status="active", sources=[FIELD_REPORTS])
        out.append(
            {
                "id": FIELD_REPORTS,
                "name": "Field reports",
                "type": "manual",
                "category": "other",
                "interval": None,
                "note": "entered by operators",
                "states": [],
                "meta": {},
                "catalog": False,
                "health": "ok",
                "event_count": len(reports),
            }
        )
        return {"sources": out}

    # --- write API -----------------------------------------------------------------------------------

    @app.post("/api/sources/{source_id}/refresh", dependencies=[Depends(require_token)])
    async def refresh_source(source_id: str) -> dict[str, Any]:
        scheduler: Scheduler | None = state["scheduler"]
        if scheduler is None or source_id not in scheduler.sources:
            raise HTTPException(404, "source not running")
        if start_scheduler:
            scheduler.trigger(source_id)
            return {"queued": True}
        return await scheduler.poll(scheduler.sources[source_id])

    @app.post("/api/reports", status_code=201, dependencies=[Depends(require_token)])
    async def create_report(report: ReportIn) -> dict[str, Any]:
        now = utcnow()
        event = Event(
            id=uuid.uuid4().hex[:12],
            source=FIELD_REPORTS,
            category=report.category,
            title=report.title.strip(),
            severity=report.severity,
            description=report.description,
            area=None,
            geometry=point(report.lon, report.lat),
            starts_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=report.expires_hours),
            metrics={"kind": "field_report", "reporter": report.reporter},
        )
        attribute(event, [])
        store.add_event(event, now)
        notifier.publish({"type": "source", "source": FIELD_REPORTS, "ok": True, "new": 1, "at": ts(now)})
        return _feature(store.get_event(FIELD_REPORTS, event.id))

    @app.post("/api/reports/{report_id}/resolve", dependencies=[Depends(require_token)])
    async def resolve_report(report_id: str) -> dict[str, Any]:
        if not store.end_event(FIELD_REPORTS, report_id):
            raise HTTPException(404, "no active report with that id")
        notifier.publish({"type": "source", "source": FIELD_REPORTS, "ok": True, "ended": 1, "at": ts(utcnow())})
        return {"resolved": report_id}

    # --- live updates --------------------------------------------------------------------------------

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        queue = notifier.subscribe()

        async def events():
            try:
                yield "retry: 5000\n\n"
                while not await request.is_disconnected():
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: update\ndata: {json.dumps(msg)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                notifier.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


BULK_KINDS = ("outage", "outage_cluster")


def _limit_events(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep every non-bulk event (alerts, closures, shelters, totals, county figures) and trim only bulk outage
    points, least important first, so a large outage never pushes other categories off the dashboard."""
    if len(rows) <= limit:
        return rows, {}
    bulk = [r for r in rows if r["metrics"].get("kind") in BULK_KINDS]
    other = [r for r in rows if r["metrics"].get("kind") not in BULK_KINDS]
    if len(other) >= limit:  # extreme case: even non-bulk events exceed the limit (rows are severity-sorted)
        kept = other[:limit]
    else:
        bulk.sort(key=lambda r: (r["severity_rank"], r["metrics"].get("customers_out") or 0), reverse=True)
        kept = other + bulk[: limit - len(other)]
    kept_ids = {r["uid"] for r in kept}
    omitted: dict[str, int] = {}
    for r in rows:
        if r["uid"] not in kept_ids:
            kind = r["metrics"].get("kind") or r["category"]
            omitted[kind] = omitted.get(kind, 0) + 1
    kept.sort(key=lambda r: r["severity_rank"], reverse=True)
    return kept, omitted


def _feature(row: dict[str, Any]) -> dict[str, Any]:
    props = {k: v for k, v in row.items() if k != "geometry"}
    return {"type": "Feature", "id": row["uid"], "geometry": row["geometry"], "properties": props}
