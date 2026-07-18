#!/usr/bin/env bash
# İnterneti OLAN makinede çalıştırın — imajları paketler (air-gap sunucuya taşımak için)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Önce: cp .env.example .env  ve şifreleri düzenleyin"
  exit 1
fi

echo "==> Imajlar derleniyor..."
docker compose -f docker-compose.yml build

echo "==> Temel imajlar çekiliyor..."
docker pull postgres:16-alpine
docker pull nginx:1.27-alpine

IMAGES=(
  postgres:16-alpine
  nginx:1.27-alpine
  pgwatch/backend:latest
  pgwatch/web:latest
)

OUT="../dist/pgwatch-images-$(date +%Y%m%d).tar"
mkdir -p ../dist
echo "==> Kaydediliyor: $OUT"
docker save -o "$OUT" "${IMAGES[@]}"

echo ""
echo "Tamam. Bu dosyayı kapalı sunucuya kopyalayın:"
echo "  $OUT"
echo "Orada: ./scripts/import-and-start.sh $OUT"
