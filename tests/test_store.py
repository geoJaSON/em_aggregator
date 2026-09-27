from datetime import timedelta

from emagg.geo import point
from emagg.models import Category, Event, Severity
from emagg.store import Store


def ev(eid, severity=Severity.minor, title=None, **kw):
    return Event(id=eid, category=Category.flood, title=title or f"event {eid}", severity=severity,
                 geometry=point(-95, 29), **kw)


def poll(store, events, now):
    changes = store.replace_source_events("src", events, now)
    store.record_poll("src", ok=True, now=now, event_count=len(events))
    return changes


def test_lifecycle_and_timeline(now):
    store = Store()
    c = poll(store, [ev("a"), ev("b")], now)
    assert c.new == ["a", "b"]
    # First successful load is the baseline: shown as active, but not announced as "new".
    assert store.timeline(now - timedelta(hours=1), now=now) == []
    assert {e["id"] for e in store.query_events(now=now)} == {"a", "b"}

    t1 = now + timedelta(minutes=5)
    c = poll(store, [ev("a", Severity.severe), ev("c")], t1)
    assert (c.new, c.updated, c.ended, c.escalated) == (["c"], ["a"], ["b"], ["a"])
    changes = {(r["id"], r["change"]) for r in store.timeline(now, now=t1)}
    assert changes == {("c", "new"), ("a", "escalated"), ("b", "ended")}
    a = store.get_event("src", "a", now=t1)
    assert a["prev_severity"] == "minor" and a["first_seen"] == now.isoformat()

    # An unchanged poll only bumps last_seen.
    c = poll(store, [ev("a", Severity.severe), ev("c")], t1 + timedelta(minutes=1))
    assert not c

    # Content change without a severity change keeps the recorded escalation.
    poll(store, [ev("a", Severity.severe, title="renamed"), ev("c")], t1 + timedelta(minutes=2))
    assert store.get_event("src", "a")["prev_severity"] == "minor"


def test_reactivated_event_counts_as_new(now):
    store = Store()
    poll(store, [ev("a")], now)
    poll(store, [], now + timedelta(minutes=1))
    c = poll(store, [ev("a")], now + timedelta(minutes=2))
    assert c.new == ["a"]
    assert store.get_event("src", "a")["ended_at"] is None


def test_supersedes_inherits_first_seen(now):
    store = Store()
    poll(store, [ev("v1")], now)
    later = now + timedelta(minutes=10)
    c = poll(store, [ev("v2", supersedes=["v1"])], later)
    assert c.new == [] and c.updated == ["v2"] and c.ended == []
    assert store.get_event("src", "v1") is None
    v2 = store.get_event("src", "v2")
    assert v2["first_seen"] == now.isoformat() and v2["baseline"]


def test_expiry_and_filters(now):
    store = Store()
    poll(store, [ev("old", expires_at=now - timedelta(minutes=1)), ev("live", Severity.extreme, expires_at=now + timedelta(hours=1))], now)
    assert [e["id"] for e in store.query_events(now=now)] == ["live"]
    assert [e["id"] for e in store.query_events(status="ended", now=now)] == ["old"]
    assert store.query_events(min_severity=Severity.extreme, now=now)[0]["id"] == "live"
    assert store.query_events(categories=["power"], now=now) == []


def test_single_event_add_and_end(now):
    store = Store()
    store.add_event(Event(id="r1", source="field_reports", category=Category.comms, title="No cell service"), now)
    assert store.query_events(sources=["field_reports"], now=now)[0]["title"] == "No cell service"
    assert store.end_event("field_reports", "r1", now)
    assert not store.end_event("field_reports", "r1", now)
    assert store.query_events(sources=["field_reports"], now=now) == []


def test_prune_and_status(now, tmp_path):
    store = Store(str(tmp_path / "x.sqlite"))
    poll(store, [ev("a")], now)
    poll(store, [], now + timedelta(minutes=1))
    assert store.prune(72, now=now + timedelta(hours=73)) == 1
    store.record_poll("src", ok=False, now=now, error="boom")
    st = store.statuses()["src"]
    assert st["consecutive_failures"] == 1 and st["last_error"] == "boom"


def test_kv_cache(now):
    store = Store()
    store.kv_set("k", {"a": 1}, now=now)
    assert store.kv_get("k") == {"a": 1}
    assert store.kv_get("k", max_age=timedelta(days=1), now=now + timedelta(days=2)) is None
