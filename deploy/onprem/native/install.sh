#!/usr/bin/env bash
# shellcheck disable=SC1090,SC1091
# dbace - Docker-less (native) install AND upgrade, ONE command, idempotent (Phase 32 Commit 11b,
# docs/ONPREM_NATIVE.md section 7). Working directory does not matter - it finds paths relative to
# its own location (deploy/onprem/native/install.sh under the repo/package root). Requires root
# (system user creation, package install).
#
#   sudo deploy/onprem/native/install.sh
#
# 1. prereq-check.sh - STOPS if anything critical is missing.
# 2. dbace system user/directories, /etc/dbace/dbace.env (missing required values are generated).
# 3. Native PostgreSQL 16 + nginx + msodbcsql18/unixODBC (from vendor, rpm/deb per distro).
# 4. Installs the new version under releases/<version>/ (portable Python venv + wheels).
# 5. Applies migrations with the NEW version's venv, before the symlink is flipped - if it fails,
#    the old version keeps running.
# 6. Flips the `current` symlink, installs/starts nginx + dbace.service + the backup timer.
# 7. Waits until /api/health returns 200.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
VENDOR="$ROOT/deploy/onprem/vendor"

DBACE_HOME="/opt/dbace"
RELEASES_DIR="$DBACE_HOME/releases"
CURRENT_LINK="$DBACE_HOME/current"
RUNTIME_DIR="$DBACE_HOME/runtime"
CONF_DIR="/etc/dbace"
ENV_FILE="$CONF_DIR/dbace.env"
TLS_DIR="$CONF_DIR/tls"
LOG_DIR="/var/log/dbace"
SVC_USER="dbace"
SVC_GROUP="dbace"

log() { printf '==> %s\n' "$1"; }
die() { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root (sudo $0) - system user creation and package install require it"

DBACE_VERSION="$(cat "$ROOT/VERSION" 2>/dev/null || (cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null) || date -u +%Y%m%d%H%M%S)"
RELEASE_DIR="$RELEASES_DIR/$DBACE_VERSION"

# --- 1) Prerequisite check -------------------------------------------------------------------------
log "Prerequisite check"
# If .env does not exist yet (fresh install), prereq-check runs with default ports; otherwise with
# the existing values.
if [ -r "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
if ! "$SCRIPT_DIR/prereq-check.sh"; then
  die "prerequisite check reported FAIL - fix the [FAIL] lines above and run again"
fi

. /etc/os-release
case "${ID:-}" in
  rhel|rocky|almalinux) DISTRO_FAMILY="rhel9" ;;
  ubuntu) DISTRO_FAMILY="ubuntu2204" ;;
  *) die "unsupported distribution: ${PRETTY_NAME:-$ID}" ;;
esac
log "Distribution family: $DISTRO_FAMILY"

if [ "$DISTRO_FAMILY" = "rhel9" ]; then
  PG_SERVICE="postgresql-16.service"
  PG_BINDIR="/usr/pgsql-16/bin"
  PG_DATADIR="/var/lib/pgsql/16/data"
  PG_CONF="$PG_DATADIR/postgresql.conf"
  NATIVE_DIR="$VENDOR/native/rhel9"
else
  PG_SERVICE="postgresql.service"
  PG_BINDIR="/usr/lib/postgresql/16/bin"
  PG_DATADIR="/var/lib/postgresql/16/main"
  PG_CONF="/etc/postgresql/16/main/postgresql.conf"
  NATIVE_DIR="$VENDOR/native/ubuntu2204"
fi

[ -d "$NATIVE_DIR" ] || die "$NATIVE_DIR is missing - run scripts/prepare-offline-artifacts.sh first to populate vendor"

# --- 2) System user + directories -----------------------------------------------------------------
log "System user and directories"
getent group "$SVC_GROUP" >/dev/null || groupadd --system "$SVC_GROUP"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin -g "$SVC_GROUP" "$SVC_USER"

mkdir -p "$RELEASES_DIR" "$RUNTIME_DIR" "$CONF_DIR" "$TLS_DIR" "$LOG_DIR"
chown -R root:"$SVC_GROUP" "$DBACE_HOME" "$CONF_DIR"
chmod 750 "$DBACE_HOME" "$RELEASES_DIR"
chown -R "$SVC_USER":"$SVC_GROUP" "$LOG_DIR"
chmod 750 "$LOG_DIR"

# --- 3) /etc/dbace/dbace.env: copy from the example if missing, generate missing required values ----
set_if_empty() {
  # $1=key $2=generated value - fills in if the key is MISSING or its value is EMPTY; leaves it
  # alone if it is already set.
  key="$1"; value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    current="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-)"
    if [ -z "$current" ]; then
      sed -i "s#^${key}=.*#${key}=${value}#" "$ENV_FILE"
    fi
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}
set_or_replace() {
  key="$1"; value="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

ADMIN_PASSWORD_GENERATED=""
if [ ! -f "$ENV_FILE" ]; then
  log "Fresh install: creating $ENV_FILE"
  cp "$SCRIPT_DIR/dbace.env.example" "$ENV_FILE"
else
  log "Keeping existing $ENV_FILE (upgrade - only missing required values are filled in)"
fi
chown "$SVC_USER":"$SVC_GROUP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

set_if_empty "DBACE_DB_PASSWORD" "$(openssl rand -hex 24)"
set_if_empty "CREDENTIALS_MASTER_KEY" "$(openssl rand -hex 32)"
set_if_empty "JWT_SECRET" "$(openssl rand -hex 32)"
if grep -qE '^ADMIN_PASSWORD=$' "$ENV_FILE" || ! grep -qE '^ADMIN_PASSWORD=' "$ENV_FILE"; then
  ADMIN_PASSWORD_GENERATED="$(openssl rand -base64 18 | tr -d '=+/' | cut -c1-20)"
  set_if_empty "ADMIN_PASSWORD" "$ADMIN_PASSWORD_GENERATED"
fi
set_if_empty "BACKUP_DIR" "/var/backups/dbace"
set_if_empty "BACKUP_RETENTION_DAYS" "14"
set_if_empty "TLS_MODE" "nginx"
set_if_empty "HTTP_PORT" "8080"
set_if_empty "HTTPS_PORT" "443"
set_if_empty "API_HOST" "127.0.0.1"
set_if_empty "API_PORT" "8000"
set_or_replace "PG_BINDIR" "$PG_BINDIR"

set -a; . "$ENV_FILE"; set +a
mkdir -p "$BACKUP_DIR"; chown "$SVC_USER":"$SVC_GROUP" "$BACKUP_DIR"; chmod 750 "$BACKUP_DIR"

DATABASE_URL="postgresql+asyncpg://dbace:${DBACE_DB_PASSWORD}@127.0.0.1:5432/dbace"
set_or_replace "DATABASE_URL" "$DATABASE_URL"
export DATABASE_URL

# --- 4) Native PostgreSQL 16 -----------------------------------------------------------------------
log "PostgreSQL 16 ($DISTRO_FAMILY)"
if [ ! -x "$PG_BINDIR/postgres" ]; then
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    rpm -Uvh --replacepkgs "$NATIVE_DIR/postgresql16"/*.rpm
  else
    DEBIAN_FRONTEND=noninteractive dpkg -i "$NATIVE_DIR/postgresql16"/*.deb || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -f
  fi
else
  log "PostgreSQL 16 is already installed, skipping"
fi

if [ ! -s "$PG_DATADIR/PG_VERSION" ]; then
  log "Initializing the PostgreSQL data directory: $PG_DATADIR"
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    PGSETUP_INITDB_OPTIONS="--auth-local=peer --auth-host=scram-sha-256" "$PG_BINDIR/postgresql-16-setup" initdb
  fi
  # Ubuntu: the postgresql-16 package's postinst auto-initializes the "16/main" cluster (with scram-sha-256).
fi

grep -qE "^\s*listen_addresses\s*=\s*'localhost'" "$PG_CONF" 2>/dev/null || {
  sed -i "/^\s*#\?\s*listen_addresses/d" "$PG_CONF"
  echo "listen_addresses = 'localhost'" >> "$PG_CONF"
}

systemctl enable --now "$PG_SERVICE"
for _ in $(seq 1 30); do "$PG_BINDIR/pg_isready" -h 127.0.0.1 >/dev/null 2>&1 && break; sleep 1; done

log "dbace role/database"
sudo -u postgres "$PG_BINDIR/psql" -tAc "SELECT 1 FROM pg_roles WHERE rolname='dbace'" | grep -q 1 || \
  sudo -u postgres "$PG_BINDIR/psql" -c "CREATE ROLE dbace LOGIN PASSWORD '${DBACE_DB_PASSWORD}'"
sudo -u postgres "$PG_BINDIR/psql" -tAc "SELECT 1 FROM pg_database WHERE datname='dbace'" | grep -q 1 || \
  sudo -u postgres "$PG_BINDIR/psql" -c "CREATE DATABASE dbace OWNER dbace"

# --- 5) nginx ----------------------------------------------------------------------------------------
log "nginx"
if ! command -v nginx >/dev/null 2>&1; then
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    rpm -Uvh --replacepkgs "$NATIVE_DIR/nginx"/*.rpm
  else
    DEBIAN_FRONTEND=noninteractive dpkg -i "$NATIVE_DIR/nginx"/*.deb || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -f
  fi
else
  log "nginx is already installed, skipping"
fi

# --- 6) msodbcsql18 + unixODBC (for the SQL Server collector) ---------------------------------------
log "msodbcsql18 + unixODBC"
if ! odbcinst -q -d -n "ODBC Driver 18 for SQL Server" >/dev/null 2>&1; then
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    ACCEPT_EULA=Y rpm -Uvh --replacepkgs "$NATIVE_DIR/msodbcsql"/*.rpm
  else
    DEBIAN_FRONTEND=noninteractive ACCEPT_EULA=Y dpkg -i "$NATIVE_DIR/msodbcsql"/*.deb || \
      DEBIAN_FRONTEND=noninteractive ACCEPT_EULA=Y apt-get install -y -f
  fi
else
  log "msodbcsql18 is already installed, skipping"
fi

# --- 7) Portable Python (shared runtime) -------------------------------------------------------------
PY_VERSION="$(cat "$VENDOR/native/python/VERSION")"
PY_RUNTIME="$RUNTIME_DIR/python-$PY_VERSION"
if [ ! -x "$PY_RUNTIME/python/install/bin/python3.12" ]; then
  log "Extracting portable Python $PY_VERSION: $PY_RUNTIME"
  mkdir -p "$PY_RUNTIME"
  tar -xzf "$VENDOR/native/python/cpython-3.12.tar.gz" -C "$PY_RUNTIME"
else
  log "Portable Python $PY_VERSION is already present, skipping"
fi
PY_BIN="$PY_RUNTIME/python/install/bin/python3.12"

# --- 8) Install the new version (releases/<version>/) -------------------------------------------------
log "Version $DBACE_VERSION -> $RELEASE_DIR"
mkdir -p "$RELEASE_DIR/bin"
rm -rf "$RELEASE_DIR/app" "$RELEASE_DIR/migrations" "$RELEASE_DIR/web-dist"
cp -r "$ROOT/backend/app" "$RELEASE_DIR/app"
cp -r "$ROOT/supabase/migrations" "$RELEASE_DIR/migrations"
if [ -d "$VENDOR/web-dist" ] && [ -f "$VENDOR/web-dist/index.html" ]; then
  cp -r "$VENDOR/web-dist" "$RELEASE_DIR/web-dist"
else
  die "$VENDOR/web-dist is empty - run scripts/prepare-offline-artifacts.sh first (frontend build)"
fi
cp "$SCRIPT_DIR/bin/start.sh" "$SCRIPT_DIR/bin/backup.sh" "$RELEASE_DIR/bin/"
chmod +x "$RELEASE_DIR/bin/start.sh" "$RELEASE_DIR/bin/backup.sh"

if [ ! -x "$RELEASE_DIR/venv/bin/python" ]; then
  log "Creating venv + installing wheels (--no-index)"
  "$PY_BIN" -m venv "$RELEASE_DIR/venv"
  "$RELEASE_DIR/venv/bin/pip" install --no-cache-dir --no-index --find-links "$VENDOR/wheels" -r "$VENDOR/requirements.lock" -q
fi
chown -R "$SVC_USER":"$SVC_GROUP" "$RELEASE_DIR"

# --- 9) Migrations - before the symlink is flipped ----------------------------------------------------
log "Applying migrations (symlink not flipped yet - if this fails, the old version keeps running)"
sudo -u "$SVC_USER" env DATABASE_URL="$DATABASE_URL" PYTHONPATH="$RELEASE_DIR" "$RELEASE_DIR/venv/bin/python" -m app.migrations_runner "$RELEASE_DIR/migrations"

# --- 10) Flip the current symlink ---------------------------------------------------------------------
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
chown -h "$SVC_USER":"$SVC_GROUP" "$CURRENT_LINK"
log "current -> $RELEASE_DIR"

# --- 11) nginx configuration (by TLS_MODE) -------------------------------------------------------------
log "nginx configuration (TLS_MODE=$TLS_MODE)"
NGINX_CONF_DIR="/etc/nginx/conf.d"
mkdir -p "$NGINX_CONF_DIR"
rm -f "$NGINX_CONF_DIR/default.conf"
if [ "$TLS_MODE" = "nginx" ]; then
  if [ ! -f "$TLS_DIR/fullchain.pem" ] || [ ! -f "$TLS_DIR/privkey.pem" ]; then
    log "WARNING: no certificate under $TLS_DIR - generating a self-signed TEST certificate for FIRST BOOT."
    log "         Put the bank's own certificate at $TLS_DIR/{fullchain.pem,privkey.pem} and restart nginx."
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
      -keyout "$TLS_DIR/privkey.pem" -out "$TLS_DIR/fullchain.pem" \
      -subj "/CN=dbace-onprem" >/dev/null 2>&1
  fi
  chmod 600 "$TLS_DIR/privkey.pem"
  sed -e "s#__HTTP_PORT__#$HTTP_PORT#g" -e "s#__HTTPS_PORT__#$HTTPS_PORT#g" \
      -e "s#__API_PORT__#$API_PORT#g" -e "s#__WEB_ROOT__#$CURRENT_LINK/web-dist#g" \
      "$SCRIPT_DIR/nginx/nginx-tls.conf.tmpl" > "$NGINX_CONF_DIR/dbace.conf"
else
  sed -e "s#__HTTP_PORT__#$HTTP_PORT#g" -e "s#__API_PORT__#$API_PORT#g" \
      -e "s#__WEB_ROOT__#$CURRENT_LINK/web-dist#g" \
      "$SCRIPT_DIR/nginx/nginx-backend.conf.tmpl" > "$NGINX_CONF_DIR/dbace.conf"
fi
nginx -t
systemctl enable --now nginx
systemctl reload nginx 2>/dev/null || systemctl restart nginx

# --- 12) dbace.service + backup timer --------------------------------------------------------------------
log "systemd units"
sed "s#__PG_SERVICE__#$PG_SERVICE#g" "$SCRIPT_DIR/systemd/dbace.service.tmpl" > /etc/systemd/system/dbace.service
cp "$SCRIPT_DIR/systemd/dbace-backup.service" "$SCRIPT_DIR/systemd/dbace-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dbace.service
systemctl enable --now dbace-backup.timer

# --- 13) Health check ----------------------------------------------------------------------------------
log "Waiting for /api/health"
OK=0
for _ in $(seq 1 60); do
  if "$PY_BIN" -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://${API_HOST}:${API_PORT}/api/health', timeout=3).status == 200 else 1)" 2>/dev/null; then
    OK=1; break
  fi
  sleep 2
done
[ "$OK" = "1" ] || { systemctl status dbace.service --no-pager -l || true; journalctl -u dbace.service --no-pager -n 80 || true; die "/api/health did not return 200"; }

echo ""
echo "dbace installed: version $DBACE_VERSION"
if [ "$TLS_MODE" = "nginx" ]; then
  echo "UI: https://<SERVER_IP>:${HTTPS_PORT}"
else
  echo "UI: http://<SERVER_IP>:${HTTP_PORT}"
fi
if [ -n "$ADMIN_PASSWORD_GENERATED" ]; then
  echo "Initial admin password (shown ONCE, ADMIN_USERNAME=admin): $ADMIN_PASSWORD_GENERATED"
fi
