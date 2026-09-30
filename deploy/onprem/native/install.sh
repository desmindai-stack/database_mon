#!/usr/bin/env bash
# dbace — Docker'sız (native) kurulum VE yükseltme, TEK komut, idempotent (Faz 32 Commit 11b,
# docs/ONPREM_NATIVE.md §7). Çalıştırma dizini önemli değil — kendi konumundan (repo/paket köküne
# göre deploy/onprem/native/install.sh) yolları bulur. root gerekir (sistem kullanıcısı/paket kurulumu).
#
#   sudo deploy/onprem/native/install.sh
#
# 1. prereq-check.sh — kritik eksik varsa DURUR.
# 2. dbace sistem kullanıcısı/dizinleri, /etc/dbace/dbace.env (eksik zorunlu değerler üretilir).
# 3. Native PostgreSQL 16 + nginx + msodbcsql18/unixODBC (vendor'dan, dağıtıma göre rpm/deb).
# 4. Yeni sürümü releases/<sürüm>/ altına kurar (taşınabilir Python venv + wheel'ler).
# 5. Migration'ları YENİ sürümün venv'iyle, symlink ÇEVRİLMEDEN önce uygular — başarısızsa eski sürüm
#    çalışmaya devam eder.
# 6. `current` symlink'i çevirir, nginx + dbace.service + yedek timer'ını kurar/başlatır.
# 7. /api/health 200 olana kadar bekler.
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
die() { printf 'HATA: %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "root ile çalıştırın (sudo $0) — sistem kullanıcısı/paket kurulumu gerekir"

DBACE_VERSION="$(cat "$ROOT/VERSION" 2>/dev/null || (cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null) || date -u +%Y%m%d%H%M%S)"
RELEASE_DIR="$RELEASES_DIR/$DBACE_VERSION"

# --- 1) Önkoşul denetimi ---------------------------------------------------------------------------
log "Önkoşul denetimi"
# .env henüz yoksa (ilk kurulum) prereq-check varsayılan portlarla çalışır; varsa mevcut değerlerle.
if [ -r "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi
if ! "$SCRIPT_DIR/prereq-check.sh"; then
  die "önkoşul denetimi FAIL verdi — yukarıdaki [FAIL] satırlarını düzeltip tekrar çalıştırın"
fi

. /etc/os-release
case "${ID:-}" in
  rhel|rocky|almalinux) DISTRO_FAMILY="rhel9" ;;
  ubuntu) DISTRO_FAMILY="ubuntu2204" ;;
  *) die "desteklenmeyen dağıtım: ${PRETTY_NAME:-$ID}" ;;
esac
log "Dağıtım ailesi: $DISTRO_FAMILY"

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

[ -d "$NATIVE_DIR" ] || die "$NATIVE_DIR yok — önce scripts/prepare-offline-artifacts.sh ile vendor doldurulmalı"

# --- 2) Sistem kullanıcısı + dizinler -----------------------------------------------------------------
log "Sistem kullanıcısı ve dizinler"
getent group "$SVC_GROUP" >/dev/null || groupadd --system "$SVC_GROUP"
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin -g "$SVC_GROUP" "$SVC_USER"

mkdir -p "$RELEASES_DIR" "$RUNTIME_DIR" "$CONF_DIR" "$TLS_DIR" "$LOG_DIR"
chown -R root:"$SVC_GROUP" "$DBACE_HOME" "$CONF_DIR"
chmod 750 "$DBACE_HOME" "$RELEASES_DIR"
chown -R "$SVC_USER":"$SVC_GROUP" "$LOG_DIR"
chmod 750 "$LOG_DIR"

# --- 3) /etc/dbace/dbace.env: yoksa örnekten kopyala, eksik zorunlu değerleri üret -----------------------
set_if_empty() {
  # $1=anahtar $2=üretilen değer — dosyada anahtar YOKSA ya da değeri BOŞSA doldurur; DOLUYSA dokunmaz.
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
  log "İlk kurulum: $ENV_FILE oluşturuluyor"
  cp "$SCRIPT_DIR/dbace.env.example" "$ENV_FILE"
else
  log "Var olan $ENV_FILE korunuyor (yükseltme — yalnızca eksik zorunlu değerler tamamlanır)"
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

# --- 4) Native PostgreSQL 16 ---------------------------------------------------------------------------
log "PostgreSQL 16 ($DISTRO_FAMILY)"
if [ ! -x "$PG_BINDIR/postgres" ]; then
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    rpm -Uvh --replacepkgs "$NATIVE_DIR/postgresql16"/*.rpm
  else
    DEBIAN_FRONTEND=noninteractive dpkg -i "$NATIVE_DIR/postgresql16"/*.deb || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -f
  fi
else
  log "PostgreSQL 16 zaten kurulu, atlanıyor"
fi

if [ ! -s "$PG_DATADIR/PG_VERSION" ]; then
  log "PostgreSQL veri dizini ilklendiriliyor: $PG_DATADIR"
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    PGSETUP_INITDB_OPTIONS="--auth-local=peer --auth-host=scram-sha-256" "$PG_BINDIR/postgresql-16-setup" initdb
  fi
  # Ubuntu: postgresql-16 paketinin postinst'i "16/main" kümesini otomatik ilklendiriyor (scram-sha-256 ile).
fi

grep -qE "^\s*listen_addresses\s*=\s*'localhost'" "$PG_CONF" 2>/dev/null || {
  sed -i "/^\s*#\?\s*listen_addresses/d" "$PG_CONF"
  echo "listen_addresses = 'localhost'" >> "$PG_CONF"
}

systemctl enable --now "$PG_SERVICE"
for _ in $(seq 1 30); do "$PG_BINDIR/pg_isready" -h 127.0.0.1 >/dev/null 2>&1 && break; sleep 1; done

log "dbace rolü/veritabanı"
sudo -u postgres "$PG_BINDIR/psql" -tAc "SELECT 1 FROM pg_roles WHERE rolname='dbace'" | grep -q 1 || \
  sudo -u postgres "$PG_BINDIR/psql" -c "CREATE ROLE dbace LOGIN PASSWORD '${DBACE_DB_PASSWORD}'"
sudo -u postgres "$PG_BINDIR/psql" -tAc "SELECT 1 FROM pg_database WHERE datname='dbace'" | grep -q 1 || \
  sudo -u postgres "$PG_BINDIR/psql" -c "CREATE DATABASE dbace OWNER dbace"

# --- 5) nginx --------------------------------------------------------------------------------------------
log "nginx"
if ! command -v nginx >/dev/null 2>&1; then
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    rpm -Uvh --replacepkgs "$NATIVE_DIR/nginx"/*.rpm
  else
    DEBIAN_FRONTEND=noninteractive dpkg -i "$NATIVE_DIR/nginx"/*.deb || \
      DEBIAN_FRONTEND=noninteractive apt-get install -y -f
  fi
else
  log "nginx zaten kurulu, atlanıyor"
fi

# --- 6) msodbcsql18 + unixODBC (SQL Server toplayıcısı için) ---------------------------------------------
log "msodbcsql18 + unixODBC"
if ! odbcinst -q -d -n "ODBC Driver 18 for SQL Server" >/dev/null 2>&1; then
  if [ "$DISTRO_FAMILY" = "rhel9" ]; then
    ACCEPT_EULA=Y rpm -Uvh --replacepkgs "$NATIVE_DIR/msodbcsql"/*.rpm
  else
    DEBIAN_FRONTEND=noninteractive ACCEPT_EULA=Y dpkg -i "$NATIVE_DIR/msodbcsql"/*.deb || \
      DEBIAN_FRONTEND=noninteractive ACCEPT_EULA=Y apt-get install -y -f
  fi
else
  log "msodbcsql18 zaten kurulu, atlanıyor"
fi

# --- 7) Taşınabilir Python (paylaşımlı runtime) ------------------------------------------------------------
PY_VERSION="$(cat "$VENDOR/native/python/VERSION")"
PY_RUNTIME="$RUNTIME_DIR/python-$PY_VERSION"
if [ ! -x "$PY_RUNTIME/python/install/bin/python3.12" ]; then
  log "Taşınabilir Python $PY_VERSION açılıyor: $PY_RUNTIME"
  mkdir -p "$PY_RUNTIME"
  tar -xzf "$VENDOR/native/python/cpython-3.12.tar.gz" -C "$PY_RUNTIME"
else
  log "Taşınabilir Python $PY_VERSION zaten hazır, atlanıyor"
fi
PY_BIN="$PY_RUNTIME/python/install/bin/python3.12"

# --- 8) Yeni sürümü kur (releases/<sürüm>/) -----------------------------------------------------------------
log "Sürüm $DBACE_VERSION -> $RELEASE_DIR"
mkdir -p "$RELEASE_DIR/bin"
rm -rf "$RELEASE_DIR/app" "$RELEASE_DIR/migrations" "$RELEASE_DIR/web-dist"
cp -r "$ROOT/backend/app" "$RELEASE_DIR/app"
cp -r "$ROOT/supabase/migrations" "$RELEASE_DIR/migrations"
if [ -d "$VENDOR/web-dist" ] && [ -f "$VENDOR/web-dist/index.html" ]; then
  cp -r "$VENDOR/web-dist" "$RELEASE_DIR/web-dist"
else
  die "$VENDOR/web-dist boş — önce scripts/prepare-offline-artifacts.sh çalıştırılmalı (frontend derlemesi)"
fi
cp "$SCRIPT_DIR/bin/start.sh" "$SCRIPT_DIR/bin/backup.sh" "$RELEASE_DIR/bin/"
chmod +x "$RELEASE_DIR/bin/start.sh" "$RELEASE_DIR/bin/backup.sh"

if [ ! -x "$RELEASE_DIR/venv/bin/python" ]; then
  log "venv kuruluyor + wheel'ler (--no-index)"
  "$PY_BIN" -m venv "$RELEASE_DIR/venv"
  "$RELEASE_DIR/venv/bin/pip" install --no-cache-dir --no-index --find-links "$VENDOR/wheels" -r "$VENDOR/requirements.lock" -q
fi
chown -R "$SVC_USER":"$SVC_GROUP" "$RELEASE_DIR"

# --- 9) Migration'lar — symlink ÇEVRİLMEDEN önce ------------------------------------------------------------
log "Migration'lar uygulanıyor (symlink henüz çevrilmedi — başarısızsa eski sürüm çalışmaya devam eder)"
sudo -u "$SVC_USER" env DATABASE_URL="$DATABASE_URL" PYTHONPATH="$RELEASE_DIR" "$RELEASE_DIR/venv/bin/python" -m app.migrations_runner "$RELEASE_DIR/migrations"

# --- 10) current symlink'i çevir -----------------------------------------------------------------------------
ln -sfn "$RELEASE_DIR" "$CURRENT_LINK"
chown -h "$SVC_USER":"$SVC_GROUP" "$CURRENT_LINK"
log "current -> $RELEASE_DIR"

# --- 11) nginx yapılandırması (TLS_MODE'a göre) ---------------------------------------------------------------
log "nginx yapılandırması (TLS_MODE=$TLS_MODE)"
NGINX_CONF_DIR="/etc/nginx/conf.d"
mkdir -p "$NGINX_CONF_DIR"
rm -f "$NGINX_CONF_DIR/default.conf"
if [ "$TLS_MODE" = "nginx" ]; then
  if [ ! -f "$TLS_DIR/fullchain.pem" ] || [ ! -f "$TLS_DIR/privkey.pem" ]; then
    log "UYARI: $TLS_DIR altında sertifika yok — İLK BOOT için kendinden imzalı, TEST sertifikası üretiliyor."
    log "       Bankanın kendi sertifikasını $TLS_DIR/{fullchain.pem,privkey.pem} olarak koyup nginx'i yeniden başlatın."
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

# --- 12) dbace.service + yedek timer'ı -------------------------------------------------------------------------
log "systemd birimleri"
sed "s#__PG_SERVICE__#$PG_SERVICE#g" "$SCRIPT_DIR/systemd/dbace.service.tmpl" > /etc/systemd/system/dbace.service
cp "$SCRIPT_DIR/systemd/dbace-backup.service" "$SCRIPT_DIR/systemd/dbace-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now dbace.service
systemctl enable --now dbace-backup.timer

# --- 13) Sağlık denetimi ----------------------------------------------------------------------------------------
log "/api/health bekleniyor"
OK=0
for _ in $(seq 1 60); do
  if "$PY_BIN" -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://${API_HOST}:${API_PORT}/api/health', timeout=3).status == 200 else 1)" 2>/dev/null; then
    OK=1; break
  fi
  sleep 2
done
[ "$OK" = "1" ] || { systemctl status dbace.service --no-pager -l || true; journalctl -u dbace.service --no-pager -n 80 || true; die "/api/health 200 olmadı"; }

echo ""
echo "dbace kuruldu: sürüm $DBACE_VERSION"
if [ "$TLS_MODE" = "nginx" ]; then
  echo "Arayüz: https://<SUNUCU_IP>:${HTTPS_PORT}"
else
  echo "Arayüz: http://<SUNUCU_IP>:${HTTP_PORT}"
fi
if [ -n "$ADMIN_PASSWORD_GENERATED" ]; then
  echo "Yönetici ilk şifresi (BİR KEZ gösteriliyor, ADMIN_USERNAME=admin): $ADMIN_PASSWORD_GENERATED"
fi
