"""Faz 20 İŞ 3 — tahmin kalitesi.

Altı kural: aralık göster, yetersiz veriyle tahmin üretme, aykırı değerleri ele, doğrusal
olmayan büyümeyi söyle, mevsimselliği ancak geçerliyse uygula, yöntemi göster.

Bu turda iki gerçek hata bulundu ve düzeltildi:

**A. Saat bazlı mevsimsellik trendi yutuyordu.** Bir günden kısa bir seride her saat kovası
bir kez görülür; "o saatin sapması" ile "o ana kadarki artış" aynı şey olur. Mevsimsel bileşen
çıkarıldığında geriye düz bir seri kalıyor, eğim sıfıra iniyor ve tahmin ŞU ANKİ DEĞERİN
ALTINA düşüyordu — yükselen bir bağlantı serisi için düşüş öngörülüyordu.

**B. İlan edilen veri gereksinimi uygulanmıyordu.** `PREDICTION_REQUIREMENTS` 40 örnek / 0.5
gün diyor ve hazırlık paneli bunu gösteriyordu, ama `_short_horizon_predictions` içinde çıplak
bir `len(points) < 5` vardı: panel "bekleniyor" derken tahmin çoktan üretilmiş oluyordu.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, MetricRollupDaily, MetricSample
from app.services import prediction as pred
from app.services.credentials import encrypt_secret
from app.services.forecasting import (
    PREDICTION_REQUIREMENTS,
    SeasonalPoint,
    assess_fit,
    detect_outliers,
    eta_days_range,
    forecast_with_seasonality,
)

BASE = datetime(2026, 1, 5, tzinfo=UTC)  # pazartesi


def _daily(values: list[float], start: datetime = BASE) -> list[SeasonalPoint]:
    return [SeasonalPoint(start + timedelta(days=i), v) for i, v in enumerate(values)]


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"qua-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


# --- Aykırı değerler ------------------------------------------------------------------------


def test_a_one_off_spike_is_excluded_from_the_trend():
    """Tek günlük bir yedek alma sıçraması eğimi olduğundan dik gösterip yanlış aciliyet üretir."""
    values = [100.0 + i * 10 for i in range(20)]
    clean = forecast_with_seasonality(_daily(values), BASE + timedelta(days=27))
    values[7] = 900.0  # tek seferlik sıçrama
    spiked = forecast_with_seasonality(_daily(values), BASE + timedelta(days=27))

    assert spiked.outliers_removed == 1, "sıçrama yakalanmadı"
    # Temizlik sonrası eğim gerçek eğime yakın kalmalı.
    assert spiked.slope_per_day == pytest.approx(clean.slope_per_day, rel=0.15)


def test_outliers_are_measured_against_the_trend_not_the_average():
    """Büyüyen bir seride değerler zaten geniş bir aralığa yayılır; ortalamadan uzaklık orada
    aykırılık DEĞİLDİR. Düzgün büyüyen bir seride hiçbir nokta atılmamalı."""
    flags = detect_outliers([float(i) for i in range(20)], [100.0 + i * 50 for i in range(20)])
    assert not any(flags)


def test_a_series_that_is_mostly_jumps_keeps_all_its_points():
    """Çok fazla nokta aykırı çıkıyorsa sorun tek bir sıçrama değil, modelin yanlış olmasıdır —
    o durumda veriyi kırpmak "veriyi tahmine uydurmak" olur."""
    random.seed(3)
    values = [100.0 + random.choice([0, 500]) for _ in range(20)]
    flags = detect_outliers([float(i) for i in range(20)], values)
    assert not any(flags)


# --- Doğrusal olmayan büyüme ----------------------------------------------------------------


@pytest.mark.parametrize(
    "label,values,expected",
    [
        ("doğrusal", [100.0 + i * 10 for i in range(20)], "linear"),
        ("üstel", [100.0 * (1.35**i) for i in range(20)], "exponential"),
        ("eğri", [100.0 + 60 * (i**0.45) for i in range(20)], "curved"),
        ("sabit", [500.0] * 20, "flat"),
    ],
)
def test_the_shape_of_the_data_is_named_not_forced_into_a_line(label, values, expected):
    assert assess_fit([float(i) for i in range(20)], values).kind == expected, label


def test_noise_is_not_reported_as_a_trend():
    random.seed(11)
    values = [100.0 + random.uniform(-60, 60) for _ in range(20)]
    quality = assess_fit([float(i) for i in range(20)], values)
    assert quality.kind == "noisy"
    assert not quality.is_linear


def _fold(text: str) -> str:
    """Türkçe'de Python'un varsayılan büyük/küçük dönüşümü uyuşmaz: "İ".lower() birleşik
    noktalı bir karakter verir, "i" değil. Metin karşılaştırmaları bunu normalleştiriyor."""
    return text.lower().replace("̇", "").replace("ı", "i")


def test_exponential_growth_says_the_linear_estimate_is_optimistic():
    """Sessizce doğru çizmek, tarihi olduğundan geç göstermek demekti."""
    quality = assess_fit([float(i) for i in range(20)], [100.0 * (1.3**i) for i in range(20)])
    assert "üstel" in _fold(quality.note)
    assert "iyimser" in _fold(quality.note)


def test_a_curve_is_not_shredded_by_outlier_removal():
    """Doğrusal bir modele göre bir eğrinin UÇLARI en büyük kalıntıya sahiptir; onları "aykırı"
    diye atmak eğriyi doğru gibi gösterir ve asıl söylenmesi gerekeni gizler."""
    result = forecast_with_seasonality(
        _daily([100.0 + 60 * (i**0.45) for i in range(20)]), BASE + timedelta(days=27)
    )
    assert result.fit is not None and result.fit.kind == "curved"
    assert result.outliers_removed == 0


def test_a_noisy_series_produces_no_prediction():
    """Gürültüden trend uydurmak, olmayan bir sinyali varmış gibi sunmaktır."""
    random.seed(5)
    noisy = forecast_with_seasonality(
        _daily([1000.0 + random.uniform(-800, 800) for _ in range(20)]), BASE + timedelta(days=27)
    )
    assert not pred._fit_is_usable(noisy)


# --- Mevsimsellik ---------------------------------------------------------------------------


def test_hourly_seasonality_is_refused_when_the_pattern_cannot_repeat():
    """BULUNAN HATA: 16 saatlik yükselen bir seride saat deseni trendi yutuyor ve tahmin şu anki
    değerin ALTINA düşüyordu."""
    start = datetime(2026, 3, 2, 6, tzinfo=UTC)
    points = [SeasonalPoint(start + timedelta(minutes=20 * i), 55.0 + i * 0.5) for i in range(50)]
    result = forecast_with_seasonality(points, points[-1].timestamp + timedelta(hours=1), seasonality="hour")

    assert result.seasonality == "none", "tekrar etmeyen desen uygulanmamalı"
    assert result.point > points[-1].value, "yükselen seri için düşüş öngörülüyor"


def test_hourly_seasonality_is_used_once_the_pattern_repeats_across_days():
    start = datetime(2026, 3, 1, tzinfo=UTC)
    points = [SeasonalPoint(start + timedelta(hours=i), 100.0 + i * 0.1) for i in range(120)]
    result = forecast_with_seasonality(points, start + timedelta(hours=121), seasonality="hour")
    assert result.seasonality == "hour"


def test_weekday_seasonality_needs_the_pattern_in_two_different_weeks():
    one_week = forecast_with_seasonality(
        _daily([100.0 + i * 2 for i in range(7)]), BASE + timedelta(days=10)
    )
    assert one_week.seasonality == "none", "tek haftada haftasonu deseni trendden ayrılamaz"


# --- Aralık ---------------------------------------------------------------------------------


def test_the_eta_is_a_range_not_a_single_date():
    """"~52 gün" demek, sahip olmadığımız bir kesinliği iddia etmektir."""
    result = forecast_with_seasonality(
        _daily([100.0 + i * 10 + random.Random(2).uniform(-8, 8) for i in range(20)]),
        BASE + timedelta(days=40),
    )
    earliest, latest = eta_days_range(500.0, result)
    assert earliest is not None and latest is not None
    assert earliest < latest, "aralık tek noktaya çökmüş"
    assert earliest > 0


def test_an_uncertain_slope_leaves_the_far_end_open_instead_of_inventing_one():
    """Eğimin alt sınırı sıfırın altındaysa eşiğe hiç ulaşılmayabilir — uydurma bir üst sınır
    yazmaktansa "belirsiz" demek doğru."""
    random.seed(9)
    result = forecast_with_seasonality(
        _daily([100.0 + i * 2 + random.uniform(-40, 40) for i in range(20)]), BASE + timedelta(days=40)
    )
    earliest, latest = eta_days_range(500.0, result)
    assert latest is None or latest > earliest


def test_prediction_messages_carry_the_range():
    """Kullanıcının gördüğü metin de aralık içermeli."""
    text = pred._eta_text(45.0, 60.0, 52.0)
    assert "45-60 gün arası" in text
    assert pred._eta_text(30.0, None, 30.0).startswith("en erken")


# --- Veri yeterliliği -----------------------------------------------------------------------


async def test_short_horizon_predictions_enforce_the_declared_requirement():
    """BULUNAN HATA: hazırlık paneli ilan edilen gereksinimi gösterirken tahmin üretimi çıplak
    bir `len(points) < 5` kullanıyordu — panel "bekleniyor" derken tahmin çıkıyordu."""
    req = PREDICTION_REQUIREMENTS["connection_trend"]
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        # Gereksinimin ALTINDA: 6 örnek, 90 saniye.
        for i in range(6):
            session.add(MetricSample(
                instance_id=instance.id, collected_at=now - timedelta(seconds=15 * (6 - i)),
                metrics_json={"connection_utilization_pct": 60.0 + i * 4},
            ))
        await session.commit()

        created = await pred.run_predictions(
            session, instance.id, {"connection_utilization_pct": 84.0}, engine="postgresql"
        )

    assert created == [], f"gereksinim {req.min_samples} örnek / {req.min_days} gün, ama tahmin üretildi"


async def test_short_horizon_predictions_fire_once_the_requirement_is_met():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        count, interval = 50, 1200  # ~16.6 saat
        # Eşiği (%85) net aşan bir tırmanış: 55 → 84.4, saatte ~1.8 puan.
        for i in range(count):
            session.add(MetricSample(
                instance_id=instance.id,
                collected_at=now - timedelta(seconds=interval * (count - i)),
                metrics_json={"connection_utilization_pct": 55.0 + i * 0.6},
            ))
        await session.commit()

        created = await pred.run_predictions(
            session, instance.id, {"connection_utilization_pct": 84.5}, engine="postgresql"
        )

    assert len(created) == 1
    assert created[0].metric_key == "connection_utilization_pct"


# --- Yöntem şeffaflığı ----------------------------------------------------------------------


async def test_every_prediction_records_how_it_was_produced():
    """Kara kutu olmasın: hangi model, kaç ölçüm, hangi dönem, verinin şekli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        start = date.today() - timedelta(days=20)
        for i in range(20):
            size = 10_000_000_000 + i * 500_000_000
            session.add(MetricRollupDaily(
                instance_id=instance.id, metric_key="database_size_bytes", day=start + timedelta(days=i),
                avg_value=size, min_value=size, max_value=size, last_value=size, sample_count=10,
            ))
        await session.commit()
        created = await pred._database_size_prediction(session, instance.id)
        await session.commit()

    assert created
    row = created[0]
    assert row.method and "regresyon" in row.method
    assert row.sample_count == 20
    assert row.span_days and row.span_days > 0
    assert row.fit_kind == "linear"
    assert row.fit_note
    assert row.eta_days_min is not None and row.eta_days_max is not None
    assert row.eta_days_min < row.eta_days_max, "tarih tek noktaya çökmüş"


def test_the_method_label_names_what_was_actually_applied():
    values = [100.0 + i * 10 for i in range(20)]
    values[7] = 900.0
    result = forecast_with_seasonality(_daily(values), BASE + timedelta(days=27))
    assert "doğrusal regresyon" in result.method
    assert "aykırı" in result.method, "atılan ölçümler yöntem etiketinde görünmeli"
