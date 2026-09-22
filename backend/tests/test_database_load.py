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
from sqlalchemy import func, select

from app.database import SessionLocal, init_db
from app.models import (
    ActiveSessionMinute,
    ActiveSessionRollupHourly,
    Instance,
    WaitLoadRollupHourly,
    WaitQuerySignature,
    WaitSampleMinute,
)
from app.services.credentials import encrypt_secret
from app.services.database_load import (
    DOMINANCE_THRESHOLD_PCT,
    MIN_SAMPLES_FOR_ANALYSIS,
    build_database_load,
    choose_bucket_seconds,
)
from app.services.retention import WAIT_LOAD_RAW_RETENTION_DAYS
from tests.auth_helper import authed_client

# GÖRECELİ (mutlak takvim tarihi DEĞİL): sabit bir geçmiş tarih, Faz 31 Commit 10c-C'nin 7 günlük ham saklama
# penceresinin DIŞINA düşerdi — o zaman bu testler farkında olmadan rollup yoluna kayardı (tam da yaşandı: bu
# dosyanın BASE'i başka bir günde yazılmıştı ve zamanla 7 günlük sınırı geçmişti). "Şimdi"ye göre YAKIN tutmak
# testi tarihten bağımsız kılıyor.
BASE = (datetime.now(UTC) - timedelta(hours=6)).replace(second=0, microsecond=0)


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


async def test_multiple_minutes_actually_collapse_into_one_wide_bucket():
    """Faz 31 Commit 10c-C'de bulundu: `_epoch_bucket` SQLAlchemy 2.0'ın gerçek (kayan noktalı) `/` bölmesi
    yüzünden HİÇBİR satırı gruplamıyordu — `(epoch / N) * N` neredeyse orijinal epoch'a geri dönüyordu. Mevcut
    testler yalnızca kova genişliği zaten dakika çözünürlüğündeyken (60 sn) çalıştığı için bu gizli kalmıştı.
    Kova genişliği > 1 dakika olması için pencere > 3 saat olmalı (`choose_bucket_seconds`); burada iki dakikalık
    satır AYNI (geniş) kovaya düşüyor mu diye doğrudan sınanıyor."""
    from app.services.database_load import _epoch_bucket

    instance = await _instance()
    minute_a, minute_b = BASE, BASE + timedelta(minutes=1)  # aynı geniş kovaya düşmeli
    async with SessionLocal() as session:
        session.add(ActiveSessionMinute(instance_id=instance.id, minute=minute_a, samples_taken=10,
                                        active_sessions_sampled=5, blocked_sessions_sampled=0))
        session.add(ActiveSessionMinute(instance_id=instance.id, minute=minute_b, samples_taken=10,
                                        active_sessions_sampled=5, blocked_sessions_sampled=0))
        await session.commit()

        bucket = _epoch_bucket(ActiveSessionMinute.minute, 3600)  # 1 saatlik kova: ikisi de İÇİNDE
        rows = (await session.execute(
            select(bucket, func.count()).where(ActiveSessionMinute.instance_id == instance.id).group_by(bucket)
        )).all()
    assert len(rows) == 1, f"1 saatlik kovada iki bitişik dakika TEK grup olmalı, {len(rows)} grup geldi"
    assert rows[0][1] == 2, "grubun içinde iki satır olmalı"


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


async def test_a_sampler_connection_failure_says_measurement_failed_not_no_data():
    """Faz 31 Commit 10c-B: RTT ≥ ~1 sn'de örnekleyici hiç bağlanamıyor, hiçbir satır yazılmıyor. Eski mesaj
    ('hiç bekleme örneği yok... ilk verinin birikmesi birkaç dakika sürer') bunu 'yeni instance, bekle' gibi
    gösterirdi — oysa hiçbir zaman ölçülemeyecek."""
    instance = await _instance()
    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
        row.last_sample_error = "Bağlantı zaman aşımına uğradı: host erişilebilir mi kontrol edin. (timeout)"
        row.last_sample_error_at = BASE - timedelta(minutes=2)
        await session.commit()
        await session.refresh(row)
        report = await build_database_load(session, row, start=BASE, end=BASE + timedelta(minutes=10))
    assert report.unavailable_reason.startswith("Ölçülemedi:")
    assert "zaman aşımına uğradı" in report.unavailable_reason
    assert "birkaç dakika sürer" not in report.unavailable_reason


async def test_negative_control_a_later_success_clears_the_broken_state():
    """NEGATİF KONTROL: hatadan SONRA başarı varsa 'ölçülemedi' denmez — eski arıza izi kalıcı olmamalı."""
    instance = await _instance()
    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
        row.last_sample_error = "Kimlik doğrulama başarısız: kullanıcı adı veya parola yanlış."
        row.last_sample_error_at = BASE - timedelta(hours=3)
        row.last_sample_ok_at = BASE - timedelta(minutes=5)
        await session.commit()
        await session.refresh(row)
        report = await build_database_load(session, row, start=BASE, end=BASE + timedelta(minutes=10))
    assert not report.unavailable_reason.startswith("Ölçülemedi:")
    assert "örnek" in report.unavailable_reason.lower()


async def test_negative_control_a_healthy_instance_with_real_zero_load_keeps_the_old_message():
    """NEGATİF KONTROL: hiç arıza kaydı yoksa (sağlıklı, gerçekten yük yok) eski 'henüz veri yok' mesajı korunur —
    her boş grafik 'ölçülemedi' denmemeli."""
    instance = await _instance()
    report = await _build(instance)
    assert not report.unavailable_reason.startswith("Ölçülemedi:")


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


# --- Saatlik toplulaştırma: 7 günden eski dönem rollup'tan okunuyor (Faz 31 Commit 10c-C) -----

OLD_HOUR = (datetime.now(UTC) - timedelta(days=WAIT_LOAD_RAW_RETENTION_DAYS + 3)).replace(
    minute=0, second=0, microsecond=0)


async def _seed_rollup(instance: Instance, hour: datetime, *, samples=600, active=300, blocked=0,
                       categories: dict[str, int]) -> None:
    async with SessionLocal() as session:
        session.add(ActiveSessionRollupHourly(instance_id=instance.id, hour=hour, samples_taken=samples,
                                              active_sessions_sampled=active, blocked_sessions_sampled=blocked))
        for category, count in categories.items():
            session.add(WaitLoadRollupHourly(instance_id=instance.id, hour=hour, wait_category=category,
                                             sample_count=count))
        await session.commit()


async def test_an_old_window_reads_the_hourly_rollup_not_the_empty_raw_tables():
    instance = await _instance()
    await _seed_rollup(instance, OLD_HOUR, samples=600, active=300, categories={"cpu": 200, "io": 100})
    async with SessionLocal() as session:
        report = await build_database_load(session, instance, start=OLD_HOUR, end=OLD_HOUR + timedelta(hours=1))
    assert report.source == "rollup"
    assert report.samples_taken == 600
    assert report.average_aas == pytest.approx(0.5)  # (200+100)/600
    assert {c.category for c in report.categories} == {"cpu", "io"}
    assert report.query_attribution_available is False and report.top_queries == []
    assert report.cadence is None, "saatlik toplamda aralık/boşluk bilgisi yok"


async def test_negative_control_an_old_window_with_only_raw_data_says_nothing_was_measured():
    """NEGATİF KONTROL: eski pencerede yalnızca HAM veri olsa (rollup boş) bu görünmez — rollup yolundan
    okunduğu için 'toplulaştırılmış veri yok' der, hamdaki satırları sessizce yok saymaz."""
    instance = await _instance()
    await _seed(instance, minutes=5, per_minute={("q1", "cpu", ""): 60}, start=OLD_HOUR)
    async with SessionLocal() as session:
        report = await build_database_load(session, instance, start=OLD_HOUR, end=OLD_HOUR + timedelta(hours=1))
    assert report.source == "rollup" and report.samples_taken == 0
    assert report.unavailable_reason and "toplulaştırılmış" in report.unavailable_reason.lower()


async def test_a_window_just_inside_the_raw_retention_boundary_still_uses_raw():
    instance = await _instance()
    just_inside = datetime.now(UTC) - timedelta(days=WAIT_LOAD_RAW_RETENTION_DAYS - 1)
    await _seed(instance, minutes=5, per_minute={("q1", "cpu", ""): 60}, start=just_inside)
    async with SessionLocal() as session:
        report = await build_database_load(session, instance, start=just_inside,
                                           end=just_inside + timedelta(minutes=5))
    assert report.source == "raw"


async def test_query_attribution_stays_true_and_top_queries_populated_on_the_raw_path():
    """NEGATİF KONTROL (ters yön): ham yolda sorgu kırılımı hâlâ dolu — rollup'a geçiş yalnızca eski pencerede."""
    instance = await _instance()
    await _seed(instance, minutes=5, per_minute={("q1", "cpu", ""): 60})
    report = await _build(instance, minutes=5)
    assert report.source == "raw"
    assert report.query_attribution_available is True and report.top_queries != []
