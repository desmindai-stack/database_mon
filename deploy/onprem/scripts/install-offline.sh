#!/usr/bin/env bash
# Kapalı (air-gap) Linux sunucuda kurulum ve YÜKSELTME — tek komut (Faz 31 Commit 8).
#
#   cd deploy/onprem && cp .env.example .env   # değerleri düzenleyin
#   ./scripts/install-offline.sh
#
# 1. Taban imajları yükler, dbace imajlarını `--network none` ile derler (scripts/build-images-offline.sh).
# 2. `docker compose up -d --no-build`: dbace-app açılışta uygulanmamış migration'ları sırayla uygular
#    (yeni kurulum ve yükseltme AYNI yol; veritabanı birimi korunur).
# 3. dbace-app sağlıklı olana kadar bekler; olmazsa log'u gösterip hata koduyla çıkar.
set -euo pipefail

ONPREM="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$ONPREM/../.." && pwd)"
cd "$ONPREM"
export MSYS_NO_PATHCONV=1

if [ ! -f .env ]; then
  echo "Önce: cp .env.example .env  ve ZORUNLU değerleri düzenleyin." >&2
  exit 1
fi
for var in DBACE_DB_PASSWORD CREDENTIALS_MASTER_KEY JWT_SECRET ADMIN_PASSWORD; do
  value="$(grep -E "^${var}=" .env | head -1 | cut -d= -f2- || true)"
  if [ -z "$value" ] || [ "${value#degistirin}" != "$value" ]; then
    echo ".env: $var verilmemiş ya da örnek değerinde bırakılmış." >&2
    exit 1
  fi
done

"$ONPREM/scripts/build-images-offline.sh" "$ROOT"

echo "==> Servisler başlatılıyor"
docker compose -f docker-compose.yml up -d --no-build

echo "==> dbace-app sağlık denetimi bekleniyor (migration'lar bu sırada uygulanıyor)"
for _ in $(seq 1 90); do
  status="$(docker inspect -f '{{.State.Health.Status}}' dbace-app 2>/dev/null || echo yok)"
  if [ "$status" = "healthy" ]; then
    docker logs dbace-app 2>&1 | grep -E "^migration:" | tail -1 || true
    echo "dbace çalışıyor: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(grep -E '^HTTP_PORT=' .env | cut -d= -f2 || echo 8080)"
    exit 0
  fi
  if [ "$(docker inspect -f '{{.State.Status}}' dbace-app 2>/dev/null || echo yok)" = "exited" ]; then
    break
  fi
  sleep 2
done
echo "dbace-app sağlıklı hâle gelmedi. Son log:" >&2
docker logs --tail 80 dbace-app >&2 || true
exit 1
