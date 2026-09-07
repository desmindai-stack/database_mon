"""Faz 20 İŞ 1 — tahminlerin beş parçalı standart önerisi.

Bulunan hata: `PredictionOut.advice` Faz 17'de şemaya eklenmişti ama `PredictionInsight`
modelinde karşılığı yoktu, dolayısıyla API HER tahmin için `advice: null` dönüyordu. Arayüz de
bu yüzden standart öneri kartı yerine tek cümlelik `recommendation` metnine düşüyordu —
kullanıcının gördüğü "öneri var ama çalıştırılacak komut yok" tam olarak buydu.

Bu testler önerinin gerçekten üretildiğini, beş parçanın da dolu olduğunu ve komutların
kopyalanabilir/çalıştırılabilir biçimde geldiğini doğruluyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, MetricRollupDaily, PredictionInsight, SchemaObjectDailySample
from app.services import prediction as pred
from app.services.credentials import encrypt_secret
from app.services.prediction_advice import (
    cache_hit_advice,
    connection_advice,
    database_size_advice,
    index_bloat_advice,
    replication_lag_advice,
    short_horizon_advice,
    table_growth_advice,
    throughput_advice,
    wraparound_advice,
)

# Görevin adlandırdığı beş tahmin türü.
FIVE_KINDS = ("database_size", "connection_trend", "table_growth", "wraparound", "index_bloat")


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"adv-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _advice_for(kind: str):
    if kind == "database_size":
        return database_size_advice(
            current_human="12.0 GB", per_day_human="500.0 MB",
            doubling_date="2026-11-01", horizon_label="24 gün içinde",
        )
    if kind == "connection_trend":
        return connection_advice(engine="postgresql", current=70, predicted=95, horizon_minutes=60)
    if kind == "table_growth":
        return table_growth_advice(
            schema_name="public", table_name="olaylar", per_day_human="200.0 MB",
            horizon_label="30 gün sonra", predicted_human="18.0 GB",
        )
    if kind == "wraparound":
        return wraparound_advice(current_age=150_000_000, freeze_max_age=200_000_000, eta_date="2026-10-15")
    return index_bloat_advice(
        schema_name="public", index_name="olaylar_ts_idx", per_day_human="30.0 MB",
        horizon_label="30 gün sonra", predicted_human="2.0 GB",
    )


# --- Beş parçanın hepsi dolu mu ------------------------------------------------------------


@pytest.mark.parametrize("kind", FIVE_KINDS)
def test_every_prediction_kind_has_the_full_five_part_advice(kind: str):
    advice = _advice_for(kind)

    assert advice.title and not advice.title.lower().startswith("öneri:"), (
        "başlık kısa bir EYLEM olmalı; 'Öneri:' ön eki gösterim katmanında ekleniyor"
    )
    assert advice.why, "neden (iş etkisi) boş"
    assert advice.steps, "adım yok"
    assert advice.cautions, "dikkat notu yok"
    assert advice.verification, "doğrulama sorgusu yok"
    assert advice.rollback, "geri alma anlatılmamış"
    assert advice.estimated_duration, "tahmini süre yok"
    assert advice.unavailable_reason is None
    assert advice.is_actionable


@pytest.mark.parametrize("kind", FIVE_KINDS)
def test_every_kind_ships_runnable_commands(kind: str):
    """Şikâyetin özü buydu: öneri metni vardı, çalıştırılacak komut yoktu."""
    advice = _advice_for(kind)
    commands = [s.command for s in advice.steps if s.command]

    assert len(commands) >= 2, f"{kind}: en az iki adımda komut bekleniyor, {len(commands)} var"
    for command in commands:
        assert command.strip(), "boş komut"
        # Yalnız yer tutucudan ibaret bir "komut" kopyalanıp çalıştırılamaz.
        assert command.strip() not in ("<sema>.<tablo>", "..."), f"{kind}: komut değil, yer tutucu"


@pytest.mark.parametrize("kind", FIVE_KINDS)
def test_why_explains_the_business_impact_not_just_the_measurement(kind: str):
    """"Neden" bölümü ölçümü tekrarlamamalı, YAPILMAZSA NE OLUR'u söylemeli."""
    why = _advice_for(kind).why.lower()
    consequence_words = (
        "durdurur", "reddeder", "durması", "hata verir", "yavaşla", "artar", "riske",
        "bozar", "kaybedilecek", "uzar", "maliyet",
    )
    assert any(w in why for w in consequence_words), f"{kind}: sonuç anlatılmamış — {why[:120]}"


@pytest.mark.parametrize("kind", FIVE_KINDS)
def test_destructive_steps_carry_an_explicit_caution(kind: str):
    """Kilitleyen/yıkıcı bir komut varsa dikkat notlarında karşılığı olmalı."""
    advice = _advice_for(kind)
    # Türkçe'de büyük/küçük harf dönüşümü Python'un varsayılanıyla uyuşmaz ("i".upper() == "I",
    # "İ" değil) — bu yüzden karşılaştırma küçük harf üzerinden ve noktasız/noktalı i'nin
    # ikisini de kabul ederek yapılıyor.
    def _fold(text: str) -> str:
        return text.lower().replace("ı", "i").replace("İ".lower(), "i")

    body = _fold(" ".join(filter(None, [s.command or "" for s in advice.steps])))
    cautions = _fold(" ".join(advice.cautions))
    if "vacuum full" in body:
        assert "vacuum full" in cautions, f"{kind}: VACUUM FULL uyarısız"
    if "drop index" in body:
        assert "drop" in cautions, f"{kind}: DROP uyarısız"
    if "alter system set max_connections" in body:
        assert "yeniden başlat" in cautions, f"{kind}: yeniden başlatma uyarısı yok"


# --- Öneri üretilemeyen durumlar boş bırakılmıyor -------------------------------------------


@pytest.mark.parametrize("metric_key", ["cache_hit_ratio", "replication_lag_bytes", "transactions_per_sec"])
def test_non_postgres_engines_say_why_no_advice_instead_of_leaving_it_blank(metric_key: str):
    advice = short_horizon_advice(
        metric_key=metric_key, engine="sqlserver", current=50, predicted=80, horizon_minutes=60
    )
    assert advice.unavailable_reason, "neden üretilemediği yazılmamış"
    assert "sqlserver" in advice.unavailable_reason.lower()
    assert advice.title, "başlıksız bırakılmamalı"


def test_an_unknown_metric_still_returns_a_reason():
    advice = short_horizon_advice(
        metric_key="bilinmeyen_metrik", engine="postgresql", current=1, predicted=2, horizon_minutes=60
    )
    assert advice.unavailable_reason
    assert not advice.is_actionable


@pytest.mark.parametrize(
    "builder",
    [
        lambda: cache_hit_advice(current=95.0, predicted=88.0),
        lambda: replication_lag_advice(current_bytes=5_000_000, predicted_bytes=20_000_000),
        lambda: throughput_advice(metric_key="transactions_per_sec", current=100, predicted=250),
    ],
)
def test_short_horizon_metrics_gained_real_steps(builder):
    """Bu üç metrik eskiden tek cümlelik öneriyle geliyordu, hiç komut yoktu."""
    advice = builder()
    assert advice.steps
    assert any(s.command for s in advice.steps)
    assert advice.why and advice.cautions


# --- Uçtan uca: üretilen tahmin öneriyi taşıyor mu ------------------------------------------


async def test_database_size_prediction_persists_its_advice():
    """Öneri, tahmin ÜRETİLDİĞİ anda kaydediliyor — tahmin geçmişi kendi önerisini taşısın."""
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

    assert created, "tahmin üretilmedi"
    stored = created[0].advice
    assert stored is not None, "advice kaydedilmemiş — API null dönerdi"
    assert stored["steps"], "adımlar kaydedilmemiş"
    assert any(s["command"] for s in stored["steps"]), "komutlar kaydedilmemiş"
    assert stored["why"] and stored["cautions"] and stored["verification"]


async def test_index_bloat_prediction_persists_its_advice():
    async with SessionLocal() as session:
        instance = await _instance(session)
        base = date.today() - timedelta(days=15)
        for i in range(15):
            session.add(SchemaObjectDailySample(
                instance_id=instance.id, day=base + timedelta(days=i), object_kind="index",
                schema_name="public", object_name="olaylar_ts_idx",
                size_bytes=1_000_000_000 + i * 30_000_000,
            ))
        await session.commit()

        created = await pred._index_bloat_predictions(session, instance.id)
        await session.commit()

    assert created
    stored = created[0].advice
    assert stored is not None
    assert "olaylar_ts_idx" in stored["title"], "öneri hangi nesneye ait olduğunu söylemeli"
    assert any("REINDEX" in (s["command"] or "").upper() for s in stored["steps"])


async def test_the_api_returns_the_advice():
    """Regresyon kilidi: şemada alan var + modelde yok = sessizce `null`."""
    from tests.auth_helper import authed_client

    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(PredictionInsight(
            instance_id=instance.id, metric_key="database_size_bytes",
            created_at=datetime.now(UTC), horizon_minutes=1440,
            current_value=1.0, predicted_value=2.0, threshold=1.0, confidence=0.8,
            severity="warning", message="test",
            advice={
                "title": "Test önerisi", "why": "neden", "cautions": [],
                "steps": [{"action": "adım", "command": "SELECT 1;"}],
                "estimated_duration": None, "rollback": None,
                "verification": "SELECT 1;", "unavailable_reason": None,
            },
        ))
        await session.commit()

    async with await authed_client() as c:
        rows = (await c.get("/api/predictions")).json()

    mine = [r for r in rows if r["instance_id"] == instance.id]
    assert mine, "tahmin listelenmedi"
    assert mine[0]["advice"] is not None, "API advice alanını boş döndü"
    assert mine[0]["advice"]["steps"][0]["command"] == "SELECT 1;"
