"""Ön koşul denetimi (Faz 16 İŞ 1): dbace'in slow-query/EXPLAIN/index-advisor/tuning
özellikleri sessizce boş sonuç döndürdüğünde ("neden hiç öneri gelmiyor?") sebebi net şekilde
göstermek için — her kontrol {ad, durum, etki, düzeltme komutu} olarak raporlanır.

PostgreSQL ve SQL Server için ayrı kontrol setleri var; MongoDB desteklenmiyor (router 400 döner).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.collectors.base import ConnectionTarget
from app.domain.engines import DatabaseEngine
from app.services.pgss import PgStatStatementsProbe, probe_pg_stat_statements

# status: "ok" | "partial" | "missing" | "unauthorized" | "unknown"
# "partial": kontrol teknik olarak geçti ama kapsamı kısıtlı — tek örneği pg_stat_statements'ın
# kısıtlı görünürlüğü (rol sadece kendi sorgularını görebiliyor). Yeşil göstermek yanıltıcı,
# kırmızı göstermek de yanlış olurdu.
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


def _partial(
    key: str, name: str, severity: str, impact: str, fix: str | None = None, detail: str | None = None
) -> PrerequisiteCheck:
    return PrerequisiteCheck(
        key=key, name=name, status="partial", severity=severity, impact=impact, fix=fix, detail=detail
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


def pg_stat_statements_checks(probe: PgStatStatementsProbe) -> list[PrerequisiteCheck]:
    """Tek probe'dan pg_stat_statements ile ilgili 5 kontrolü üretir.

    Ayrı bir fonksiyon: hem canlı denetim (check_postgresql_prerequisites) hem de yavaş sorgu
    kullanılabilirlik mesajı (services/slow_query_status.py) aynı probe nesnesinden aynı
    sonuçları türetsin diye — iki tarafın çelişmesi artık yapısal olarak imkânsız.
    """
    checks: list[PrerequisiteCheck] = []

    # 1. Uzantı gerçekten kullanılabilir mi? Katalogda kayıtlı olması YETMEZ: yönetilen
    # servislerde eklenti search_path dışı bir şemada olabilir ya da rol view'ı okuyamayabilir.
    if probe.installed and probe.reachable:
        checks.append(
            _ok(
                "pg_stat_statements",
                "pg_stat_statements uzantısı",
                "Yavaş sorgu listesi, sorgu geçmişi, EXPLAIN ve index önerisi bu uzantıya dayanır.",
                detail=f"şema: {probe.schema}",
            )
        )
    elif probe.installed and probe.read_error_code == "unauthorized":
        checks.append(
            _unauthorized(
                "pg_stat_statements",
                "pg_stat_statements uzantısı",
                "high",
                "Uzantı kurulu ama bağlanan kullanıcı view'ı okuyamıyor — yavaş sorgu listesi boş kalır.",
                "GRANT pg_read_all_stats TO <kullanıcı>;\n"
                "-- veya: GRANT SELECT ON pg_stat_statements TO <kullanıcı>;",
                detail=probe.read_error,
            )
        )
    elif probe.installed:
        # Kurulu ama erişilemiyor: neredeyse her zaman search_path sorunu (Supabase eklentiyi
        # `extensions` şemasına kurar). dbace collector'ı artık view'ı şema-nitelikli çağırdığı
        # için veri yine de toplanır; bu kontrol elle sorgu çalıştıracak DBA'yı uyarır.
        schema = probe.schema or "extensions"
        checks.append(
            _missing(
                "pg_stat_statements",
                "pg_stat_statements uzantısı",
                "medium",
                f"Uzantı '{schema}' şemasında kurulu ama bağlanan rolün search_path'inde bu şema yok — "
                "dbace şema-nitelikli sorguladığı için veri toplamaya devam eder, ancak elle "
                "çalıştıracağınız sorgularda şema adını yazmanız gerekir.",
                f'ALTER ROLE <kullanıcı> SET search_path = public, "{schema}";\n'
                f'-- Elle sorgularken: SELECT * FROM "{schema}".pg_stat_statements;',
                detail=probe.read_error,
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

    # 2. shared_preload_libraries — extension CREATE edilmiş olsa bile preload yoksa hiç
    # istatistik toplanmaz; ikisi ayrı ayrı yanlış gidebilir.
    if probe.preloaded:
        checks.append(
            _ok(
                "shared_preload_libraries",
                "shared_preload_libraries",
                "pg_stat_statements önyüklü; istatistik toplanabiliyor.",
                detail=probe.preload_raw or "(boş)",
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
                detail=probe.preload_raw or "(boş)",
            )
        )

    # 3. pg_stat_statements.track
    if not probe.installed:
        checks.append(
            _unknown(
                "pg_stat_statements_track",
                "pg_stat_statements.track",
                "high",
                "pg_stat_statements kurulu olmadığından bu ayar kontrol edilemedi.",
            )
        )
    elif probe.track in ("top", "all"):
        checks.append(
            _ok(
                "pg_stat_statements_track",
                "pg_stat_statements.track",
                f"Değer '{probe.track}' — sorgular izleniyor.",
                detail=probe.track,
            )
        )
    else:
        checks.append(
            _missing(
                "pg_stat_statements_track",
                "pg_stat_statements.track",
                "high",
                f"Değer '{probe.track or 'okunamadı'}' — hiçbir sorgu izlenmiyor.",
                "ALTER SYSTEM SET pg_stat_statements.track = 'top';\nSELECT pg_reload_conf();",
                detail=probe.track,
            )
        )

    # 4. Okuma yetkisi — probe'un gerçek SELECT denemesinden türetiliyor (ayrı bir sorgu değil).
    if not probe.installed:
        checks.append(
            _unknown(
                "pg_stat_statements_read",
                "pg_stat_statements okuma yetkisi",
                "high",
                "pg_stat_statements kurulu olmadığından bu yetki kontrol edilemedi.",
            )
        )
    elif probe.reachable:
        checks.append(
            _ok(
                "pg_stat_statements_read",
                "pg_stat_statements okuma yetkisi",
                "Bağlanan kullanıcı pg_stat_statements'ı sorgulayabiliyor.",
                detail=f"{probe.total_rows} satır görünüyor" if probe.total_rows is not None else None,
            )
        )
    elif probe.read_error_code == "unauthorized":
        checks.append(
            _unauthorized(
                "pg_stat_statements_read",
                "pg_stat_statements okuma yetkisi",
                "high",
                "Bağlanan kullanıcı pg_stat_statements'ı okuyamıyor — yavaş sorgu/sorgu geçmişi hep boş döner.",
                "GRANT pg_monitor TO <kullanıcı>;\n-- veya: GRANT SELECT ON pg_stat_statements TO <kullanıcı>;",
                detail=probe.read_error,
            )
        )
    else:
        checks.append(
            _unknown(
                "pg_stat_statements_read",
                "pg_stat_statements okuma yetkisi",
                "high",
                probe.read_error or "Sorgu başarısız oldu, sebep belirsiz.",
            )
        )

    # 5. Görünürlük kapsamı — yönetilen servislerde (Supabase/RDS) en sık yanılgı kaynağı:
    # eklenti kurulu, okunabiliyor, ama rol superuser/pg_read_all_stats olmadığı için BAŞKA
    # kullanıcıların sorgu metnini göremiyor; o satırlar '<insufficient privilege>' olarak gelir.
    if not probe.installed or not probe.reachable:
        checks.append(
            _unknown(
                "pg_stat_statements_visibility",
                "pg_stat_statements görünürlük kapsamı",
                "medium",
                "pg_stat_statements okunamadığından görünürlük kapsamı belirlenemedi.",
            )
        )
    elif probe.privileged:
        checks.append(
            _ok(
                "pg_stat_statements_visibility",
                "pg_stat_statements görünürlük kapsamı",
                "Tüm kullanıcıların sorguları görülebiliyor (superuser veya pg_read_all_stats üyesi).",
            )
        )
    elif probe.restricted_visibility:
        checks.append(
            _partial(
                "pg_stat_statements_visibility",
                "pg_stat_statements görünürlük kapsamı",
                "medium",
                "Kurulu ve okunabiliyor, AMA sadece kendi sorgularınızı görebiliyorsunuz: "
                f"{probe.redacted_rows} satırın metni '<insufficient privilege>' olarak maskeli. "
                "Uygulamanız farklı bir rolle bağlanıyorsa asıl yavaş sorgular listede hiç görünmez.",
                "GRANT pg_read_all_stats TO <kullanıcı>;\n-- (pg_monitor bu rolü de kapsar)",
                detail=f"{probe.visible_rows}/{probe.total_rows} satır okunabilir",
            )
        )
    else:
        checks.append(
            _ok(
                "pg_stat_statements_visibility",
                "pg_stat_statements görünürlük kapsamı",
                "Şu an maskelenmiş satır yok. Rol pg_read_all_stats üyesi değil; başka bir rol sorgu "
                "çalıştırmaya başlarsa o sorgular maskeli görünecektir.",
                detail=f"{probe.total_rows} satır",
            )
        )

    return checks


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

        # Sürüm numarası: aşağıdaki bekleme kontrolleri sürüme bağlı (query_id 14+ ile geldi).
        version_num = int(await conn.fetchval("SELECT current_setting('server_version_num')::int"))

        # 1-5. pg_stat_statements ailesi — hepsi TEK probe'dan (services/pgss.py) türetiliyor.
        # Daha önce bu blok kendi katalog sorgusunu yapıyordu ve DPA'nın "yavaş sorgu yok"
        # mesajı bambaşka bir kontrole dayanıyordu; ikisi çelişebiliyordu. Artık aynı kaynak.
        probe = await probe_pg_stat_statements(conn)
        checks.extend(pg_stat_statements_checks(probe))

        # 6. pg_monitor rolü
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

        # 7. hypopg — index advisor'ın hipotetik index maliyet tahmini için.
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

        # 8. pg_qualstats — hangi kolonların filtrelendiğinin daha kesin tespiti için.
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

        # 9. pg_buffercache — dbace şu an bunu tüketmiyor, ileri seviye manuel DBA analizi için.
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

        # 10. track_io_timing — I/O darboğazı tespiti (Performans Tuning, İŞ 3) bu olmadan güvenilmez.
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
        # 11-12. BEKLEME ANALİZİ ÖN KOŞULLARI (Faz 25 İŞ 5).
        #
        # Bekleme örnekleyicisi pg_stat_activity'yi okuyor. Yetkisiz bir rol bu görünümde
        # DİĞER kullanıcıların satırlarını görür ama `query`, `state` ve `wait_event`
        # alanları NULL gelir. Sonuç sinsi: örnekleyici çalışır, veri birikir, grafik çizilir
        # — ama yalnızca kendi oturumlarını sayar. Yani ekran "sunucu sakin" der, sunucu
        # yanarken. Bu yüzden kontrol yalnızca rol üyeliğine değil, GÖRÜNÜRLÜĞÜN ÖLÇÜSÜNE de
        # bakıyor: kaç oturumun maskelendiği sayılıp kullanıcıya "ne kadarını görebiliyorsun"
        # olarak yazılıyor.
        stats_role = await conn.fetchval(
            "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'member') "
            "OR pg_has_role(current_user, 'pg_monitor', 'member')"
        )
        visibility = await conn.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE backend_type = 'client backend') AS toplam,
                count(*) FILTER (
                    WHERE backend_type = 'client backend' AND state IS NULL
                ) AS maskeli
            FROM pg_stat_activity
            WHERE pid <> pg_backend_pid()
            """
        )
        total_backends = int(visibility["toplam"] or 0)
        masked = int(visibility["maskeli"] or 0)
        visible = total_backends - masked
        if stats_role:
            checks.append(
                _ok(
                    "wait_visibility",
                    "Bekleme görünürlüğü (pg_read_all_stats)",
                    "Tüm oturumların bekleme durumu okunabiliyor — veritabanı yükü (AAS) "
                    "gerçek değerini gösteriyor.",
                    detail=f"{total_backends} istemci oturumunun tamamı görülebiliyor",
                )
            )
        elif masked > 0:
            checks.append(
                _partial(
                    "wait_visibility",
                    "Bekleme görünürlüğü (pg_read_all_stats)",
                    "high",
                    f"Bağlanan rol {total_backends} istemci oturumundan yalnızca {visible} "
                    f"tanesini görebiliyor; {masked} oturumun durumu ve beklemesi maskeli "
                    "geliyor. Veritabanı yükü grafiği bu oturumları HİÇ saymaz — yani sunucu "
                    "yoğunken bile sakin görünebilir. Sayı bir tahmin değil, şu anki ölçüm.",
                    "GRANT pg_read_all_stats TO <kullanıcı>;\n"
                    "-- ya da daha geniş kapsamlı: GRANT pg_monitor TO <kullanıcı>;",
                    detail=f"{visible}/{total_backends} oturum görülebiliyor",
                )
            )
        else:
            # Rol yok ama şu an maskeli oturum da yok (tek kullanıcılı sunucu ya da o anda
            # başka oturum yok). Yeşil göstermek yanıltıcı olurdu: yük geldiğinde körleşir.
            checks.append(
                _partial(
                    "wait_visibility",
                    "Bekleme görünürlüğü (pg_read_all_stats)",
                    "high",
                    "Bağlanan rol pg_read_all_stats/pg_monitor üyesi değil. Şu anda başka "
                    "kullanıcının oturumu olmadığı için ölçülebilir bir kayıp görünmüyor, ama "
                    "başka kullanıcılar bağlandığında onların beklemeleri maskelenecek ve "
                    "veritabanı yükü olduğundan düşük görünecek.",
                    "GRANT pg_read_all_stats TO <kullanıcı>;",
                    detail="şu an karşılaştırılacak başka oturum yok",
                )
            )

        # compute_query_id — beklemeyi SORGUYA bağlayan tek alan.
        if version_num >= 140_000:
            compute_query_id = await conn.fetchval("SHOW compute_query_id")
            # 'auto' = pg_stat_statements yüklüyse açık. Yüklü olup olmadığını yukarıdaki
            # probe zaten biliyor; burada onu tekrar sorgulamak yerine ondan yararlanıyoruz.
            effective_on = compute_query_id in ("on", "regress") or (
                compute_query_id == "auto" and probe.installed
            )
            if effective_on:
                checks.append(
                    _ok(
                        "compute_query_id",
                        "compute_query_id",
                        "Açık — bekleme örnekleri sorgu bazında ayrıştırılabiliyor "
                        "(\"bu sorgu süresinin yüzde kaçını kilitte geçirdi\").",
                        detail=compute_query_id,
                    )
                )
            else:
                checks.append(
                    _missing(
                        "compute_query_id",
                        "compute_query_id",
                        "medium",
                        f"Değer '{compute_query_id}' — pg_stat_activity.query_id boş geliyor. "
                        "Bekleme kırılımı yine üretilir (\"sistem neyi bekliyor\" cevaplanır) "
                        "ama hangi SORGUNUN beklediği ayrıştırılamaz.",
                        "ALTER SYSTEM SET compute_query_id = on;\nSELECT pg_reload_conf();",
                        detail=compute_query_id,
                    )
                )
        else:
            checks.append(
                _missing(
                    "compute_query_id",
                    "compute_query_id (PostgreSQL 14+)",
                    "low",
                    f"Sunucu sürümü {version_num} — pg_stat_activity.query_id PostgreSQL 14 ile "
                    "geldi. Bekleme kırılımı çalışıyor ama sorgu bazında ayrıştırma bu sürümde "
                    "mümkün değil. Sürüm yükseltmesi dışında yapılabilecek bir şey yok.",
                    "-- Sunucu sürümü yükseltmesi gerekir (PostgreSQL 14+).",
                    detail=str(version_num),
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

        # 4. BEKLEME ANALİZİ DMV'LERİ (Faz 25 İŞ 5).
        #
        # VIEW SERVER STATE yukarıda genel olarak kontrol ediliyor ama bekleme örnekleyicisi
        # için ayrı bir kontrol var: yetki bayrağı ile GERÇEK erişim ayrışabiliyor (sunucu
        # düzeyinde DENY, sınırlı sürüm, Azure SQL kısıtları). Bayrağa güvenip örneklemeyi
        # açmak, boş bir grafiğin sebebini gizlemek olurdu.
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COUNT(*) FROM sys.dm_exec_requests r "
                    "LEFT JOIN sys.dm_os_waiting_tasks w ON w.session_id = r.session_id"
                )
                await cur.fetchone()
            checks.append(
                _ok(
                    "wait_visibility",
                    "Bekleme görünürlüğü (dm_exec_requests)",
                    "Aktif istekler ve bekleme görevleri okunabiliyor — veritabanı yükü (AAS) "
                    "ve bekleme kırılımı üretilebiliyor.",
                )
            )
        except Exception as exc:
            lower = str(exc).lower()
            if "permission" in lower or "denied" in lower or "state" in lower:
                checks.append(
                    _unauthorized(
                        "wait_visibility",
                        "Bekleme görünürlüğü (dm_exec_requests)",
                        "high",
                        "DMV reddedildi — bekleme örnekleyicisi yalnızca KENDİ oturumunu "
                        "görebilir. Veritabanı yükü grafiği bu durumda sunucu yoğunken bile "
                        "sakin görünür; boş değil, YANLIŞ veri üretir.",
                        "GRANT VIEW SERVER STATE TO <login>;",
                    )
                )
            else:
                checks.append(
                    _unknown(
                        "wait_visibility",
                        "Bekleme görünürlüğü (dm_exec_requests)",
                        "high",
                        f"Sorgu başarısız oldu, sebep belirsiz: {exc}",
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
