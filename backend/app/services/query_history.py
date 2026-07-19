from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


def build_query_series(rows: list[Any]) -> list[dict[str, Any]]:
    """Build time series from cumulative SlowQuerySample rows ordered by collected_at."""
    series: list[dict[str, Any]] = []
    prev_calls: int | None = None
    prev_total: float | None = None
    prev_at: datetime | None = None

    for row in rows:
        calls = int(row.calls or 0)
        total = float(row.total_time_ms or 0)
        mean = float(row.mean_time_ms or 0)
        collected_at: datetime = row.collected_at

        calls_delta = None
        total_delta = None
        mean_delta_ms = None
        if prev_calls is not None and prev_total is not None and prev_at is not None:
            # pg_stat_statements reset can drop counters
            if calls >= prev_calls and total >= prev_total:
                calls_delta = calls - prev_calls
                total_delta = total - prev_total
                if calls_delta > 0:
                    mean_delta_ms = total_delta / calls_delta

        series.append(
            {
                "collected_at": collected_at,
                "calls": calls,
                "total_time_ms": total,
                "mean_time_ms": mean,
                "rows": int(row.rows or 0),
                "calls_delta": calls_delta,
                "total_time_delta_ms": total_delta,
                "interval_mean_ms": mean_delta_ms if mean_delta_ms is not None else mean,
            }
        )
        prev_calls = calls
        prev_total = total
        prev_at = collected_at

    return series


def summarize_history(
    queryid: str,
    query_text: str,
    series: list[dict[str, Any]],
) -> dict[str, Any]:
    if not series:
        return {
            "queryid": queryid,
            "query": query_text,
            "points": [],
            "latest_mean_ms": 0.0,
            "latest_calls": 0,
            "max_mean_ms": 0.0,
            "min_mean_ms": 0.0,
            "avg_mean_ms": 0.0,
            "calls_delta_sum": 0,
            "trend_pct": 0.0,
        }

    means = [float(p["interval_mean_ms"] or p["mean_time_ms"] or 0) for p in series]
    latest = series[-1]
    first_half = means[: max(1, len(means) // 2)]
    second_half = means[len(means) // 2 :]
    avg_first = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    trend_pct = ((avg_second - avg_first) / avg_first * 100) if avg_first > 0 else 0.0
    calls_delta_sum = sum(int(p["calls_delta"] or 0) for p in series if p["calls_delta"] is not None)

    return {
        "queryid": queryid,
        "query": query_text,
        "points": series,
        "latest_mean_ms": float(latest["mean_time_ms"] or 0),
        "latest_calls": int(latest["calls"] or 0),
        "max_mean_ms": max(means) if means else 0.0,
        "min_mean_ms": min(means) if means else 0.0,
        "avg_mean_ms": (sum(means) / len(means)) if means else 0.0,
        "calls_delta_sum": calls_delta_sum,
        "trend_pct": round(trend_pct, 2),
    }


def group_rows_by_queryid(rows: list[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        qid = row.queryid or f"hash:{hash(row.query) & 0xFFFFFFFF}"
        grouped[str(qid)].append(row)
    for qid in grouped:
        grouped[qid].sort(key=lambda r: r.collected_at)
    return grouped
