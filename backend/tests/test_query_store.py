"""Plan regresyonu tespitinin kuralları (Faz 31 Commit 9, madde 4) — sunucusuz, sentetik veriyle.

Canlı karşılığı `tests/test_query_store_live_mssql.py` (gerçek SQL Server, gerçek regresyon). Burada kuralın
kendisi sınanıyor: hangi durum regresyon SAYILIR, hangisi sayılmaz.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.query_store import MIN_AVG_DURATION_MS, MIN_EXECUTIONS, detect_regressions

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def row(plan_id: int, avg_ms: float, *, minutes: int, executions: int = MIN_EXECUTIONS, text_id: int = 1,
        forced: bool = False) -> dict:
    return {
        "query_text_id": text_id, "query_id": text_id, "plan_id": plan_id, "is_forced_plan": forced,
        "executions": executions, "avg_duration_ms": avg_ms, "avg_cpu_ms": avg_ms * 0.8,
        "avg_logical_reads": 100.0, "first_seen": NOW - timedelta(minutes=minutes + 5),
        "last_seen": NOW - timedelta(minutes=minutes), "query_text": "SELECT 1 FROM orders WHERE status = @p1",
    }


def test_new_plan_slower_than_the_old_one_is_a_regression():
    regressions, with_history, plans = detect_regressions([row(1, 20.0, minutes=60), row(2, 164.0, minutes=1)])
    assert plans == 2 and with_history == 1
    assert len(regressions) == 1
    regression = regressions[0]
    assert regression.current.plan_id == 2 and regression.baseline.plan_id == 1
    assert regression.slowdown_factor == 8.2


def test_new_plan_faster_is_not_a_regression():
    regressions, with_history, _ = detect_regressions([row(1, 164.0, minutes=60), row(2, 20.0, minutes=1)])
    assert with_history == 1 and regressions == []


def test_small_difference_is_not_a_regression():
    regressions, _, _ = detect_regressions([row(1, 20.0, minutes=60), row(2, 24.0, minutes=1)])
    assert regressions == []


def test_rarely_executed_or_too_fast_plans_are_ignored():
    """Tek çalıştırmalık ya da mikro saniyelik farklar gürültü — kat farkı anlamlı değil."""
    rare = detect_regressions([row(1, 20.0, minutes=60, executions=MIN_EXECUTIONS - 1),
                               row(2, 200.0, minutes=1, executions=MIN_EXECUTIONS - 1)])
    fast = detect_regressions([row(1, MIN_AVG_DURATION_MS / 4, minutes=60),
                               row(2, MIN_AVG_DURATION_MS / 2, minutes=1)])
    assert rare[0] == [] and rare[1] == 0
    assert fast[0] == [] and fast[1] == 0


def test_baseline_is_the_best_earlier_plan_not_the_previous_one():
    regressions, _, _ = detect_regressions([
        row(1, 20.0, minutes=180), row(2, 60.0, minutes=120), row(3, 150.0, minutes=1),
    ])
    assert regressions[0].baseline.plan_id == 1 and regressions[0].slowdown_factor == 7.5


def test_plans_of_different_queries_are_not_compared():
    regressions, with_history, plans = detect_regressions([
        row(1, 20.0, minutes=60, text_id=1), row(2, 200.0, minutes=1, text_id=2),
    ])
    assert plans == 2 and with_history == 0 and regressions == []


def test_forced_plan_that_got_slower_is_still_reported():
    """Zorlanmış planın kötüye gitmesi DBA'nın bilmesi gereken bir şey — gizlenmiyor."""
    regressions, _, _ = detect_regressions([row(1, 20.0, minutes=60), row(2, 100.0, minutes=1, forced=True)])
    assert len(regressions) == 1 and regressions[0].current.is_forced is True
