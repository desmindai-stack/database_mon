"""Query Store plan regresyonu — GERÇEK SQL Server (Faz 31 Commit 9, madde 4).

Üç durum ölçülüyor, hepsi bankadaki salt-okunur login'le (VIEW SERVER STATE + VIEW DATABASE STATE, sysadmin
YOK):

1. **Query Store AÇIK, gerçek regresyon:** aynı sorgu önce paralel planla, sonra (MAXDOP 1 yapılınca)
   seri planla çalışıyor — gerçek hayattaki "yapılandırma değişti, plan bozuldu" durumu. Query Store iki planı
   da tutuyor; dbace yeni planın eskisinden kaç kat yavaş olduğunu ölçüyor (bu sunucuda ~8 kat).
2. **Query Store KAPALI:** "ölçülemedi" + gerekçe + AÇMA KOMUTU (DBA çalıştırır; uygulama yazmıyor).
3. **Yetkisiz login:** "ölçülemedi" + gereken GRANT komutu.

Kurulum ve veri üretimi `sa` ile (DBA'nın işi); ÖLÇÜM her zaman salt-okunur login'le.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from app.services.query_store import (
    KIND_DISABLED,
    KIND_UNAUTHORIZED,
    MIN_EXECUTIONS,
    build_report,
)
from tests.live_mssql import (
    LOGINS,
    MONITOR_LOGIN,
    MONITOR_PASSWORD,
    MSSQL_SKIP_REASON,
    MSSQL_TARGETS,
    SA_PASSWORD,
    odbc_options,
    prepare_monitor_login,
    standalone_target,
)

pyodbc = pytest.importorskip("pyodbc")
# Havuzlama KAPALI: test veritabanı düşürülüp yeniden kuruluyor; havuzdaki eski bağlantı
# "ConnectionRead (recv())" hatası veriyor (ölçüldü).
pyodbc.pooling = False
pytest.importorskip("aioodbc")
pytestmark = pytest.mark.skipif("standalone" not in MSSQL_TARGETS, reason=MSSQL_SKIP_REASON)

QS_DATABASE = "dbace_qs_it"


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


def _connect(user: str, password: str, database: str, autocommit: bool = True):
    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    return pyodbc.connect(build_odbc_connection_string(standalone_target(user, password, database)),
                          autocommit=autocommit)


def _instance(database: str, user: str = MONITOR_LOGIN, password: str = MONITOR_PASSWORD):
    """Meta veritabanına yazmadan, yalnızca bağlantı bilgisi taşıyan bir Instance nesnesi."""
    from app.models import Instance
    from app.services.credentials import encrypt_secret

    target = standalone_target(user, password, database)
    return Instance(name=f"qs-{uuid.uuid4().hex[:6]}", engine="sqlserver", host=target.host, port=target.port,
                    database=database, username=user, password=encrypt_secret(password),
                    options=odbc_options() or None, enabled=True)


def _prepare_database(*, query_store: bool) -> None:
    """Temiz veritabanı, izleme login'i için kullanıcı ve (istenirse) açık Query Store."""
    conn = _connect("sa", SA_PASSWORD, "master")
    cur = conn.cursor()
    cur.execute(f"IF DB_ID('{QS_DATABASE}') IS NOT NULL BEGIN ALTER DATABASE [{QS_DATABASE}] "
                f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{QS_DATABASE}]; END")
    cur.execute(f"CREATE DATABASE [{QS_DATABASE}]")
    if query_store:
        cur.execute(f"ALTER DATABASE [{QS_DATABASE}] SET QUERY_STORE = ON")
        # Aralık en kısa değere çekiliyor ki test içinde istatistikler toplansın.
        cur.execute(f"ALTER DATABASE [{QS_DATABASE}] SET QUERY_STORE (OPERATION_MODE = READ_WRITE, "
                    "INTERVAL_LENGTH_MINUTES = 1, DATA_FLUSH_INTERVAL_SECONDS = 60)")
    else:
        cur.execute(f"ALTER DATABASE [{QS_DATABASE}] SET QUERY_STORE = OFF")
    cur.execute(f"USE [{QS_DATABASE}]; CREATE USER [{MONITOR_LOGIN}] FOR LOGIN [{MONITOR_LOGIN}]; "
                f"GRANT VIEW DATABASE STATE TO [{MONITOR_LOGIN}];")
    conn.close()


def _produce_regression() -> dict:
    """Aynı sorgu metni için İKİ plan: önce paralel plan (hızlı), sonra MAXDOP 1 ile seri plan (yavaş).

    Gerçek hayattaki sebep: sunucu/veritabanı yapılandırması değişince (MAXDOP, cost threshold, uyumluluk
    seviyesi) sorgu YENİDEN DERLENİYOR ve yeni plan eskisinden yavaş olabiliyor. Bu sunucuda ölçülen etki
    ~8 kat. Sorgu metni değişmediği için Query Store ikisini aynı sorgunun iki planı olarak tutuyor.

    Ölçülen ikinci davranış: Query Store çalışma istatistiklerini bir tur GECİKMELİ yazıyor — son partiden
    sonra ikinci bir parti ve flush gerekiyor.
    """
    conn = _connect("sa", SA_PASSWORD, QS_DATABASE)
    cur = conn.cursor()
    cur.execute("CREATE TABLE dbo.orders (id INT IDENTITY PRIMARY KEY, status INT NOT NULL, note CHAR(200) NOT NULL)")
    cur.execute("INSERT INTO dbo.orders (status, note) SELECT TOP 400000 ABS(CHECKSUM(NEWID())) % 400, 'x' "
                "FROM sys.all_objects a CROSS JOIN sys.all_objects b")

    query = "SELECT COUNT(*), MAX(note) FROM dbo.orders WHERE status < @p1"
    runs = MIN_EXECUTIONS + 3

    def run() -> None:
        for _ in range(runs):
            cur.execute("EXEC sp_executesql N'SELECT COUNT(*), MAX(note) FROM dbo.orders WHERE status < @p1', "
                        "N'@p1 INT', @p1 = ?", 400).fetchall()

    def flush() -> None:
        cur.execute("EXEC sys.sp_query_store_flush_db")

    run()                                                            # paralel plan (hızlı)
    flush()
    cur.execute("ALTER DATABASE SCOPED CONFIGURATION SET MAXDOP = 1")  # yapılandırma değişikliği
    run()                                                            # aynı metin, yeni plan (yavaş)
    flush()
    time.sleep(2)
    run()                                                            # istatistikler bir tur gecikmeli
    flush()
    rows = cur.execute(
        "SELECT p.plan_id, SUM(rs.count_executions), "
        "SUM(rs.avg_duration * rs.count_executions) / NULLIF(SUM(rs.count_executions), 0) / 1000.0 "
        "FROM sys.query_store_plan p JOIN sys.query_store_query q ON q.query_id = p.query_id "
        "JOIN sys.query_store_query_text t ON t.query_text_id = q.query_text_id "
        "LEFT JOIN sys.query_store_runtime_stats rs ON rs.plan_id = p.plan_id "
        "WHERE t.query_sql_text LIKE '%dbo.orders WHERE status < @p1%' GROUP BY p.plan_id").fetchall()
    conn.close()
    return {"sorgu": query, "parti başına çalıştırma": runs,
            "planlar (plan_id, çalıştırma, ort ms)": [(r[0], r[1], round(float(r[2] or 0), 2)) for r in rows]}


@pytest.fixture(scope="module", autouse=True)
def monitor_login():
    prepare_monitor_login()


async def test_plan_regression_is_measured_with_the_read_only_login():
    await asyncio.to_thread(_prepare_database, query_store=True)
    produced = await asyncio.to_thread(_produce_regression)
    report = await build_report(_instance(QS_DATABASE), hours=24)

    log("Query Store durumu", report.state)
    log("üretilen iş yükü", produced)
    log("plan geçmişi", {"plan kaydı": report.plans, "karşılaştırılabilir sorgu": report.queries_with_history,
                         "regresyon": [(r.query_id, r.slowdown_factor, round(r.baseline.avg_duration_ms, 1),
                                        round(r.current.avg_duration_ms, 1)) for r in report.regressions]})
    log("gerekçe", report.unavailable_reason)

    assert report.state == "read_write"
    assert report.unavailable_reason is None, report.unavailable_reason
    assert report.plans >= 2 and report.queries_with_history >= 1
    assert report.regressions, "yapılandırma değişince plan yavaşladı; regresyon görülmeliydi"
    worst = report.regressions[0]
    assert worst.slowdown_factor >= 1.5
    assert worst.current.avg_duration_ms > worst.baseline.avg_duration_ms
    assert worst.current.plan_id != worst.baseline.plan_id
    assert "orders" in worst.query_text.lower()


async def test_query_store_off_says_not_measured_with_the_enable_command():
    await asyncio.to_thread(_prepare_database, query_store=False)
    report = await build_report(_instance(QS_DATABASE), hours=24)
    log("Query Store kapalı", {"durum": report.state, "tür": report.unavailable_kind,
                               "gerekçe": (report.unavailable_reason or "")[:160],
                               "gereken ayar": (report.required_setting or "").splitlines()[:1]})
    assert report.unavailable_kind == KIND_DISABLED
    assert report.state in ("off", None)
    assert "ölçülemedi" in (report.unavailable_reason or "").lower()
    assert "SET QUERY_STORE = ON" in (report.required_setting or "")
    assert report.regressions == []


async def test_unauthorized_login_says_not_measured_with_the_grant():
    """Negatif kontrol: yetkisiz login'de boş liste değil, gereken GRANT komutu."""
    await asyncio.to_thread(_prepare_database, query_store=True)
    user, password = LOGINS["noperm"]
    conn = _connect("sa", SA_PASSWORD, QS_DATABASE)
    conn.cursor().execute(f"CREATE USER [{user}] FOR LOGIN [{user}]")  # veritabanına girebilsin, yetkisi olmasın
    conn.close()

    report = await build_report(_instance(QS_DATABASE, user=user, password=password), hours=24)
    log("yetkisiz login", {"tür": report.unavailable_kind, "gerekçe": (report.unavailable_reason or "")[:160],
                           "gereken yetki": report.required_setting})
    assert report.unavailable_kind == KIND_UNAUTHORIZED
    assert "GRANT VIEW DATABASE STATE" in (report.required_setting or "")
    assert report.regressions == []
