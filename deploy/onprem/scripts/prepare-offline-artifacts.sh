#!/usr/bin/env bash
# İnterneti OLAN makinede — çevrimdışı kurulumun ihtiyaç duyduğu her şeyi deploy/onprem/vendor/ altına indirir
# (Faz 31 Commit 8). Yerel araç gerektirmez: indirmeler imajın AYNI tabanında, konteynerde yapılır.
#
#   vendor/wheels/            Python 3.12 / linux x86_64 wheel'leri (pip wheel)
#   vendor/requirements.lock  wheel'lerden kurulan tam sürüm listesi (pip freeze) — paket sabit
#   vendor/debs/              ODBC Driver 18 for SQL Server + bağımlılıkları (.deb, bookworm)
#   vendor/web-dist/          frontend derleme çıktısı (npm ci + npm run build, package-lock.json'dan)
#   vendor/base-images.tar    taban imajlar: python:3.12-slim-bookworm, nginx:1.27-alpine, postgres:16-alpine
#
# Sonra: scripts/build-images-offline.sh (ağ kapalı derleme) ya da scripts/make-release-package.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
VENDOR="$ROOT/deploy/onprem/vendor"
PY_IMAGE="python:3.12-slim-bookworm"
NODE_IMAGE="node:22-alpine"
BASE_IMAGES=("$PY_IMAGE" "nginx:1.27-alpine" "postgres:16-alpine")

# Git Bash (Windows) docker bağlama yollarını çevirmesin.
export MSYS_NO_PATHCONV=1

rm -rf "$VENDOR/wheels" "$VENDOR/debs" "$VENDOR/web-dist" "$VENDOR/requirements.lock" "$VENDOR/base-images.tar"
mkdir -p "$VENDOR/wheels" "$VENDOR/debs" "$VENDOR/web-dist"

for image in "${BASE_IMAGES[@]}" "$NODE_IMAGE"; do
  docker pull "$image"
done

echo "==> Python wheel'leri ($PY_IMAGE)"
docker run --rm -v "$ROOT/backend:/src:ro" -v "$VENDOR:/vendor" "$PY_IMAGE" sh -euc '
  pip wheel --no-cache-dir -r /src/requirements.txt -w /vendor/wheels
  python -m venv /tmp/lock && /tmp/lock/bin/pip install --no-cache-dir --no-index --find-links /vendor/wheels -r /src/requirements.txt
  /tmp/lock/bin/pip freeze --all | grep -v "^pip==\|^setuptools==\|^wheel==" > /vendor/requirements.lock
'

echo "==> ODBC Driver 18 .deb paketleri ($PY_IMAGE)"
# İndirme imajın KENDİ tabanında ve ondan başka paket kurmadan: curl/gnupg kurmak ortak bağımlılıkları "zaten
# kurulu" gösterip listeden düşürürdü. Anahtar python ile iniyor, apt zırhlı (.asc) anahtarı doğrudan okuyor.
docker run --rm -v "$VENDOR:/vendor" "$PY_IMAGE" sh -euc '
  python -c "import urllib.request; urllib.request.urlretrieve(\"https://packages.microsoft.com/keys/microsoft.asc\", \"/usr/share/keyrings/microsoft.asc\")"
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.asc] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql.list
  apt-get update
  apt-get install -y --no-install-recommends --download-only msodbcsql18 unixodbc libpq5
  cp /var/cache/apt/archives/*.deb /vendor/debs/
'
# Ağ KAPALI, boş tabanda dpkg ile kurulum sınanıyor: eksik bağımlılık varsa burada kırılır, bankada değil.
docker run --rm --network none -v "$VENDOR/debs:/debs:ro" "$PY_IMAGE" sh -euc '
  ACCEPT_EULA=Y dpkg -i /debs/*.deb
  odbcinst -q -d -n "ODBC Driver 18 for SQL Server"
'

echo "==> Frontend derleme çıktısı ($NODE_IMAGE, package-lock.json)"
docker run --rm -v "$ROOT/frontend:/src:ro" -v "$VENDOR/web-dist:/out" "$NODE_IMAGE" sh -euc '
  cp -r /src /build && rm -rf /build/node_modules /build/dist
  cd /build && npm ci --no-audit --no-fund && VITE_API_URL= npm run build
  cp -r dist/. /out/
'
test -f "$VENDOR/web-dist/index.html"

echo "==> Taban imajlar"
# `-o` değil yönlendirme: Windows'taki docker istemcisi Git Bash'in /c/... yolunu açamıyor.
docker save "${BASE_IMAGES[@]}" > "$VENDOR/base-images.tar"

echo ""
echo "Hazır: $VENDOR"
ls -la "$VENDOR"
echo "wheel: $(ls "$VENDOR/wheels" | wc -l)  deb: $(ls "$VENDOR/debs" | wc -l)  lock: $(wc -l < "$VENDOR/requirements.lock") satır"
