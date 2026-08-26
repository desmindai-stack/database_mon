"""Small in-memory TTL cache for expensive, on-demand, user-triggered probes against a
monitored instance (EXPLAIN, index advice) — avoids re-running an expensive or
execution-triggering operation (EXPLAIN ANALYZE actually runs the query; index advice does
catalog scans and sometimes a hypopg re-plan) on every accidental double click or component
re-render within the TTL window.

Deliberately process-local and not persisted — these are point-in-time diagnostics tied to
whatever the target's data/indexes looked like at request time, not something that needs to
survive a restart or be shared across API workers.
"""

from __future__ import annotations

import time
from typing import Any, Hashable

_DEFAULT_TTL_SECONDS = 300.0

_store: dict[Hashable, tuple[float, Any]] = {}


def get(key: Hashable) -> Any | None:
    entry = _store.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() > expires_at:
        _store.pop(key, None)
        return None
    return value


def set(key: Hashable, value: Any, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
    _store[key] = (time.monotonic() + ttl_seconds, value)
