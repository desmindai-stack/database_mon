"""Faz 18 İŞ 2 — gürültü filtresi eşikleri ayarlanabilir ve rapor/DPA ortak.

Eşikler koda gömülü olsaydı her ortam için doğru olamazdı (OLTP'de 1 sn ciddi, raporlama
veritabanında sıradan). Bu testler eşiklerin gerçekten etki ettiğini VE rapor ile DPA'nın
aynı ayarı okuduğunu kanıtlıyor — ikisinin ayrışması Faz 18 İŞ 1'deki hatayı geri getirirdi.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance, SlowQuerySample
from app.services import health_report as hr
from app.services.credentials import encrypt_secret
from app.services.noise_settings import DEFAULTS, get_noise_settings, set_noise_settings
from app.services.report_sections import performance_section
from app.services.slow_query_selection import select_slow_queries
from tests.auth_helper import authed_client


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()
    yield
    # Ayarlar global; diğer testleri etkilememesi için varsayılana döndür.
    async with SessionLocal() as session:
        await set_noise_settings(
            session,
            list_min_total_ms=DEFAULTS["list_min_total_ms"],
            list_min_calls=DEFAULTS["list_min_calls"],
            finding_min_total_ms=DEFAULTS["finding_min_total_ms"],
            finding_min_calls=DEFAULTS["finding_min_calls"],
            show_system_queries=DEFAULTS["show_system_queries"],
        )


def _at(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)


async def _instance(session) -> Instance:
    instance = Instance(
        name=f"noise-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"), enabled=True,
    )
    session.add(instance)
    await session.commit()
    return instance


async def _seed_query(session, instance_id: int, *, calls: int, total_ms: float, text: str) -> None:
    session.add(SlowQuerySample(
        instance_id=instance_id, collected_at=_at(90), queryid="q1", query=text,
        calls=0, total_time_ms=0, mean_time_ms=0,
    ))
    session.add(SlowQuerySample(
        instance_id=instance_id, collected_at=_at(5), queryid="q1", query=text,
        calls=calls, total_time_ms=total_ms, mean_time_ms=total_ms / max(calls, 1),
    ))
    await session.commit()


def _ctx(session, instances) -> hr.ReportContext:
    end = datetime.now(UTC)
    return hr.ReportContext(
        session=session, scope=hr.ReportScope("global", None, "x"), instances=instances,
        period_start=end - timedelta(hours=3), period_end=end, previous=None, previous_findings={},
    )


async def test_defaults_are_returned_when_nothing_is_configured():
    async with SessionLocal() as session:
        settings = await get_noise_settings(session)

    assert settings["list_min_total_ms"] == DEFAULTS["list_min_total_ms"]
    assert settings["finding_min_calls"] == DEFAULTS["finding_min_calls"]
    assert settings["show_system_queries"] is False


async def test_list_threshold_actually_filters():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_query(session, instance.id, calls=10, total_ms=250, text="SELECT * FROM small_table")

        low = await select_slow_queries(session, instance.id, start=_at(180), end=_at(0), min_total_ms=100)
        high = await select_slow_queries(session, instance.id, start=_at(180), end=_at(0), min_total_ms=500)

    assert len(low.entries) == 1
    assert high.entries == []
    assert high.filtered_insignificant == 1, "elenen sorgu sayısı görünür olmalı"


async def test_raising_the_finding_threshold_silences_a_finding():
    """Aynı sorgu, eşik yükseltilince bulgu üretmemeli — ayarın gerçekten etkisi var."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_query(session, instance.id, calls=20, total_ms=4000, text="SELECT * FROM orders WHERE x = $1")

        await set_noise_settings(session, finding_min_total_ms=1000, finding_min_calls=5)
        before = await performance_section(_ctx(session, [instance]))

        await set_noise_settings(session, finding_min_total_ms=10_000)
        after = await performance_section(_ctx(session, [instance]))

    assert before.findings, "varsayılan eşikte bulgu üretilmeliydi"
    assert after.findings == [], "eşik yükseltilince bulgu susmalıydı"
    # Liste eşiği değişmediği için sorgu listede kalmalı — susturulan yalnızca bulgu.
    assert after.data["top_queries"]


async def test_report_and_dpa_read_the_same_setting():
    """Eşik değiştiğinde ikisi de aynı anda etkilenmeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_query(session, instance.id, calls=10, total_ms=250, text="SELECT * FROM t WHERE a = $1")

        await set_noise_settings(session, list_min_total_ms=500)
        noise = await get_noise_settings(session)

        report = await performance_section(_ctx(session, [instance]))
        dpa = await select_slow_queries(
            session, instance.id, start=_at(180), end=_at(0),
            min_total_ms=noise["list_min_total_ms"], min_calls=noise["list_min_calls"],
        )

    assert report.data.get("top_queries", []) == []
    assert dpa.entries == []


async def test_show_system_queries_setting_changes_the_default_view():
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_query(
            session, instance.id, calls=10, total_ms=900,
            text="SELECT pg_walfile_name_offset(pg_current_wal_lsn())",
        )
        await set_noise_settings(session, show_system_queries=False)
        instance_id = instance.id

    async with await authed_client() as c:
        hidden = (await c.get(f"/api/queries/{instance_id}")).json()

    async with SessionLocal() as session:
        await set_noise_settings(session, show_system_queries=True)

    async with await authed_client() as c:
        shown = (await c.get(f"/api/queries/{instance_id}")).json()

    assert hidden["items"] == []
    assert hidden["filtered_system"] == 1
    assert [i["queryid"] for i in shown["items"]] == ["q1"]
    assert shown["items"][0]["system_reason"]


async def test_explicit_query_param_overrides_the_setting():
    """Kullanıcı tek seferlik göstermek isteyebilir; ayar varsayılan, parametre son sözü söyler."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        await _seed_query(
            session, instance.id, calls=10, total_ms=900, text="SELECT * FROM pg_catalog.pg_class",
        )
        await set_noise_settings(session, show_system_queries=False)
        instance_id = instance.id

    async with await authed_client() as c:
        forced = (await c.get(f"/api/queries/{instance_id}?include_system=true")).json()

    assert len(forced["items"]) == 1


async def test_settings_endpoint_round_trip_and_validation():
    async with await authed_client() as c:
        updated = await c.put(
            "/api/admin/noise-settings",
            json={"list_min_total_ms": 250, "finding_min_calls": 12, "show_system_queries": True},
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["list_min_total_ms"] == 250
        assert body["finding_min_calls"] == 12
        assert body["show_system_queries"] is True
        assert body["defaults"]["finding_min_calls"] == DEFAULTS["finding_min_calls"]

        read_back = (await c.get("/api/admin/noise-settings")).json()
        assert read_back["list_min_total_ms"] == 250

        bad = await c.put("/api/admin/noise-settings", json={"finding_min_calls": -5})
        assert bad.status_code == 422


async def test_viewer_cannot_change_noise_settings():
    async with await authed_client(role="viewer") as c:
        r = await c.put("/api/admin/noise-settings", json={"list_min_total_ms": 1})
        assert r.status_code == 403
