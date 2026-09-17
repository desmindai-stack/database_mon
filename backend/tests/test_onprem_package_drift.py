"""On-prem paketi ile uygulama AYRIŞMASIN (Faz 31 Commit 8).

Commit 8 envanteri paketin uygulamanın gerisinde kaldığını gösterdi: 52 migration'dan 4'ü, 40 ayardan 20'si,
yetki bilgisi hiç yoktu. Bu test her karşılaştırmayı KODDAN yapıyor (elle liste yok) ve CI'da koşuyor:

1. Migration'lar: imaj dizini BÜTÜN olarak kopyalıyor, açılış çalıştırıcıyı çağırıyor, compose initdb'ye dosya
   bağlamıyor, sürüm paketi dizini içeriyor.
2. Ortam değişkenleri: `Settings` modelinin her alanı `.env.example`'da belgeli; örnekte modelde olmayan anahtar
   yok; sırlar compose'ta ZORUNLU (`${VAR:?...}`).
3. Servisler: uygulamanın rolleri (api + worker, `Settings.run_mode`) compose'ta karşılanıyor; sağlık uç
   noktası uygulamada var; nginx uygulama servisine ve portuna yönleniyor.
4. Yetki matrisi: paketteki matrisin nesneleri kodun AST envanteriyle birebir; rol SQL'leri matrisin ölçtüğü
   yetkileri veriyor.

Her denetim bir işlev; negatif kontroller aynı işlevlere bilerek bozulmuş girdi veriyor.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import get_args

import pytest
import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
ONPREM = ROOT / "deploy" / "onprem"
sys.path.insert(0, str(ROOT / "backend" / "scripts"))

import permission_inventory  # noqa: E402

#: `.env.example`'da olup Settings'te olmayan, yalnızca compose'un okuduğu anahtarlar.
COMPOSE_ONLY_KEYS = {"DBACE_DB_PASSWORD", "HTTP_PORT"}
#: Verilmezse güvensiz varsayılanla açılan ayarlar — compose'ta zorunlu olmalı.
REQUIRED_SECRETS = {"CREDENTIALS_MASTER_KEY", "JWT_SECRET", "ADMIN_PASSWORD"}


def _read(name: str) -> str:
    return (ONPREM / name).read_text(encoding="utf-8")


# --- 1. Migration'lar -------------------------------------------------------------------------------


def migration_path_problems(dockerfile: str, entrypoint: str, compose: str, release_script: str) -> list[str]:
    problems = []
    if not re.search(r"^COPY supabase/migrations/? \./migrations/?\s*$", dockerfile, re.M):
        problems.append("Dockerfile.backend migration dizinini bütün olarak kopyalamıyor")
    if not re.search(r"^\s*python -m app\.migrations_runner /app/migrations\s*$", entrypoint, re.M):
        problems.append("entrypoint.sh migration çalıştırıcısını çağırmıyor")
    if "docker-entrypoint-initdb.d" in compose or re.search(r"supabase/migrations/[^\s:]+\.sql", compose):
        problems.append("docker-compose.yml tek tek migration bağlıyor (initdb) — iki şema yolu yarışır")
    if not re.search(r'cp -r "\$ROOT/supabase/migrations"', release_script):
        problems.append("make-release-package.sh migration dizinini pakete koymuyor")
    return problems


def test_package_applies_every_migration():
    assert migration_path_problems(
        _read("Dockerfile.backend"), _read("entrypoint.sh"), _read("docker-compose.yml"),
        _read("scripts/make-release-package.sh"),
    ) == []
    assert b"\r\n" not in (ONPREM / "entrypoint.sh").read_bytes(), "entrypoint.sh CRLF — sh konteynerde çalışmaz"


def test_negative_control_old_initdb_path_is_detected():
    old_compose = "volumes:\n  - ../../supabase/migrations/20250717120000_dbace_core.sql:/docker-entrypoint-initdb.d/01.sql:ro\n"
    problems = migration_path_problems("COPY supabase/migrations/20250717120000_dbace_core.sql ./migrations/\n",
                                       "exec uvicorn app.main:app\n", old_compose, "cp -r scripts dist/\n")
    assert len(problems) == 4, problems


# --- 2. Ortam değişkenleri ---------------------------------------------------------------------------


def env_example_keys(text: str) -> set[str]:
    """Etkin ya da yorumda belgelenmiş `ANAHTAR=` satırları."""
    return set(re.findall(r"^#?\s?([A-Z][A-Z0-9_]+)=", text, re.M))


def env_problems(settings_fields: set[str], example: str, compose: str) -> list[str]:
    keys = env_example_keys(example)
    expected = {name.upper() for name in settings_fields}
    problems = [f".env.example'da belgelenmemiş ayar: {k}" for k in sorted(expected - keys)]
    problems += [f".env.example'da modelde olmayan anahtar: {k}" for k in sorted(keys - expected - COMPOSE_ONLY_KEYS)]
    problems += [f"compose'ta zorunlu değil: {k}" for k in sorted(REQUIRED_SECRETS)
                 if not re.search(rf"\b{k}: \$\{{{k}:\?", compose)]
    return problems


def test_every_setting_is_documented_and_secrets_are_required():
    assert env_problems(set(Settings.model_fields), _read(".env.example"), _read("docker-compose.yml")) == []


def test_negative_control_new_setting_or_optional_secret_is_detected():
    problems = env_problems(set(Settings.model_fields) | {"new_feature_interval_seconds"}, _read(".env.example"),
                            _read("docker-compose.yml").replace("${JWT_SECRET:?", "${JWT_SECRET:-"))
    assert problems == [".env.example'da belgelenmemiş ayar: NEW_FEATURE_INTERVAL_SECONDS", "compose'ta zorunlu değil: JWT_SECRET"]
    assert env_problems(set(Settings.model_fields) - {"log_level"}, _read(".env.example"), _read("docker-compose.yml")) == [
        ".env.example'da modelde olmayan anahtar: LOG_LEVEL"]


# --- 3. Servisler --------------------------------------------------------------------------------------


def service_problems(compose_text: str, nginx: str, app_routes: set[str]) -> list[str]:
    compose = yaml.safe_load(compose_text)
    services = compose.get("services", {})
    roles: set[str] = set()
    app_service = None
    for name, service in services.items():
        image = str(service.get("image", ""))
        dockerfile = str((service.get("build") or {}).get("dockerfile", ""))
        if "backend" not in image and "Dockerfile.backend" not in dockerfile:
            continue
        mode = str((service.get("environment") or {}).get("RUN_MODE", "all"))
        roles |= {"api", "worker"} if mode == "all" else {mode}
        if mode in ("all", "api"):
            app_service = name
    expected_roles = {m for m in get_args(Settings.model_fields["run_mode"].annotation) if m != "all"}
    problems = [f"uygulama rolü hiçbir serviste çalışmıyor: {r}" for r in sorted(expected_roles - roles)]
    if app_service is None:
        return problems + ["API servisi yok"]
    health = " ".join(map(str, (services[app_service].get("healthcheck") or {}).get("test", [])))
    match = re.search(r"127\.0\.0\.1:(\d+)(/[\w/]+)", health)
    if not match or match.group(2) not in app_routes:
        problems.append(f"sağlık denetimi uygulamada olmayan bir uca bakıyor: {health[:120]}")
    elif int(match.group(1)) != Settings.model_fields["api_port"].default:
        problems.append("sağlık denetimi API portuna bakmıyor")
    upstreams = set(re.findall(r"proxy_pass http://([\w-]+):(\d+)", nginx))
    if upstreams != {(app_service, str(Settings.model_fields["api_port"].default))}:
        problems.append(f"nginx uygulama servisine yönlenmiyor: {sorted(upstreams)}")
    if not {"dbace-db", "dbace-web"} <= set(services):
        problems.append(f"veritabanı ya da web servisi eksik: {sorted(services)}")
    return problems


def _app_routes() -> set[str]:
    from app.main import app

    return {getattr(route, "path", "") for route in app.routes}


def test_compose_runs_api_and_worker_and_wires_health_and_proxy():
    assert service_problems(_read("docker-compose.yml"), _read("nginx.conf"), _app_routes()) == []


def test_negative_control_api_only_service_and_wrong_health_path_are_detected():
    compose = _read("docker-compose.yml").replace("RUN_MODE: all", "RUN_MODE: api").replace("/api/health", "/api/healthz")
    nginx = _read("nginx.conf").replace("dbace-app:8000", "dbace-api:8000")
    problems = service_problems(compose, nginx, _app_routes())
    assert any("worker" in p for p in problems) and any("sağlık" in p for p in problems) and any("nginx" in p for p in problems), problems


# --- 4. Yetki matrisi ---------------------------------------------------------------------------------


def matrix_problems(matrix: str, inventory_objects: set[tuple[str, str]], pg_sql: str, ms_sql: str) -> list[str]:
    in_matrix = permission_inventory.matrix_objects(matrix)
    problems = [f"kod kullanıyor, matriste yok: {e}:{o}" for e, o in sorted(inventory_objects - in_matrix)]
    problems += [f"matriste var, kod kullanmıyor: {e}:{o}" for e, o in sorted(in_matrix - inventory_objects)]
    needed = set(re.findall(r"^\| (?:postgresql|sqlserver) \| `[^`]+` \| [^|]* \| ([^|]+) \|", matrix, re.M))
    needed = {n.strip() for n in needed}
    grants = {
        "pg_monitor": r"^GRANT pg_monitor TO",
        "tabloda SELECT": r"^GRANT SELECT ON ALL TABLES IN SCHEMA",
        "VIEW SERVER STATE": r"^GRANT VIEW SERVER STATE TO",
        "VIEW DATABASE STATE": r"^GRANT VIEW DATABASE STATE TO",
    }
    for label, pattern in grants.items():
        sql = ms_sql if "STATE" in label else pg_sql
        if any(label in n for n in needed) and not re.search(pattern, sql, re.M):
            problems.append(f"matris '{label}' gerektiğini ölçtü, rol SQL'i vermiyor")
    if "—" in needed or any(n.startswith("❌") for n in needed):
        problems.append("matriste ölçülmemiş satır var — `permission_inventory.py --measure --write` çalıştırın")
    if not re.search(r"Hedef veritabanına yazan ifade \(DDL/DML\): YOK", matrix):
        problems.append("matris hedefe yazan ifade listeliyor")
    return problems


@pytest.fixture(scope="module")
def inventory_objects():
    return set(permission_inventory.build_inventory().objects)


def test_permission_matrix_matches_code_and_role_sql(inventory_objects):
    assert matrix_problems(_read("sql/permission-matrix.md"), inventory_objects, _read("sql/postgresql-monitor-role.sql"),
                           _read("sql/sqlserver-monitor-login.sql")) == []


def test_negative_control_matrix_drift_and_missing_grant_are_detected(inventory_objects):
    matrix = _read("sql/permission-matrix.md")
    removed = next(line for line in matrix.splitlines() if "`pg_stat_statements`" in line)
    problems = matrix_problems(
        matrix.replace(removed + "\n", ""),
        inventory_objects | {("postgresql", "pg_stat_new_view")},
        re.sub(r"^GRANT pg_monitor TO .*$", "", _read("sql/postgresql-monitor-role.sql"), flags=re.M),
        _read("sql/sqlserver-monitor-login.sql"),
    )
    assert "kod kullanıyor, matriste yok: postgresql:pg_stat_new_view" in problems
    assert "kod kullanıyor, matriste yok: postgresql:pg_stat_statements" in problems
    assert "matris 'pg_monitor' gerektiğini ölçtü, rol SQL'i vermiyor" in problems
