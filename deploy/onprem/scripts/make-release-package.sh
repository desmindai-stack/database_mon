#!/usr/bin/env bash
# Sürüm paketi: Docker imajları + on-prem kurulum dosyaları (Faz 2 → Faz 3)
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  echo "Kullanım: $0 v0.3.0"
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ONPREM="$ROOT/deploy/onprem"
DIST="$ROOT/deploy/dist/pgwatch-onprem-${VERSION}"
mkdir -p "$DIST"

echo "==> Sürüm: $VERSION"
echo "$VERSION" > "$DIST/VERSION"
date -u +"%Y-%m-%dT%H:%M:%SZ" > "$DIST/BUILD_TIME"

if [ ! -f "$ONPREM/.env" ]; then
  echo "Uyarı: $ONPREM/.env yok — export-images için .env.example kopyalayın"
  cp "$ONPREM/.env.example" "$ONPREM/.env"
fi

echo "==> Docker imajları derleniyor..."
(cd "$ONPREM" && docker compose -f docker-compose.yml build)
docker pull postgres:16-alpine
docker pull nginx:1.27-alpine

IMAGES=(postgres:16-alpine nginx:1.27-alpine pgwatch/backend:latest pgwatch/web:latest)
IMG_TAR="$DIST/pgwatch-images.tar"
docker save -o "$IMG_TAR" "${IMAGES[@]}"

echo "==> Kurulum dosyaları kopyalanıyor..."
cp -r "$ONPREM/docker-compose.yml" "$ONPREM/docker-compose.demo-db.yml" \
  "$ONPREM/nginx.conf" "$ONPREM/.env.example" "$ONPREM/KURULUM.md" \
  "$ONPREM/scripts" "$DIST/"

# README for package recipient
cat > "$DIST/PAKET-OKU.txt" <<EOF
pgwatch on-prem paketi — $VERSION

1. Bu klasörü kapalı sunucuya kopyalayın.
2. cp .env.example .env  → şifreleri düzenleyin.
3. chmod +x scripts/import-and-start.sh
4. ./scripts/import-and-start.sh pgwatch-images.tar
5. Tarayıcı: http://SUNUCU_IP:8080

Detay: KURULUM.md
Bulut geliştirme tamamlandıktan sonra üretilen pakettir.
EOF

ARCHIVE="$ROOT/deploy/dist/pgwatch-onprem-${VERSION}.tar.gz"
tar -czf "$ARCHIVE" -C "$ROOT/deploy/dist" "pgwatch-onprem-${VERSION}"

echo ""
echo "Paket hazır:"
echo "  Klasör: $DIST"
echo "  Arşiv:  $ARCHIVE"
