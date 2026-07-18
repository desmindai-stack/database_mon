#!/usr/bin/env bash
# İnternet bağlantılı sunucuda hızlı kurulum (geliştirme / pilot)
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  echo "Docker kurulu değil. Önce Docker Engine kurun."
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ".env oluşturuldu — PGWATCH_DB_PASSWORD ve CREDENTIALS_MASTER_KEY değiştirin!"
  read -r -p "Devam etmek için Enter (şifreleri düzenledikten sonra tekrar çalıştırın)..."
  exit 0
fi

docker compose -f docker-compose.yml up -d --build

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "pgwatch çalışıyor."
echo "  Arayüz: http://${IP:-localhost}:${HTTP_PORT:-8080}"
echo "  Demo DB eklemek için: docker compose -f docker-compose.yml -f docker-compose.demo-db.yml up -d"
echo "  Demo Postgres: host=demo-postgres veya sunucu IP port 5433, user/pass postgres"
