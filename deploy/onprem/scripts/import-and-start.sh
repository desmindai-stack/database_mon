#!/usr/bin/env bash
# Kapalı (air-gap) Linux sunucuda — imajları yükler ve stack'i başlatır
set -euo pipefail
cd "$(dirname "$0")/.."

TAR="${1:-}"
if [ -z "$TAR" ] || [ ! -f "$TAR" ]; then
  echo "Kullanım: $0 /yol/pgwatch-images-YYYYMMDD.tar"
  exit 1
fi

echo "==> Imajlar yükleniyor..."
docker load -i "$TAR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "ÖNEMLİ: .env dosyasını düzenleyin (PGWATCH_DB_PASSWORD, CREDENTIALS_MASTER_KEY)"
  echo "Sonra tekrar: docker compose up -d"
  exit 0
fi

echo "==> Servisler başlatılıyor..."
docker compose -f docker-compose.yml up -d

echo ""
echo "Web arayüzü: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${HTTP_PORT:-8080}"
echo "Durum: docker compose -f docker-compose.yml ps"
echo "Log:  docker compose -f docker-compose.yml logs -f pgwatch-app"
