"""Bir veritabanının hatası turun tamamını düşürmemeli (Faz 30 İŞ 1).

Eskiden `collect_all_instances` bütün veritabanları için TEK oturum ve sonda TEK commit
kullanıyordu. Bir veritabanında flush hatası olduğunda oturum geri alınmış duruma düşüyor,
aynı turdaki diğer veritabanları `PendingRollbackError` alıyor ve commit de düşüyordu.

Canlıda yaşandı: çalıştırılmamış bir migration yüzünden HİÇBİR veritabanı metrik yazamadı.
Bu dosya o davranışı kalıcı olarak kapatıyor.
"""

from __future__ import annotations

import uuid

import app.collectors.scheduler as scheduler_module
from app.database import SessionLocal, init_db
from app.models import Instance, MetricSample
from app.services import collection as collection_module
from app.services import collection_status
from sqlalchemy import select, update


async def _only_these_instances(session, count: int) -> list[Instance]:
    """Yalnızca bu testin veritabanları etkin olsun.

    Test veritabanı diğer testlerden kalma instance'lar içeriyor; "denenen kaç veritabanı"
    sayısı onlardan etkilenirse test kendi kurduğu senaryoyu değil, çalışma sırasını ölçer.
    """
    await session.execute(update(Instance).values(enabled=False))
    created = []
    for _ in range(count):
        instance = Instance(
            name=f"isolation-{uuid.uuid4().hex[:8]}",
            engine="postgresql",
            host="fake.internal",
            port=5432,
            database="postgres",
            username="postgres",
            password="x",
            enabled=True,
        )
        session.add(instance)
        created.append(instance)
    await session.flush()
    for instance in created:
        collection_module._previous_state.pop(instance.id, None)  # sırası gelmiş sayılsın
    await session.commit()
    return created


async def test_one_failing_instance_does_not_lose_the_others_data(monkeypatch):
    """ASIL REGRESYON: hata veren veritabanı, sorunsuz olanın verisini götürmemeli."""
    await init_db()
    collection_status.reset()
    async with SessionLocal() as session:
        failing, healthy = await _only_these_instances(session, 2)
        failing_id, healthy_id = failing.id, healthy.id

    async def fake_collect_instance(instance, session):
        if instance.id == failing_id:
            raise RuntimeError("bağlantı kurulamadı")
        session.add(MetricSample(instance_id=instance.id))

    monkeypatch.setattr(scheduler_module, "collect_instance", fake_collect_instance)
    await scheduler_module.collect_all_instances()

    # Ayrı bir oturumda okunuyor: gerçekten COMMIT edilmiş mi, yoksa yalnızca oturumda mı
    # duruyor?
    async with SessionLocal() as session:
        saved = (
            await session.execute(select(MetricSample.id).where(MetricSample.instance_id == healthy_id))
        ).scalars().all()
        assert len(saved) == 1, "sağlıklı veritabanının metriği kaydedilmedi"
        lost = (
            await session.execute(select(MetricSample.id).where(MetricSample.instance_id == failing_id))
        ).scalars().all()
        assert lost == [], "hata veren veritabanı için metrik yazılmamalı"


async def test_the_failure_is_recorded_not_silently_swallowed(monkeypatch):
    await init_db()
    collection_status.reset()
    async with SessionLocal() as session:
        (failing,) = await _only_these_instances(session, 1)
        failing_id = failing.id

    async def fake_collect_instance(instance, session):
        raise RuntimeError("password authentication failed for user")

    monkeypatch.setattr(scheduler_module, "collect_instance", fake_collect_instance)
    await scheduler_module.collect_all_instances()

    async with SessionLocal() as session:
        row = await session.get(Instance, failing_id)
        assert row.last_collect_error, "hata sessizce geçilmiş"
        assert row.last_collect_error_at is not None
        # O veritabanına özel bir sorun — kurulumun değil.
        assert row.last_collect_error_kind == collection_status.KIND_INSTANCE
        assert row.last_collect_ok_at is None


async def test_a_successful_collection_clears_the_previous_error(monkeypatch):
    """Hata kaydı YAPIŞIP KALMAMALI: düzelen bir veritabanı sonsuza kadar hatalı görünürdü."""
    await init_db()
    collection_status.reset()
    async with SessionLocal() as session:
        (instance,) = await _only_these_instances(session, 1)
        instance_id = instance.id

    async def failing(instance, session):
        raise RuntimeError("bağlantı kurulamadı")

    async def succeeding(instance, session):
        session.add(MetricSample(instance_id=instance.id))

    monkeypatch.setattr(scheduler_module, "collect_instance", failing)
    await scheduler_module.collect_all_instances()

    collection_module._previous_state.pop(instance_id, None)
    monkeypatch.setattr(scheduler_module, "collect_instance", succeeding)
    await scheduler_module.collect_all_instances()

    async with SessionLocal() as session:
        row = await session.get(Instance, instance_id)
        assert row.last_collect_error is None
        assert row.last_collect_error_kind is None
        assert row.last_collect_ok_at is not None


async def test_a_schema_mismatch_is_reported_as_one_systemic_notice(monkeypatch):
    """Şema uyumsuzluğu tek tek veritabanlarının sorunu DEĞİL.

    "12 veritabanı hata verdi" listesi operatörü yanlış yere bakmaya gönderirdi: hiçbiri
    hatalı değil, çalıştırılmamış bir migration var.
    """
    await init_db()
    collection_status.reset()
    async with SessionLocal() as session:
        instances = await _only_these_instances(session, 3)
        ids = [i.id for i in instances]

    async def fake_collect_instance(instance, session):
        raise RuntimeError('column instances.last_collect_ok_at does not exist')

    monkeypatch.setattr(scheduler_module, "collect_instance", fake_collect_instance)
    await scheduler_module.collect_all_instances()

    notice = collection_status.systemic_notice()
    assert notice is not None, "sistemik uyarı üretilmedi"
    assert notice.kind == collection_status.KIND_SCHEMA
    assert notice.affected == len(ids)
    assert "migration" in notice.message.lower()

    async with SessionLocal() as session:
        for instance_id in ids:
            row = await session.get(Instance, instance_id)
            assert row.last_collect_error_kind == collection_status.KIND_SCHEMA


async def test_the_systemic_notice_clears_when_the_next_cycle_is_clean(monkeypatch):
    """Uyarı ASILI KALMAMALI: migration çalıştırıldıktan sonra da uyarı dursaydı, bir
    sonraki gerçek sorun fark edilmezdi."""
    await init_db()
    collection_status.reset()
    async with SessionLocal() as session:
        (instance,) = await _only_these_instances(session, 1)
        instance_id = instance.id

    async def schema_error(instance, session):
        raise RuntimeError("no such column: foo")

    monkeypatch.setattr(scheduler_module, "collect_instance", schema_error)
    await scheduler_module.collect_all_instances()
    assert collection_status.systemic_notice() is not None

    collection_module._previous_state.pop(instance_id, None)

    async def ok(instance, session):
        session.add(MetricSample(instance_id=instance.id))

    monkeypatch.setattr(scheduler_module, "collect_instance", ok)
    await scheduler_module.collect_all_instances()

    assert collection_status.systemic_notice() is None


async def test_a_schema_error_is_not_mistaken_for_a_target_problem():
    """`classify_connection_error` "does not exist" metnini "veritabanı bulunamadı" diye
    yorumluyor. Şema kontrolü ÖNCE yapılmazsa eksik bir sütun, hedefteki bir sorun gibi
    gösterilir — operatör yanlış yere bakar."""
    kind, message = collection_status.classify_collection_error(
        RuntimeError('column slow_query_samples.wal_bytes does not exist')
    )
    assert kind == collection_status.KIND_SCHEMA
    assert "migration" in message.lower()
