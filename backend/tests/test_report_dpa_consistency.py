"""Faz 18 İŞ 1 — rapor ve DPA aynı gerçeği göstermeli.

**Bildirilen hata:** Rapor "en pahalı sorgular"da bir sorgu gösteriyor, aynı sorgu DPA'da hiç
yok. Öneri "DPA'da EXPLAIN'e bakın" diyor ama tıklanınca sorgu orada bulunamıyor.

**Kök neden:** iki ayrı seçim mantığı vardı — rapor dönem farkına, DPA yalnızca son toplama
döngüsüne bakıyordu. Collector her döngüde ilk 20 satırı sakladığı için, dönem içinde bir kez
öne çıkıp sonra listeden düşen bir sorgu raporda görünüyor ama DPA'da görünmüyordu.

Bu testler tek tek fonksiyonları değil, **iki görünümün asla ayrışamayacağını** kanıtlıyor.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.report_sections import performance_section
from app.services.slow_query_selection import (
    classify_system_query,
    find_slow_query,
    query_fingerprint,
    select_slow_queries,
)


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _at(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


async def _instance(session) -> Instance:
    instance = Instance(
        name=f"cons-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


async def _seed(session, instance_id: int, rows: list[dict]) -> None:
    for row in rows:
        session.add(SlowQuerySample(instance_id=instance_id, **row))
    await session.commit()


def _ctx(session, instances, hours: int = 2) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session,
        scope=hr.ReportScope("global", None, "Tüm sistem"),
        instances=instances,
        period_start=end - timedelta(hours=hours),
        period_end=end,
        previous=None,
        previous_findings={},
    )


async def _seed_the_reported_scenario(session, instance_id: int) -> None:
    """Bildirilen senaryo: dönem içinde öne çıkıp SON döngüde listeden düşen bir sorgu.

    Eski kodda rapor bunu gösterip DPA gösteremiyordu.
    """
    await _seed(
        session,
        instance_id,
        [
            # "kaybolan": ilk iki döngüde var, son döngüde yok (collector'ın ilk 20'sinden düştü)
            dict(collected_at=_at(60), queryid="vanish", query="SELECT * FROM orders WHERE id = $1",
                 calls=100, total_time_ms=5_000, mean_time_ms=50),
            dict(collected_at=_at(40), queryid="vanish", query="SELECT * FROM orders WHERE id = $1",
                 calls=200, total_time_ms=25_000, mean_time_ms=125),
            # "kalıcı": her döngüde var
            dict(collected_at=_at(60), queryid="stay", query="SELECT * FROM users WHERE id = $1",
                 calls=10, total_time_ms=500, mean_time_ms=50),
            dict(collected_at=_at(40), queryid="stay", query="SELECT * FROM users WHERE id = $1",
                 calls=20, total_time_ms=1_500, mean_time_ms=75),
            dict(collected_at=_at(5), queryid="stay", query="SELECT * FROM users WHERE id = $1",
                 calls=30, total_time_ms=3_000, mean_time_ms=100),
        ],
    )


# --- Asıl regresyon ----------------------------------------------------------------------


async def test_a_query_the_report_mentions_is_findable_in_dpa():
    """Bildirilen hatanın birebir regresyonu: son döngüde olmayan bir sorgu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_the_reported_scenario(session, instance.id)
        ctx = _ctx(session, [instance])

        result = await performance_section(ctx)
        mentioned = {row["key"] for row in result.data["top_queries"]}
        assert mentioned, "rapor hiç sorgu göstermedi — senaryo kurulamadı"

        # DPA'nın gördüğü (aynı pencere, aynı servis).
        dpa = await select_slow_queries(
            session, instance.id, start=ctx.period_start, end=ctx.period_end, limit=100
        )
        visible = {e.key for e in dpa.entries}

    assert mentioned <= visible, (
        "Raporun bahsettiği sorgular DPA'da bulunamadı: " f"{mentioned - visible}"
    )


async def test_every_performance_finding_link_lands_on_an_existing_query():
    """Bulgunun derin bağlantısı gerçekten o sorguya gitmeli — 'bulunamadı' bir hatadır."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_the_reported_scenario(session, instance.id)
        ctx = _ctx(session, [instance])

        result = await performance_section(ctx)
        assert result.findings, "senaryo bulgu üretmedi"

        for finding in result.findings:
            link = finding.link_hint
            assert link, f"{finding.title}: derin bağlantı yok"
            params = parse_qs(urlparse(link).query)
            assert params["tab"] == ["queries"]

            found = await find_slow_query(
                session,
                instance.id,
                key=params["qkey"][0],
                start=datetime.fromisoformat(params["start"][0]),
                end=datetime.fromisoformat(params["end"][0]),
            )
            assert found is not None, f"{finding.title}: bağlantının hedefi bulunamadı ({link})"
            assert found.key == params["qkey"][0]


async def test_report_and_dpa_rank_identically_for_the_same_window():
    """Aynı pencere + aynı sıralama ölçütü → aynı sıra. Tek servis kullanıldığının kanıtı."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_the_reported_scenario(session, instance.id)
        ctx = _ctx(session, [instance])

        report = await performance_section(ctx)
        dpa = await select_slow_queries(
            session, instance.id, start=ctx.period_start, end=ctx.period_end, sort="total", limit=100
        )

    report_order = [row["key"] for row in report.data["top_queries"] if row["instance_id"] == instance.id]
    dpa_order = [e.key for e in dpa.entries]
    assert report_order == dpa_order[: len(report_order)]


# --- Kimlik parçalanması (aynı sorgunun iki kez listelenmesi) ----------------------------


async def test_same_query_with_and_without_queryid_is_not_listed_twice():
    """pg_stat_statements ayrıcalıksız rolde bazı satırlarda queryid'yi NULL döndürür.

    Eski gruplama (`queryid or query`) bu yüzden tek sorguyu iki gruba bölüyor ve raporda aynı
    sorgu iki kez görünüyordu.
    """
    text = "SELECT * FROM invoices WHERE customer_id = $1"
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed(
            session,
            instance.id,
            [
                dict(collected_at=_at(60), queryid=None, query=text, calls=10, total_time_ms=1_000, mean_time_ms=100),
                dict(collected_at=_at(30), queryid="abc", query=text, calls=50, total_time_ms=9_000, mean_time_ms=180),
                dict(collected_at=_at(5), queryid="abc", query=text, calls=90, total_time_ms=20_000, mean_time_ms=222),
            ],
        )
        selection = await select_slow_queries(session, instance.id, start=_at(120), end=_at(0), limit=50)

    assert len(selection.entries) == 1, f"aynı sorgu {len(selection.entries)} kez listelendi"
    assert selection.entries[0].calls == 80  # 90 - 10, tek grup olarak


async def test_query_fingerprint_ignores_whitespace_and_case():
    a = query_fingerprint("SELECT  *   FROM t\nWHERE x = 1")
    b = query_fingerprint("select * from T where x = 1".replace("T", "t"))
    assert a == b


# --- Sistem sorgusu sınıflandırması (İŞ 2'nin temeli) -----------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "SELECT pg_walfile_name_offset(pg_current_wal_lsn())",
        "SELECT * FROM pg_catalog.pg_class",
        "SELECT * FROM pg_stat_activity",
        "SELECT * FROM information_schema.columns",
        "-- ext:hypopg\nSELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = $1)",
    ],
)
def test_system_queries_are_classified_with_a_reason(query: str):
    reason = classify_system_query(query)
    assert reason, f"sistem sorgusu tanınmadı: {query}"


def test_application_queries_are_not_classified_as_system():
    for query in (
        "SELECT * FROM orders WHERE customer_id = $1",
        "UPDATE invoices SET paid = true WHERE id = $1",
        "INSERT INTO events (name, at) VALUES ($1, $2)",
    ):
        assert classify_system_query(query) is None, query


async def test_system_queries_are_excluded_by_default_but_counted():
    """Bildirilen örnekteki pg_walfile_name_offset sorgusu artık bulgu üretmemeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed(
            session,
            instance.id,
            [
                dict(collected_at=_at(60), queryid="wal", query="SELECT pg_walfile_name_offset(pg_current_wal_lsn())",
                     calls=0, total_time_ms=0, mean_time_ms=0),
                dict(collected_at=_at(5), queryid="wal", query="SELECT pg_walfile_name_offset(pg_current_wal_lsn())",
                     calls=1, total_time_ms=206, mean_time_ms=205.6),
            ],
        )
        default = await select_slow_queries(session, instance.id, start=_at(120), end=_at(0))
        with_system = await select_slow_queries(
            session, instance.id, start=_at(120), end=_at(0), include_system=True, min_total_ms=0, min_calls=0
        )

    assert default.entries == []
    assert default.filtered_system == 1, "filtrelenen sorgu sayısı görünür olmalı"
    assert [e.queryid for e in with_system.entries] == ["wal"]
    assert with_system.entries[0].system_reason


async def test_single_call_query_produces_no_finding():
    """Bildirilen örnek: 1 çağrı, 206 ms, "%+229" — istatistiksel olarak değişmiş ama
    pratikte önemsiz. Bulgu üretilmemeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed(
            session,
            instance.id,
            [
                # Uygulama sorgusu (sistem filtresine takılmasın), ama tek çağrı.
                dict(collected_at=_at(90), queryid="one", query="SELECT * FROM rare_report_table",
                     calls=0, total_time_ms=0, mean_time_ms=0),
                dict(collected_at=_at(5), queryid="one", query="SELECT * FROM rare_report_table",
                     calls=1, total_time_ms=206, mean_time_ms=205.6),
            ],
        )
        ctx = _ctx(session, [instance])
        result = await performance_section(ctx)

    assert result.findings == [], "tek çağrılık sorgu trend bulgusu üretmemeli"


# --- Tüm bölümlerin hedef denetimi -------------------------------------------------------

# Arayüzdeki gerçek sekme adları. Bir bölüm bunlardan biri dışına işaret ediyorsa kullanıcı
# boş/yanlış bir sayfaya düşer.
INSTANCE_TABS = {
    "overview", "metrics", "queries", "activity", "cluster",
    "schema", "tuning", "alerts", "predictions",
}
GROUP_TABS = {"nodes", "parameters", "alwayson"}

# Bölüm → instance sekmesi varsayılan eşlemesi (frontend ReportFindingCard.deepLink ile birebir).
SECTION_TAB = {
    "availability": "overview",
    "cluster": "cluster",
    "performance": "queries",
    "resources": "metrics",
    "schema": "schema",
    "alerts": "alerts",
    "capacity": "predictions",
    "parameters": "tuning",
    "prerequisites": "tuning",
}


def test_section_tab_mapping_only_points_at_tabs_that_exist():
    """Bölüm→sekme eşlemesindeki her hedef arayüzde gerçekten var olmalı."""
    unknown = {section: tab for section, tab in SECTION_TAB.items() if tab not in INSTANCE_TABS}
    assert not unknown, f"var olmayan sekmeye işaret eden bölümler: {unknown}"


def test_explicit_link_hints_point_at_existing_pages():
    """`link_hint` üreten yardımcılar var olan sayfa/sekme kombinasyonlarını üretmeli."""
    from types import SimpleNamespace

    from app.services.report_sections import _parameter_link

    # Gruba bağlı instance → grup sayfasının Parametreler sekmesi.
    link = _parameter_link(SimpleNamespace(group_id=7))
    assert link == "/groups/7?tab=parameters"
    assert link.split("tab=")[1] in GROUP_TABS

    # Grupsuz instance için parametre denetimi sayfası YOK — bağlantı vaat edilmemeli.
    assert _parameter_link(SimpleNamespace(group_id=None)) is None


async def test_parameter_findings_do_not_point_at_the_instance_tuning_tab():
    """Parametre denetimi arayüzü grup sayfasında; instance "tuning" sekmesinde parametre yok.

    Bu, Faz 18 İŞ 1 hedef denetiminde bulunan ikinci yanlış yönlendirmeydi.
    """
    from app.models import DailyStateSnapshot, DatabaseGroup, Application, Customer
    from app.services.report_sections import parameters_section

    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"c-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"a-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"g-{suffix}", engine="postgresql",
            topology="standalone", environment="prod",
        )
        session.add(group)
        await session.commit()

        instance = await _instance(session)
        instance.group_id = group.id
        await session.commit()

        session.add(
            DailyStateSnapshot(
                instance_id=instance.id, day=datetime.now(UTC).date(), kind="parameters",
                payload={
                    "findings": [{"name": "fsync", "severity": "critical", "current_value": "off",
                                  "detail": "beklenen: on", "recommendation": "fsync=on yapın"}],
                    "values": {"fsync": "off"},
                },
            )
        )
        await session.commit()

        result = await parameters_section(_ctx(session, [instance]))

    finding = next(f for f in result.findings if "fsync" in f.title)
    assert finding.link_hint == f"/groups/{group.id}?tab=parameters"


async def test_unused_index_finding_is_bound_to_an_instance():
    """Kapsam seviyesi bulgu hiçbir sayfaya bağlanamıyordu; artık instance başına üretiliyor."""
    from app.models import SchemaObjectDailySample
    from app.services.report_sections import schema_section

    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(
            SchemaObjectDailySample(
                instance_id=instance.id, day=datetime.now(UTC).date(), object_kind="index",
                schema_name="app", object_name="orders.idx_unused", size_bytes=500_000,
                extra={"idx_scan": 0},
            )
        )
        await session.commit()
        result = await schema_section(_ctx(session, [instance]))

    finding = next(f for f in result.findings if "kullanılmayan index" in f.title)
    assert finding.related_object_type == "instance"
    assert finding.related_object_id == instance.id
    assert SECTION_TAB[finding.section] in INSTANCE_TABS
