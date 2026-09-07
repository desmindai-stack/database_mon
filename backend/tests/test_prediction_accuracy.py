"""Faz 20 İŞ 2 — tahmin doğruluğu geri besleme döngüsü.

Öncesinde her tahminde bir `confidence` vardı ama bu regresyonun R² değeriydi: "model GEÇMİŞ
veriye ne kadar iyi oturdu" demek. Tahminin tutup tutmadığıyla ilgisi yok — gürültüsüz ama
tamamen yanlış eğimli bir seri de R²=0.99 verir. Yani ürün doğruluk İDDİA ediyordu, ölçmüyordu.

Bu testler döngünün üç adımını da kapatıyor: kayıt, değerlendirme, metrik.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import (
    Instance,
    MetricRollupDaily,
    MetricSample,
    PredictionOutcome,
    SchemaObjectDailySample,
)
from app.services import prediction as pred
from app.services.credentials import encrypt_secret
from app.services.prediction_accuracy import (
    RELIABILITY_MIN_SAMPLES,
    accuracy_by_kind,
    evaluate_due_outcomes,
    record_prediction,
)
from tests.auth_helper import authed_client


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"acc-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


async def _record(session, instance_id: int, **over) -> PredictionOutcome:
    kwargs = dict(
        prediction_id=None, instance_id=instance_id, kind="database_size",
        metric_key="database_size_bytes", target_at=datetime.now(UTC) - timedelta(hours=1),
        checkpoint_days=7.0, predicted_value=100.0, lower_bound=90.0, upper_bound=110.0,
        method="linear_regression+weekday", sample_count=14, span_days=14.0, r_squared=0.9,
        source="rollup",
    )
    kwargs.update(over)
    outcome = record_prediction(session, **kwargs)
    await session.commit()
    return outcome


# --- 1. Kayıt --------------------------------------------------------------------------------


async def test_a_generated_prediction_records_what_it_predicted():
    """Tahmin üretilirken ne tahmin ettiği, hangi tarih için ve hangi yöntemle kaydedilmeli —
    sonradan ölçülebilmesi için."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        base = date.today() - timedelta(days=20)
        for i in range(20):
            size = 10_000_000_000 + i * 500_000_000
            session.add(MetricRollupDaily(
                instance_id=instance.id, metric_key="database_size_bytes", day=base + timedelta(days=i),
                avg_value=size, min_value=size, max_value=size, last_value=size, sample_count=10,
            ))
        await session.commit()

        created = await pred._database_size_prediction(session, instance.id)
        await session.commit()
        assert created

        outcomes = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().all()

    assert len(outcomes) == 1, "tahmin kaydedilmedi — ölçülemez"
    row = outcomes[0]
    assert row["kind"] == "database_size"
    assert row["status"] == "pending"
    assert row["prediction_id"] == created[0].id, "tahminle ilişkilendirilmemiş"
    assert row["sample_count"] == 20 and row["span_days"] > 0, "veri miktarı kaydedilmemiş"
    assert row["method"].startswith("linear_regression"), "yöntem kaydedilmemiş"
    assert row["lower_bound"] is not None and row["upper_bound"] is not None


async def test_the_checkpoint_horizon_is_short_enough_to_actually_measure():
    """Manşet ufuk 180 gün; o tarihi beklemek altı ay geri besleme almamak demekti. Aynı
    modelden kısa bir kontrol noktası alınıp o ölçülüyor."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        base = date.today() - timedelta(days=20)
        for i in range(20):
            size = 10_000_000_000 + i * 500_000_000
            session.add(MetricRollupDaily(
                instance_id=instance.id, metric_key="database_size_bytes", day=base + timedelta(days=i),
                avg_value=size, min_value=size, max_value=size, last_value=size, sample_count=10,
            ))
        await session.commit()
        await pred._database_size_prediction(session, instance.id)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert row["checkpoint_days"] <= 14, "kontrol noktası ölçülemeyecek kadar uzak"


# --- 2. Değerlendirme ------------------------------------------------------------------------


async def test_an_accurate_prediction_is_scored_as_within_interval():
    async with SessionLocal() as session:
        instance = await _instance(session)
        target = datetime.now(UTC) - timedelta(hours=2)
        await _record(session, instance.id, target_at=target, predicted_value=100.0,
                      lower_bound=90.0, upper_bound=110.0)
        session.add(MetricRollupDaily(
            instance_id=instance.id, metric_key="database_size_bytes", day=target.date(),
            avg_value=102.0, min_value=102.0, max_value=102.0, last_value=102.0, sample_count=5,
        ))
        await session.commit()

        counts = await evaluate_due_outcomes(session)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert counts["evaluated"] == 1
    assert row["status"] == "evaluated"
    assert row["actual_value"] == 102.0
    assert row["absolute_error"] == pytest.approx(2.0)
    assert row["percent_error"] == pytest.approx(2 / 102 * 100)
    assert row["within_interval"] is True


async def test_a_prediction_that_missed_its_interval_is_recorded_as_a_miss():
    """Ölçüm ancak yanlışı da kaydediyorsa anlamlıdır."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        target = datetime.now(UTC) - timedelta(hours=2)
        await _record(session, instance.id, target_at=target, predicted_value=100.0,
                      lower_bound=90.0, upper_bound=110.0)
        session.add(MetricRollupDaily(
            instance_id=instance.id, metric_key="database_size_bytes", day=target.date(),
            avg_value=300.0, min_value=300.0, max_value=300.0, last_value=300.0, sample_count=5,
        ))
        await session.commit()
        await evaluate_due_outcomes(session)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert row["within_interval"] is False
    assert row["absolute_error"] == pytest.approx(200.0)


async def test_a_missing_actual_is_unevaluable_not_a_wrong_prediction():
    """Toplama durmuşsa/nesne silinmişse modeli cezalandırmak yanlış olurdu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _record(session, instance.id, target_at=datetime.now(UTC) - timedelta(days=5))
        counts = await evaluate_due_outcomes(session)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert counts["expired"] == 1
    assert row["status"] == "expired"
    assert row["within_interval"] is None, "ölçülemeyen tahmin isabetsiz sayılmamalı"
    assert row["unevaluable_reason"], "neden ölçülemediği yazılmamış"


async def test_a_recently_due_prediction_waits_for_its_grace_period():
    """Günlük rollup bir tur gecikmiş olabilir; hemen 'ölçülemedi' demek erken.

    Test veritabanı paylaşımlı olduğu için global sayaca değil KENDİ satırının durumuna
    bakılıyor — `evaluate_due_outcomes` bütün bekleyen satırları işler.
    """
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _record(session, instance.id, target_at=datetime.now(UTC) - timedelta(hours=1))
        await evaluate_due_outcomes(session)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert row["status"] == "pending", "hedefi yeni geçmiş satır hemen ölçülemedi sayılmamalı"
    assert row["unevaluable_reason"] is None


async def test_short_horizon_outcomes_read_the_raw_sample_nearest_the_target():
    async with SessionLocal() as session:
        instance = await _instance(session)
        target = datetime.now(UTC) - timedelta(minutes=40)
        await _record(
            session, instance.id, kind="connection_trend", metric_key="active_connections",
            target_at=target, source="sample", predicted_value=50.0,
            lower_bound=40.0, upper_bound=60.0, checkpoint_days=1 / 24,
        )
        # Hedefe 2 dakika uzak örnek kabul edilmeli; 3 saat uzaktaki kullanılmamalı.
        for offset_min, value in ((2, 55), (180, 999)):
            session.add(MetricSample(
                instance_id=instance.id, collected_at=target - timedelta(minutes=offset_min),
                active_connections=value, max_connections=100, cache_hit_ratio=99.0,
            ))
        await session.commit()
        await evaluate_due_outcomes(session)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert row["actual_value"] == 55.0, "hedefe en yakın örnek kullanılmadı"
    assert row["within_interval"] is True


async def test_schema_object_outcomes_read_that_object_only():
    async with SessionLocal() as session:
        instance = await _instance(session)
        target = datetime.now(UTC) - timedelta(days=1)
        await _record(
            session, instance.id, kind="index_bloat", metric_key="index_bloat:public.idx_a",
            target_at=target, source="schema_object", schema_name="public", object_name="idx_a",
            predicted_value=1000.0, lower_bound=900.0, upper_bound=1100.0,
        )
        for name, size in (("idx_a", 1050), ("idx_b", 99999)):
            session.add(SchemaObjectDailySample(
                instance_id=instance.id, day=target.date(), object_kind="index",
                schema_name="public", object_name=name, size_bytes=size,
            ))
        await session.commit()
        await evaluate_due_outcomes(session)
        await session.commit()

        row = (await session.execute(
            PredictionOutcome.__table__.select().where(PredictionOutcome.instance_id == instance.id)
        )).mappings().one()

    assert row["actual_value"] == 1050.0, "yanlış nesnenin değeri okundu"


# --- 3. Metrik ve güvenilirlik ---------------------------------------------------------------


async def _seed_evaluated(session, instance_id: int, kind: str, hits: int, misses: int) -> None:
    now = datetime.now(UTC)
    for i in range(hits + misses):
        within = i < hits
        session.add(PredictionOutcome(
            instance_id=instance_id, kind=kind, metric_key="k",
            created_at=now - timedelta(days=2), target_at=now - timedelta(days=1),
            checkpoint_days=7.0, predicted_value=100.0, lower_bound=90.0, upper_bound=110.0,
            method="linear_regression+weekday", sample_count=14, span_days=14.0, r_squared=0.9,
            source="rollup", status="evaluated", evaluated_at=now - timedelta(hours=1),
            actual_value=105.0 if within else 300.0,
            absolute_error=5.0 if within else 200.0,
            percent_error=5.0 if within else 200.0,
            within_interval=within,
        ))
    await session.commit()


async def test_accuracy_reports_error_and_interval_hit_rate():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_evaluated(session, instance.id, "database_size", hits=8, misses=2)
        stats = await accuracy_by_kind(session, days=30, instance_id=instance.id)

    row = next(s for s in stats if s.kind == "database_size")
    assert row.evaluated_count == 10
    assert row.interval_hit_rate == pytest.approx(0.8)
    assert row.mean_absolute_error == pytest.approx((8 * 5 + 2 * 200) / 10)
    assert row.reliability == "high"
    assert "%80" in row.note


async def test_a_kind_that_keeps_missing_is_marked_low_reliability():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_evaluated(session, instance.id, "table_growth", hits=1, misses=9)
        stats = await accuracy_by_kind(session, days=30, instance_id=instance.id)

    row = next(s for s in stats if s.kind == "table_growth")
    assert row.reliability == "low"
    assert "güvenilir değil" in row.note


async def test_too_few_measurements_is_unknown_not_a_confident_score():
    """Üç ölçümle "%100 isabet" demek, ölçmemekten daha yanıltıcıdır."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_evaluated(session, instance.id, "wraparound", hits=RELIABILITY_MIN_SAMPLES - 2, misses=0)
        stats = await accuracy_by_kind(session, days=30, instance_id=instance.id)

    row = next(s for s in stats if s.kind == "wraparound")
    assert row.reliability == "unknown"
    assert str(RELIABILITY_MIN_SAMPLES) in row.note


async def test_expired_outcomes_do_not_drag_the_score_down():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_evaluated(session, instance.id, "database_size", hits=6, misses=0)
        now = datetime.now(UTC)
        for _ in range(20):
            session.add(PredictionOutcome(
                instance_id=instance.id, kind="database_size", metric_key="k",
                created_at=now - timedelta(days=2), target_at=now - timedelta(days=1),
                checkpoint_days=7.0, predicted_value=100.0, method="m", sample_count=14,
                span_days=14.0, r_squared=0.9, source="rollup", status="expired",
                evaluated_at=now, unevaluable_reason="veri yok",
            ))
        await session.commit()
        stats = await accuracy_by_kind(session, days=30, instance_id=instance.id)

    row = next(s for s in stats if s.kind == "database_size")
    assert row.evaluated_count == 6
    assert row.expired_count == 20
    assert row.interval_hit_rate == pytest.approx(1.0)
    assert row.reliability == "high"


# --- API -------------------------------------------------------------------------------------


async def test_the_accuracy_endpoint_labels_each_kind():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_evaluated(session, instance.id, "index_bloat", hits=7, misses=3)

    async with await authed_client() as c:
        rows = (await c.get(f"/api/predictions/accuracy?instance_id={instance.id}")).json()

    row = next(r for r in rows if r["kind"] == "index_bloat")
    assert row["label"] == "Index şişmesi", "teknik anahtar değil okunur etiket beklenir"
    assert row["interval_hit_rate"] == pytest.approx(0.7)
    assert row["window_days"] == 30


async def test_predictions_carry_their_kinds_measured_reliability():
    """"Bu tahmin türü son 30 günde %X oranında güven aralığı içinde kaldı" — arayüzün
    gösterdiği bilgi buradan geliyor.

    Güvenilirlik TÜR bazında ve instance'lar arası hesaplanıyor: sorulan soru "bu model ne
    kadar tutuyor", "bu instance'ta ne kadar tutuyor" değil. Tek bir instance'ta beş
    tamamlanmış ölçüme ulaşmak haftalar sürerdi ve rozet pratikte hep "bilinmiyor" kalırdı.
    Bu yüzden test mutlak sayı beklemiyor; rozetin doğruluk ucuyla TUTARLI olduğunu
    doğruluyor (aynı kaynaktan beslenmezlerse kullanıcı iki farklı sayı görür).
    """
    from app.models import PredictionInsight

    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_evaluated(session, instance.id, "database_size", hits=9, misses=1)
        session.add(PredictionInsight(
            instance_id=instance.id, metric_key="database_size_bytes",
            created_at=datetime.now(UTC), horizon_minutes=1440,
            current_value=1.0, predicted_value=2.0, threshold=1.0, confidence=0.8,
            severity="warning", message="test",
        ))
        await session.commit()

    async with await authed_client() as c:
        rows = (await c.get("/api/predictions")).json()
        accuracy = (await c.get("/api/predictions/accuracy")).json()

    mine = next(r for r in rows if r["instance_id"] == instance.id)
    overall = next(a for a in accuracy if a["kind"] == "database_size")

    assert mine["reliability"] is not None, "güvenilirlik işareti yok"
    assert mine["reliability"]["level"] == overall["reliability"]
    assert mine["reliability"]["evaluated_count"] == overall["evaluated_count"]
    assert mine["reliability"]["interval_hit_rate"] == pytest.approx(overall["interval_hit_rate"])
    assert mine["reliability"]["evaluated_count"] >= 10, "az önce yazılan ölçümler sayılmamış"


async def test_reliability_is_unknown_when_the_kind_was_never_measured():
    """Hiç ölçülmemiş bir tür için "bilinmiyor" denmeli — sessizce yüksek güven değil."""
    from app.models import PredictionInsight

    async with SessionLocal() as session:
        instance = await _instance(session)
        # Bu testin kendi türü: başka hiçbir test bu metriği tohumlamıyor (paylaşımlı test DB).
        session.add(PredictionInsight(
            instance_id=instance.id, metric_key="cache_hit_ratio",
            created_at=datetime.now(UTC), horizon_minutes=60,
            current_value=1.0, predicted_value=2.0, threshold=1.0, confidence=0.8,
            severity="info", message="test",
        ))
        await session.commit()

    async with await authed_client() as c:
        rows = (await c.get("/api/predictions")).json()
        accuracy = (await c.get("/api/predictions/accuracy")).json()

    assert not any(a["kind"] == "cache_hit_ratio" for a in accuracy), (
        "bu tür hiç ölçülmemiş olmalıydı — test artık izole değil"
    )
    mine = next(r for r in rows if r["instance_id"] == instance.id)
    assert mine["reliability"]["level"] == "unknown"
    assert mine["reliability"]["note"], "neden bilinmediği yazılmalı"
