"""Canlı PostgreSQL test konteynerlerini SIFIRDAN ve TEKRARLANABİLİR biçimde kurar (Faz 31 Commit 4).

NEDEN VAR: Faz 29–31 boyunca canlı testler elle kurulmuş konteynerlere karşı koştu. Elle
kurulum sürümden sürüme farklıydı (17'de `pg_stat_statements.track=all`, 15'te varsayılan;
hypopg yalnızca 17'ye elle yüklenmişti) ve bir testin geçmesi konteynerin geçmişine bağlıydı.
Bu betik her sürümü aynı ayarlarla kuruyor; test verisi ve roller ise
`tests/live_pg.py::prepare_live_database` ile testlerin kendisi tarafından idempotent kuruluyor.

Kullanım (backend/ içinden):

    python scripts/live_pg.py up                 # 15, 16, 17 — varsa dokunmaz, eksiği tamamlar
    python scripts/live_pg.py up --recreate      # konteynerleri SİLİP sıfırdan kurar
    python scripts/live_pg.py dsn                # DBACE_TEST_PG_DSN değerini yazar

Kurulan her şey:
- `postgres:<sürüm>` resmî imajı, `shared_preload_libraries=pg_stat_statements`,
  `track_activity_query_size=2048`, `compute_query_id=on`, `logging_collector=on`. `pg_stat_statements.track`
  VARSAYILANDA (`top`) bırakılıyor — bankadaki gerçekçi kurulum; `all` gerektiren ölçüm testleri
  kendi rolleri için oturum düzeyinde açıyor.
- hypopg: resmî imajın zaten tanımlı PGDG deposundan `postgresql-<sürüm>-hypopg` paketi.
- `dbace` veritabanında `pg_stat_statements` ve `hypopg` eklentileri.
- `dbace_nohypopg` veritabanı: yalnızca `pg_stat_statements` — hypopg'siz yolun testi için
  (eklenti veritabanı başına kurulur; aynı sunucuda iki durum da sınanabiliyor).
- Faz 31 Commit 6: her sürüme bir STREAMING REPLİKA (`dbace-pg<sürüm>-replica`), ortak
  `dbace-live` ağında, `pg_basebackup -R` ile. Topoloji tespiti testi replikayı SQL'le koparıp
  (`ALTER SYSTEM SET primary_conninfo = ''`) geri bağlıyor — docker CLI gerekmiyor. `up`
  replikanın akışta olduğunu doğruluyor, kopuk kalmışsa bağlantıyı geri yazıyor.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

PASSWORD = "dbace"
VERSIONS = {15: 55433, 16: 55434, 17: 55432, 18: 55435}
REPLICA_PORTS = {15: 55443, 16: 55444, 17: 55442, 18: 55445}
NETWORK = "dbace-live"
REPLICA_APPLICATION_NAME = "dbace_replica"
DEFAULT_VERSIONS = (15, 16, 17)
SERVER_ARGS = [
    "-c", "shared_preload_libraries=pg_stat_statements",
    "-c", "track_activity_query_size=2048",
    "-c", "compute_query_id=on",
    # Faz 31 Commit 5: deadlock testi sunucu log'unu SQL'le okuyor (pg_read_file). Deadlock'taki
    # sorgu metni istemciye gönderilmiyor, yalnızca log'a yazılıyor.
    "-c", "logging_collector=on",
]
#: Var olan konteynerde de doğrulanan ayarlar: (ayar, beklenen, yalnızca yeniden başlatmayla mı).
REQUIRED_SETTINGS = [
    ("shared_preload_libraries", "pg_stat_statements", True),
    ("track_activity_query_size", "2048", True),
    ("compute_query_id", "on", False),
    ("logging_collector", "on", True),
]


def run(*args: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        sys.exit(f"KOMUT BAŞARISIZ: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    if not quiet and result.stdout.strip():
        print(result.stdout.strip())
    return result


def name(version: int) -> str:
    return f"dbace-pg{version}"


def psql(version: int, sql: str, database: str = "dbace") -> str:
    return run("docker", "exec", name(version), "psql", "-U", "postgres", "-d", database, "-v", "ON_ERROR_STOP=1",
               "-tAc", sql, quiet=True).stdout.strip()


def exists(version: int) -> bool:
    out = run("docker", "ps", "-a", "--filter", f"name=^{name(version)}$", "--format", "{{.Names}}", quiet=True)
    return out.stdout.strip() == name(version)


def wait_ready(version: int, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = run("docker", "exec", name(version), "psql", "-U", "postgres", "-d", "dbace", "-tAc", "SELECT 1",
                    check=False, quiet=True)
        if probe.returncode == 0 and probe.stdout.strip() == "1":
            return
        time.sleep(1)
    sys.exit(f"{name(version)} {timeout:.0f} sn içinde hazır olmadı")


def up(version: int, recreate: bool) -> None:
    container = name(version)
    if recreate and exists(version):
        print(f"[{container}] siliniyor")
        run("docker", "rm", "-f", container, quiet=True)
    if not exists(version):
        print(f"[{container}] oluşturuluyor (postgres:{version}, port {VERSIONS[version]})")
        run("docker", "run", "-d", "--name", container, "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-e", "POSTGRES_DB=dbace", "-p", f"{VERSIONS[version]}:5432", f"postgres:{version}", *SERVER_ARGS,
            quiet=True)
    else:
        run("docker", "start", container, quiet=True)
    wait_ready(version)

    # Eski ayarlarla kurulmuş konteyner: ALTER SYSTEM + yeniden başlatma (silmeden).
    drift = [(n, v, r) for n, v, r in REQUIRED_SETTINGS if psql(version, f"SELECT setting FROM pg_settings WHERE name = '{n}'") != v]
    if drift:
        for setting, value, _ in drift:
            print(f"[{container}] {setting} = {value} ayarlanıyor")
            psql(version, f"ALTER SYSTEM SET {setting} = '{value}'")
        run("docker", "restart", container, quiet=True)
        wait_ready(version)
        still = [n for n, v, _ in REQUIRED_SETTINGS if psql(version, f"SELECT setting FROM pg_settings WHERE name = '{n}'") != v]
        if still:
            sys.exit(f"{container}: ayarlar uygulanamadı: {still} — --recreate ile kurun")

    control = f"/usr/share/postgresql/{version}/extension/hypopg.control"
    if run("docker", "exec", container, "test", "-f", control, check=False, quiet=True).returncode != 0:
        print(f"[{container}] hypopg paketi kuruluyor")
        run("docker", "exec", "-u", "root", container, "sh", "-c",
            f"apt-get update -qq && apt-get install -y -qq postgresql-{version}-hypopg >/dev/null", quiet=True)

    psql(version, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    psql(version, "CREATE EXTENSION IF NOT EXISTS hypopg")
    if psql(version, "SELECT 1 FROM pg_database WHERE datname = 'dbace_nohypopg'") != "1":
        psql(version, "CREATE DATABASE dbace_nohypopg")
    psql(version, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements", database="dbace_nohypopg")

    extensions = psql(version, "SELECT string_agg(extname || ' ' || extversion, ', ' ORDER BY extname) FROM pg_extension")
    no_hypopg = psql(version, "SELECT count(*) FROM pg_extension WHERE extname = 'hypopg'", database="dbace_nohypopg")
    print(f"[{container}] {psql(version, 'SHOW server_version')} | dbace: {extensions} | "
          f"dbace_nohypopg hypopg sayısı: {no_hypopg}")


def replica_name(version: int) -> str:
    return f"{name(version)}-replica"


def primary_conninfo(version: int) -> str:
    return (f"host={name(version)} port=5432 user=postgres password={PASSWORD} "
            f"application_name={REPLICA_APPLICATION_NAME}")


def replica_psql(version: int, sql: str) -> str:
    result = run("docker", "exec", replica_name(version), "psql", "-U", "postgres", "-d", "dbace", "-tAc", sql,
                 check=False, quiet=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def up_replica(version: int, recreate: bool) -> None:
    """Birincile streaming replika. Testler replikayı SQL'le koparıp geri bağlıyor."""
    container = replica_name(version)
    if run("docker", "network", "inspect", NETWORK, check=False, quiet=True).returncode != 0:
        run("docker", "network", "create", NETWORK, quiet=True)
    connected = run("docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", name(version), quiet=True).stdout
    if f'"{NETWORK}"' not in connected:
        run("docker", "network", "connect", NETWORK, name(version), quiet=True)
    hba = "host replication all all scram-sha-256"
    if psql(version, "SELECT count(*) FROM pg_hba_file_rules WHERE 'replication' = ANY(database) AND address = 'all'") == "0":
        run("docker", "exec", name(version), "sh", "-c", f"echo '{hba}' >> \"$PGDATA/pg_hba.conf\"", quiet=True)
        psql(version, "SELECT pg_reload_conf()")

    replica_exists = run("docker", "ps", "-a", "--filter", f"name=^{container}$", "--format", "{{.Names}}",
                         quiet=True).stdout.strip() == container
    if recreate and replica_exists:
        run("docker", "rm", "-f", container, quiet=True)
        replica_exists = False
    if not replica_exists:
        print(f"[{container}] oluşturuluyor (port {REPLICA_PORTS[version]}, pg_basebackup)")
        boot = (
            'if [ ! -s "$PGDATA/PG_VERSION" ]; then '
            'mkdir -p "$PGDATA" && chown postgres:postgres "$PGDATA" && chmod 700 "$PGDATA" && '
            f'gosu postgres env PGPASSWORD={PASSWORD} pg_basebackup -h {name(version)} -U postgres '
            f'-D "$PGDATA" -X stream -R -d "application_name={REPLICA_APPLICATION_NAME}"; fi; '
            'exec docker-entrypoint.sh postgres ' + " ".join(SERVER_ARGS)
        )
        run("docker", "run", "-d", "--name", container, "--network", NETWORK, "-e", f"POSTGRES_PASSWORD={PASSWORD}",
            "-p", f"{REPLICA_PORTS[version]}:5432", "--entrypoint", "bash", f"postgres:{version}", "-c", boot, quiet=True)
    else:
        run("docker", "start", container, quiet=True)

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and replica_psql(version, "SELECT pg_is_in_recovery()") != "t":
        time.sleep(1)
    if replica_psql(version, "SELECT pg_is_in_recovery()") != "t":
        sys.exit(f"{container} replika olarak açılmadı")
    # Önceki bir test koparıp geri bağlayamadıysa: bağlantıyı geri yaz.
    if replica_psql(version, "SELECT current_setting('primary_conninfo') <> ''") != "t":
        replica_psql(version, f"ALTER SYSTEM SET primary_conninfo = '{primary_conninfo(version)}'")
        replica_psql(version, "SELECT pg_reload_conf()")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and replica_psql(version, "SELECT status FROM pg_stat_wal_receiver") != "streaming":
        time.sleep(1)
    status = replica_psql(version, "SELECT status FROM pg_stat_wal_receiver")
    if status != "streaming":
        sys.exit(f"{container} akışta değil (wal receiver: {status or 'yok'})")
    print(f"[{container}] {replica_psql(version, 'SHOW server_version')} | recovery=t | wal receiver: {status}")


def dsn(versions) -> str:
    return ",".join(f"postgresql://postgres:{PASSWORD}@127.0.0.1:{VERSIONS[v]}/dbace" for v in versions)


def replica_dsn(versions) -> str:
    return ",".join(f"postgresql://postgres:{PASSWORD}@127.0.0.1:{REPLICA_PORTS[v]}/dbace" for v in versions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "dsn"])
    parser.add_argument("--versions", type=int, nargs="+", default=list(DEFAULT_VERSIONS), choices=sorted(VERSIONS))
    parser.add_argument("--recreate", action="store_true", help="konteynerleri silip sıfırdan kur")
    args = parser.parse_args()
    if args.command == "up":
        for version in args.versions:
            up(version, args.recreate)
            up_replica(version, args.recreate)
    # İki satır da GITHUB_ENV'e yazılıyor (ci.yml) — sıra birincil DSN'lerle aynı.
    print(f"DBACE_TEST_PG_DSN={dsn(args.versions)}")
    print(f"DBACE_TEST_PG_REPLICA_DSN={replica_dsn(args.versions)}")


if __name__ == "__main__":
    main()
