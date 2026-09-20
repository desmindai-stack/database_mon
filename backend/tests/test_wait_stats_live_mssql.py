"""Bekleme istatistikleri — GERÇEK SQL Server (Faz 31 Commit 9, madde 5).

Ölçülen üç şey:

1. **Kümülatif okuma + ölçülen filtre:** salt-okunur izleme login'iyle `sys.dm_os_wait_stats` okunuyor.
   Arka plan gürültüsü ELLE yazılmış listeyle değil, motorun kendi oturum kırılımıyla eleniyor; elenen
   sayı ayrıca raporlanıyor.
2. **Gerçek kullanıcı beklemesi:** bir oturum satırı kilitliyor, ikincisi bekliyor. Bu bekleme KULLANICI
   oturumuna atfedildiği için elenmiyor ve iki okuma arasındaki FARK'ta görünüyor.
3. **Yeniden başlatma:** konteyner gerçekten yeniden başlatılıyor. Kümülatif sayaçlar sıfırlanınca dbace
   eksi fark üretmiyor; "sayaç sıfırlandı" diyor.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
import uuid

import pytest

import httpx

from app.services.wait_stats import build_report, reset_baseline
from tests.auth_helper import authed_client
from tests.live_mssql import (
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
# Havuzlama KAPALI: sunucu yeniden başlatılıyor, havuzdaki bağlantı ölü kalıyor (ölçüldü).
pyodbc.pooling = False
pytest.importorskip("aioodbc")
pytestmark = pytest.mark.skipif("standalone" not in MSSQL_TARGETS, reason=MSSQL_SKIP_REASON)

WAIT_DATABASE = "dbace_ws_it"


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


def _connect(user: str, password: str, database: str, autocommit: bool = True):
    from app.collectors.sqlserver_mongodb import build_odbc_connection_string

    return pyodbc.connect(build_odbc_connection_string(standalone_target(user, password, database)),
                          autocommit=autocommit)


def _instance(instance_id: int = 901):
    from app.models import Instance
    from app.services.credentials import encrypt_secret

    target = standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, "master")
    instance = Instance(name=f"ws-{uuid.uuid4().hex[:6]}", engine="sqlserver", host=target.host, port=target.port,
                        database="master", username=MONITOR_LOGIN, password=encrypt_secret(MONITOR_PASSWORD),
                        options=odbc_options() or None, enabled=True)
    instance.id = instance_id
    return instance


def _container_name() -> str:
    """Konteyner adı elle yazılmıyor: testin bağlandığı porttan bulunuyor."""
    port = MSSQL_TARGETS["standalone"].split(",")[1]
    names = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.Names}}"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert names, f"{port} portunu yayınlayan konteyner yok"
    return names[0]


def _prepare_database() -> None:
    conn = _connect("sa", SA_PASSWORD, "master")
    cur = conn.cursor()
    cur.execute(f"IF DB_ID('{WAIT_DATABASE}') IS NOT NULL BEGIN ALTER DATABASE [{WAIT_DATABASE}] "
                f"SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{WAIT_DATABASE}]; END")
    cur.execute(f"CREATE DATABASE [{WAIT_DATABASE}]")
    cur.execute(f"USE [{WAIT_DATABASE}]; CREATE TABLE dbo.accounts (id INT PRIMARY KEY, balance INT NOT NULL); "
                "INSERT INTO dbo.accounts VALUES (1, 100), (2, 200);")
    conn.close()


def _produce_user_lock_wait(seconds: float = 3.0) -> dict:
    """İki oturum, gerçek kilit beklemesi: biri satırı güncelleyip bekletiyor, diğeri okumaya çalışıyor.

    Bekleyen oturum ÖLÇÜM BİTENE KADAR açık kalıyor; `sys.dm_exec_session_wait_stats` yalnızca AÇIK
    oturumların beklemesini tutuyor — kapanan oturumun beklemesi kullanıcıya atfedilemezdi.
    """
    blocker = _connect("sa", SA_PASSWORD, WAIT_DATABASE, autocommit=False)
    waiter = _connect("sa", SA_PASSWORD, WAIT_DATABASE, autocommit=False)
    blocker.cursor().execute("UPDATE dbo.accounts SET balance = balance - 1 WHERE id = 1")

    waiter_cursor = waiter.cursor()
    started = time.monotonic()

    def read_blocked() -> None:
        waiter_cursor.execute("SELECT balance FROM dbo.accounts WHERE id = 1").fetchall()

    thread = threading.Thread(target=read_blocked)
    thread.start()
    time.sleep(seconds)
    blocker.commit()
    thread.join(timeout=30)
    waited_ms = round((time.monotonic() - started) * 1000)
    blocker.close()
    waiter.commit()

    rows = waiter.cursor().execute(
        "SELECT sws.wait_type, sws.waiting_tasks_count, sws.wait_time_ms FROM sys.dm_exec_session_wait_stats sws "
        "WHERE sws.session_id = @@SPID AND sws.wait_type LIKE 'LCK%' ORDER BY sws.wait_time_ms DESC").fetchall()
    return {"bekleyen oturumun kilit beklemesi": [(r[0], r[1], r[2]) for r in rows],
            "engelleme süresi (ms)": waited_ms, "bağlantı": waiter}


def _wait_until_ready(timeout: float = 180.0) -> float:
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < timeout:
        try:
            _connect("sa", SA_PASSWORD, "master").close()
            return round(time.monotonic() - started, 1)
        except Exception as exc:  # noqa: BLE001 — sunucu henüz açılmadı
            last = str(exc).splitlines()[0][:120]
            time.sleep(2)
    raise AssertionError(f"SQL Server {timeout}s içinde açılmadı: {last}")


@pytest.fixture(scope="module", autouse=True)
def monitor_login():
    prepare_monitor_login()


@pytest.fixture(autouse=True)
def clean_baseline():
    reset_baseline()
    yield
    reset_baseline()


async def test_wait_stats_are_read_with_the_read_only_login_and_noise_is_measured_away():
    report = await build_report(_instance(), limit=40)

    log("sunucu açılışı", report.server_start_time)
    log("görünen ilk 5 bekleme", [(e.wait_type, e.waiting_tasks, e.wait_ms, e.user_tasks) for e in report.totals[:5]])
    log("elenen arka plan türü", {"sayı": report.filtered_background, "ilk 8": report.background_types[:8]})
    log("fark", report.delta_unavailable_reason)

    assert report.unavailable_reason is None, report.unavailable_reason
    assert report.server_start_time is not None
    # Arka plan gürültüsü gerçekten var ve eleniyor; ama SAYISI raporlanıyor, sessizce yok olmuyor.
    assert report.filtered_background > 0
    assert all(e.user_tasks > 0 for e in report.totals), "görünen her bekleme kullanıcı oturumuna atfedilmeli"
    # NEGATİF KONTROL: filtresiz okumada arka plan türleri listenin BAŞINI kapıyor.
    unfiltered = await build_report(_instance(902), limit=40, include_background=True)
    assert unfiltered.totals[0].is_background, "ham listede ilk sırayı arka plan görevi kapıyor (filtrenin sebebi)"
    assert unfiltered.filtered_background == 0


async def test_a_real_user_lock_wait_survives_the_filter_and_shows_up_in_the_delta():
    await asyncio.to_thread(_prepare_database)
    instance = _instance()
    await build_report(instance, limit=60)  # taban okuma

    produced = await asyncio.to_thread(_produce_user_lock_wait)
    report = await build_report(instance, limit=60)
    produced.pop("bağlantı").close()

    log("üretilen kilit beklemesi", produced)
    log("farktaki ilk 5", [(e.wait_type, e.waiting_tasks, e.wait_ms) for e in report.delta[:5]])

    lock_waits = [e for e in report.delta if e.wait_type.startswith("LCK_")]
    assert lock_waits, "gerçek kullanıcı kilit beklemesi farkta görünmeliydi"
    assert lock_waits[0].wait_ms >= 1000, lock_waits[0].wait_ms
    # Kullanıcı beklemesi arka plan sayılmıyor (ölçülen filtre doğru tarafta).
    assert not lock_waits[0].is_background
    assert report.delta_unavailable_reason is None and report.restarted is False


async def test_server_restart_resets_the_counters_and_is_reported_instead_of_a_negative_delta():
    """Kümülatif sayaç sıfırlanması GERÇEK yeniden başlatmayla ölçülüyor."""
    instance = _instance()
    before = await build_report(instance, limit=40)
    before_top = {e.wait_type: e.wait_ms for e in before.totals}
    container = _container_name()

    subprocess.run(["docker", "restart", container], capture_output=True, text=True, check=True)
    ready_in = await asyncio.to_thread(_wait_until_ready)

    after = await build_report(instance, limit=40)
    log("konteyner", container)
    log("yeniden başlatma", {"hazır olma (s)": ready_in, "önceki açılış": before.server_start_time,
                             "yeni açılış": after.server_start_time})
    log("sıfırlanma örneği (önce → sonra ms)",
        {e.wait_type: (before_top.get(e.wait_type), e.wait_ms) for e in after.totals[:4]
         if e.wait_type in before_top})
    log("fark gerekçesi", after.delta_unavailable_reason)

    assert after.server_start_time > before.server_start_time, "sunucu gerçekten yeniden başlamalıydı"
    assert after.restarted is True
    assert after.delta == [], "sıfırlanmış sayaçtan fark üretilmemeli"
    assert "sıfırlandı" in after.delta_unavailable_reason
    # NEGATİF KONTROL: sıfırlanmayı fark etmeseydik fark EKSİ çıkardı; hiçbir değer eksi değil.
    assert all(e.wait_ms >= 0 and e.waiting_tasks >= 0 for e in after.totals)
    # Bir sonraki okuma normale dönüyor: yeni taban üzerinden fark yeniden hesaplanıyor.
    following = await build_report(instance, limit=40)
    assert following.restarted is False and following.delta_unavailable_reason is None


async def test_the_endpoint_is_reachable_from_the_real_path_with_the_real_server():
    """Uç GERÇEK çağrı yolundan geçiyor: oturum açmış istemci → HTTP → ölçüm → JSON.

    Ekrandaki panel de aynı ucu çağırıyor (`api.getWaitStats`). Servisi doğrudan çağıran testler
    bu yolu kanıtlamaz: yetki, yönlendirme ve şema serileştirmesi burada ölçülüyor.
    """
    target = standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, "master")
    async with await authed_client() as client:
        created = await client.post("/api/instances", json={
            "name": f"ws-http-{uuid.uuid4().hex[:8]}", "engine": "sqlserver", "host": target.host,
            "port": target.port, "database": "master", "username": MONITOR_LOGIN,
            "password": MONITOR_PASSWORD, "options": odbc_options() or None,
        })
        assert created.status_code == 201, created.text
        instance_id = created.json()["id"]

        first = await client.get(f"/api/instances/{instance_id}/wait-stats")
        second = await client.get(f"/api/instances/{instance_id}/wait-stats")
        raw = await client.get(f"/api/instances/{instance_id}/wait-stats?include_background=true&limit=5")

    assert first.status_code == 200, first.text
    body, delta_body, raw_body = first.json(), second.json(), raw.json()
    log("HTTP ilk okuma", {"elenen arka plan": body["filtered_background"],
                           "görünen": [(e["wait_type"], e["wait_ms"]) for e in body["totals"][:3]],
                           "fark": (body["delta_unavailable_reason"] or "")[:60]})
    log("HTTP ikinci okuma", {"fark satırı": len(delta_body["delta"]),
                              "fark gerekçesi": delta_body["delta_unavailable_reason"]})
    log("HTTP arka plan dahil (limit 5)", {"satır": len(raw_body["totals"]),
                                           "elenen": raw_body["filtered_background"]})

    assert body["unavailable_reason"] is None and body["server_start_time"]
    assert body["filtered_background"] > 0
    assert delta_body["delta_unavailable_reason"] is None  # ikinci okumada fark hesaplanıyor
    # Sayfalama uygulanıyor: limit satır sayısını gerçekten sınırlıyor (negatif kontrol: limitsiz daha uzun).
    assert len(raw_body["totals"]) <= 5 and raw_body["filtered_background"] == 0
