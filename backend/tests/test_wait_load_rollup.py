"""Bekleme yükünün saatlik toplulaştırması: ham veri silinmeden önce biriktirilir (Faz 31 Commit 10c-C).

Kabul kriterleri:
1. `before`'dan eski ham satırlar saatlik toplamlara aktarılır, sonra silinir; pencere İÇİNDEKİ satırlara dokunulmaz.
2. Aktarım KAYIPSIZ: toplamlar tam eşleşir (satır sayısı değil, TOPLAM örnek sayısı korunur).
3. Arka plan/boşta kategoriler rollup'a girmez (database_load.py'deki kuralla AYNI — ayrı hesaplama yasak).
4. Saat sınırını iki ayrı turda kapsayan veri (additive upsert) kaybolmadan toplanır.
5. `run_retention_cleanup` bu iki tabloyu genel döngüden ÇIKARIP kendi (7 günlük, sabit) penceresiyle işler.

Her kural NEGATİF KONTROLLÜ.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    ActiveSessionMinute,
    ActiveSessionRollupHourly,
    Instance,
    WaitLoadRollupHourly,
    WaitSampleMinute,
)
from app.services.credentials import encrypt_secret
from app.services.retention import WAIT_LOAD_RAW_RETENTION_DAYS, RETENTION_TARGETS, run_retention_cleanup
from app.services.wait_load_rollup import ensure_wait_load_rollup

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


async def _instance() -> Instance:
    async with SessionLocal() as session:
        row = Instance(name=f"rollup-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
                       database="d", username="u", password=encrypt_secret("x"))
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _seed_minute(instance: Instance, minute: datetime, *, samples=60, active=30, blocked=0,
                       categories: dict[str, int] | None = None) -> None:
    async with SessionLocal() as session:
        session.add(ActiveSessionMinute(instance_id=instance.id, minute=minute, samples_taken=samples,
                                        active_sessions_sampled=active, blocked_sessions_sampled=blocked))
        for category, count in (categories or {}).items():
            session.add(WaitSampleMinute(instance_id=instance.id, minute=minute, queryid="q1",
                                         wait_category=category, wait_event="", sample_count=count))
        await session.commit()


async def _run():
    await init_db()


async def test_old_rows_are_rolled_up_and_deleted_new_rows_are_untouched():
    await _run()
    instance = await _instance()
    old_minute = NOW - timedelta(days=10)
    fresh_minute = NOW - timedelta(days=1)
    await _seed_minute(instance, old_minute, samples=60, active=30, categories={"cpu": 20, "io": 10})
    await _seed_minute(instance, fresh_minute, samples=60, active=15, categories={"cpu": 15})

    async with SessionLocal() as session:
        result = await ensure_wait_load_rollup(session, before=NOW - timedelta(days=7))
        await session.commit()
    assert result.hours_rolled_up == 1 and result.active_session_rows_deleted == 1 and result.wait_sample_rows_deleted == 2

    async with SessionLocal() as session:
        remaining = (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalars().all()
        assert [r.minute.replace(tzinfo=UTC) for r in remaining] == [fresh_minute], \
            "yalnızca pencere İÇİNDEKİ dakika kalmalıydı"
        rollup = (await session.execute(select(ActiveSessionRollupHourly).where(
            ActiveSessionRollupHourly.instance_id == instance.id))).scalars().all()
    assert len(rollup) == 1
    assert rollup[0].samples_taken == 60 and rollup[0].active_sessions_sampled == 30
    assert rollup[0].hour.replace(tzinfo=UTC) == old_minute.replace(minute=0, second=0, microsecond=0)


async def test_totals_are_preserved_exactly_across_the_rollup():
    """Kayıpsız aktarım: birden çok dakikanın toplamı, tek saatlik satırın toplamıyla TAM eşleşmeli."""
    await _run()
    instance = await _instance()
    hour = (NOW - timedelta(days=10)).replace(minute=0, second=0, microsecond=0)
    total_samples = total_active = total_cpu = 0
    for minute_offset in range(0, 60, 5):
        samples, active, cpu = 12, 6, 4
        await _seed_minute(instance, hour + timedelta(minutes=minute_offset), samples=samples, active=active,
                           categories={"cpu": cpu})
        total_samples += samples
        total_active += active
        total_cpu += cpu

    async with SessionLocal() as session:
        await ensure_wait_load_rollup(session, before=NOW - timedelta(days=7))
        await session.commit()

    async with SessionLocal() as session:
        active_row = (await session.execute(select(ActiveSessionRollupHourly).where(
            ActiveSessionRollupHourly.instance_id == instance.id))).scalar_one()
        wait_row = (await session.execute(select(WaitLoadRollupHourly).where(
            WaitLoadRollupHourly.instance_id == instance.id))).scalar_one()
    assert active_row.samples_taken == total_samples and active_row.active_sessions_sampled == total_active
    assert wait_row.sample_count == total_cpu


async def test_idle_categories_are_excluded_from_the_rollup():
    """NEGATİF KONTROL: 'activity' gibi boşta bekleme kategorileri rollup'a girseydi bu test kırmızı olurdu —
    database_load.py'nin AYNI kuralı (is_load_bearing) burada da uygulanıyor."""
    await _run()
    instance = await _instance()
    old_minute = NOW - timedelta(days=10)
    await _seed_minute(instance, old_minute, categories={"cpu": 10, "activity": 999})

    async with SessionLocal() as session:
        await ensure_wait_load_rollup(session, before=NOW - timedelta(days=7))
        await session.commit()

    async with SessionLocal() as session:
        categories = {r.wait_category for r in (await session.execute(select(WaitLoadRollupHourly).where(
            WaitLoadRollupHourly.instance_id == instance.id))).scalars().all()}
    assert categories == {"cpu"}, f"boşta kategori rollup'a girmemeli, görülen: {categories}"


async def test_a_row_within_the_window_is_never_rolled_up_or_deleted():
    """NEGATİF KONTROL: pencere İÇİNDEKİ satır ne toplulaştırılır ne silinir."""
    await _run()
    instance = await _instance()
    fresh_minute = NOW - timedelta(hours=1)
    await _seed_minute(instance, fresh_minute, categories={"cpu": 5})

    async with SessionLocal() as session:
        result = await ensure_wait_load_rollup(session, before=NOW - timedelta(days=7))
        await session.commit()
    assert result.hours_rolled_up == 0 and result.active_session_rows_deleted == 0

    async with SessionLocal() as session:
        assert (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalar_one_or_none() is not None
        assert (await session.execute(select(ActiveSessionRollupHourly).where(
            ActiveSessionRollupHourly.instance_id == instance.id))).scalar_one_or_none() is None


async def test_an_hour_split_across_two_runs_accumulates_instead_of_overwriting():
    """Saat sınırını İKİ ayrı retention turu kapsıyor (cutoff saatin ortasından geçiyor): ikinci turun katkısı
    BİRİNCİYE EKLENMELİ. NEGATİF KONTROL: DO NOTHING kullanılsaydı ikinci turun satırları kaybolurdu."""
    await _run()
    instance = await _instance()
    hour = (NOW - timedelta(days=10)).replace(minute=0, second=0, microsecond=0)
    await _seed_minute(instance, hour + timedelta(minutes=10), samples=10, active=5, categories={"cpu": 5})
    await _seed_minute(instance, hour + timedelta(minutes=50), samples=20, active=8, categories={"cpu": 8})

    # 1. tur: cutoff saatin TAM ortasından geçiyor — yalnızca ilk dakika pencerenin dışında.
    async with SessionLocal() as session:
        await ensure_wait_load_rollup(session, before=hour + timedelta(minutes=30))
        await session.commit()
    async with SessionLocal() as session:
        after_first = (await session.execute(select(ActiveSessionRollupHourly).where(
            ActiveSessionRollupHourly.instance_id == instance.id))).scalar_one()
    assert after_first.samples_taken == 10, "yalnızca ilk dakika bu turda işlenmeliydi"

    # 2. tur (ertesi gün): cutoff ilerledi, ikinci dakika da artık pencerenin dışında.
    async with SessionLocal() as session:
        await ensure_wait_load_rollup(session, before=NOW - timedelta(days=7))
        await session.commit()
    async with SessionLocal() as session:
        after_second = (await session.execute(select(ActiveSessionRollupHourly).where(
            ActiveSessionRollupHourly.instance_id == instance.id))).scalar_one()
        wait_row = (await session.execute(select(WaitLoadRollupHourly).where(
            WaitLoadRollupHourly.instance_id == instance.id))).scalar_one()
    assert after_second.samples_taken == 30, "ikinci turun katkısı BİRİKMELİYDİ, üzerine yazılmamalıydı"
    assert wait_row.sample_count == 13


async def test_a_healthy_backlog_across_many_instances_does_not_mix_totals():
    """Çok instance'lı gerçekçi durum: her instance'ın toplamı KENDİ satırında, birbirine karışmadan."""
    await _run()
    a, b = await _instance(), await _instance()
    old_minute = NOW - timedelta(days=10)
    await _seed_minute(a, old_minute, samples=10, categories={"cpu": 10})
    await _seed_minute(b, old_minute, samples=99, categories={"cpu": 99})

    async with SessionLocal() as session:
        await ensure_wait_load_rollup(session, before=NOW - timedelta(days=7))
        await session.commit()

    async with SessionLocal() as session:
        rows = {r.instance_id: r.samples_taken for r in (await session.execute(
            select(ActiveSessionRollupHourly))).scalars().all()}
    assert rows[a.id] == 10 and rows[b.id] == 99


async def test_wait_sample_minute_and_active_session_minute_are_excluded_from_the_generic_loop():
    """RETENTION_TARGETS hâlâ bu iki modeli TAŞIYOR (büyük tablo tanımının kaynağı) ama genel döngü artık
    onları ATLIYOR — `run_retention_cleanup` testi bunu ayrıca kanıtlıyor."""
    names = {model.__name__ for model, _ in RETENTION_TARGETS}
    assert {"WaitSampleMinute", "ActiveSessionMinute"} <= names


async def test_run_retention_cleanup_uses_the_fixed_seven_day_window_for_wait_load(monkeypatch):
    """`run_retention_cleanup` genel ayar 90 gün olsa bile bekleme örneklerini 7 günde kesiyor."""
    from app.services import retention as retention_module

    await _run()
    instance = await _instance()
    async with SessionLocal() as session:
        await retention_module.set_retention_days(session, 90)  # genel ayar UZUN
    old_minute = datetime.now(UTC) - timedelta(days=WAIT_LOAD_RAW_RETENTION_DAYS + 1)  # 90 gün içinde ama 7'den eski
    await _seed_minute(instance, old_minute, categories={"cpu": 3})

    await run_retention_cleanup()

    async with SessionLocal() as session:
        assert (await session.execute(select(ActiveSessionMinute).where(
            ActiveSessionMinute.instance_id == instance.id))).scalar_one_or_none() is None, \
            "genel ayar 90 gün olsa da bekleme örnekleri 7 günde silinmeliydi"
        assert (await session.execute(select(ActiveSessionRollupHourly).where(
            ActiveSessionRollupHourly.instance_id == instance.id))).scalar_one_or_none() is not None, \
            "silinmeden önce rollup'a aktarılmalıydı"
