"""Ön koşul denetimi (Faz 16 İŞ 1): dbace'in slow-query/EXPLAIN/index-advisor/tuning
özellikleri sessizce boş sonuç döndürdüğünde ("neden hiç öneri gelmiyor?") sebebi net şekilde
göstermek için — her kontrol {ad, durum, etki, düzeltme komutu} olarak raporlanır.

PostgreSQL ve SQL Server için ayrı kontrol setleri var; MongoDB desteklenmiyor (router 400 döner).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.collectors.base import ConnectionTarget
from app.domain.engines import DatabaseEngine

# status: "ok" | "missing" | "unauthorized" | "unknown"
# severity: bu kontrol "missing"/"unauthorized" olduğunda ne kadar ciddi — dashboard
# entegrasyonunda (dashboard_snapshot.py) hangi eksikliğin "high", hangisinin "medium" olarak
# gösterileceğini belirler.


@dataclass
class PrerequisiteCheck:
    key: str
    name: str
    status: str
    severity: str
    impact: str
    fix: str | None = None
    detail: str | None = None


def _ok(key: str, name: str, impact: str, detail: str | None = None, severity: str = "medium") -> PrerequisiteCheck:
    return PrerequisiteCheck(key=key, name=name, status="ok", severity=severity, impact=impact, detail=detail)


def _missing(
    key: str, name: str, severity: str, impact: str, fix: str, detail: str | None = None
) -> PrerequisiteCheck:
    return PrerequisiteCheck(key=key, name=name, status="missing", severity=severity, impact=impact, fix=fix, detail=detail)


def _unauthorized(
    key: str, name: str, severity: str, impact: str, fix: str, detail: str | None = None
) -> PrerequisiteCheck:
    return PrerequisiteCheck(
        key=key, name=name, status="unauthorized", severity=severity, impact=impact, fix=fix, detail=detail
    )


def _unknown(key: str, name: str, severity: str, impact: str, detail: str | None = None) -> PrerequisiteCheck:
    return PrerequisiteCheck(key=key, name=name, status="unknown", severity=severity, impact=impact, detail=detail)


async def _pg_extension_installed(conn, extname: str) -> bool:
    # The -- ext:<name> tag doesn't change the query's behavior (extname is still bound as a
    # real parameter, never interpolated) — it exists so tests using FakeAsyncConnection's
    # substring matching can tell the four otherwise-identical extension checks apart.
    return bool(
        await conn.fetchval(
            f"-- ext:{extname}\nSELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = $1)", extname
        )
    )


async def check_postgresql_prerequisites(target: ConnectionTarget) -> list[PrerequisiteCheck]:
    import asyncpg

    checks: list[PrerequisiteCheck] = []
    ssl_mode = (target.options or {}).get("ssl_mode")
    conn = await asyncpg.connect(
        host=target.host,
        port=target.port,
        database=target.database,
        user=target.username,
        password=target.password,
        timeout=10,
        ssl=True if ssl_mode == "require" else None,
        statement_cache_size=0,
    )
    try:
        await conn.execute("SET statement_timeout = '5000ms'")

        # 1. pg_stat_statements extension
        has_pgss = await _pg_extension_installed(conn, "pg_stat_statements")
        if has_pgss:
            checks.append(
                _ok(
                    "pg_stat_statements",
                    "pg_stat_statements uzantısı",
                    "Yavaş sorgu listesi, sorgu geçmişi, EXPLAIN ve index önerisi bu uzantıya dayanır.",
                )
            )
        else:
            checks.append(
                _missing(
                    "pg_stat_statements",
                    "pg_stat_statements uzantısı",
                    "high",
                    "Kurulu değil: yavaş sorgu listesi, sorgu geçmişi ve index önerisi tamamen boş döner.",
                    "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;\n"
                    "-- Ayrıca shared_preload_libraries'e eklenip PostgreSQL yeniden başlatılmalı (aşağıya bakın).",
                )
            )

        # 2. shared_preload_libraries — pg_stat_statements sadece extension CREATE edilmişse
        # değil, preload edilmişse veri toplar; ikisi ayrı ayrı yanlış gidebilir.
        preload = await conn.fetchval("SHOW shared_preload_libraries")
        preload_libs = {p.strip() for p in (preload or "").split(",") if p.strip()}
        if "pg_stat_statements" in preload_libs:
            checks.append(
                _ok(
                    "shared_preload_libraries",
                    "shared_preload_libraries",
                    "pg_stat_statements önyüklü; istatistik toplanabiliyor.",
                    detail=preload or "(boş)",
                )
            )
        else:
            checks.append(
                _missing(
                    "shared_preload_libraries",
                    "shared_preload_libraries",
                    "high",
                    "pg_stat_statements shared_preload_libraries'de değil — extension CREATE edilse bile "
                    "sessizce hiç istatistik toplamaz.",
                    "-- postgresql.conf içinde (veya):\n"
                    "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';\n"
                    "-- Ardından PostgreSQL'i YENİDEN BAŞLATIN (reload yetmez).",
                    detail=preload or "(boş)",
                )
            )

        # 3. pg_stat_statements.track — sadece extension yüklüyse anlamlı bir GUC.
        if has_pgss:
            track = await conn.fetchval("SHOW pg_stat_statements.track")
            if track in ("top", "all"):
                checks.append(
                    _ok("pg_stat_statements_track", "pg_stat_statements.track", f"Değer '{track}' — sorgular izleniyor.", detail=track)
                )
            else:
                checks.append(
                    _missing(
                        "pg_stat_statements_track",
                        "pg_stat_statements.track",
                        "high",
                        f"Değer '{track}' — hiçbir sorgu izlenmiyor.",
                        "ALTER SYSTEM SET pg_stat_statements.track = 'top';\nSELECT pg_reload_conf();",
                        detail=track,
                    )
                )
        else:
            checks.append(
                _unknown(
                    "pg_stat_statements_track",
                    "pg_stat_statements.track",
                    "high",
                    "pg_stat_statements kurulu olmadığından bu ayar kontrol edilemedi.",
                )
            )

        # 4. pg_stat_statements okuma yetkisi — canlı bir SELECT ile test edilir (rol
        # denetiminden bağımsız gerçek bir fonksiyonel kontrol).
        if has_pgss:
            try:
                await conn.fetchval("SELECT count(*) FROM pg_stat_statements")
                checks.append(
                    _ok(
                        "pg_stat_statements_read",
                        "pg_stat_statements okuma yetkisi",
                        "Bağlanan kullanıcı pg_stat_statements'ı sorgulayabiliyor.",
                    )
                )
            except Exception as exc:
                lower = str(exc).lower()
                if "permission denied" in lower or "must be" in lower:
                    checks.append(
                        _unauthorized(
                            "pg_stat_statements_read",
                            "pg_stat_statements okuma yetkisi",
                            "high",
                            "Bağlanan kullanıcı pg_stat_statements'ı okuyamıyor — yavaş sorgu/sorgu geçmişi hep boş döner.",
                            "GRANT pg_monitor TO <kullanıcı>;\n-- veya: GRANT SELECT ON pg_stat_statements TO <kullanıcı>;",
                        )
                    )
                else:
                    checks.append(
                        _unknown(
                            "pg_stat_statements_read",
                            "pg_stat_statements okuma yetkisi",
                            "high",
                            f"Sorgu başarısız oldu, sebep belirsiz: {exc}",
                        )
                    )
        else:
            checks.append(
                _unknown(
                    "pg_stat_statements_read",
                    "pg_stat_statements okuma yetkisi",
                    "high",
                    "pg_stat_statements kurulu olmadığından bu yetki kontrol edilemedi.",
                )
            )

        # 5. pg_monitor rolü
        has_pg_monitor = await conn.fetchval(
            "SELECT pg_has_role(current_user, 'pg_monitor', 'member')"
        )
        if has_pg_monitor:
            checks.append(_ok("pg_monitor", "pg_monitor rolü", "Bağlanan kullanıcı pg_monitor üyesi."))
        else:
            checks.append(
                _missing(
                    "pg_monitor",
                    "pg_monitor rolü",
                    "high",
                    "pg_monitor üyesi değil — pg_stat_activity ve pg_stat_statements gibi sistem view'ları "
                    "kısıtlı görünebilir (Activity sekmesi ve yavaş sorgu listesi eksik/boş dönebilir).",
                    "GRANT pg_monitor TO <kullanıcı>;",
                )
            )

        # 6. hypopg — index advisor'ın hipotetik index maliyet tahmini için.
        if await _pg_extension_installed(conn, "hypopg"):
            checks.append(
                _ok(
                    "hypopg",
                    "hypopg uzantısı",
                    "Index önerisi için hipotetik index maliyet tahmini (before/after EXPLAIN) yapılabiliyor.",
                )
            )
        else:
            checks.append(
                _missing(
                    "hypopg",
                    "hypopg uzantısı",
                    "medium",
                    "Kurulu değil — index önerisi yine üretilir ama tahmini iyileşme yüzdesi hipotetik index "
                    "testi olmadan kaba bir istatistiksel tahmine dayanır (gerçek EXPLAIN maliyet karşılaştırması yapılmaz).",
                    "CREATE EXTENSION IF NOT EXISTS hypopg;",
                )
            )

        # 7. pg_qualstats — hangi kolonların filtrelendiğinin daha kesin tespiti için.
        if await _pg_extension_installed(conn, "pg_qualstats"):
            checks.append(
                _ok(
                    "pg_qualstats",
                    "pg_qualstats uzantısı",
                    "Hangi kolonların WHERE/JOIN'de filtrelendiği doğrudan istatistikten okunabiliyor.",
                )
            )
        else:
            checks.append(
                _missing(
                    "pg_qualstats",
                    "pg_qualstats uzantısı",
                    "medium",
                    "Kurulu değil — dbace şu an index önerisini sorgu METNİNİ ayrıştırarak (regex ile) çıkarıyor; "
                    "pg_qualstats olsaydı hangi kolonun gerçekten filtrelendiği doğrudan istatistikten okunurdu "
                    "(daha isabetli öneri). Bu olmadan da index advisor çalışmaya devam eder.",
                    "CREATE EXTENSION IF NOT EXISTS pg_qualstats;\n"
                    "-- Tam istatistik toplaması için shared_preload_libraries'e de eklenmesi önerilir.",
                )
            )

        # 8. pg_buffercache — dbace şu an bunu tüketmiyor, ileri seviye manuel DBA analizi için.
        if await _pg_extension_installed(conn, "pg_buffercache"):
            checks.append(
                _ok(
                    "pg_buffercache",
                    "pg_buffercache uzantısı",
                    "Kurulu. dbace şu an bunu otomatik analizde kullanmıyor; manuel buffer içeriği incelemesi için hazır.",
                )
            )
        else:
            checks.append(
                _missing(
                    "pg_buffercache",
                    "pg_buffercache uzantısı",
                    "medium",
                    "Kurulu değil. dbace şu an bunu otomatik analizde kullanmıyor — sadece manuel DBA "
                    "incelemesi (hangi tablo/index RAM'de) için önerilir, eksikliği hiçbir dbace özelliğini bozmaz.",
                    "CREATE EXTENSION IF NOT EXISTS pg_buffercache;",
                )
            )

        # 9. track_io_timing — I/O darboğazı tespiti (Performans Tuning, İŞ 3) bu olmadan güvenilmez.
        track_io = await conn.fetchval("SHOW track_io_timing")
        if track_io == "on":
            checks.append(
                _ok(
                    "track_io_timing",
                    "track_io_timing",
                    "Açık — EXPLAIN (BUFFERS) ve I/O süre istatistikleri gerçek değer döndürüyor.",
                )
            )
        else:
            checks.append(
                _missing(
                    "track_io_timing",
                    "track_io_timing",
                    "medium",
                    "Kapalı — I/O süre ölçümleri (blk_read_time/blk_write_time) hep 0 görünür; "
                    "Performans Tuning sayfasındaki I/O darboğazı tespiti güvenilir olmaz.",
                    "ALTER SYSTEM SET track_io_timing = on;\nSELECT pg_reload_conf();",
                    detail=track_io,
                )
            )
    finally:
        await conn.close()

    return checks


async def check_sqlserver_prerequisites(target: ConnectionTarget) -> list[PrerequisiteCheck]:
    import aioodbc

    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    checks: list[PrerequisiteCheck] = []
    conn = await aioodbc.connect(dsn=build_odbc_connection_string(target), timeout=10, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET LOCK_TIMEOUT 5000")

        # 1. VIEW SERVER STATE — çoğu DMV'nin önkoşulu.
        async with conn.cursor() as cur:
            await cur.execute("SELECT HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE')")
            row = await cur.fetchone()
            has_view_server_state = bool(row and row[0])
        if has_view_server_state:
            checks.append(
                _ok("view_server_state", "VIEW SERVER STATE yetkisi", "Bağlanan login bu yetkiye sahip.")
            )
        else:
            checks.append(
                _missing(
                    "view_server_state",
                    "VIEW SERVER STATE yetkisi",
                    "high",
                    "Yetki yok — çoğu DMV (dm_exec_query_stats, dm_os_performance_counters, dm_exec_sessions) "
                    "erişilemez; metrik toplama ve yavaş sorgu listesi boş döner.",
                    "GRANT VIEW SERVER STATE TO <login>;\n"
                    "-- SQL Server 2022+ için daha dar kapsamlı alternatif:\n"
                    "-- GRANT VIEW SERVER PERFORMANCE STATE TO <login>;",
                )
            )

        # 2. Gerçek DMV erişimi — izin bayrağından bağımsız fonksiyonel test.
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT TOP 1 query_hash FROM sys.dm_exec_query_stats")
                await cur.fetchone()
            checks.append(
                _ok("dmv_query_stats", "sys.dm_exec_query_stats erişimi", "DMV sorgulanabiliyor.")
            )
        except Exception as exc:
            lower = str(exc).lower()
            if "permission" in lower or "denied" in lower or "state" in lower:
                checks.append(
                    _unauthorized(
                        "dmv_query_stats",
                        "sys.dm_exec_query_stats erişimi",
                        "high",
                        "DMV sorgusu reddedildi — yavaş sorgu listesi ve metrik toplama çalışmaz.",
                        "GRANT VIEW SERVER STATE TO <login>;",
                    )
                )
            else:
                checks.append(
                    _unknown(
                        "dmv_query_stats",
                        "sys.dm_exec_query_stats erişimi",
                        "high",
                        f"Sorgu başarısız oldu, sebep belirsiz: {exc}",
                    )
                )

        # 3. Query Store — hedef veritabanı için.
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT actual_state_desc, desired_state_desc FROM sys.database_query_store_options")
                row = await cur.fetchone()
            state = row[0] if row else None
            if state == "READ_WRITE":
                checks.append(
                    _ok("query_store", "Query Store", "Aktif (READ_WRITE) — geçmiş plan/performans analizi mümkün.", detail=state)
                )
            else:
                checks.append(
                    _missing(
                        "query_store",
                        "Query Store",
                        "high",
                        f"Durum: {state or 'kapalı'} — yavaş sorgu geçmişi ve plan analizi çok kısıtlı olur "
                        "(sadece anlık DMV verisiyle sınırlı kalır, sunucu yeniden başladığında sıfırlanır).",
                        "ALTER DATABASE CURRENT SET QUERY_STORE = ON;\n"
                        "ALTER DATABASE CURRENT SET QUERY_STORE (OPERATION_MODE = READ_WRITE);",
                        detail=state,
                    )
                )
        except Exception as exc:
            checks.append(
                _unknown(
                    "query_store",
                    "Query Store",
                    "high",
                    f"Durum okunamadı (SQL Server sürümü Query Store'u desteklemiyor olabilir — 2016+ gerekir): {exc}",
                )
            )
    finally:
        await conn.close()

    return checks


async def run_prerequisite_checks(engine: DatabaseEngine, target: ConnectionTarget) -> list[PrerequisiteCheck]:
    if engine == DatabaseEngine.POSTGRESQL:
        return await check_postgresql_prerequisites(target)
    if engine == DatabaseEngine.SQLSERVER:
        return await check_sqlserver_prerequisites(target)
    raise ValueError(f"Ön koşul denetimi bu engine için desteklenmiyor: {engine}")
