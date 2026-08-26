from __future__ import annotations

from app.services import query_cache


def test_get_returns_none_for_missing_key():
    assert query_cache.get(("missing", 1)) is None


def test_set_then_get_returns_the_value():
    query_cache.set(("k", 1), {"a": 1}, ttl_seconds=60)
    assert query_cache.get(("k", 1)) == {"a": 1}


def test_expired_entry_returns_none_and_is_evicted(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(query_cache.time, "monotonic", lambda: t[0])

    query_cache.set(("k", 2), "value", ttl_seconds=10)
    assert query_cache.get(("k", 2)) == "value"

    t[0] += 11  # past the 10s TTL
    assert query_cache.get(("k", 2)) is None
    # Second read confirms it was actually evicted, not just expired-but-present.
    assert ("k", 2) not in query_cache._store
