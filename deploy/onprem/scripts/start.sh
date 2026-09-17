#!/usr/bin/env bash
# İnternet bağlantılı sunucuda hızlı kurulum (geliştirme / pilot) — bankadaki kurulumla AYNI yol (Faz 31 Commit 8):
# çevrimdışı bağımlılıklar indirilir, imajlar ağ kapalı derlenir, migration'lar açılışta uygulanır.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  echo "Docker kurulu değil. Önce Docker Engine kurun."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env oluşturuldu — ZORUNLU değerleri (DBACE_DB_PASSWORD, CREDENTIALS_MASTER_KEY, JWT_SECRET, ADMIN_PASSWORD) düzenleyip tekrar çalıştırın."
  exit 0
fi

if [ ! -f vendor/requirements.lock ]; then
  ./scripts/prepare-offline-artifacts.sh
fi
./scripts/install-offline.sh
