"""Düğümler arası yapılandırma sapması (Faz 28 İŞ 4).

Mevcut parametre denetimi ZAMAN eksenli ("dünden beri ne değişti"); eksik olan DÜĞÜMLER
ARASI sapma. Cluster'da asıl arıza sebebi budur çünkü sapma normal çalışmada hiçbir belirti
vermez ve **tam olarak failover anında** ortaya çıkar.

Bu dosyanın koruduğu fikirler:

1. **Sınıflandırma şart.** "Bütün parametreler aynı olsun" demek gürültü üretir: replikada
   `hot_standby` açık, `primary_conninfo` dolu olur. Hepsini sapma saymak gerçek sapmayı
   görünmez yapardı.
2. **Okunamayan değer "aynı" değildir.** Eksik veriyi uyum gibi sunmak, tam da görülmesi
   gereken sapmayı gizler.
3. **Canlı ve saklanmış yol aynı karşılaştırmayı kullanır** — sekmede "sapma yok" derken
   raporda "3 sapma" yazmamalı.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.domain.config_drift import (
    PG_COMPARED,
    SQLSERVER_COMPARED,
    DriftClass,
    compare_nodes,
    spec_for,
)
from app.models import Application, Customer, DailyStateSnapshot, DatabaseGroup, Instance
from app.services import health_report as hr
from app.services.config_comparison import (
    SNAPSHOT_KIND,
    build_comparison,
    compare_group_from_snapshots,
)
from app.services.credentials import encrypt_secret
from app.services.report_sections import config_drift_section


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


# --- Sınıflandırma ------------------------------------------------------------------------


def test_every_compared_parameter_explains_its_consequence():
    """"Farklı" demek yetmiyor: sapmanın neden önemli olduğu, ancak failover anında ne
    olacağı söylenerek anlaşılıyor."""
    for parameter in PG_COMPARED + SQLSERVER_COMPARED:
        assert parameter.consequence, parameter.name
        if parameter.drift_class == DriftClass.MAY_DIFFER:
            # Bunlar bulgu üretmiyor; tek satırlık bir "neden normal" açıklaması yeterli.
            continue
        # Bulgu üreten sınıflarda açıklama, sapmanın SONUCUNU anlatacak kadar dolu olmalı;
        # "farklı" demek kullanıcıya hiçbir karar verdirmiyor.
        assert len(parameter.consequence) > 30, parameter.name


def test_parameters_that_postgresql_itself_requires_are_must_match():
    """Bu beş parametre tercih değil, motorun kuralı: standby'daki değer primary'dekinden
    küçükse kurtarma DURUR."""
    for name in (
        "max_connections",
        "max_worker_processes",
        "max_wal_senders",
        "max_prepared_transactions",
        "max_locks_per_transaction",
    ):
        spec = spec_for("postgresql", name)
        assert spec is not None, name
        assert spec.drift_class == DriftClass.MUST_MATCH, name


def test_role_specific_parameters_are_allowed_to_differ():
    """Replikada `hot_standby` açık, `primary_conninfo` dolu olur. Bunları sapma saymak,
    listeyi gürültüye boğup gerçek sapmayı görünmez yapardı."""
    for name in ("hot_standby", "primary_conninfo", "synchronous_standby_names", "port"):
        spec = spec_for("postgresql", name)
        assert spec is not None, name
        assert spec.drift_class == DriftClass.MAY_DIFFER, name


def test_delayed_replica_setting_is_not_a_drift():
    """Gecikmeli replika bilinçli olarak farklıdır."""
    assert spec_for("postgresql", "recovery_min_apply_delay").drift_class == DriftClass.MAY_DIFFER


def test_sqlserver_maxdop_is_compared():
    """Always On'da en sık gözden kaçan sapma budur."""
    spec = spec_for("sqlserver", "max degree of parallelism")
    assert spec is not None
    assert spec.drift_class == DriftClass.SHOULD_MATCH


# --- Karşılaştırma ------------------------------------------------------------------------


def test_identical_nodes_produce_no_drift():
    rows = compare_nodes(
        "postgresql",
        {
            "node-1": {"work_mem": "4MB", "max_connections": "200"},
            "node-2": {"work_mem": "4MB", "max_connections": "200"},
        },
    )
    assert not any(r.diverged for r in rows)


def test_must_match_divergence_is_critical():
    rows = compare_nodes(
        "postgresql",
        {"node-1": {"max_connections": "200"}, "node-2": {"max_connections": "100"}},
    )
    row = next(r for r in rows if r.name == "max_connections")
    assert row.diverged
    assert row.severity == "critical"


def test_should_match_divergence_is_a_warning():
    rows = compare_nodes(
        "postgresql", {"node-1": {"work_mem": "4MB"}, "node-2": {"work_mem": "16MB"}}
    )
    row = next(r for r in rows if r.name == "work_mem")
    assert row.diverged
    assert row.severity == "warning"


def test_may_differ_divergence_is_only_informational():
    rows = compare_nodes(
        "postgresql", {"node-1": {"hot_standby": "on"}, "node-2": {"hot_standby": "off"}}
    )
    row = next(r for r in rows if r.name == "hot_standby")
    assert row.diverged
    assert row.severity == "info"


def test_an_unreadable_value_is_not_counted_as_matching():
    """Eksik veriyi "uyumlu" saymak, tam da görülmesi gereken sapmayı gizlerdi."""
    rows = compare_nodes(
        "postgresql", {"node-1": {"work_mem": "4MB"}, "node-2": {}}
    )
    row = next(r for r in rows if r.name == "work_mem")
    assert row.diverged is False  # karşılaştırılacak ikinci değer yok
    assert row.missing_nodes == ["node-2"]


def test_severe_rows_sort_first():
    """Alfabetik sıralamada kritik bir sapma listenin ortasında kalır ve gözden kaçardı."""
    rows = compare_nodes(
        "postgresql",
        {
            "node-1": {"work_mem": "4MB", "max_connections": "200", "hot_standby": "on"},
            "node-2": {"work_mem": "8MB", "max_connections": "100", "hot_standby": "off"},
        },
    )
    severities = [r.severity for r in rows if r.diverged]
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s])
    assert rows[0].name == "max_connections"


def test_trace_flag_difference_is_reported_for_sqlserver():
    result = build_comparison(
        "sqlserver",
        {
            "node-1": {"_trace_flags": "1117,1118", "max degree of parallelism": "4"},
            "node-2": {"_trace_flags": "1117", "max degree of parallelism": "4"},
        },
    )
    row = next(r for r in result["rows"] if r["name"] == "Trace flag'ler")
    assert row["diverged"] is True
    assert row["severity"] == "warning"


def test_unreadable_trace_flags_are_not_shown_as_empty():
    """Trace flag okumak ayrı yetki gerektirebiliyor; "(yok)" yazmak kapalı oldukları
    izlenimini verirdi."""
    result = build_comparison(
        "sqlserver",
        {"node-1": {"_trace_flags": "1117"}, "node-2": {"_trace_flags": None}},
    )
    row = next(r for r in result["rows"] if r["name"] == "Trace flag'ler")
    assert row["missing_nodes"] == ["node-2"]
    assert row["diverged"] is False


def test_trace_flag_row_is_absent_when_nothing_was_read():
    result = build_comparison("sqlserver", {"node-1": {}, "node-2": {}})
    assert not [r for r in result["rows"] if r["name"] == "Trace flag'ler"]


# --- Saklanmış fotoğraflardan karşılaştırma ------------------------------------------------


async def _group(session, engine="postgresql") -> DatabaseGroup:
    customer = Customer(name=f"c-{uuid.uuid4().hex[:8]}")
    session.add(customer)
    await session.commit()
    application = Application(customer_id=customer.id, name=f"a-{uuid.uuid4().hex[:8]}")
    session.add(application)
    await session.commit()
    group = DatabaseGroup(
        application_id=application.id, name=f"g-{uuid.uuid4().hex[:8]}",
        engine=engine, topology="patroni",
    )
    session.add(group)
    await session.commit()
    return group


async def _instance(session, group, **over) -> Instance:
    base = dict(
        name=f"cfg-{uuid.uuid4().hex[:8]}", engine=group.engine, host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
        group_id=group.id,
    )
    base.update(over)
    instance = Instance(**base)
    session.add(instance)
    await session.commit()
    return instance


def _snapshot(instance_id: int, settings: dict, day: date | None = None) -> DailyStateSnapshot:
    return DailyStateSnapshot(
        instance_id=instance_id,
        day=day or datetime.now(UTC).date(),
        kind=SNAPSHOT_KIND,
        payload={"settings": settings},
    )


async def test_snapshot_comparison_finds_the_drift():
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        session.add(_snapshot(primary.id, {"work_mem": "4MB", "max_connections": "200"}))
        session.add(_snapshot(replica.id, {"work_mem": "4MB", "max_connections": "100"}))
        await session.commit()

        result = await compare_group_from_snapshots(session, group, [primary, replica])

    assert result["critical_count"] == 1
    row = next(r for r in result["rows"] if r["name"] == "max_connections")
    assert row["values"][primary.name] == "200"
    assert row["values"][replica.name] == "100"


async def test_a_missing_snapshot_is_reported_not_silently_ignored():
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        session.add(_snapshot(primary.id, {"work_mem": "4MB"}))
        await session.commit()

        result = await compare_group_from_snapshots(session, group, [primary, replica])

    assert replica.name in result["errors"]
    assert result["diverged_count"] == 0


async def test_single_node_group_says_why_it_cannot_compare():
    async with SessionLocal() as session:
        group = await _group(session)
        only = await _instance(session, group)
        result = await compare_group_from_snapshots(session, group, [only])
    assert result["unavailable_reason"]
    assert result["rows"] == []


async def test_the_newest_snapshot_per_node_is_used():
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        today = datetime.now(UTC).date()
        session.add(_snapshot(primary.id, {"work_mem": "4MB"}, day=today))
        session.add(_snapshot(primary.id, {"work_mem": "64MB"}, day=today - timedelta(days=1)))
        session.add(_snapshot(replica.id, {"work_mem": "4MB"}, day=today))
        await session.commit()

        result = await compare_group_from_snapshots(session, group, [primary, replica])

    row = next(r for r in result["rows"] if r["name"] == "work_mem")
    assert row["diverged"] is False


# --- Rapor bölümü -------------------------------------------------------------------------


def _ctx(session, instances) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(days=1), period_end=end, previous=None, previous_findings={},
    )


async def test_section_produces_a_critical_finding_with_failover_impact():
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        session.add(_snapshot(primary.id, {"max_connections": "200"}))
        session.add(_snapshot(replica.id, {"max_connections": "100"}))
        await session.commit()

        result = await config_drift_section(_ctx(session, [primary, replica]))

    assert result.status == "critical"
    finding = next(f for f in result.findings if "max_connections" in f.title)
    # İş etkisi failover riski olarak anlatılmalı.
    assert "failover" in finding.advice.why.lower()
    assert finding.advice.steps and finding.advice.cautions and finding.advice.verification
    assert finding.related_object_type == "group"


async def test_section_says_unknown_without_two_readable_snapshots():
    """İki düğümün yapılandırması okunamadıysa "sapma yok" DEMİYORUZ."""
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        await session.commit()

        result = await config_drift_section(_ctx(session, [primary, replica]))

    assert result.status == "unknown"
    assert result.unknown_reason


async def test_section_is_ok_when_nodes_agree():
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        session.add(_snapshot(primary.id, {"max_connections": "200", "work_mem": "4MB"}))
        session.add(_snapshot(replica.id, {"max_connections": "200", "work_mem": "4MB"}))
        await session.commit()

        result = await config_drift_section(_ctx(session, [primary, replica]))

    assert result.status == "ok"
    assert result.findings == []


async def test_role_specific_differences_do_not_create_findings():
    """`hot_standby` farkı bulguya dönüşmemeli — her replikada olur ve her raporda görünmesi
    listeyi işe yaramaz hale getirirdi."""
    async with SessionLocal() as session:
        group = await _group(session)
        primary = await _instance(session, group)
        replica = await _instance(session, group)
        session.add(_snapshot(primary.id, {"hot_standby": "off", "work_mem": "4MB"}))
        session.add(_snapshot(replica.id, {"hot_standby": "on", "work_mem": "4MB"}))
        await session.commit()

        result = await config_drift_section(_ctx(session, [primary, replica]))

    assert result.findings == []
    assert result.status == "ok"


async def test_single_node_groups_are_not_compared_at_all():
    async with SessionLocal() as session:
        group = await _group(session)
        only = await _instance(session, group)
        result = await config_drift_section(_ctx(session, [only]))
    assert result.status == "unknown"
    assert "iki düğüm" in (result.unknown_reason or "")
