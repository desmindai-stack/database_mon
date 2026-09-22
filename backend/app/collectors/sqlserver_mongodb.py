from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.collectors.base import (
    BaseCollector,
    ConnectionTarget,
    SamplingConnection,
    classify_connection_error,
)
from app.domain.waits import classify_sqlserver_wait
from app.services.deadlocks import SQLSERVER_DEADLOCK_SQL

logger = logging.getLogger(__name__)

# ProductMajorVersion (SERVERPROPERTY) -> release: 2016=13, 2017=14, 2019=15, 2022=16.
# dbace targets SQL Server 2016+; below that is best-effort (a warning is logged, the
# collector still tries — DMV shapes are far more stable than exact SP/CU column additions).
SQLSERVER_MIN_SUPPORTED_MAJOR = 13

# See _connect()'s docstring comment — bounds lock-wait time on every collector query.
COLLECTOR_LOCK_TIMEOUT_MS = 5_000
# Örnekleyicinin kendi tavanı — saniyede bir çalışan bir sorgunun 5 saniye kilit beklemesi
# ölçümde beş saniyelik delik açar.
SAMPLER_LOCK_TIMEOUT_MS = 1_000

# Aktif oturum fotoğrafı (Faz 25 İŞ 1).
#
# `dm_exec_requests` ÇALIŞAN istekleri verir (boşta oturumlar yok — zaten istediğimiz bu).
# `dm_os_waiting_tasks` ikinci kaynak olarak duruyor: paralel plan çalıştıran bir istekte
# bloklayan oturum isteğin kendisinde değil, GÖREV (task) düzeyinde görünür; yalnızca
# `r.blocking_session_id`'ye bakmak paralel sorgularda bloklanmaları görünmez yapardı.
#
# `dm_exec_sql_text` plan cache'ten okuyan bir bellek aramasıdır; aktif istek sayısı doğası
# gereği küçük olduğu için (yüzlerce değil, onlarca) saniyelik örneklemede kabul edilebilir.
# TOP sınırı, patolojik bir durumda (bağlantı fırtınası) örnekleme maliyetinin patlamasını
# engelliyor.
_ACTIVE_SESSION_SAMPLE_SQL = """
SELECT TOP ({limit})
    CONVERT(VARCHAR(34), r.query_hash, 1) AS queryid,
    r.wait_type AS wait_type,
    COALESCE(NULLIF(r.blocking_session_id, 0), w.blocking_session_id, 0) AS blocking_session_id,
    SUBSTRING(t.text, 1, 400) AS query_text
FROM sys.dm_exec_requests r
LEFT JOIN (
    SELECT session_id, MAX(blocking_session_id) AS blocking_session_id
    FROM sys.dm_os_waiting_tasks
    WHERE blocking_session_id IS NOT NULL AND blocking_session_id <> 0
    GROUP BY session_id
) w ON w.session_id = r.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.session_id <> @@SPID
  AND r.session_id > 50
"""

_PERF_COUNTER_QUERY = """
SELECT RTRIM(counter_name), cntr_value
FROM sys.dm_os_performance_counters
WHERE counter_name IN ('Batch Requests/sec', 'Buffer cache hit ratio',
                        'Buffer cache hit ratio base', 'Number of Deadlocks/sec')
"""

# sys.dm_exec_query_stats.total_rows/min_rows/max_rows/last_rows were added by a specific
# SP/CU rather than cleanly at a major-version boundary (unlike PostgreSQL's catalog, which
# changes atomically at major-version release) — so this is handled with a try-then-retry
# fallback (see collect_slow_queries) instead of a version-number gate, which would risk being
# wrong for a specific SP level we can't verify without a live server of every combination.
_SLOW_QUERY_SQL = """
SELECT TOP ({limit})
    CONVERT(VARCHAR(64), qs.query_hash, 1) AS queryid,
    SUBSTRING(
        st.text,
        (qs.statement_start_offset / 2) + 1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text) ELSE qs.statement_end_offset END
            - qs.statement_start_offset) / 2) + 1
    ) AS query,
    qs.execution_count AS calls,
    qs.total_worker_time / 1000.0 AS total_time_ms,
    (qs.total_worker_time / 1000.0) / NULLIF(qs.execution_count, 0) AS mean_time_ms,
    qs.total_rows AS rows
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_worker_time DESC
"""

_SLOW_QUERY_SQL_NO_ROWS = """
SELECT TOP ({limit})
    CONVERT(VARCHAR(64), qs.query_hash, 1) AS queryid,
    SUBSTRING(
        st.text,
        (qs.statement_start_offset / 2) + 1,
        ((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text) ELSE qs.statement_end_offset END
            - qs.statement_start_offset) / 2) + 1
    ) AS query,
    qs.execution_count AS calls,
    qs.total_worker_time / 1000.0 AS total_time_ms,
    (qs.total_worker_time / 1000.0) / NULLIF(qs.execution_count, 0) AS mean_time_ms,
    NULL AS rows
FROM sys.dm_exec_query_stats qs
CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st
ORDER BY qs.total_worker_time DESC
"""

# --- FAZ 29 İŞ 2c: sys.dm_exec_query_stats'ın tam sömürüsü ----------------------------------
#
# ÖNCEKİ HÂLİN SEMANTİK HATASI: `total_time_ms` alanına `total_worker_time` yazılıyordu.
# `total_worker_time` CPU süresidir; `total_elapsed_time` ise duvar saati. İkisinin FARKI
# BEKLEMEDİR (kilit, I/O, ağ). Yani "yavaş sorgu" listesi aslında "CPU yiyen sorgu" listesiydi:
# kilitte 10 saniye bekleyip 5 ms CPU kullanan bir sorgu listede HIZLI görünüyordu — oysa
# kullanıcının şikâyet ettiği tam olarak odur.
#
# Artık `total_time_ms` = elapsed, CPU ayrı alanda ve ikisinin farkı "bekleme" olarak
# türetiliyor.
#
# SÜRÜM/SP FARKI: SQL Server'da sütunlar temiz sürüm sınırlarında değil, SP/CU ile geliyor
# (mevcut kod bunu `total_rows` için try/retry ile çözüyordu). Tahmin yerine SORUYORUZ:
# `sys.all_columns` üzerinden DMV'nin gerçekten hangi sütunlara sahip olduğu okunuyor ve
# SELECT ona göre kuruluyor. Böylece hiçbir SP kombinasyonunda "invalid column name" ile
# toplama düşmüyor.

_QUERY_STATS_COLUMNS_SQL = """
SELECT c.name
FROM sys.all_columns c
JOIN sys.all_objects o ON o.object_id = c.object_id
WHERE o.name = 'dm_exec_query_stats'
"""

#: Her SQL Server 2008+ sürümünde bulunan sütunlar — bunlar için varlık kontrolü yapılmıyor.
_QS_BASE_COLUMNS = {
    "execution_count",
    "total_worker_time",
    "total_elapsed_time",
    "total_logical_reads",
    "total_physical_reads",
    "total_logical_writes",
}

#: Sürüme/SP'ye göre var olabilen sütunlar: (dbace alan adı, DMV sütunu, dönüşüm).
#: Dönüşüm `{col}` yer tutucusunu kullanıyor.
_QS_OPTIONAL_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("rows", "total_rows", "qs.{col}"),
    ("min_time_ms", "min_elapsed_time", "qs.{col} / 1000.0"),
    ("max_time_ms", "max_elapsed_time", "qs.{col} / 1000.0"),
    ("min_cpu_ms", "min_worker_time", "qs.{col} / 1000.0"),
    ("max_cpu_ms", "max_worker_time", "qs.{col} / 1000.0"),
    # Bellek izni: SQL Server 2016+. Talep edilen ile KULLANILAN arasındaki fark, sorgunun
    # gereğinden fazla bellek rezerve edip başkalarını beklettiğini gösterir.
    ("grant_kb", "total_grant_kb", "qs.{col}"),
    ("used_grant_kb", "total_used_grant_kb", "qs.{col}"),
    # Spill: 2016 SP2 / 2017+. tempdb'ye taşma — PostgreSQL'deki temp_blks karşılığı.
    ("spills", "total_spills", "qs.{col}"),
    ("plan_generation_num", "plan_generation_num", "qs.{col}"),
    ("last_execution_time", "last_execution_time", "qs.{col}"),
)


def build_slow_query_sql(available_columns: set[str]) -> str:
    """Sunucunun GERÇEKTEN sahip olduğu sütunlara göre sorguyu kurar.

    Sütun listesini tahmin etmek yerine sormak, SQL Server'ın SP/CU bazlı sütun ekleme
    alışkanlığında tek güvenli yol: "invalid column name" hatası tüm yavaş sorgu toplamasını
    düşürür ve o hata yalnızca belirli bir SP seviyesinde ortaya çıkacağı için test ortamında
    hiç görünmeyebilir.
    """
    parts = [
        "CONVERT(VARCHAR(64), qs.query_hash, 1) AS queryid",
        (
            "SUBSTRING(st.text, (qs.statement_start_offset / 2) + 1, "
            "((CASE qs.statement_end_offset WHEN -1 THEN DATALENGTH(st.text) "
            "ELSE qs.statement_end_offset END - qs.statement_start_offset) / 2) + 1) AS query"
        ),
        "qs.execution_count AS calls",
        # total_time_ms ARTIK ELAPSED: bekleme dahil gerçek süre.
        "qs.total_elapsed_time / 1000.0 AS total_time_ms",
        "(qs.total_elapsed_time / 1000.0) / NULLIF(qs.execution_count, 0) AS mean_time_ms",
        "qs.total_worker_time / 1000.0 AS cpu_time_ms",
        "qs.total_logical_reads AS logical_reads",
        "qs.total_physical_reads AS physical_reads",
        "qs.total_logical_writes AS logical_writes",
    ]
    for field, column, expression in _QS_OPTIONAL_COLUMNS:
        if column in available_columns:
            parts.append(expression.format(col=column) + f" AS {field}")
        else:
            # Alan HEP var, değeri NULL: tüketicilerin sütun varlığı kontrolü yapmasına
            # gerek kalmıyor ve "ölçülmedi" ile "sıfır" ayrımı korunuyor.
            parts.append(f"NULL AS {field}")

    return (
        "SELECT TOP ({limit})\n    "
        + ",\n    ".join(parts)
        + "\nFROM sys.dm_exec_query_stats qs"
        + "\nCROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) st"
        # Sıralama da elapsed'e göre: CPU'ya göre sıralamak, bekleyen sorguları listenin
        # dışında bırakıyordu.
        + "\nORDER BY qs.total_elapsed_time DESC"
    )


# --- FAZ 29 İŞ 2c: SQL Server'ın KENDİ eksik index önerileri --------------------------------
#
# SQL Server, sorguları planlarken "şu index olsaydı işim kolaylaşırdı" bilgisini
# `sys.dm_db_missing_index_*` DMV'lerinde biriktiriyor. Bu HAZIR bir kaynak ve dbace onu hiç
# kullanmıyordu — PostgreSQL tarafında hypopg ile ölçtüğümüz şeyin karşılığı burada motorun
# kendisi tarafından zaten üretiliyor.
#
# DİKKAT (öneri metninde de yazıyor): bu öneriler HAM. SQL Server aynı tablo için onlarca
# örtüşen öneri üretebilir, kolon sırasını optimize etmez ve INCLUDE listesini şişirir.
# Körlemesine uygulamak, yazma maliyetini patlatan bir index yığını bırakır. Bu yüzden
# `improvement_measure` ile sıralanıp yalnızca en yüksek etkili olanlar taşınıyor.
_MISSING_INDEX_SQL = """
SELECT TOP ({limit})
    DB_NAME(mid.database_id) AS database_name,
    OBJECT_SCHEMA_NAME(mid.object_id, mid.database_id) AS schema_name,
    OBJECT_NAME(mid.object_id, mid.database_id) AS table_name,
    mid.equality_columns,
    mid.inequality_columns,
    mid.included_columns,
    migs.user_seeks,
    migs.user_scans,
    migs.avg_total_user_cost,
    migs.avg_user_impact,
    migs.last_user_seek,
    -- SQL Server topluluğunun yerleşik "etki ölçüsü" formülü: maliyet x etki x kullanım.
    -- Tek başına avg_user_impact yanıltıcı: günde bir çalışan bir sorgu için %99 iyileşme,
    -- saniyede bin kez çalışan bir sorgu için %20 iyileşmeden daha az değerlidir.
    CONVERT(DECIMAL(28, 2),
        migs.avg_total_user_cost * (migs.avg_user_impact / 100.0) *
        (migs.user_seeks + migs.user_scans)) AS improvement_measure
FROM sys.dm_db_missing_index_details mid
JOIN sys.dm_db_missing_index_groups mig ON mig.index_handle = mid.index_handle
JOIN sys.dm_db_missing_index_group_stats migs ON migs.group_handle = mig.index_group_handle
WHERE mid.database_id = DB_ID()
ORDER BY improvement_measure DESC
"""

# --- Kullanılmayan / az kullanılan index'ler -------------------------------------------------
#
# PostgreSQL tarafındaki `idx_scan = 0` kontrolünün karşılığı. Fark: SQL Server bu sayaçları
# SERVİS YENİDEN BAŞLATILDIĞINDA sıfırlıyor, bu yüzden "hiç kullanılmadı" iddiası ancak
# sayaçların ne kadar süredir biriktiği bilinirse anlamlı. Sorgu bu yüzden sayaç yaşını da
# döndürüyor ve öneri metni onu açıkça söylüyor.
_UNUSED_INDEX_SQL = """
SELECT TOP ({limit})
    SCHEMA_NAME(o.schema_id) AS schema_name,
    o.name AS table_name,
    i.name AS index_name,
    i.type_desc AS index_type,
    ISNULL(us.user_seeks, 0) AS user_seeks,
    ISNULL(us.user_scans, 0) AS user_scans,
    ISNULL(us.user_lookups, 0) AS user_lookups,
    ISNULL(us.user_updates, 0) AS user_updates,
    ISNULL(ps.reserved_page_count, 0) * 8 * 1024 AS index_bytes,
    (SELECT DATEDIFF(second, sqlserver_start_time, GETDATE()) FROM sys.dm_os_sys_info) AS stats_age_seconds
FROM sys.indexes i
JOIN sys.objects o ON o.object_id = i.object_id
LEFT JOIN sys.dm_db_index_usage_stats us
       ON us.object_id = i.object_id AND us.index_id = i.index_id AND us.database_id = DB_ID()
LEFT JOIN sys.dm_db_partition_stats ps
       ON ps.object_id = i.object_id AND ps.index_id = i.index_id
WHERE o.is_ms_shipped = 0
  AND i.type_desc <> 'HEAP'
  AND i.is_primary_key = 0
  AND i.is_unique_constraint = 0
  AND ISNULL(us.user_seeks, 0) + ISNULL(us.user_scans, 0) + ISNULL(us.user_lookups, 0) = 0
  AND ISNULL(ps.reserved_page_count, 0) > 1
ORDER BY ps.reserved_page_count DESC
"""


_ACTIVITY_SQL = """
SELECT TOP ({limit})
    s.session_id,
    s.login_name,
    DB_NAME(s.database_id) AS datname,
    s.program_name,
    c.client_net_address,
    s.status,
    r.wait_type,
    r.start_time,
    s.last_request_end_time,
    r.blocking_session_id,
    st.text AS query_text,
    s.open_transaction_count
FROM sys.dm_exec_sessions s
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
LEFT JOIN sys.dm_exec_connections c ON c.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) st
WHERE s.is_user_process = 1
ORDER BY r.start_time DESC
"""


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    """datetime → ISO metin. msdb saatleri SUNUCU YEREL saatinde; saat dilimi bilgisi yok
    ve uydurulmuyor — çağıran taraf bunu naive kabul edip UTC varsayıyor (bkz.
    services/backup_monitor.py)."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_datetimeoffset(raw: bytes) -> datetime:
    """SQL Server `datetimeoffset` (ör. `sys.query_store_runtime_stats_interval.start_time`) → aware `datetime`.

    pyodbc bu tipi (ODBC SQL type -155) hiçbir sürücüyle KENDİLİĞİNDEN çözmüyor — kayıt
    çekilirken "ODBC SQL type -155 is not yet supported" ile patlıyor (ODBC Driver 18,
    18.6.2.1'de gerçek Query Store karşısında ölçüldü; Faz 31 Commit 10c). `raw`,
    ODBC'nin `SQL_SS_TIMESTAMPOFFSET_STRUCT`'ı: 6×int16 (yıl..saniye) + uint32 (saniyenin
    milyarda biri) + 2×int16 (saat dilimi saat/dakika) — Microsoft'un belgelediği çözüm.

    Bu pyodbc sürümünde (5.3.0) modül seviyesinde `pyodbc.add_output_converter` YOK —
    yalnızca `Connection.add_output_converter` var, yani her yeni bağlantıda AYRI
    kaydedilmesi gerekiyor (bkz. `register_datetimeoffset_converter`).
    """
    import struct
    from datetime import timedelta, timezone

    year, month, day, hour, minute, second, fraction, tz_hour, tz_minute = struct.unpack("<6hI2h", raw)
    return datetime(
        year, month, day, hour, minute, second, fraction // 1000,
        timezone(timedelta(hours=tz_hour, minutes=tz_minute)),
    )


async def register_datetimeoffset_converter(conn) -> None:
    """Her yeni aioodbc bağlantısında ÇAĞRILMALI — `build_odbc_connection_string` kullanan her yer."""
    await conn.add_output_converter(-155, _decode_datetimeoffset)


def build_odbc_connection_string(target: ConnectionTarget) -> str:
    opts = target.options or {}
    driver = opts.get("odbc_driver", "ODBC Driver 18 for SQL Server")
    encrypt = "yes" if opts.get("encrypt", True) else "no"
    trust_cert = "yes" if opts.get("trust_server_certificate", True) else "no"
    base = (
        f"DRIVER={{{driver}}};SERVER={target.host},{target.port};"
        f"DATABASE={target.database};"
        f"Encrypt={encrypt};TrustServerCertificate={trust_cert};Connection Timeout=10"
    )
    # "windows" = integrated auth (the ODBC driver runs under the worker's own OS identity —
    # only meaningful if the collector process itself runs on a domain-joined Windows host with
    # that identity trusted by the target; "sql" (default) is username/password auth.
    if opts.get("auth_type") == "windows":
        return f"{base};Trusted_Connection=yes"
    return f"{base};UID={target.username};PWD={target.password}"


# Bloklama ağacı için oturum ayrıntısı (Faz 26 İŞ 3).
#
# İKİ KAYNAK: `dm_exec_requests.blocking_session_id` isteğin kendi blokçusunu verir, ama
# PARALEL bir planda blokçu görev (task) düzeyinde görünür ve istekte 0 kalır. Yalnızca
# birincisine bakmak, paralel sorgulardaki bloklanmaları görünmez yapardı.
#
# `dm_exec_sessions` üzerinden gidiliyor (istekler değil), çünkü SESSİZ BLOKLAYANLAR açık
# transaction'ı olan ama HİÇBİR İSTEK ÇALIŞTIRMAYAN oturumlardır — `dm_exec_requests`'te
# hiç görünmezler.
_BLOCKING_SQL = """
SELECT TOP ({limit})
    s.session_id                                   AS pid,
    s.login_name                                   AS username,
    s.program_name                                 AS application,
    s.status                                       AS state,
    SUBSTRING(ISNULL(t.text, ''), 1, 2000)         AS query,
    DATEDIFF(second, r.start_time, GETDATE())      AS query_seconds,
    DATEDIFF(second, tat.transaction_begin_time, GETDATE()) AS transaction_seconds,
    r.wait_time / 1000.0                           AS wait_seconds,
    COALESCE(NULLIF(r.blocking_session_id, 0), w.blocking_session_id, 0) AS blocking_session_id,
    r.wait_type                                    AS lock_type,
    w.resource_description                         AS lock_object,
    ISNULL(lk.held, 0)                             AS held_locks
FROM sys.dm_exec_sessions s
LEFT JOIN sys.dm_exec_requests r ON r.session_id = s.session_id
OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
LEFT JOIN sys.dm_tran_session_transactions tst ON tst.session_id = s.session_id
LEFT JOIN sys.dm_tran_active_transactions tat ON tat.transaction_id = tst.transaction_id
LEFT JOIN (
    SELECT session_id,
           MAX(blocking_session_id) AS blocking_session_id,
           MAX(resource_description) AS resource_description
    FROM sys.dm_os_waiting_tasks
    WHERE blocking_session_id IS NOT NULL AND blocking_session_id <> 0
    GROUP BY session_id
) w ON w.session_id = s.session_id
LEFT JOIN (
    SELECT request_session_id, COUNT(*) AS held
    FROM sys.dm_tran_locks
    WHERE request_status = 'GRANT'
    GROUP BY request_session_id
) lk ON lk.request_session_id = s.session_id
WHERE s.session_id <> @@SPID
  AND s.session_id > 50
  AND (r.session_id IS NOT NULL OR tst.transaction_id IS NOT NULL)
"""


# Yedek geçmişi (Faz 28 İŞ 1) — `msdb.dbo.backupset`.
#
# SQL Server'ın PostgreSQL'e göre büyük avantajı: yedek geçmişi merkezi ve sorgulanabilir.
# `type` sütunu: D = full, I = differential, L = log, F = file/filegroup, G/P/Q = kısmi.
# Yalnızca D/I/L alınıyor; diğerleri yedek stratejisinin parçası olsa da yaş eşiği mantığına
# oturmuyor ve karıştırmak yanıltıcı olurdu.
#
# `backup_finish_date` NULL ise yedek DEVAM EDİYOR demek — bu satırlar da alınıyor çünkü
# "devam eden yedek ve süresi" ayrı bir gereksinim.
#
# LEFT JOIN backupmediafamily: fiziksel hedef (disk/URL) teşhis için değerli ama satır
# olmayabiliyor; INNER JOIN kullanmak o yedekleri tamamen kaybettirirdi.
_BACKUP_HISTORY_SQL = """
SELECT TOP ({limit})
    bs.backup_set_id                              AS external_id,
    bs.database_name                              AS database_name,
    bs.type                                       AS backup_type,
    bs.backup_start_date                          AS started_at,
    bs.backup_finish_date                         AS finished_at,
    DATEDIFF(second, bs.backup_start_date,
             ISNULL(bs.backup_finish_date, GETDATE())) AS duration_seconds,
    bs.backup_size                                AS size_bytes,
    bs.compressed_backup_size                     AS compressed_bytes,
    bs.server_name                                AS server_name,
    bs.is_copy_only                               AS is_copy_only,
    mf.physical_device_name                       AS device_name
FROM msdb.dbo.backupset bs
LEFT JOIN msdb.dbo.backupmediafamily mf ON mf.media_set_id = bs.media_set_id
WHERE bs.type IN ('D', 'I', 'L')
  AND bs.backup_start_date >= DATEADD(day, -{days}, GETDATE())
ORDER BY bs.backup_start_date DESC
"""

# Recovery model ile yedek stratejisi uyumu (Faz 28 İŞ 1).
#
# FULL recovery model'deki bir veritabanında log yedeği ALINMIYORSA transaction log dosyası
# sınırsız büyür ve eninde sonunda diski doldurur. Bu, SQL Server'da en sık görülen
# "disk doldu" sebebi ve tamamen önlenebilir bir hata — o yüzden ayrıca sorgulanıyor.
_RECOVERY_MODEL_SQL = """
SELECT
    d.name                                        AS database_name,
    d.recovery_model_desc                         AS recovery_model,
    d.state_desc                                  AS state,
    (SELECT MAX(bs.backup_finish_date)
     FROM msdb.dbo.backupset bs
     WHERE bs.database_name = d.name AND bs.type = 'D')   AS last_full_at,
    (SELECT MAX(bs.backup_finish_date)
     FROM msdb.dbo.backupset bs
     WHERE bs.database_name = d.name AND bs.type = 'L')   AS last_log_at
FROM sys.databases d
WHERE d.database_id > 4          -- sistem veritabanları hariç
  AND d.state_desc = 'ONLINE'
"""

#: SQL Server yedek türü kodları → ortak adlar.
_BACKUP_TYPE_MAP = {"D": "full", "I": "differential", "L": "log"}


class SqlServerCollector(BaseCollector):
    """DMV tabanlı SQL Server collector (aioodbc + bir ODBC sürücüsü gerektirir)."""

    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def _connect(self):
        try:
            import aioodbc
        except ImportError as exc:
            raise RuntimeError(
                "SQL Server collector requires aioodbc + an ODBC driver "
                "(e.g. 'ODBC Driver 18 for SQL Server') on the worker image."
            ) from exc
        conn = await aioodbc.connect(dsn=build_odbc_connection_string(self.target), timeout=10, autocommit=True)
        await register_datetimeoffset_converter(conn)
        # SQL Server has no direct, client-agnostic equivalent of PostgreSQL's
        # statement_timeout settable via plain SQL — LOCK_TIMEOUT bounds the most common real
        # cause of a monitoring query hanging (waiting on a lock held by another session), but
        # does NOT bound raw CPU/IO-bound execution time (see SORULAR.md).
        async with conn.cursor() as cur:
            await cur.execute(f"SET LOCK_TIMEOUT {COLLECTOR_LOCK_TIMEOUT_MS}")
        return conn

    async def open_connection(self):
        return await self._connect()

    @staticmethod
    async def _topology_facts(conn) -> dict[str, Any]:
        """IsHadrEnabled + sys.dm_hadr_availability_replica_states. KATALOG (`sys.availability_groups`)
        KULLANILMIYOR: VIEW SERVER STATE ile 0 satır dönüyor (meta veri görünürlüğü, ölçüldü); DMV'ler
        aynı login'le AG'yi ve kopuk replikayı gösteriyor."""
        facts: dict[str, Any] = {"hadr_enabled": None, "replicas": None, "error": None}
        try:
            async with conn.cursor() as cur:
                await cur.execute("SELECT CAST(SERVERPROPERTY('IsHadrEnabled') AS INT)")
                row = await cur.fetchone()
                facts["hadr_enabled"] = bool(row[0]) if row and row[0] is not None else False
        except Exception as exc:  # noqa: BLE001
            facts["error"] = str(exc)
            return facts
        if not facts["hadr_enabled"]:
            return facts
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT cs.replica_server_name, rs.role_desc, rs.connected_state_desc,
                           rs.synchronization_health_desc, CAST(rs.is_local AS INT)
                    FROM sys.dm_hadr_availability_replica_states rs
                    LEFT JOIN sys.dm_hadr_availability_replica_cluster_states cs ON cs.replica_id = rs.replica_id
                    """
                )
                rows = await cur.fetchall()
            facts["replicas"] = [
                {"replica_server_name": r[0], "role_desc": r[1], "connected_state_desc": r[2],
                 "synchronization_health_desc": r[3], "is_local": bool(r[4])}
                for r in rows
            ]
        except Exception as exc:  # noqa: BLE001
            facts["error"] = str(exc)
        return facts

    async def _detect_version(self, conn) -> tuple[int, str]:
        """Returns (ProductMajorVersion, @@VERSION text). 2016=13, 2017=14, 2019=15, 2022=16."""
        async with conn.cursor() as cur:
            await cur.execute("SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS INT), @@VERSION")
            row = await cur.fetchone()
        major = int(row[0]) if row and row[0] is not None else 0
        version_string = row[1] if row and row[1] else "unknown"
        if major and major < SQLSERVER_MIN_SUPPORTED_MAJOR:
            logger.warning(
                "SQL Server ProductMajorVersion=%s desteklenen minimumun (2016, major=13) "
                "altında — yine de en yakın davranışla devam ediliyor",
                major,
            )
        return major, version_string

    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            import aioodbc  # noqa: F401
        except ImportError:
            return (
                False,
                "SQL Server collector requires aioodbc + an ODBC driver on the worker image.",
                {"engine": "sqlserver", "status": "stub"},
            )
        try:
            conn = await self._connect()
        except Exception as exc:
            logger.exception("SQL Server connection test failed")
            return False, classify_connection_error(exc), {"engine": "sqlserver"}
        try:
            major, version_string = await self._detect_version(conn)
            return True, "Connection successful", {
                "engine": "sqlserver",
                "version": version_string,
                "server_version_major": major,
            }
        except Exception as exc:
            logger.exception("SQL Server connection test query failed")
            return False, classify_connection_error(exc), {"engine": "sqlserver"}
        finally:
            await conn.close()

    async def collect_metrics(
        self, previous: dict[str, float] | None = None, conn: Any | None = None
    ) -> dict[str, Any]:
        owns_conn = conn is None
        if owns_conn:
            conn = await self._connect()
        # metric_key -> Turkish reason it couldn't be collected this cycle — same channel as
        # PostgreSQLCollector, surfaced via Instance.unsupported_metrics.
        unsupported: dict[str, str] = {}
        try:
            version_major, version_string = await self._detect_version(conn)

            # Core session counts — stable DMVs since SQL Server 2005, not individually guarded
            # (if these fail, the connection itself is broken; classify_connection_error already
            # covers that at the test_connection level, and collect_all_instances logs+skips a
            # collector that raises here).
            async with conn.cursor() as cur:
                await cur.execute("SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE is_user_process = 1")
                active_connections = int((await cur.fetchone())[0] or 0)

                await cur.execute(
                    "SELECT CAST(value_in_use AS INT) FROM sys.configurations WHERE name = 'user connections'"
                )
                row = await cur.fetchone()
                # user connections = 0 means "no limit"; use SQL Server's hard ceiling instead.
                max_connections = int(row[0]) if row and row[0] else 32767

                await cur.execute("SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id <> 0")
                blocked_sessions = int((await cur.fetchone())[0] or 0)

            # Perf counters vary more by edition/config than by version proper — e.g. Azure SQL
            # Database doesn't expose "Buffer cache hit ratio" the same way a standalone/AG
            # instance does — so this is guarded independently rather than assumed always present.
            counters: dict[str, float] = {}
            try:
                async with conn.cursor() as cur:
                    await cur.execute(_PERF_COUNTER_QUERY)
                    counters = {name: float(value) for name, value in await cur.fetchall()}
            except Exception as exc:
                logger.warning("SQL Server perf counter query failed (major=%s): %s", version_major, exc)
                for key in ("transactions_per_sec", "cache_hit_ratio", "deadlocks"):
                    unsupported[key] = f"Toplama hatası: {exc}"

            db_size = 0.0
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT SUM(CAST(size AS BIGINT)) * 8.0 * 1024 FROM sys.database_files")
                    db_size = float((await cur.fetchone())[0] or 0)
            except Exception as exc:
                logger.warning("SQL Server database size query failed: %s", exc)
                unsupported["database_size_bytes"] = f"Toplama hatası: {exc}"

            # tempdb file size as a proxy for temp space usage — the exact "used" figure needs
            # sys.dm_db_file_space_usage, which is only queryable from within tempdb itself.
            # Guarded separately since it needs cross-database visibility some restricted
            # logins won't have.
            temp_bytes = 0.0
            try:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT SUM(CAST(size AS BIGINT)) * 8.0 * 1024 FROM tempdb.sys.database_files")
                    temp_bytes = float((await cur.fetchone())[0] or 0)
            except Exception as exc:
                logger.warning("SQL Server tempdb size query failed (permissions?): %s", exc)
                unsupported["temp_bytes"] = f"Toplama hatası: {exc}"
        finally:
            if owns_conn:
                await conn.close()

        batch_requests_cum = counters.get("Batch Requests/sec", 0.0)
        transactions_per_sec = 0.0
        if previous and "batch_requests_cum" in previous:
            delta = batch_requests_cum - previous["batch_requests_cum"]
            delta_time = previous.get("delta_time", 15)
            transactions_per_sec = max(delta / delta_time, 0)

        hit = counters.get("Buffer cache hit ratio", 0.0)
        base = counters.get("Buffer cache hit ratio base", 0.0)
        cache_hit_ratio = (hit / base * 100) if base else 100.0

        metrics: dict[str, Any] = {
            "active_connections": active_connections,
            "max_connections": max_connections,
            "replication_lag_bytes": None,
            "blocked_sessions": blocked_sessions,
            # Faz 31 Commit 6: topoloji ham verisi (services/server_topology.py).
            "_topology_facts": await self._topology_facts(conn),
            "_state": {"batch_requests_cum": batch_requests_cum},
            "_server_version": version_string,
            "_server_version_num": version_major,
            "_unsupported_metrics": unsupported,
        }
        # SQL Server'da Postgres tarzı "transaction/sec" yok; canonical transactions_per_sec
        # slotunu Batch Requests/sec ile dolduruyoruz — perf counter query başarısız olduysa
        # bu üçü de yukarıda unsupported'a düştü, metrics'te hiç görünmüyorlar (0 değil, yok).
        if "transactions_per_sec" not in unsupported:
            metrics["transactions_per_sec"] = round(transactions_per_sec, 2)
        if "cache_hit_ratio" not in unsupported:
            metrics["cache_hit_ratio"] = round(cache_hit_ratio, 2)
        if "deadlocks" not in unsupported:
            metrics["deadlocks"] = int(counters.get("Number of Deadlocks/sec", 0))
        if "database_size_bytes" not in unsupported:
            metrics["database_size_bytes"] = db_size
        if "temp_bytes" not in unsupported:
            metrics["temp_bytes"] = temp_bytes

        return metrics

    async def collect_slow_queries(self, limit: int = 20, conn: Any | None = None) -> list[dict[str, Any]]:
        owns_conn = conn is None
        if owns_conn:
            conn = await self._connect()
        try:
            # FAZ 29 İŞ 2c: SÜTUNLARI TAHMİN ETMEK YERİNE SORUYORUZ.
            #
            # Önceki kod `total_rows` için "dene, patlarsa sütunsuz tekrar dene" yapıyordu.
            # Sömürülecek sütun sayısı arttıkça bu desen kombinatoryal hale geliyor (her
            # opsiyonel sütun için ayrı bir yedek sorgu). DMV'nin gerçek sütun listesini
            # okumak tek round-trip ve hiçbir SP kombinasyonunda "invalid column name"
            # riski bırakmıyor.
            async with conn.cursor() as cur:
                await cur.execute(_QUERY_STATS_COLUMNS_SQL)
                available = {str(r[0]) for r in await cur.fetchall()}

            missing_base = _QS_BASE_COLUMNS - available
            if missing_base:
                # Temel sütunlar yoksa bu bir DMV değil ya da yetki sorunu var; sessizce boş
                # dönmek "yavaş sorgu yok" izlenimi verirdi.
                logger.warning(
                    "sys.dm_exec_query_stats beklenen sütunları taşımıyor (%s); yavaş sorgu "
                    "toplanamadı", sorted(missing_base),
                )
                return []

            async with conn.cursor() as cur:
                await cur.execute(build_slow_query_sql(available).format(limit=int(limit)))
                columns = [c[0] for c in cur.description]
                rows = await cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            if owns_conn:
                await conn.close()

    async def collect_index_advice(self, limit: int = 20) -> dict[str, Any]:
        """SQL Server'ın kendi eksik index önerileri + kullanılmayan index'ler (Faz 29 İŞ 2c).

        İkisi bir arada dönüyor çünkü birlikte okunmaları gerekiyor: yeni bir index eklemeden
        önce kullanılmayanları silmek, aynı tabloda yazma maliyetini dengede tutar.
        """
        conn = await self._connect()
        result: dict[str, Any] = {"missing_indexes": [], "unused_indexes": [], "errors": {}}
        try:
            async with conn.cursor() as cur:
                await cur.execute("SET LOCK_TIMEOUT 5000")
            try:
                async with conn.cursor() as cur:
                    await cur.execute(_MISSING_INDEX_SQL.format(limit=int(limit)))
                    columns = [c[0] for c in cur.description]
                    result["missing_indexes"] = [
                        dict(zip(columns, row)) for row in await cur.fetchall()
                    ]
            except Exception as exc:
                # Yetki eksikse (VIEW SERVER STATE) sessizce boş dönmek "eksik index yok"
                # izlenimi verirdi; sebep taşınıyor.
                result["errors"]["missing_indexes"] = str(exc)[:300]

            try:
                async with conn.cursor() as cur:
                    await cur.execute(_UNUSED_INDEX_SQL.format(limit=int(limit)))
                    columns = [c[0] for c in cur.description]
                    result["unused_indexes"] = [
                        dict(zip(columns, row)) for row in await cur.fetchall()
                    ]
            except Exception as exc:
                result["errors"]["unused_indexes"] = str(exc)[:300]
            return result
        finally:
            await conn.close()

    async def collect_schema_health(self, limit: int = 50) -> dict[str, Any]:
        """SQL Server şema sağlığı (Faz 29 İŞ 2c).

        Bu motor için ÖNCEDEN HİÇ VERİ YOKTU: taban sınıf boş sözlük döndürüyordu, yani
        SQL Server kullanan bir müşteride Şema sekmesi hep boştu.

        İki kaynak:

        * `sys.dm_db_missing_index_*` — SQL Server'ın KENDİ eksik index önerileri. Motor
          zaten planlama sırasında "şu index olsaydı" bilgisini biriktiriyor; bunu
          kullanmamak hazır bir kaynağı çöpe atmaktı.
        * `sys.dm_db_index_usage_stats` — hiç kullanılmayan index'ler.

        PostgreSQL'deki `bloated_tables` / `vacuum_lag` karşılıkları BOŞ dönüyor: SQL
        Server'da ölü satır/vacuum kavramı yok, karşılığı index parçalanması ve o ayrı bir
        ölçüm (`sys.dm_db_index_physical_stats`, tabloları kilitlemeden okumak için
        LIMITED modu gerekir). Boş bırakmak, olmayan bir şeyi varmış gibi doldurmaktan iyi.
        """
        advice = await self.collect_index_advice(limit=limit)

        unused_indexes = []
        for row in advice.get("unused_indexes") or []:
            schema = row.get("schema_name") or "dbo"
            table = row.get("table_name") or "?"
            index = row.get("index_name") or "?"
            unused_indexes.append(
                {
                    "schema_name": schema,
                    "table_name": table,
                    "index_name": index,
                    "index_bytes": int(row.get("index_bytes") or 0),
                    # PostgreSQL alan adları korunuyor ki arayüz tek tablo ile iki motoru
                    # gösterebilsin; SQL Server'ın seek/scan/lookup üçlüsü tek "kullanım"
                    # sayısına indirgeniyor ve ham değerler `usage_detail`'de duruyor.
                    "idx_scan": int(row.get("user_seeks") or 0) + int(row.get("user_scans") or 0),
                    "idx_tup_read": int(row.get("user_lookups") or 0),
                    "idx_tup_fetch": int(row.get("user_updates") or 0),
                    "index_def": f"{row.get('index_type') or 'NONCLUSTERED'} index",
                    "drop_ddl": f"DROP INDEX [{index}] ON [{schema}].[{table}];",
                    # SQL Server kullanım sayaçlarını SERVİS YENİDEN BAŞLATILDIĞINDA
                    # sıfırlıyor. Sayaçlar bir saatlik ise "hiç kullanılmadı" demek yanlış
                    # olur; bu yüzden yaş küçükse ciddiyet düşürülüyor.
                    "severity": (
                        "medium"
                        if int(row.get("stats_age_seconds") or 0) >= 7 * 86400
                        else "low"
                    ),
                    "stats_age_seconds": int(row.get("stats_age_seconds") or 0),
                }
            )

        missing_indexes = [
            {
                "schema_name": row.get("schema_name") or "dbo",
                "table_name": row.get("table_name") or "?",
                "equality_columns": row.get("equality_columns"),
                "inequality_columns": row.get("inequality_columns"),
                "included_columns": row.get("included_columns"),
                "user_seeks": int(row.get("user_seeks") or 0),
                "user_scans": int(row.get("user_scans") or 0),
                "avg_user_impact": float(row.get("avg_user_impact") or 0),
                "avg_total_user_cost": float(row.get("avg_total_user_cost") or 0),
                "improvement_measure": float(row.get("improvement_measure") or 0),
                "last_user_seek": (
                    row["last_user_seek"].isoformat() if row.get("last_user_seek") else None
                ),
            }
            for row in advice.get("missing_indexes") or []
        ]

        return {
            "unused_indexes": unused_indexes,
            "bloated_tables": [],
            "vacuum_lag": [],
            "missing_indexes": missing_indexes,
            "errors": advice.get("errors") or {},
            "totals": {
                "unused_indexes": len(unused_indexes),
                "unused_index_bytes": sum(i["index_bytes"] for i in unused_indexes),
                "bloated_tables": 0,
                "vacuum_lag_tables": 0,
                "missing_indexes": len(missing_indexes),
            },
        }

    async def open_sampling_connection(self) -> SamplingConnection | None:
        conn = await self._connect()
        async with conn.cursor() as cur:
            await cur.execute(f"SET LOCK_TIMEOUT {SAMPLER_LOCK_TIMEOUT_MS}")
        major, _ = await self._detect_version(conn)
        return SamplingConnection(raw=conn, capabilities={"product_major_version": major})

    async def sample_active_sessions(
        self, conn: SamplingConnection, limit: int = 200
    ) -> dict[str, Any] | None:
        async with conn.raw.cursor() as cur:
            await cur.execute(_ACTIVE_SESSION_SAMPLE_SQL.format(limit=int(limit)))
            columns = [c[0] for c in cur.description]
            rows = [dict(zip(columns, row)) for row in await cur.fetchall()]

        sessions: list[dict[str, Any]] = []
        blocked = 0
        for row in rows:
            wait_type = row.get("wait_type")
            is_blocked = bool(row.get("blocking_session_id"))
            if is_blocked:
                blocked += 1
            sessions.append(
                {
                    # query_hash sıfır olabilir (ad-hoc toplu iş) — boş dizgi "kimlik yok"
                    # demek ve dakikalık toplamada tek kovada birleşiyor.
                    "queryid": (row.get("queryid") or "").strip() or "",
                    "query": (row.get("query_text") or "").strip(),
                    "wait_category": str(classify_sqlserver_wait(wait_type)),
                    "wait_event": (wait_type or "").strip(),
                    "blocked": is_blocked,
                }
            )
        # SQL Server'da query_hash her zaman var (sürüme bağlı değil) — PostgreSQL'deki
        # `query_id` sürüm kısıtının karşılığı burada yok.
        return {"sessions": sessions, "blocked": blocked, "has_query_id": True}

    async def collect_blocking(self, limit: int = 200, conn: Any | None = None) -> list[dict[str, Any]]:
        owns_conn = conn is None
        if owns_conn:
            conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_BLOCKING_SQL.format(limit=int(limit)))
                columns = [c[0] for c in cur.description]
                rows = [dict(zip(columns, row)) for row in await cur.fetchall()]
        finally:
            if owns_conn:
                await conn.close()

        out: list[dict[str, Any]] = []
        for row in rows:
            blocker = int(row.get("blocking_session_id") or 0)
            status = (row.get("state") or "").strip().lower()
            transaction_seconds = row.get("transaction_seconds")
            # SQL Server'da "sleeping" + açık transaction, PostgreSQL'deki
            # `idle in transaction`ın karşılığıdır. Adı farklı, tehlikesi aynı — ortak ada
            # çevriliyor ki bloklama mantığı iki motorda tek kod olsun.
            if status == "sleeping" and transaction_seconds is not None:
                state = "idle in transaction"
            else:
                state = row.get("state")
            out.append(
                {
                    "pid": int(row["pid"]),
                    "username": row.get("username"),
                    "application": (row.get("application") or "").strip() or None,
                    "state": state,
                    "query": (row.get("query") or "").strip(),
                    "query_seconds": _as_float(row.get("query_seconds")),
                    "transaction_seconds": _as_float(transaction_seconds),
                    "wait_seconds": _as_float(row.get("wait_seconds")),
                    "blocking_pids": [blocker] if blocker else [],
                    "lock_type": row.get("lock_type"),
                    "lock_mode": None,
                    "lock_object": row.get("lock_object"),
                    "held_locks": int(row.get("held_locks") or 0),
                }
            )
        return out

    async def collect_backups(self, limit: int = 500, days: int = 90) -> dict[str, Any]:
        """`msdb` yedek geçmişi + recovery model uyumu (Faz 28 İŞ 1).

        `days` penceresi bilerek geniş (90 gün): yaş eşiği hesabı için EN SON yedeği bulmak
        gerekiyor ve haftalık tam yedek alan bir kurumda dar bir pencere "hiç yedek yok"
        sonucunu verirdi — ki bu tam olarak kaçınmamız gereken yanlış.
        """
        conn = await self._connect()
        try:
            result: dict[str, Any] = {
                "archiver": None, "slots": [], "running": [], "records": [], "errors": {}
            }

            try:
                async with conn.cursor() as cur:
                    await cur.execute(_BACKUP_HISTORY_SQL.format(limit=int(limit), days=int(days)))
                    columns = [c[0] for c in cur.description]
                    rows = [dict(zip(columns, row)) for row in await cur.fetchall()]
                result["records"] = [self._backup_record(row) for row in rows]
            except Exception as exc:
                # Yetki yoksa ya da geçmiş temizlenmişse: sessizce boş dönmek "yedek yok"
                # izlenimi verirdi. Sebep kaydediliyor ve kullanıcıya gösteriliyor.
                result["errors"]["msdb"] = str(exc)

            try:
                async with conn.cursor() as cur:
                    await cur.execute(_RECOVERY_MODEL_SQL)
                    columns = [c[0] for c in cur.description]
                    result["recovery_models"] = [
                        {
                            "database_name": r["database_name"],
                            "recovery_model": r["recovery_model"],
                            "last_full_at": _iso(r["last_full_at"]),
                            "last_log_at": _iso(r["last_log_at"]),
                        }
                        for r in (dict(zip(columns, row)) for row in await cur.fetchall())
                    ]
            except Exception as exc:
                result["errors"]["recovery_model"] = str(exc)

            return result
        finally:
            await conn.close()

    @staticmethod
    def _backup_record(row: dict[str, Any]) -> dict[str, Any]:
        finished = row.get("finished_at")
        return {
            "external_id": str(row.get("external_id") or ""),
            "database_name": row.get("database_name"),
            "backup_type": _BACKUP_TYPE_MAP.get((row.get("backup_type") or "").strip(), "full"),
            "started_at": _iso(row.get("started_at")),
            "finished_at": _iso(finished),
            "duration_seconds": _as_float(row.get("duration_seconds")),
            "size_bytes": _as_float(row.get("size_bytes")),
            # `backup_finish_date` NULL = yedek hâlâ sürüyor. msdb başarısız yedekleri
            # KAYDETMEZ (satır yalnızca başarıda yazılır), bu yüzden burada "failed"
            # üretilmiyor — başarısızlık ayrı bir kaynaktan (SQL Server Agent iş geçmişi)
            # gelmeli ve o kapsamda değil. Uydurma bir "failed" yazmak yanlış olurdu.
            "status": "running" if finished is None else "success",
            "source": "msdb",
            "detail": {
                "server_name": row.get("server_name"),
                "device_name": row.get("device_name"),
                "compressed_bytes": _as_float(row.get("compressed_bytes")),
                "is_copy_only": bool(row.get("is_copy_only")),
            },
        }

    async def run_readonly(self, sql: str, conn: Any | None = None) -> list[dict[str, Any]]:
        """Salt-okunur tek sorgu — sonuç sözlük listesi (Faz 31 Commit 9).

        Query Store ve bekleme istatistikleri gibi "yalnızca oku, yorumu serviste yap" yolları için. Yazma
        ifadesi göndermiyor; hedefe giden her sorgu gibi dbace imzasını taşıyor (query_marker).
        """
        owns_conn = conn is None
        if owns_conn:
            conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                columns = [c[0] for c in cur.description]
                return [dict(zip(columns, row)) for row in await cur.fetchall()]
        finally:
            if owns_conn:
                await conn.close()

    async def collect_deadlocks(self, limit: int = 20) -> list[dict[str, Any]]:
        """system_health halka tamponundaki deadlock raporları (Faz 26 İŞ 3).

        Deadlock ANLIK bir olaydır: veritabanı döngüyü kırar ve bir tarafı iptal eder; canlı
        ekranda hiçbir izi kalmaz. SQL Server bu olayları `system_health` genişletilmiş olay
        oturumunda tutuyor — 2012+ ile VARSAYILAN OLARAK açık, ek yapılandırma gerektirmiyor.

        Halka tamponu döngüsel: eski olaylar zamanla düşüyor. Bu yüzden periyodik olarak
        okunup kalıcı tabloya yazılıyor.
        """
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(SQLSERVER_DEADLOCK_SQL.format(limit=int(limit)))
                columns = [c[0] for c in cur.description]
                return [dict(zip(columns, row)) for row in await cur.fetchall()]
        finally:
            await conn.close()

    async def collect_activity(self, limit: int = 100) -> dict[str, Any]:
        conn = await self._connect()
        try:
            async with conn.cursor() as cur:
                await cur.execute(_ACTIVITY_SQL.format(limit=int(limit)))
                columns = [c[0] for c in cur.description]
                rows = [dict(zip(columns, row)) for row in await cur.fetchall()]
        finally:
            await conn.close()

        sessions: list[dict[str, Any]] = []
        wait_counts: dict[str, int] = {}
        state_counts: dict[str, int] = {}
        blocking: list[dict[str, Any]] = []

        for row in rows:
            status = (row.get("status") or "").strip().lower()
            wait_type = row.get("wait_type")
            blocking_session_id = row.get("blocking_session_id") or 0
            blocked = blocking_session_id != 0
            start_time = row.get("start_time")
            query_duration_sec = 0.0
            if start_time is not None:
                now = datetime.now(UTC)
                started = start_time if start_time.tzinfo else start_time.replace(tzinfo=UTC)
                query_duration_sec = max((now - started).total_seconds(), 0.0)

            if status == "running":
                state = "waiting" if blocked or wait_type else "active"
            elif status == "sleeping":
                state = "idle in transaction" if row.get("open_transaction_count") else "idle"
            else:
                state = status or "unknown"
            state_counts[state] = state_counts.get(state, 0) + 1

            if wait_type:
                wait_counts[wait_type] = wait_counts.get(wait_type, 0) + 1

            sessions.append(
                {
                    "pid": row["session_id"],
                    "usename": row.get("login_name"),
                    "datname": row.get("datname"),
                    "application_name": row.get("program_name") or "",
                    "client_addr": row.get("client_net_address"),
                    "state": state,
                    "wait_event_type": None,
                    "wait_event": wait_type,
                    "backend_type": None,
                    "query_start": start_time.isoformat() if start_time else None,
                    "state_change": row["last_request_end_time"].isoformat()
                    if row.get("last_request_end_time")
                    else None,
                    "xact_start": None,
                    "query_duration_sec": round(query_duration_sec, 2),
                    "xact_duration_sec": None,
                    "query": row.get("query_text") or "",
                    "blocking_pids": [blocking_session_id] if blocked else [],
                    "blocked": blocked,
                }
            )

            if blocked:
                blocking.append(
                    {
                        "blocked_pid": row["session_id"],
                        "blocking_pid": blocking_session_id,
                        "blocked_query": row.get("query_text") or "",
                        "wait_event_type": None,
                        "wait_event": wait_type,
                        "duration_sec": round(query_duration_sec, 2),
                    }
                )

        totals = {
            "total": len(sessions),
            "active": state_counts.get("active", 0),
            "idle": state_counts.get("idle", 0),
            "idle_in_transaction": state_counts.get("idle in transaction", 0),
            "waiting": state_counts.get("waiting", 0),
            "blocked": sum(1 for s in sessions if s["blocked"]),
        }

        return {
            "sessions": sessions,
            "wait_events": [
                {"wait_event_type": "sqlserver", "wait_event": name, "count": count}
                for name, count in sorted(wait_counts.items(), key=lambda kv: kv[1], reverse=True)
            ],
            "state_summary": [{"state": state, "count": count} for state, count in state_counts.items()],
            "blocking": blocking,
            "totals": totals,
        }


class MongoDBCollector(BaseCollector):
    def __init__(self, target: ConnectionTarget) -> None:
        self.target = target

    async def test_connection(self) -> tuple[bool, str, dict[str, Any]]:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError:
            return (
                False,
                "MongoDB collector requires motor on the worker image.",
                {"engine": "mongodb", "status": "stub"},
            )

        uri = self._build_uri()
        client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
        try:
            info = await client.server_info()
            return True, "Connection successful", {
                "engine": "mongodb",
                "version": info.get("version"),
            }
        except Exception as exc:
            logger.exception("MongoDB connection test failed")
            return False, classify_connection_error(exc), {}
        finally:
            client.close()

    def _build_uri(self) -> str:
        auth = f"{self.target.username}:{self.target.password}@"
        db = self.target.database or "admin"
        opts = self.target.options or {}
        auth_source = opts.get("authSource", db)
        query = f"authSource={auth_source}"
        replica_set = opts.get("replica_set")
        if replica_set:
            query += f"&replicaSet={replica_set}"
        return f"mongodb://{auth}{self.target.host}:{self.target.port}/{db}?{query}"

    async def collect_metrics(
        self, previous: dict[str, float] | None = None, conn: Any | None = None
    ) -> dict[str, Any]:
        # MongoDBCollector doesn't override open_connection() (motor's AsyncIOMotorClient
        # pools internally rather than needing an explicit shared connection across calls), so
        # `conn` is always None here — accepted for interface consistency with
        # collect_instance()'s uniform call, otherwise unused.
        from motor.motor_asyncio import AsyncIOMotorClient

        client = AsyncIOMotorClient(self._build_uri(), serverSelectionTimeoutMS=8000)
        try:
            status = await client.admin.command("serverStatus")
            conn = status.get("connections", {})
            active = int(conn.get("current", 0))
            available = int(conn.get("available", 0))
            max_conn = active + available
            util = (active / max_conn * 100) if max_conn else 0.0

            opcounters = status.get("opcounters", {})
            total_ops = sum(float(opcounters.get(k, 0)) for k in ("insert", "query", "update", "delete"))
            ops_per_sec = 0.0
            if previous and "total_ops" in previous:
                delta = total_ops - previous["total_ops"]
                delta_time = previous.get("delta_time", 15)
                ops_per_sec = max(delta / delta_time, 0)

            wired = status.get("wiredTiger", {}).get("cache", {})
            bytes_in = float(wired.get("bytes read into cache", 0))
            bytes_out = float(wired.get("bytes written from cache", 0))
            hit_ratio = 100.0
            if bytes_in + bytes_out > 0:
                hit_ratio = bytes_out / (bytes_in + bytes_out) * 100

            db_stats = await client[self.target.database or "admin"].command("dbStats")
            size = float(db_stats.get("dataSize", 0))

            repl = status.get("repl", {})
            lag_bytes = None
            if repl.get("ismaster") is False:
                lag_bytes = float(repl.get("lag", 0) or 0)

            return {
                "active_connections": active,
                "max_connections": max_conn,
                "connection_utilization_pct": round(util, 2),
                "ops_per_sec": round(ops_per_sec, 2),
                "cache_hit_ratio": round(hit_ratio, 2),
                "replication_lag_bytes": lag_bytes,
                "database_size_bytes": size,
                "deadlocks": 0,
                "temp_bytes": 0.0,
                "_state": {"total_ops": total_ops},
            }
        finally:
            client.close()
