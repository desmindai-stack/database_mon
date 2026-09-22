"""Bekleme istatistiği farkı: çok süreçte ve yeniden başlatmada ne oluyor? (Faz 31 Commit 10c-A)

GERÇEK `uvicorn app.main:app` süreçleri (her biri kendi portunda, tek işçi), gerçek SQL Server (kısıtlı `dbace_monitor`
login'i), gerçek HTTP. İstekler süreçlere DÖNÜŞÜMLÜ gider: yük dengeleyicili çoğaltılmış servisin (Railway `numReplicas > 1`)
ya da `WEB_CONCURRENCY > 1`'in yaptığı tam olarak bu. Yanıttaki `process_id` alanı isteği işleyen süreci gösterir.

    python scripts/waitstats_multiprocess_demo.py --processes 2 --baseline process
    python scripts/waitstats_multiprocess_demo.py --processes 2 --baseline shared

Canlı yapılandırma (railway.toml): `uvicorn app.main:app` — `--workers` YOK, tek süreç. Ama uvicorn `WEB_CONCURRENCY`
ortam değişkenini okuyor: bir platform/kullanıcı onu ayarlarsa süreç sayısı sessizce değişir. Bu betik o durumu üretir.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

PORT = 8765
PASSWORD = "demo-pass-not-for-prod-1"


def env_for(db_path: Path, baseline: str, workers: int) -> dict:
    return {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path.as_posix()}", "RUN_MODE": "api",
            "WAIT_STATS_BASELINE": baseline, "LOG_LEVEL": "INFO",
            "PYTHONIOENCODING": "utf-8", "WAIT_SAMPLING_ENABLED": "false",
            "DBACE_TEST_MSSQL_STANDALONE": os.environ.get("DBACE_TEST_MSSQL_STANDALONE", "127.0.0.1,14333"),
            "DBACE_TEST_MSSQL_ODBC_DRIVER": os.environ.get("DBACE_TEST_MSSQL_ODBC_DRIVER", "SQL Server")}


async def prepare(db_path: Path) -> int:
    from app.database import SessionLocal, init_db
    from app.models import Instance, User
    from app.services.credentials import encrypt_secret
    from app.services.security import hash_password
    from tests.live_mssql import APP_DATABASE, MONITOR_LOGIN, MONITOR_PASSWORD, odbc_options, standalone_target

    await init_db()
    target = standalone_target(MONITOR_LOGIN, MONITOR_PASSWORD, APP_DATABASE)
    async with SessionLocal() as session:
        session.add(User(username="demo", password_hash=hash_password(PASSWORD), role="admin", is_active=True,
                         must_change_password=False))
        row = Instance(name="demo-mssql", engine="sqlserver", host=target.host, port=target.port, database=target.database,
                       username=target.username, password=encrypt_secret(target.password), options=odbc_options() or None,
                       enabled=True)
        session.add(row)
        await session.commit()
        return row.id


def _detached() -> dict:
    """Windows: sunucu süreçleri konsolu paylaşıyor; kapatırken gelen Ctrl-C bu betiği de öldürüyordu."""
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}


def start_servers(db_path: Path, baseline: str, processes: int, log_path: Path) -> list[subprocess.Popen]:
    """Süreçler SIRAYLA başlatılıp her biri sağlıklı olana dek beklenir.

    Eşzamanlı başlatma, açılışta HER sürecin çalıştırdığı `ensure_default_admin()`in "admin" kullanıcısını
    var mı diye kontrol edip yoksa eklemesi arasında yarış oluşturuyordu (fresh DB'de iki süreç aynı anda
    INSERT deniyor, biri UNIQUE constraint ile çöküyordu) — bu betiğin ürettiği bir durum, ürün hatası değil
    (gerçek dağıtımda replikalar aynı milisaniyede değil, birbiri ardına açılır). Sırayla başlatmak bunu ortadan
    kaldırıyor: ilk süreç admin'i oluşturup sağlıklı olduktan SONRA ikinci süreç başlıyor.
    """
    import httpx

    servers = []
    for index in range(processes):
        log = open(log_path, "ab")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT + index)],
            cwd=BACKEND, env=env_for(db_path, baseline, 1), stdout=log, stderr=subprocess.STDOUT, **_detached())
        servers.append(proc)
        for _ in range(120):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT + index}/api/health", timeout=2).status_code == 200:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            raise SystemExit(f"sunucu açılmadı (port {PORT + index})")
    return servers


def stop_servers(servers: list[subprocess.Popen]) -> None:
    for proc in servers:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True) if os.name == "nt" else proc.terminate()
        proc.wait(timeout=20)


def _one_read(instance_id: int, port: int) -> dict:
    import httpx

    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
        token = client.post("/api/auth/login", json={"username": "demo", "password": PASSWORD}).json()["access_token"]
        return client.get(f"/api/instances/{instance_id}/wait-stats", headers={"Authorization": f"Bearer {token}"}).json()


def request_reads(instance_id: int, count: int, processes: int, gap: float) -> list[dict]:
    """Ardışık istekler süreçlere DÖNÜŞÜMLÜ (0,1,0,1…)."""
    out = []
    for i in range(count):
        out.append(_one_read(instance_id, PORT + i % processes))
        time.sleep(gap)
    return out


def summarize(label: str, reads: list[dict]) -> None:
    print(f"{chr(10)}### {label}")
    print("| # | süreç (pid) | fark var mı | pencere | gerekçe / not |")
    print("|---|---|---|---|---|")
    for index, body in enumerate(reads, 1):
        has = body.get("delta_unavailable_reason") is None
        window = body.get("delta_window_seconds")
        note = (body.get("delta_unavailable_reason") or body.get("delta_note") or "")[:80]
        print(f"| {index} | {body.get('process_id')} | {'EVET' if has else 'hayır'} | {'—' if window is None else f'{window:.1f} sn'} | {note} |")
    pids = {b.get("process_id") for b in reads}
    without = sum(1 for b in reads if b.get("delta_unavailable_reason") is not None)
    print(f"{chr(10)}farklı süreç: {len(pids)} ({sorted(pids)}); farksız (gerekçeli) okuma: {without}/{len(reads)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--baseline", choices=["process", "shared"], required=True)
    parser.add_argument("--requests", type=int, default=6)
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="waitstats-demo-"))
    db_path, log_path = workdir / "meta.db", workdir / "server.log"
    os.environ.update(env_for(db_path, args.baseline, 1))
    instance_id = asyncio.run(prepare(db_path))

    servers = start_servers(db_path, args.baseline, args.processes, log_path)
    try:
        reads = request_reads(instance_id, args.requests, args.processes, gap=1.2)
    finally:
        stop_servers(servers)
    summarize(f"{args.processes} süreç, taban={args.baseline}: {args.requests} ardışık istek, süreçlere dönüşümlü", reads)

    # Yeniden başlatma: AYNI meta veritabanı, yeni süreçler.
    servers = start_servers(db_path, args.baseline, args.processes, log_path)
    try:
        after = request_reads(instance_id, args.processes, args.processes, gap=1.2)
    finally:
        stop_servers(servers)
    summarize(f"YENİDEN BAŞLATMA sonrası ilk okumalar (taban={args.baseline})", after)


if __name__ == "__main__":
    main()
