"""Faz 18 İŞ 5 — bölüm bölüm doğruluk denetiminde bulunan hatalar.

Her bulgu tipi için dört soru soruldu: (a) veri kaynağı doğru mu, (b) eşik mantıklı mı,
(c) işaret ettiği hedef sayfada var mı, (d) önerisi uygulanabilir mi. Bu testler denetimde
bulunan somut hataları kalıcı olarak kapatıyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import GroupHealthSnapshot, Instance, MetricSample
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import (
    SERVICE_DOWN_MIN_SAMPLES,
    TEMP_BYTES_WARN,
    availability_section,
    cluster_section,
    resources_section,
)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"aud-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _ctx(session, instances, hours: int = 2) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(hours=hours), period_end=end, previous=None, previous_findings={},
    )


# --- (A) "Hiç metrik toplanmamış" bulgusu ulaşılamaz koddaydı ----------------------------


async def test_enabled_instance_with_no_samples_produces_a_finding():
    """Bu bulgu `continue`'dan sonra yazıldığı için HİÇ üretilmiyordu — etkin ama veri gelmeyen
    bir instance sessizce görünmez kalıyordu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        result = await availability_section(_ctx(session, [instance]))

    finding = next(f for f in result.findings if "hiç metrik toplanmamış" in f.title)
    assert finding.severity == "critical"
    assert finding.evidence["value"] == 0
    assert finding.link_hint, "hedef sayfaya bağlantı olmalı"
    assert finding.recommendation


async def test_disabled_instance_with_no_samples_does_not_produce_a_finding():
    """Kapalı bir instance'tan veri gelmemesi beklenen durumdur, bulgu değil."""
    async with SessionLocal() as session:
        instance = await _instance(session, enabled=False)
        result = await availability_section(_ctx(session, [instance]))

    assert not any("hiç metrik" in f.title for f in result.findings)


# --- (B) temp_bytes kümülatif sayaç, gauge değil ------------------------------------------


async def test_temp_files_uses_the_period_delta_not_the_cumulative_counter():
    """Eskiden max() alınıyordu; kümülatif sayaçta bu "son değer" demek, yani geçmişte bir kez
    geçici dosya kullanmış her veritabanı sonsuza kadar bulgu üretirdi."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        # Sayaç yüksek ama dönem içinde HİÇ artmamış → bulgu üretilmemeli.
        for offset in (3600, 60):
            session.add(MetricSample(
                instance_id=instance.id, collected_at=now - timedelta(seconds=offset),
                active_connections=5, max_connections=100, cache_hit_ratio=99.9,
                temp_bytes=5_000_000_000,
            ))
        await session.commit()
        result = await resources_section(_ctx(session, [instance]))

    assert not any("geçici dosya" in f.title for f in result.findings)
    row = next(r for r in result.data["instances"] if r["instance_id"] == instance.id)
    assert row["temp_bytes_in_period"] == 0


async def test_temp_files_finding_fires_when_the_counter_actually_grows():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset, temp in ((3600, 1_000_000), (60, 1_000_000 + int(TEMP_BYTES_WARN) * 3)):
            session.add(MetricSample(
                instance_id=instance.id, collected_at=now - timedelta(seconds=offset),
                active_connections=5, max_connections=100, cache_hit_ratio=99.9, temp_bytes=temp,
            ))
        await session.commit()
        result = await resources_section(_ctx(session, [instance]))

    finding = next(f for f in result.findings if "geçici dosya" in f.title)
    assert finding.evidence["value"] == pytest.approx(TEMP_BYTES_WARN * 3)
    assert finding.evidence["threshold"] == TEMP_BYTES_WARN


async def test_small_temp_usage_stays_below_the_threshold():
    """Birkaç kilobayt her veritabanında olur; eşiksiz kontrol kalıcı yanlış pozitifti."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset, temp in ((3600, 0), (60, 4096)):
            session.add(MetricSample(
                instance_id=instance.id, collected_at=now - timedelta(seconds=offset),
                active_connections=5, max_connections=100, cache_hit_ratio=99.9, temp_bytes=temp,
            ))
        await session.commit()
        result = await resources_section(_ctx(session, [instance]))

    assert not any("geçici dosya" in f.title for f in result.findings)


async def test_temp_counter_reset_does_not_produce_a_negative_value():
    async with SessionLocal() as session:
        instance = await _instance(session)
        now = datetime.now(UTC)
        for offset, temp in ((3600, 9_000_000), (60, 2_000_000)):
            session.add(MetricSample(
                instance_id=instance.id, collected_at=now - timedelta(seconds=offset),
                active_connections=5, max_connections=100, cache_hit_ratio=99.9, temp_bytes=temp,
            ))
        await session.commit()
        result = await resources_section(_ctx(session, [instance]))

    row = next(r for r in result.data["instances"] if r["instance_id"] == instance.id)
    assert row["temp_bytes_in_period"] == 2_000_000


# --- (C) Tek ölçümlük "servis down" gürültüsü ----------------------------------------------


async def _seed_cluster(session, instance_id: int, down_samples: int) -> None:
    now = datetime.now(UTC)
    for i in range(down_samples):
        session.add(MetricSample(
            instance_id=instance_id, collected_at=now - timedelta(seconds=600 - i * 15),
            metrics_json={
                "cluster_services": {
                    "cluster": {"leader": "n1", "has_leader": True},
                    "services": [{"service": "etcd", "status": "down"}],
                }
            },
        ))
    # En az iki anlık görüntü olsun ki bölüm "unknown" dönmesin.
    session.add(MetricSample(
        instance_id=instance_id, collected_at=now - timedelta(seconds=30),
        metrics_json={"cluster_services": {"cluster": {"leader": "n1", "has_leader": True}, "services": []}},
    ))
    await session.commit()


async def test_a_single_down_sample_is_treated_as_a_probe_hiccup():
    async with SessionLocal() as session:
        instance = await _instance(session, cluster_name="pg", services=["etcd"])
        await _seed_cluster(session, instance.id, down_samples=1)
        result = await cluster_section(_ctx(session, [instance]))

    assert not any("etcd servisi" in f.title for f in result.findings)


async def test_repeated_down_samples_produce_a_finding():
    async with SessionLocal() as session:
        instance = await _instance(session, cluster_name="pg", services=["etcd"])
        await _seed_cluster(session, instance.id, down_samples=SERVICE_DOWN_MIN_SAMPLES)
        result = await cluster_section(_ctx(session, [instance]))

    finding = next(f for f in result.findings if "etcd servisi" in f.title)
    assert finding.evidence["value"] == SERVICE_DOWN_MIN_SAMPLES


# --- (D) Grup bulguları anlık görüntüden geliyor, dönemsel değil --------------------------


async def test_group_findings_say_they_come_from_a_point_in_time_snapshot():
    """GroupHealthSnapshot grup başına TEK satır tutar (son kontrol). Dönemsel bir ölçüm gibi
    sunmak yanıltıcı olurdu."""
    async with SessionLocal() as session:
        from app.models import Application, Customer, DatabaseGroup

        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"c-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"a-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"g-{suffix}", engine="postgresql",
            topology="patroni", environment="prod",
        )
        session.add(group)
        await session.commit()

        instance = await _instance(session, cluster_name="pg", services=["patroni"])
        instance.group_id = group.id
        await session.commit()
        await _seed_cluster(session, instance.id, down_samples=0)

        session.add(GroupHealthSnapshot(
            group_id=group.id, overall="critical",
            report_json={"split_brain": True, "split_brain_nodes": ["n1", "n2"], "nodes": []},
            checked_at=datetime.now(UTC) - timedelta(days=5),
        ))
        await session.commit()

        result = await cluster_section(_ctx(session, [instance]))

    finding = next(f for f in result.findings if "split-brain" in f.title)
    assert finding.note, "anlık görüntü uyarısı olmalı"
    assert "anlık" in finding.note
    # Görüntü dönem dışındaysa bu da söylenmeli.
    assert "dışında" in finding.note
