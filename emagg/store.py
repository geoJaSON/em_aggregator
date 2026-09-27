"""SQLite persistence: current events per source, their lifecycle, and source health."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from emagg.geo import representative_point
from emagg.models import Event, Severity, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    severity_rank INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    area TEXT,
    geometry TEXT,
    lon REAL,
    lat REAL,
    starts_at TEXT,
    updated_at TEXT,
    expires_at TEXT,
    url TEXT,
    metrics TEXT,
    content_hash TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    ended_at TEXT,
    prev_severity TEXT,
    severity_changed_at TEXT,
    baseline INTEGER NOT NULL DEFAULT 0,
    states TEXT,
    fips TEXT,
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS events_ended ON events (ended_at);
CREATE INDEX IF NOT EXISTS events_first_seen ON events (first_seen);
CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    last_attempt TEXT,
    last_success TEXT,
    last_error TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    event_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS kv_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

ACTIVE_SQL = "ended_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"


def ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _hash(event: Event) -> str:
    payload = event.model_dump_json(exclude={"source", "supersedes"})
    return hashlib.sha1(payload.encode()).hexdigest()


@dataclass
class Changes:
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    escalated: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.new or self.updated or self.ended)

    def counts(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in ("new", "updated", "ended", "escalated")}


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(events)")}
        for col in ("states", "fips"):
            if col not in have:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
        self._conn.execute("CREATE INDEX IF NOT EXISTS events_states ON events (states)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- writes ------------------------------------------------------------------------------------

    def replace_source_events(self, source: str, events: Iterable[Event], now: datetime | None = None) -> Changes:
        """Make ``events`` the complete current set for ``source``; anything missing is marked ended."""
        now = now or utcnow()
        now_s = ts(now)
        changes = Changes()
        with self._lock:
            cur = self._conn.cursor()
            first_load = cur.execute(
                "SELECT last_success FROM source_status WHERE source = ?", (source,)
            ).fetchone()
            baseline = 1 if not first_load or not first_load["last_success"] else 0
            existing = {
                r["id"]: r
                for r in cur.execute(
                    "SELECT id, content_hash, severity, severity_rank, first_seen, ended_at, baseline "
                    "FROM events WHERE source = ?",
                    (source,),
                )
            }
            seen: set[str] = set()
            for ev in events:
                if ev.id in seen:
                    continue
                seen.add(ev.id)
                ev.source = source
                h = _hash(ev)
                row = existing.get(ev.id)
                if row is not None and row["ended_at"] is None:
                    if row["content_hash"] == h:
                        cur.execute(
                            "UPDATE events SET last_seen = ? WHERE source = ? AND id = ?", (now_s, source, ev.id)
                        )
                        continue
                    sev_changed = row["severity_rank"] != ev.severity.rank
                    if sev_changed and ev.severity.rank > row["severity_rank"]:
                        changes.escalated.append(ev.id)
                    self._write(
                        cur,
                        ev,
                        h,
                        first_seen=row["first_seen"],
                        now_s=now_s,
                        baseline=row["baseline"],
                        prev_severity=row["severity"] if sev_changed else None,
                        severity_changed_at=now_s if sev_changed else None,
                        keep_severity_change=not sev_changed,
                    )
                    changes.updated.append(ev.id)
                    continue

                # New (or re-activated) event. Inherit history from any earlier version it supersedes.
                first_seen, inherited_baseline = now_s, baseline
                prior = [existing[p] for p in ev.supersedes if p in existing]
                if prior:
                    first_seen = min(p["first_seen"] for p in prior)
                    inherited_baseline = min(p["baseline"] for p in prior)
                    for p in prior:
                        cur.execute("DELETE FROM events WHERE source = ? AND id = ?", (source, p["id"]))
                        seen.add(p["id"])
                    changes.updated.append(ev.id)
                else:
                    changes.new.append(ev.id)
                self._write(cur, ev, h, first_seen=first_seen, now_s=now_s, baseline=inherited_baseline)

            for eid, row in existing.items():
                if eid not in seen and row["ended_at"] is None:
                    cur.execute("UPDATE events SET ended_at = ? WHERE source = ? AND id = ?", (now_s, source, eid))
                    changes.ended.append(eid)
            self._conn.commit()
        return changes

    def _write(
        self,
        cur: sqlite3.Cursor,
        ev: Event,
        h: str,
        *,
        first_seen: str,
        now_s: str,
        baseline: int,
        prev_severity: str | None = None,
        severity_changed_at: str | None = None,
        keep_severity_change: bool = False,
    ) -> None:
        pt = representative_point(ev.geometry)
        values = {
            "source": ev.source,
            "id": ev.id,
            "category": ev.category.value,
            "severity": ev.severity.value,
            "severity_rank": ev.severity.rank,
            "title": ev.title,
            "description": ev.description,
            "area": ev.area,
            "geometry": json.dumps(ev.geometry, separators=(",", ":")) if ev.geometry else None,
            "lon": pt[0] if pt else None,
            "lat": pt[1] if pt else None,
            "starts_at": ts(ev.starts_at),
            "updated_at": ts(ev.updated_at),
            "expires_at": ts(ev.expires_at),
            "url": ev.url,
            "metrics": json.dumps(ev.metrics, default=str, separators=(",", ":")),
            "content_hash": h,
            "first_seen": first_seen,
            "last_seen": now_s,
            "ended_at": None,
            "prev_severity": prev_severity,
            "severity_changed_at": severity_changed_at,
            "baseline": baseline,
            "states": "," + ",".join(ev.states) + "," if ev.states else None,
            "fips": ev.fips,
        }
        if keep_severity_change:
            # Content changed but severity did not: preserve the last recorded severity transition.
            old = cur.execute(
                "SELECT prev_severity, severity_changed_at FROM events WHERE source = ? AND id = ?",
                (ev.source, ev.id),
            ).fetchone()
            if old:
                values["prev_severity"] = old["prev_severity"]
                values["severity_changed_at"] = old["severity_changed_at"]
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur.execute(f"INSERT OR REPLACE INTO events ({cols}) VALUES ({marks})", list(values.values()))

    def add_event(self, event: Event, now: datetime | None = None) -> None:
        """Insert a single event without touching the rest of its source (used for field reports)."""
        now_s = ts(now or utcnow())
        with self._lock:
            cur = self._conn.cursor()
            self._write(cur, event, _hash(event), first_seen=now_s, now_s=now_s, baseline=0)
            self._conn.commit()

    def end_event(self, source: str, event_id: str, now: datetime | None = None) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE events SET ended_at = ? WHERE source = ? AND id = ? AND ended_at IS NULL",
                (ts(now or utcnow()), source, event_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def prune(self, retention_hours: int, now: datetime | None = None) -> int:
        cutoff = ts((now or utcnow()) - timedelta(hours=retention_hours))
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM events WHERE (ended_at IS NOT NULL AND ended_at < ?) "
                "OR (ended_at IS NULL AND expires_at IS NOT NULL AND expires_at < ?)",
                (cutoff, cutoff),
            )
            self._conn.execute("DELETE FROM kv_cache WHERE updated_at < ?", (ts((now or utcnow()) - timedelta(days=30)),))
            self._conn.commit()
            return cur.rowcount

    # --- reads -------------------------------------------------------------------------------------

    def get_event(self, source: str, event_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE source = ? AND id = ?", (source, event_id)).fetchone()
        return _row(row, now or utcnow()) if row else None

    def query_events(
        self,
        *,
        status: str = "active",
        categories: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        min_severity: Severity | None = None,
        since: datetime | None = None,
        states: Iterable[str] | None = None,
        now: datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        now = now or utcnow()
        where, args = [], []
        _state_filter(states, where, args)
        if status == "active":
            where.append(ACTIVE_SQL)
            args.append(ts(now))
        elif status == "ended":
            where.append(f"NOT ({ACTIVE_SQL})")
            args.append(ts(now))
        if categories:
            cats = list(categories)
            where.append(f"category IN ({','.join('?' for _ in cats)})")
            args.extend(cats)
        if sources:
            srcs = list(sources)
            where.append(f"source IN ({','.join('?' for _ in srcs)})")
            args.extend(srcs)
        if min_severity is not None:
            where.append("severity_rank >= ?")
            args.append(min_severity.rank)
        if since is not None:
            where.append("last_seen >= ?")
            args.append(ts(since))
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY severity_rank DESC, COALESCE(updated_at, first_seen) DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row(r, now) for r in rows]

    def timeline(
        self, since: datetime, now: datetime | None = None, limit: int = 200, states: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """What changed since ``since``: new events, severity changes, and things that cleared or expired.

        Events loaded on a source's very first successful poll are "baseline" and not reported as new.
        """
        now = now or utcnow()
        s, n = ts(since), ts(now)
        queries = [
            ("new", "first_seen", "first_seen >= ? AND baseline = 0", (s,)),
            ("severity", "severity_changed_at", "severity_changed_at >= ? AND prev_severity IS NOT NULL", (s,)),
            ("ended", "ended_at", "ended_at >= ?", (s,)),
            ("expired", "expires_at", "ended_at IS NULL AND expires_at >= ? AND expires_at <= ?", (s, n)),
        ]
        extra, extra_args = [], []
        _state_filter(states, extra, extra_args)
        state_sql = f" AND {extra[0]}" if extra else ""
        out = []
        with self._lock:
            for change, col, cond, args in queries:
                sql = f"SELECT * FROM events WHERE {cond}{state_sql} ORDER BY {col} DESC LIMIT ?"
                for r in self._conn.execute(sql, (*args, *extra_args, int(limit))).fetchall():
                    d = _row(r, now)
                    if change == "severity":
                        prev = Severity(r["prev_severity"]).rank
                        change_name = "escalated" if r["severity_rank"] > prev else "deescalated"
                    else:
                        change_name = change
                    d["change"] = change_name
                    d["change_at"] = r[col]
                    out.append(d)
        out.sort(key=lambda d: d["change_at"], reverse=True)
        return out[:limit]

    # --- source health -----------------------------------------------------------------------------

    def record_poll(
        self,
        source: str,
        *,
        ok: bool,
        now: datetime | None = None,
        error: str | None = None,
        event_count: int = 0,
        duration_ms: int | None = None,
    ) -> None:
        now_s = ts(now or utcnow())
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_status (source) VALUES (?) ON CONFLICT(source) DO NOTHING", (source,)
            )
            if ok:
                self._conn.execute(
                    "UPDATE source_status SET last_attempt = ?, last_success = ?, consecutive_failures = 0, "
                    "event_count = ?, duration_ms = ? WHERE source = ?",
                    (now_s, now_s, event_count, duration_ms, source),
                )
            else:
                self._conn.execute(
                    "UPDATE source_status SET last_attempt = ?, last_error = ?, last_error_at = ?, "
                    "consecutive_failures = consecutive_failures + 1, duration_ms = ? WHERE source = ?",
                    (now_s, (error or "unknown error")[:2000], now_s, duration_ms, source),
                )
            self._conn.commit()

    def statuses(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM source_status").fetchall()
        return {r["source"]: dict(r) for r in rows}

    # --- small key/value cache (e.g. NWS zone outlines, Kubra service areas) -------------------------

    def kv_get(self, key: str, max_age: timedelta | None = None, now: datetime | None = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value, updated_at FROM kv_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        if max_age is not None and row["updated_at"] < ts((now or utcnow()) - max_age):
            return None
        return json.loads(row["value"])

    def kv_set(self, key: str, value: Any, now: datetime | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv_cache (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, separators=(",", ":")), ts(now or utcnow())),
            )
            self._conn.commit()


def _state_filter(states: Iterable[str] | None, where: list[str], args: list[Any]) -> None:
    codes = [s.upper() for s in states or [] if s]
    if codes:
        where.append("(" + " OR ".join("states LIKE ?" for _ in codes) + ")")
        args.extend(f"%,{c},%" for c in codes)


def _row(r: sqlite3.Row, now: datetime) -> dict[str, Any]:
    now_s = ts(now)
    active = r["ended_at"] is None and (r["expires_at"] is None or r["expires_at"] > now_s)
    return {
        "uid": f"{r['source']}:{r['id']}",
        "source": r["source"],
        "id": r["id"],
        "category": r["category"],
        "severity": r["severity"],
        "severity_rank": r["severity_rank"],
        "title": r["title"],
        "description": r["description"],
        "area": r["area"],
        "geometry": json.loads(r["geometry"]) if r["geometry"] else None,
        "lon": r["lon"],
        "lat": r["lat"],
        "starts_at": r["starts_at"],
        "updated_at": r["updated_at"],
        "expires_at": r["expires_at"],
        "url": r["url"],
        "metrics": json.loads(r["metrics"]) if r["metrics"] else {},
        "first_seen": r["first_seen"],
        "last_seen": r["last_seen"],
        "ended_at": r["ended_at"],
        "prev_severity": r["prev_severity"],
        "severity_changed_at": r["severity_changed_at"],
        "baseline": bool(r["baseline"]),
        "states": [s for s in (r["states"] or "").split(",") if s],
        "fips": r["fips"],
        "active": active,
    }
