#!/usr/bin/env bash
# Sürüm paketi — bankaya gidecek, İNTERNETSİZ kurulan arşiv (Faz 31 Commit 8).
#
#   scripts/make-release-package.sh v0.4.0
#
# İnternetli makinede çalışır: önce vendor/ doldurulur (scripts/prepare-offline-artifacts.sh), sonra paket
# depo düzeninin YALNIZCA kurulum için gereken alt kümesiyle kurulur. Pakette hazır imaj değil kaynak + çevrimdışı
# bağımlılıklar var: imajlar kapalı sunucuda `docker build --network none` ile derleniyor
# (scripts/install-offline.sh). Böylece pakette ne olduğu okunabilir, ağdan hiçbir şey inmez.
#
# Paket içeriği (dbace-onprem-<sürüm>/):
#   backend/app, backend/requirements.txt, supabase/migrations
#   deploy/onprem/{docker-compose.yml, Dockerfile.backend, Dockerfile.web.offline, entrypoint.sh, nginx.conf,
#                  .env.example, KURULUM.md, sql/, scripts/, vendor/}
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "Kullanım: $0 v0.4.0" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ONPREM="$ROOT/deploy/onprem"
OUT_DIR="${DBACE_RELEASE_DIR:-$ROOT/deploy/dist}"
NAME="dbace-onprem-${VERSION}"
DIST="$OUT_DIR/$NAME"

if [ "${DBACE_SKIP_PREPARE:-0}" != "1" ] || [ ! -f "$ONPREM/vendor/requirements.lock" ]; then
  "$ONPREM/scripts/prepare-offline-artifacts.sh"
fi

rm -rf "$DIST"
mkdir -p "$DIST/backend" "$DIST/supabase" "$DIST/deploy/onprem"
cp -r "$ROOT/backend/app" "$DIST/backend/app"
cp "$ROOT/backend/requirements.txt" "$DIST/backend/"
cp -r "$ROOT/supabase/migrations" "$DIST/supabase/migrations"
for item in docker-compose.yml Dockerfile.backend Dockerfile.web.offline entrypoint.sh nginx.conf .env.example \
            KURULUM.md sql scripts vendor; do
  cp -r "$ONPREM/$item" "$DIST/deploy/onprem/"
done
find "$DIST" -name "__pycache__" -type d -prune -exec rm -rf {} +

echo "$VERSION" > "$DIST/VERSION"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DIST/BUILD_TIME"
(cd "$ROOT" && git rev-parse HEAD 2>/dev/null || echo "git yok") > "$DIST/COMMIT"

cat > "$DIST/PAKET-OKU.txt" <<EOF
dbace on-prem paketi — $VERSION (internetsiz kurulum)

1. Arşivi kapalı sunucuya kopyalayıp açın; cd $NAME/deploy/onprem
2. cp .env.example .env  → ZORUNLU değerleri düzenleyin (DBACE_DB_PASSWORD, CREDENTIALS_MASTER_KEY,
   JWT_SECRET, ADMIN_PASSWORD).
3. ./scripts/install-offline.sh   (yükseltmede de aynı komut; veri birimi korunur)
4. İzlenen veritabanlarında DBA: sql/postgresql-monitor-role.sql ya da sql/sqlserver-monitor-login.sql
   (yalnızca okuma yetkisi; hangi yetkinin hangi özellik için gerektiği sql/permission-matrix.md'de).

Detay: deploy/onprem/KURULUM.md
EOF

tar -czf "$OUT_DIR/$NAME.tar.gz" -C "$OUT_DIR" "$NAME"
echo ""
echo "Paket hazır: $OUT_DIR/$NAME.tar.gz ($(du -h "$OUT_DIR/$NAME.tar.gz" | cut -f1))"
