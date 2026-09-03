"""Faz 16 İŞ 6 — tahmin motoru: doğrusal regresyon + güven aralığı + basit mevsimsellik.
Kapalı-form istatistik olduğundan (ML/LLM yok), beklenen çıktı elle hesaplanabilir örneklerle
doğrulanıyor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.forecasting import (
    DataSufficiency,
    SeasonalPoint,
    check_sufficiency,
    forecast_with_seasonality,
    linear_regression_with_ci,
)


def test_linear_regression_recovers_exact_line_with_no_noise():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    ys = [10.0, 12.0, 14.0, 16.0, 18.0]  # y = 10 + 2x, no noise
    result = linear_regression_with_ci(xs, ys, x_new=10.0)
    assert result.point == 30.0  # 10 + 2*10
    assert result.r_squared > 0.99
    # No residual noise -> the prediction interval collapses to (near) the point estimate.
    assert result.upper - result.lower < 0.01


def test_linear_regression_ci_widens_with_noise():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 13.0, 11.0, 16.0, 12.0, 19.0]  # noisy but roughly increasing
    result = linear_regression_with_ci(xs, ys, x_new=5.0)
    assert result.upper > result.lower
    assert result.lower <= result.point <= result.upper


def test_linear_regression_flat_series_has_near_zero_slope():
    xs = [0.0, 1.0, 2.0, 3.0]
    ys = [100.0, 100.0, 100.0, 100.0]
    result = linear_regression_with_ci(xs, ys, x_new=10.0)
    assert abs(result.slope_per_x) < 1e-9
    assert abs(result.point - 100.0) < 1e-9


def test_forecast_falls_back_to_no_seasonality_with_few_points():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    points = [SeasonalPoint(base + timedelta(days=i), 10.0 + i) for i in range(3)]
    result = forecast_with_seasonality(points, base + timedelta(days=10), seasonality="weekday")
    assert result.seasonality == "none"


def test_forecast_detects_weekday_weekend_pattern():
    # Two full weeks: weekday value trends up, weekend value is consistently lower — a real
    # additive seasonal pattern the deseasonalization should pick up on.
    base = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
    points = []
    for day in range(14):
        ts = base + timedelta(days=day)
        is_weekend = ts.weekday() >= 5
        trend = 100.0 + day * 2.0
        value = trend - 15.0 if is_weekend else trend
        points.append(SeasonalPoint(ts, value))
    result = forecast_with_seasonality(points, base + timedelta(days=14), seasonality="weekday")
    assert result.seasonality == "weekday"
    assert result.slope_per_day > 0


def test_check_sufficiency_ready_when_thresholds_met():
    s = check_sufficiency("database_size", have_days=10, have_samples=10)
    assert isinstance(s, DataSufficiency)
    assert s.ready is True
    assert s.days_remaining == 0.0


def test_check_sufficiency_reports_days_remaining_when_not_ready():
    s = check_sufficiency("database_size", have_days=2, have_samples=2)
    assert s.ready is False
    assert s.days_remaining == 5.0
    assert s.need_days == 7
