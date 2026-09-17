#!/usr/bin/env bash
# dbace imajlarını İNTERNETSİZ derler (Faz 31 Commit 8): `docker build --network none`.
#
# Girdi: deploy/onprem/vendor/ (scripts/prepare-offline-artifacts.sh ya da sürüm paketi) ve yüklü taban imajlar
# (vendor/base-images.tar varsa önce yüklenir). Bir paket internetten bir şey indirmeye kalkarsa derleme KIRILIR —
# bankadaki kurulumun aynısı.
#
#   scripts/build-images-offline.sh [kaynak kök dizini]
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "$0")/../../.." && pwd)}"
VENDOR="$ROOT/deploy/onprem/vendor"
export MSYS_NO_PATHCONV=1
# Windows (Git Bash) geliştirme makinesinde docker istemcisi /c/... yolunu açamıyor; Linux'ta yol aynen kalır.
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

for required in "$VENDOR/requirements.lock" "$VENDOR/web-dist/index.html"; do
  if [ ! -f "$required" ]; then
    echo "Eksik: $required — önce scripts/prepare-offline-artifacts.sh (internetli makinede)." >&2
    exit 1
  fi
done
if ! ls "$VENDOR"/wheels/*.whl >/dev/null 2>&1 || ! ls "$VENDOR"/debs/*.deb >/dev/null 2>&1; then
  echo "Eksik: vendor/wheels ya da vendor/debs boş — önce scripts/prepare-offline-artifacts.sh." >&2
  exit 1
fi

if [ -f "$VENDOR/base-images.tar" ]; then
  echo "==> Taban imajlar yükleniyor"
  docker load < "$VENDOR/base-images.tar"
fi

echo "==> dbace/backend (ağ kapalı)"
docker build --network none --pull=false -f "$(native "$ROOT/deploy/onprem/Dockerfile.backend")" -t dbace/backend:latest "$(native "$ROOT")"
echo "==> dbace/web (ağ kapalı)"
docker build --network none --pull=false -f "$(native "$ROOT/deploy/onprem/Dockerfile.web.offline")" -t dbace/web:latest "$(native "$ROOT")"

docker image inspect dbace/backend:latest dbace/web:latest --format '{{.RepoTags}} {{.Id}}'
