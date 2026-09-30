"""On-prem paketi uçtan uca: İNTERNETSİZ kurulum (Faz 31 Commit 8; Faz 31 Commit 10g'de yükseltme senaryosu kaldırıldı).

Ağı KAPALI (`--network none`) bir docker:dind konteynerinde koşuyor — bankadaki kapalı sunucunun karşılığı. İçeri
yalnızca sürüm paketi arşivi giriyor (`scripts/make-release-package.sh`); imajlar orada `docker build --network
none` ile derleniyor, izlenen hedef aynı konteynerde `postgres:16-alpine` (paketteki taban imaj). Hedefteki izleme
rolü PAKETİN rol SQL'iyle, DBA'nın yapacağı gibi `psql -v monitor_password=... -f` ile kuruluyor; veritabanında
TEMP/CREATE PUBLIC'ten alınmış.

Kurulum: api + worker (RUN_MODE=all) ayağa kalkıyor, bütün migration'lar uygulanıyor, şema modellerle birebir,
web arayüzü ve /api vekili çalışıyor, kısıtlı rolle eklenen hedeften bir toplama turu veri üretiyor.

Eski Docker paketinden YÜKSELTME testi kaldırıldı (Faz 31 Commit 10g): senaryo bankada hiç gerçekleşmeyecek bir
durumu sınıyordu — Faz 32'de on-prem kurulum Docker'sız (native) yola geçti, bankanın hiçbir zaman ESKİ bir Docker
kurulumu olmadı/olmayacak ki ondan yükseltilsin. Gerekçe ve bu kararın SORULAR.md'deki kaydı: "Eski Docker
paketinden yükseltme testi kaldırıldı" maddesi.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.live_onprem import ONPREM_SKIP_REASON, ONPREM_TARGETS

pytestmark = pytest.mark.skipif(not ONPREM_TARGETS, reason=ONPREM_SKIP_REASON)

ROOT = Path(__file__).resolve().parents[2]
ONPREM = ROOT / "deploy" / "onprem"
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
DIND_IMAGE = "dbace-test-dind:27"
VERSION = "e2e"
PACKAGE = f"dbace-onprem-{VERSION}"
DB_PASSWORD, MASTER_KEY = "Db_pw_e2e_2026", "k" * 44
JWT_SECRET, ADMIN_PASSWORD, NEW_ADMIN_PASSWORD = "j" * 64, "Ilk_admin_2026", "Yeni_admin_2026"
MONITOR_PASSWORD = "Mon_pw_e2e_2026"
#: Paket betikleri bash. Windows'ta PATH'teki `bash` WSL'inki olabiliyor (yollar /mnt/c): Git Bash tercih ediliyor.
_GIT_BASH = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git" / "bin" / "bash.exe"
BASH = os.environ.get("DBACE_TEST_BASH") or (str(_GIT_BASH) if os.name == "nt" and _GIT_BASH.exists() else "bash")


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


def run(args: list[str], *, timeout: int = 900, check: bool = True, input: bytes | None = None, env=None) -> str:
    out = subprocess.run(args, capture_output=True, timeout=timeout, input=input, env=env)
    text = out.stdout.decode("utf-8", "replace")
    if check and out.returncode != 0:
        raise AssertionError(f"KOMUT BAŞARISIZ ({out.returncode}): {' '.join(args)[:300]}\n{text[-3000:]}\n"
                             f"{out.stderr.decode('utf-8', 'replace')[-3000:]}")
    return text


def bash_path(path: Path) -> str:
    """Git Bash'te `C:/...` yolu tar'a uzak makine gibi görünüyor; Linux'ta değişmez."""
    posix = path.as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", posix)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else posix


@pytest.fixture(scope="module")
def dind_image():
    run(["docker", "build", "-t", DIND_IMAGE, "-"], input=b"FROM docker:27-dind\nRUN apk add --no-cache bash coreutils\n",
        timeout=900)
    return DIND_IMAGE


@pytest.fixture(scope="module")
def release_dir(tmp_path_factory):
    if not (ONPREM / "vendor" / "requirements.lock").is_file():
        pytest.fail("deploy/onprem/vendor boş — önce deploy/onprem/scripts/prepare-offline-artifacts.sh (internetli).")
    out = tmp_path_factory.mktemp("release")
    env = {**os.environ, "DBACE_SKIP_PREPARE": "1", "DBACE_RELEASE_DIR": bash_path(out), "MSYS_NO_PATHCONV": "1"}
    log("sürüm paketi", run([BASH, bash_path(ONPREM / "scripts" / "make-release-package.sh"), VERSION], env=env,
                             timeout=1800).strip().splitlines()[-1])
    (out / "driver.py").write_bytes((Path(__file__).parent / "onprem_driver.py").read_bytes())
    return out


@contextmanager
def dind(name: str, mount: Path, image: str):
    run(["docker", "rm", "-f", "-v", name], check=False)
    run(["docker", "run", "-d", "--privileged", "--network", "none", "--name", name,
         "-v", f"{mount.as_posix()}:/pkg", image])
    try:
        for _ in range(60):
            if subprocess.run(["docker", "exec", name, "docker", "info"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            raise AssertionError(run(["docker", "logs", name], check=False)[-2000:])
        yield name
    finally:
        run(["docker", "rm", "-f", "-v", name], check=False)


def dx(name: str, script: str, *, timeout: int = 900, check: bool = True) -> str:
    return run(["docker", "exec", name, "bash", "-euo", "pipefail", "-c", script], timeout=timeout, check=check)


def assert_offline(name: str) -> str:
    routes = dx(name, "ip route || true")
    reach = subprocess.run(["docker", "exec", name, "wget", "-q", "-T", "5", "-O", "/dev/null", "https://pypi.org/simple/"],
                           capture_output=True)
    assert "default" not in routes and reach.returncode != 0, "dind konteynerinin ağı kapalı olmalı"
    return f"route: {routes.strip() or 'yalnızca loopback'}; pypi.org erişimi: çıkış {reach.returncode}"


def install_new_package(name: str) -> str:
    env_file = "\n".join([
        f"DBACE_DB_PASSWORD={DB_PASSWORD}", f"CREDENTIALS_MASTER_KEY={MASTER_KEY}", f"JWT_SECRET={JWT_SECRET}",
        f"ADMIN_PASSWORD={ADMIN_PASSWORD}", "HTTP_PORT=8080", "COLLECT_INTERVAL_SECONDS=5",
    ])
    return dx(name, f"""
        rm -rf /work && mkdir -p /work && tar -xzf /pkg/{PACKAGE}.tar.gz -C /work
        cd /work/{PACKAGE}/deploy/onprem
        printf '%s\\n' '{env_file}' > .env
        ./scripts/install-offline.sh
    """, timeout=2400)


def start_target(name: str) -> None:
    """İzlenen PostgreSQL: TEMP/CREATE PUBLIC'ten alınmış veritabanı, rol PAKETİN SQL'iyle (psql değişkeni)."""
    sql_dir = f"/work/{PACKAGE}/deploy/onprem/sql"
    dx(name, f"""
        docker rm -f dbace-target >/dev/null 2>&1 || true
        docker run -d --name dbace-target --network dbace-onprem_dbace-internal -e POSTGRES_PASSWORD=target_pw \\
            postgres:16-alpine postgres -c shared_preload_libraries=pg_stat_statements
        for i in $(seq 1 60); do docker exec dbace-target pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
        sleep 2
        docker exec dbace-target psql -v ON_ERROR_STOP=1 -U postgres -c 'CREATE DATABASE appdb'
        docker exec dbace-target psql -v ON_ERROR_STOP=1 -U postgres -d appdb -c "
            REVOKE TEMPORARY, CREATE ON DATABASE appdb FROM PUBLIC; REVOKE CREATE ON SCHEMA public FROM PUBLIC;
            CREATE EXTENSION pg_stat_statements;
            CREATE TABLE orders (id int PRIMARY KEY, status text); INSERT INTO orders SELECT g, 's' || (g % 5) FROM generate_series(1, 1000) g;"
        sed -e 's/<izlenen_veritabanı>/appdb/g' -e 's/<şema>/public/g' {sql_dir}/postgresql-monitor-role.sql > /tmp/role.sql
        docker cp /tmp/role.sql dbace-target:/tmp/role.sql
        docker exec dbace-target psql -v ON_ERROR_STOP=1 -U postgres -d appdb -v monitor_password='{MONITOR_PASSWORD}' -f /tmp/role.sql
    """)


def _driver_raw(name: str, args, user: str, password: str, *, check: bool) -> str:
    dx(name, "docker cp /pkg/driver.py dbace-app:/tmp/driver.py")
    return dx(name, "docker exec -e PYTHONPATH=/app -e DBACE_USER={u} -e DBACE_PASS={p} -e DBACE_NEW_PASS={n} dbace-app "
                    "python /tmp/driver.py {a} 2>&1".format(u=user, p=password, n=NEW_ADMIN_PASSWORD, a=" ".join(args)),
              check=check)


def driver(name: str, *args: str, user: str = "admin", password: str = ADMIN_PASSWORD) -> dict:
    return json.loads(_driver_raw(name, args, user, password, check=True).strip().splitlines()[-1])


def driver_error(name: str, *args: str, user: str = "admin", password: str = ADMIN_PASSWORD) -> str:
    out = _driver_raw(name, args, user, password, check=False)
    assert "Traceback" in out or "→ 4" in out or "→ 5" in out, f"hata bekleniyordu, çıktı: {out[-500:]}"
    return out


def psql(name: str, sql: str) -> str:
    escaped = sql.replace('"', '\\"')
    return dx(name, f'docker exec dbace-db psql -U dbace -d dbace -tAc "{escaped}"').strip()


def wait_for_collection(name: str, instance_id: int, *, more_than: int = 0, timeout: int = 240) -> int:
    count = 0
    for _ in range(timeout // 3):
        count = int(psql(name, f"SELECT count(*) FROM metric_samples WHERE instance_id = {instance_id}") or 0)
        if count > more_than:
            return count
        time.sleep(3)
    raise AssertionError(f"toplama veri üretmedi (metric_samples={count}); log:\n" +
                         dx(name, "docker logs --tail 60 dbace-app 2>&1", check=False))


def live_schema(name: str) -> dict[str, set[str]]:
    raw = psql(name, "SELECT json_object_agg(table_name, cols) FROM (SELECT table_name, json_agg(column_name) AS cols "
                     "FROM information_schema.columns WHERE table_schema = 'public' GROUP BY table_name) x")
    return {table: set(cols) for table, cols in json.loads(raw).items()}


def model_schema() -> dict[str, set[str]]:
    from app.models import Base

    return {table.name: {column.name for column in table.columns} for table in Base.metadata.sorted_tables}


def schema_diff(left: dict[str, set[str]], right: dict[str, set[str]]) -> list[str]:
    diff = [f"yalnız kurulumda tablo: {t}" for t in sorted(set(left) - set(right))]
    diff += [f"yalnız modelde tablo: {t}" for t in sorted(set(right) - set(left))]
    for table in sorted(set(left) & set(right)):
        diff += [f"yalnız kurulumda kolon: {table}.{c}" for c in sorted(left[table] - right[table])]
        diff += [f"yalnız modelde kolon: {table}.{c}" for c in sorted(right[table] - left[table])]
    return diff




IDENTITY_MIGRATION = "20260918090000_slow_query_sample_identity.sql"
INDEX_MIGRATION = "20250719140000_query_history_index.sql"


def exercise_runner_at_scale(name: str, instance_id: int, rows: int = 60_000) -> dict:
    """Paketin KENDİ uygulayıcısı (`python -m app.migrations_runner`, dbace-app konteynerinde) gerçek veritabanında, gerçek
    veriyle (Faz 31 Commit 10b): #53'ü sıfırlayıp `rows` satırlık tabloda yeniden çalıştırır — parçalı backfill ve
    CONCURRENTLY, uygulama (worker) çalışıp yazmaya DEVAM ederken. Negatif kontrol: aynı CONCURRENTLY dosyası SQL Editor'ün
    yaptığı gibi tek işleme sarılınca (BEGIN…COMMIT) PostgreSQL'in reddettiği hata.
    """
    psql(name, f"INSERT INTO slow_query_samples (instance_id, collected_at, queryid, query, calls, total_time_ms, mean_time_ms, rows) "
               f"SELECT {instance_id}, now() - (g || ' seconds')::interval, (g % 200)::text, "
               f"'SELECT   a,b FROM t_' || (g % 200) || ' WHERE x = 1', 1, 1, 1, 1 FROM generate_series(1, {rows}) g")
    # #53'ün sonucunu sıfırla: eski kurulumdaki "migration henüz uygulanmamış" hâli.
    psql(name, "DROP INDEX IF EXISTS ix_slow_query_samples_instance_hash")
    psql(name, "UPDATE slow_query_samples SET query_hash = NULL")
    psql(name, f"DELETE FROM dbace_meta.applied_migrations WHERE version = '{IDENTITY_MIGRATION}'")
    total = int(psql(name, "SELECT count(*) FROM slow_query_samples"))

    # NEGATİF KONTROL: SQL Editor gibi tek işlem — CONCURRENTLY reddediliyor.
    editor = dx(name, "docker exec dbace-app cat /app/migrations/" + INDEX_MIGRATION + " | "
                "docker exec -i dbace-db sh -c 'psql -U dbace -d dbace -v ON_ERROR_STOP=1 -c \"BEGIN; $(cat); COMMIT;\"' 2>&1",
                check=False)

    started = time.monotonic()
    output = dx(name, "docker exec dbace-app python -m app.migrations_runner /app/migrations 2>&1")
    elapsed = time.monotonic() - started
    null_hashes = int(psql(name, "SELECT count(*) FROM slow_query_samples WHERE query_hash IS NULL"))
    invalid = psql(name, "SELECT count(*) FROM pg_index WHERE NOT indisvalid")
    index_valid = psql(name, "SELECT indisvalid AND indisready FROM pg_index WHERE indexrelid = to_regclass('ix_slow_query_samples_instance_hash')")
    recorded = int(psql(name, "SELECT count(*) FROM dbace_meta.applied_migrations"))
    return {"rows": total, "seconds": round(elapsed, 1), "output": output, "editor": editor, "null_hashes": null_hashes,
            "invalid_indexes": invalid, "index_valid": index_valid, "recorded": recorded}


# --- Kurulum ----------------------------------------------------------------------------------------


def test_offline_install_runs_api_worker_and_collects_with_the_restricted_role(dind_image, release_dir):
    with dind("dbace-onprem-install", release_dir, dind_image) as name:
        log("ağ", assert_offline(name))
        installed = install_new_package(name)
        log("kurulum", installed.strip().splitlines()[-2:])
        start_target(name)
        setup = driver(name, "setup", "dbace-target", "appdb", "dbace_monitor", MONITOR_PASSWORD)
        instance_id = setup["instance_id"]
        samples = wait_for_collection(name, instance_id)
        state = driver(name, "read", str(instance_id), password=NEW_ADMIN_PASSWORD)
        migrations = int(psql(name, "SELECT count(*) FROM dbace_meta.applied_migrations"))
        diff = schema_diff(live_schema(name), model_schema())
        web_index = dx(name, "wget -qO- http://127.0.0.1:8080/")
        web_api = dx(name, "wget -qO- http://127.0.0.1:8080/api/health")
        containers = dx(name, "docker ps --format '{{.Names}} {{.Status}}'")
        app_log = dx(name, "docker logs dbace-app 2>&1 | grep -E '^migration:|Uvicorn running|scheduler|Scheduler' | head -5",
                     check=False)
        scale = exercise_runner_at_scale(name, instance_id)
        grants = dx(name, "docker exec dbace-target psql -U postgres -d appdb -tAc \"SELECT rolsuper, "
                          "pg_has_role('dbace_monitor', 'pg_monitor', 'USAGE'), has_database_privilege('dbace_monitor', 'appdb', 'TEMP'), "
                          "pg_has_role('dbace_monitor', 'pg_read_server_files', 'USAGE') FROM pg_roles WHERE rolname = 'dbace_monitor'\"")

    log("konteynerler", containers.strip().splitlines())
    log("uygulama log'u", app_log.strip().splitlines())
    log("hedef rol (süper/pg_monitor/TEMP/pg_read_server_files)", grants.strip())
    log("toplama", {"metric_samples": samples, **{k: state[k] for k in ("last_collect_ok_at", "last_collect_error")},
                    "topoloji": state["topology"] and state["topology"]["kind"]})
    log("migration / şema", {"uygulanan": migrations, "dosya": len(MIGRATIONS), "şema farkı": diff or "yok"})
    log("web", {"index": "<div id=\"root\">" in web_index, "api/health": web_api.strip()})
    log("uygulayıcı, gerçek kurulumda (%d satır)" % scale["rows"], {
        "süre (sn)": scale["seconds"], "boş parmak izi": scale["null_hashes"], "geçersiz index": scale["invalid_indexes"],
        "index geçerli": scale["index_valid"], "kayıtlı migration": scale["recorded"]})
    log("uygulayıcı çıktısı (parçalı ifade)", [l for l in scale["output"].splitlines() if "parça" in l or "uygulanıyor" in l][-4:])
    log("SQL Editor gibi tek işlem (negatif kontrol)", next((l for l in scale["editor"].splitlines() if "ERROR" in l), scale["editor"])[:200])

    assert grants.strip() == "f|t|f|f"
    # Paketin uygulayıcısı CONCURRENTLY'yi ve parçalı ifadeyi işlem DIŞINDA çalıştırabiliyor (Faz 31 Commit 10b).
    assert scale["rows"] >= 60_000 and scale["null_hashes"] == 0 and scale["invalid_indexes"] == "0"
    assert scale["index_valid"] == "t" and scale["recorded"] == len(MIGRATIONS)
    # Log biçimi ve bu regex AYNI kaynaktan (`CHUNK_PROGRESS_LOG_RE`, migrations_runner.py) besleniyor —
    # Faz 31 Commit 10g: elle yazılmış bir regex log biçimi değiştiğinde sessizce ayrışıyordu.
    from app.migrations_runner import CHUNK_PROGRESS_LOG_RE

    progress_lines = list(CHUNK_PROGRESS_LOG_RE.finditer(scale["output"]))
    assert progress_lines, scale["output"][-800:]
    assert int(progress_lines[-1].group("chunk_no")) >= 3, "60 bin satır ≥ 3 parça (20 bin)"
    assert "cannot run inside a transaction block" in scale["editor"], "aynı dosya tek işlemde REDDEDİLMELİ (negatif kontrol)"
    assert migrations == len(MIGRATIONS)
    assert diff == []
    assert all(c in containers for c in ("dbace-db", "dbace-app", "dbace-web"))
    assert "(healthy)" in next(line for line in containers.splitlines() if line.startswith("dbace-app"))
    assert state["last_collect_error"] is None and state["last_collect_ok_at"]
    assert state["topology"]["kind"] == "standalone"
    assert state["deadlock_detail_reason"] and "host-agent" in state["deadlock_detail_reason"]
    assert '<div id="root">' in web_index and json.loads(web_api)["status"] == "ok"
