"""Bekleme analizi ön koşulları (Faz 25 İŞ 5).

Bekleme örnekleyicisinin yetkisiz çalışması, boş bir ekrandan DAHA KÖTÜDÜR. Yetkisiz bir
PostgreSQL rolü `pg_stat_activity`'de diğer kullanıcıların satırlarını görür ama `state`,
`query` ve `wait_event` alanları NULL gelir. Sonuç sinsi: örnekleyici çalışır, veri birikir,
grafik çizilir — ama yalnızca kendi oturumlarını sayar. Ekran "sunucu sakin" der, sunucu
yanarken. Yani hata mesajı değil, YANLIŞ VERİ üretir.

Bu yüzden kontrol yalnızca rol üyeliğine bakmıyor: kaç oturumun maskelendiğini SAYIP
kullanıcıya "ne kadarını görebiliyorsun" olarak yazıyor — kullanıcının isteği de tam buydu.
"""

from __future__ import annotations

import sys
import types

from app.collectors.base import ConnectionTarget
from app.services.prerequisites import (
    PrerequisiteCheck,
    check_postgresql_prerequisites,
    check_sqlserver_prerequisites,
)
from tests.fakes import FakeAsyncConnection, FakeSqlServerConnection


def _pg_target() -> ConnectionTarget:
    return ConnectionTarget(host="pg.internal", port=5432, database="app", username="dbace", password="p")


def _sqlserver_target() -> ConnectionTarget:
    return ConnectionTarget(host="sql.internal", port=1433, database="master", username="sa", password="p")


def _pg_responses(**overrides) -> dict:
    base = {
        "WHERE e.extname = $1": "public",
        "SHOW shared_preload_libraries": "pg_stat_statements,auto_explain",
        "SHOW pg_stat_statements.track": "top",
        "pg_has_role(current_user, 'pg_read_all_stats'": True,
        'FROM "public".pg_stat_statements': {"total": 42, "redacted": 0},
        "pg_has_role(current_user, 'pg_monitor'": True,
        "-- ext:hypopg": True,
        "-- ext:pg_qualstats": True,
        "-- ext:pg_buffercache": True,
        "SHOW track_io_timing": "on",
        "server_version_num": 160000,
        "FROM pg_stat_activity": {"toplam": 5, "maskeli": 0},
        "SHOW compute_query_id": "on",
        # Faz 26 İŞ 1 — auto_explain ayarları.
        "SHOW auto_explain.log_min_duration": "1s",
        "SHOW auto_explain.log_format": "json",
        "SHOW auto_explain.log_analyze": "on",
    }
    base.update(overrides)
    return base


def _sqlserver_responses(**overrides) -> dict:
    base = {
        "SET LOCK_TIMEOUT": ([], []),
        "HAS_PERMS_BY_NAME": ([(1,)], [("x",)]),
        "sys.dm_exec_query_stats": ([(b"hash",)], [("query_hash",)]),
        "sys.database_query_store_options": (
            [("READ_WRITE", "READ_WRITE")],
            [("actual", ""), ("desired", "")],
        ),
        "sys.dm_os_waiting_tasks": ([(3,)], [("", "")]),
    }
    base.update(overrides)
    return base


async def _patch_pg(monkeypatch, conn: FakeAsyncConnection) -> None:
    async def fake_connect(**kwargs):
        return conn

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(connect=fake_connect))


async def _patch_mssql(monkeypatch, conn: FakeSqlServerConnection) -> None:
    async def fake_connect(**kwargs):
        return conn

    monkeypatch.setitem(sys.modules, "aioodbc", types.SimpleNamespace(connect=fake_connect))


def _by_key(checks: list[PrerequisiteCheck], key: str) -> PrerequisiteCheck:
    return next(c for c in checks if c.key == key)


# --- PostgreSQL: görünürlük -------------------------------------------------------------


async def test_full_visibility_is_reported_ok(monkeypatch):
    conn = FakeAsyncConnection(_pg_responses())
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "wait_visibility")
    assert check.status == "ok"


async def test_partial_visibility_says_HOW_MUCH_can_be_seen(monkeypatch):
    """Kullanıcının açık isteği: "Yetki yoksa ne kadarını görebileceğini söyle."

    "Yetkiniz eksik" demek yetmez — 20 oturumdan 3'ünü görüyor olmak ile 19'unu görüyor olmak
    bambaşka kararlar gerektirir.
    """
    conn = FakeAsyncConnection(
        _pg_responses(
            **{
                "pg_has_role(current_user, 'pg_read_all_stats'": False,
                "FROM pg_stat_activity": {"toplam": 20, "maskeli": 17},
            }
        )
    )
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "wait_visibility")

    assert check.status == "partial"
    assert check.severity == "high"
    # Sayılar ölçüm: "20 oturumdan yalnızca 3 tanesini görebiliyor".
    assert "20" in check.impact and "3" in check.impact
    assert "17" in check.impact
    assert check.detail == "3/20 oturum görülebiliyor"
    assert "GRANT pg_read_all_stats" in check.fix


async def test_partial_visibility_warns_the_chart_will_be_WRONG_not_empty(monkeypatch):
    """Bu ayrım kritik: boş grafik kullanıcıyı uyarır, EKSİK grafik kandırır."""
    conn = FakeAsyncConnection(
        _pg_responses(
            **{
                "pg_has_role(current_user, 'pg_read_all_stats'": False,
                "FROM pg_stat_activity": {"toplam": 10, "maskeli": 8},
            }
        )
    )
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "wait_visibility")
    assert "sakin görünebilir" in check.impact


async def test_missing_role_is_not_green_just_because_nobody_else_is_connected(monkeypatch):
    """Tek kullanıcılı bir anda ölçülebilir kayıp olmaz — ama yeşil göstermek, yük geldiğinde
    körleşecek bir kurulumu onaylamak olurdu."""
    conn = FakeAsyncConnection(
        _pg_responses(
            **{
                "pg_has_role(current_user, 'pg_read_all_stats'": False,
                "FROM pg_stat_activity": {"toplam": 0, "maskeli": 0},
            }
        )
    )
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "wait_visibility")
    assert check.status == "partial"
    assert "başka kullanıcılar bağlandığında" in check.impact


# --- PostgreSQL: sorgu bazında ayrıştırma -----------------------------------------------


async def test_compute_query_id_off_is_reported_as_a_partial_loss(monkeypatch):
    """Kapalıysa bekleme kırılımı yine üretilir; kaybolan yalnızca "hangi SORGU" bilgisi.
    Bunu yüksek önem derecesiyle işaretlemek, çalışan bir özelliği bozukmuş gibi
    göstermek olurdu."""
    conn = FakeAsyncConnection(_pg_responses(**{"SHOW compute_query_id": "off"}))
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "compute_query_id")

    assert check.status == "missing"
    assert check.severity == "medium"
    assert "ALTER SYSTEM SET compute_query_id = on" in check.fix
    assert "yine üretilir" in check.impact


async def test_compute_query_id_auto_counts_as_on_when_the_extension_is_installed(monkeypatch):
    """'auto' tek başına bir cevap değil: pg_stat_statements yüklüyse açık demektir.
    'auto' görüp "kapalı" demek, kullanıcıyı gereksiz bir ayar değişikliğine yollardı."""
    conn = FakeAsyncConnection(_pg_responses(**{"SHOW compute_query_id": "auto"}))
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "compute_query_id")
    assert check.status == "ok"


async def test_pre_14_servers_are_told_the_truth_not_given_a_fake_fix(monkeypatch):
    """PostgreSQL 13'te query_id YOK. "Şu ayarı açın" demek çalışmayan bir düzeltme
    önermek olurdu; tek yol sürüm yükseltmesi ve öyle yazılıyor."""
    conn = FakeAsyncConnection(_pg_responses(**{"server_version_num": 130000}))
    await _patch_pg(monkeypatch, conn)
    check = _by_key(await check_postgresql_prerequisites(_pg_target()), "compute_query_id")

    assert check.status == "missing"
    assert check.severity == "low"
    assert "14" in check.name or "14" in check.impact
    assert "ALTER SYSTEM" not in (check.fix or "")


# --- SQL Server -------------------------------------------------------------------------


async def test_sqlserver_wait_dmvs_readable(monkeypatch):
    conn = FakeSqlServerConnection(_sqlserver_responses())
    await _patch_mssql(monkeypatch, conn)
    check = _by_key(await check_sqlserver_prerequisites(_sqlserver_target()), "wait_visibility")
    assert check.status == "ok"


async def test_sqlserver_denied_dmv_warns_about_wrong_data(monkeypatch):
    """Yetki bayrağı ile GERÇEK erişim ayrışabiliyor (sunucu düzeyinde DENY, Azure SQL
    kısıtları). Bayrağa güvenip örneklemeyi açmak, boş bir grafiğin sebebini gizlemek
    olurdu — bu yüzden fonksiyonel test var."""

    def deny(sql: str):
        raise RuntimeError("The user does not have permission to perform this action.")

    conn = FakeSqlServerConnection(_sqlserver_responses(**{"sys.dm_os_waiting_tasks": deny}))
    await _patch_mssql(monkeypatch, conn)
    check = _by_key(await check_sqlserver_prerequisites(_sqlserver_target()), "wait_visibility")

    assert check.status == "unauthorized"
    assert check.severity == "high"
    assert "GRANT VIEW SERVER STATE" in check.fix
    assert "YANLIŞ veri" in check.impact


async def test_sqlserver_unexpected_failure_is_unknown_not_unauthorized(monkeypatch):
    """Sebebi bilinmeyen bir hatayı "yetki eksik" diye raporlamak, kullanıcıyı yanlış
    düzeltmeye yollar."""

    def boom(sql: str):
        raise RuntimeError("Connection reset by peer")

    conn = FakeSqlServerConnection(_sqlserver_responses(**{"sys.dm_os_waiting_tasks": boom}))
    await _patch_mssql(monkeypatch, conn)
    check = _by_key(await check_sqlserver_prerequisites(_sqlserver_target()), "wait_visibility")
    assert check.status == "unknown"
