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
        # SQL Server has no direct, client-agnostic equivalent of PostgreSQL's
        # statement_timeout settable via plain SQL — LOCK_TIMEOUT bounds the most common real
        # cause of a monitoring query hanging (waiting on a lock held by another session), but
        # does NOT bound raw CPU/IO-bound execution time (see SORULAR.md).
        async with conn.cursor() as cur:
            await cur.execute(f"SET LOCK_TIMEOUT {COLLECTOR_LOCK_TIMEOUT_MS}")
        return conn

    async def open_connection(self):
        return await self._connect()

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
            try:
                async with conn.cursor() as cur:
                    await cur.execute(_SLOW_QUERY_SQL.format(limit=int(limit)))
                    columns = [c[0] for c in cur.description]
                    rows = await cur.fetchall()
            except Exception as exc:
                # sys.dm_exec_query_stats.total_rows was added by a specific SP/CU, not
                # cleanly by major version — on a server that doesn't have it, retry once
                # without that column rather than losing slow-query data entirely.
                logger.warning(
                    "sys.dm_exec_query_stats.total_rows sorgusu başarısız (muhtemelen bu SQL "
                    "Server sürümü/SP'sinde yok), rows olmadan yeniden deneniyor: %s", exc
                )
                async with conn.cursor() as cur:
                    await cur.execute(_SLOW_QUERY_SQL_NO_ROWS.format(limit=int(limit)))
                    columns = [c[0] for c in cur.description]
                    rows = await cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
        finally:
            if owns_conn:
                await conn.close()

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
