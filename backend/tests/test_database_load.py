"""Veritabanı yükü — Average Active Sessions (Faz 25 İŞ 2).

AAS bekleme analizinin merkez metriği: bir aralıkta ortalama kaç oturumun aynı anda iş
yaptığı. Bu dosyanın koruduğu üç şey:

1. **Aritmetik doğru.** AAS = aktif oturum toplamı / ALINAN örnek sayısı. Payda sabit
   varsayılırsa (60/aralık) örnekleyicinin geciktiği dakikalar olduğundan sakin görünür.
2. **Veri yetersizse sayı üretilmez.** Tek dakikalık örnekle "yükünüz 3.2" demek, ölçüm gibi
   görünen bir tahmindir; `unavailable_reason` doluyor ve seri boş dönüyor.
3. **"En yavaş sorgu" ile "en çok yük üreten sorgu" farklı sorulardır.** 5 saniye süren ama
   günde iki kez çalışan bir sorgu, 20 ms süren ama saniyede 300 kez çalışan bir sorgunun
   yanında hiçbir şey; AAS ikincisini öne çıkarır.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import ActiveSessionMinute, Instance, WaitQuerySignature, WaitSampleMinute
from app.services.credentials import encrypt_secret
from app.services.database_load import (
    DOMINANCE_THRESHOLD_PCT,
    MIN_SAMPLES_FOR_ANALYSIS,
    build_database_load,
    choose_bucket_seconds,
)
from tests.auth_helper import authed_client

BASE = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(engine: str = "postgresql") -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"load-{uuid.uuid4().hex[:8]}", engine=engine, host="h",
            port=5432, database="d", username="u", password=encrypt_secret("x"),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _seed(
    instance: Instance,
    minutes: int,
    per_minute: dict[tuple[str, str, str], int],
    samples_per_minute: int = 60,
    blocked_per_minute: int = 0,
    start: datetime = BASE,
) -> None:
    """`per_minute`: (queryid, kategori, olay) -> o dakikadaki örnek sayısı."""
    async with SessionLocal() as session:
        for i in range(minutes):
            minute = start + timedelta(minutes=i)
            session.add(
                ActiveSessionMinute(
                    instance_id=instance.id,
                    minute=minute,
                    samples_taken=samples_per_minute,
                    active_sessions_sampled=sum(per_minute.values()),
                    blocked_sessions_sampled=blocked_per_minute,
                )
            )
            for (queryid, category, event), count in per_minute.items():
                session.add(
                    WaitSampleMinute(
                        instance_id=instance.id, minute=minute, queryid=queryid,
                        wait_category=category, wait_event=event, sample_count=count,
                    )
                )
        existing = {
            row.queryid
            for row in (
                await session.execute(
                    select(WaitQuerySignature).where(WaitQuerySignature.instance_id == instance.id)
                )
            ).scalars().all()
        }
        for queryid in {q for q, _, _ in per_minute}:
            if queryid and queryid not in existing:
                session.add(
                    WaitQuerySignature(
                        instance_id=instance.id, queryid=queryid,
                        query_text=f"SELECT * FROM t WHERE id = {queryid}",
                    )
                )
        await session.commit()


async def _build(instance: Instance, minutes: int = 10, **kw):
    async with SessionLocal() as session:
        return await build_database_load(
            session, instance, start=BASE, end=BASE + timedelta(minutes=minutes), **kw
        )


# --- Aritmetik ---------------------------------------------------------------------------


async def test_aas_is_sessions_divided_by_samples_actually_taken():
    """60 örnekte her seferinde 2 oturum görüldüyse AAS = 2.0, ne eksik ne fazla."""
    instance = await _instance()
    await _seed(instance, minutes=5, per_minute={("q1", "cpu", ""): 60, ("q1", "io", "DataFileRead"): 60})
    report = await _build(instance, minutes=5)

    assert report.unavailable_reason is None
    assert report.average_aas == pytest.approx(2.0)
    assert report.samples_taken == 300


async def test_a_minute_with_fewer_samples_is_not_diluted():
    """ÖRNEKLEYİCİ GECİKTİĞİNDE O DAKİKA SAKİN GÖRÜNMEMELİ.

    İkinci dakikada yalnızca 12 örnek alınmış ama her örnekte 4 oturum görülmüş. Payda
    ölçüldüğü için o dakikanın AAS'i 4.0; sabit 60 varsaysaydık 0.8 çıkardı ve grafik, yükün
    zirve yaptığı dakikayı en sakin dakika gibi gösterirdi.
    """
    instance = await _instance()
    await _seed(instance, minutes=1, per_minute={("q1", "cpu", ""): 60}, samples_per_minute=60)
    await _seed(
        instance, minutes=1, per_minute={("q1", "cpu", ""): 48},
        samples_per_minute=12, start=BASE + timedelta(minutes=1),
    )
    report = await _build(instance, minutes=2)

    by_bucket = {p.bucket_start: p.total_aas for p in report.series}
    assert by_bucket[BASE] == pytest.approx(1.0)
    assert by_bucket[BASE + timedelta(minutes=1)] == pytest.approx(4.0)
    assert report.peak_aas == pytest.approx(4.0)


async def test_category_shares_sum_to_one_hundred():
    instance = await _instance()
    await _seed(
        instance, minutes=5,
        per_minute={("q1", "cpu", ""): 30, ("q1", "io", "DataFileRead"): 20, ("q2", "lock", "relation"): 10},
    )
    report = await _build(instance, minutes=5)
    assert sum(c.share_pct for c in report.categories) == pytest.approx(100.0, abs=0.2)
    assert [c.category for c in report.categories] == ["cpu", "io", "lock"]


async def test_background_idle_waits_are_excluded_from_load():
    """`activity` kategorisi arka plan süreçlerinin boşta bekleme noktası. Yük saymak,
    grafiğe hiç inmeyen yalancı bir taban ekler."""
    instance = await _instance()
    await _seed(
        instance, minutes=5,
        per_minute={("q1", "cpu", ""): 60, ("", "activity", "WalWriterMain"): 60},
    )
    report = await _build(instance, minutes=5)
    assert [c.category for c in report.categories] == ["cpu"]
    assert report.average_aas == pytest.approx(1.0)


async def test_blocked_sessions_are_reported_separately():
    instance = await _instance()
    await _seed(
        instance, minutes=5, per_minute={("q1", "lock", "relation"): 60}, blocked_per_minute=60
    )
    report = await _build(instance, minutes=5)
    assert report.blocked_aas == pytest.approx(1.0)


# --- Veri yetersizliği -------------------------------------------------------------------


async def test_no_samples_gives_a_reason_not_an_empty_chart():
    instance = await _instance()
    report = await _build(instance)
    assert report.series == []
    assert report.unavailable_reason
    assert "örnek" in report.unavailable_reason.lower()


async def test_too_few_samples_refuses_to_produce_an_average():
    """Kanıt zorunlu: 30 örnekten yük ortalaması üretmek, ölçüm gibi görünen bir tahmin."""
    instance = await _instance()
    await _seed(instance, minutes=1, per_minute={("q1", "cpu", ""): 30}, samples_per_minute=30)
    report = await _build(instance, minutes=1)
    assert report.average_aas == 0.0
    assert report.unavailable_reason and str(MIN_SAMPLES_FOR_ANALYSIS) in report.unavailable_reason


async def test_mongodb_says_why_instead_of_returning_zeros():
    instance = await _instance(engine="mongodb")
    report = await _build(instance)
    assert report.unavailable_reason and "MongoDB" in report.unavailable_reason


# --- Baskın kaynak -----------------------------------------------------------------------


async def test_dominant_category_is_stated_in_words_not_left_to_the_reader():
    instance = await _instance()
    await _seed(
        instance, minutes=5,
        per_minute={("q1", "io", "DataFileRead"): 90, ("q1", "cpu", ""): 10},
    )
    report = await _build(instance, minutes=5)
    assert report.dominant_category == "io"
    assert report.dominant_share_pct >= DOMINANCE_THRESHOLD_PCT
    assert "Disk G/Ç".lower() in report.dominant_verdict.lower()


async def test_no_dominant_category_says_so_instead_of_naming_a_weak_leader():
    """%34'lük bir kategoriye bakıp "IO darboğazı" demek yanıltıcı olurdu — yük dağılmışsa
    öyle söylenir."""
    instance = await _instance()
    await _seed(
        instance, minutes=5,
        per_minute={
            ("q1", "io", "DataFileRead"): 34,
            ("q1", "cpu", ""): 33,
            ("q2", "lock", "relation"): 33,
        },
    )
    report = await _build(instance, minutes=5)
    assert report.dominant_category is None
    assert "tek bir baskın kaynak yok" in report.dominant_verdict.lower()


# --- Yük üreten sorgular -----------------------------------------------------------------


async def test_top_queries_are_ranked_by_load_not_by_duration():
    instance = await _instance()
    await _seed(
        instance, minutes=5,
        per_minute={("cheap-but-frequent", "cpu", ""): 50, ("slow-but-rare", "io", "DataFileRead"): 5},
    )
    report = await _build(instance, minutes=5)
    assert [q.queryid for q in report.top_queries] == ["cheap-but-frequent", "slow-but-rare"]
    assert report.top_queries[0].share_pct > report.top_queries[1].share_pct


async def test_each_query_carries_its_own_wait_profile():
    """"Bu sorgu süresinin yüzde kaçını hangi beklemede geçirdi" — önerinin dayanağı bu."""
    instance = await _instance()
    await _seed(
        instance, minutes=5,
        per_minute={("q1", "io", "DataFileRead"): 75, ("q1", "cpu", ""): 25},
    )
    report = await _build(instance, minutes=5)
    profile = {c.category: c.share_pct for c in report.top_queries[0].wait_profile}
    assert profile["io"] == pytest.approx(75.0, abs=0.5)
    assert profile["cpu"] == pytest.approx(25.0, abs=0.5)
    assert report.top_queries[0].dominant_category == "io"


async def test_query_text_comes_from_the_signature_dictionary():
    instance = await _instance()
    await _seed(instance, minutes=5, per_minute={("q1", "cpu", ""): 60})
    report = await _build(instance, minutes=5)
    assert report.top_queries[0].query == "SELECT * FROM t WHERE id = q1"


async def test_missing_query_text_is_admitted_not_invented():
    instance = await _instance()
    async with SessionLocal() as session:
        for i in range(5):
            minute = BASE + timedelta(minutes=i)
            session.add(
                ActiveSessionMinute(
                    instance_id=instance.id, minute=minute, samples_taken=60,
                    active_sessions_sampled=60, blocked_sessions_sampled=0,
                )
            )
            session.add(
                WaitSampleMinute(
                    instance_id=instance.id, minute=minute, queryid="orphan",
                    wait_category="cpu", wait_event="", sample_count=60,
                )
            )
        await session.commit()
    report = await _build(instance, minutes=5)
    assert "kaydedilmemiş" in report.top_queries[0].query
    assert "orphan" in report.top_queries[0].query


async def test_rows_without_a_queryid_do_not_become_a_fake_query():
    """PostgreSQL 14 öncesinde queryid boş gelir. Boş kimliği bir "sorgu" gibi listelemek,
    kullanıcıya var olmayan bir sorgu göstermek olurdu."""
    instance = await _instance()
    await _seed(instance, minutes=5, per_minute={("", "cpu", ""): 60})
    report = await _build(instance, minutes=5)
    assert report.top_queries == []
    assert report.query_attribution_available is False
    # Kırılım yine de var — "sistem neyi bekliyor" sorusu cevaplanabiliyor.
    assert report.average_aas == pytest.approx(1.0)


# --- Kova genişliği ----------------------------------------------------------------------


def test_bucket_width_keeps_the_point_count_readable():
    assert choose_bucket_seconds(BASE, BASE + timedelta(hours=1)) == 60
    for hours in (1, 6, 24, 168):
        bucket = choose_bucket_seconds(BASE, BASE + timedelta(hours=hours))
        points = hours * 3600 / bucket
        assert points <= 185, f"{hours} saat -> {points} nokta"
        assert bucket >= 60


# --- Uç ---------------------------------------------------------------------------------


async def test_endpoint_returns_the_report():
    instance = await _instance()
    await _seed(instance, minutes=5, per_minute={("q1", "io", "DataFileRead"): 60})
    async with await authed_client() as client:
        response = await client.get(
            f"/api/instances/{instance.id}/database-load",
            params={"start": BASE.isoformat(), "end": (BASE + timedelta(minutes=5)).isoformat()},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["average_aas"] == pytest.approx(1.0)
    assert body["categories"][0]["category"] == "io"
    # Etiket ve anlam SUNUCUDAN geliyor: arayüz ile rapor aynı sözlüğü konuşsun.
    assert body["categories"][0]["label"]
    assert body["categories"][0]["meaning"]


async def test_endpoint_404s_for_a_deleted_instance():
    async with await authed_client() as client:
        response = await client.get("/api/instances/999999/database-load")
    assert response.status_code == 404
