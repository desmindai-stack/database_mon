#!/usr/bin/env bash
# İnterneti OLAN makinede — çevrimdışı kurulumun ihtiyaç duyduğu her şeyi deploy/onprem/vendor/ altına indirir
# (Faz 31 Commit 8). Yerel araç gerektirmez: indirmeler imajın AYNI tabanında, konteynerde yapılır.
#
#   vendor/wheels/            Python 3.12 / linux x86_64 wheel'leri (pip wheel)
#   vendor/requirements.lock  wheel'lerden kurulan tam sürüm listesi (pip freeze) — paket sabit
#   vendor/debs/              ODBC Driver 18 for SQL Server + bağımlılıkları (.deb, bookworm — Docker imajı için)
#   vendor/web-dist/          frontend derleme çıktısı (npm ci + npm run build, package-lock.json'dan)
#   vendor/base-images.tar    taban imajlar: python:3.12-slim-bookworm, nginx:1.27-alpine, postgres:16-alpine
#
# Faz 32 Commit 11b — Docker'sız (native) kurulum için EK çıktılar (docs/ONPREM_NATIVE.md):
#   vendor/native/python/            taşınabilir CPython 3.12 (python-build-standalone, tar.gz — hedefte
#                                     zstd gerektirmesin diye burada .tar.zst'den yeniden sıkıştırılıyor)
#   vendor/native/rhel9/{postgresql16,nginx,msodbcsql}/*.rpm      RHEL9 ailesi (PGDG + AppStream + Microsoft)
#   vendor/native/ubuntu2204/{postgresql16,nginx,msodbcsql}/*.deb Ubuntu 22.04 (PGDG + main + Microsoft)
#
# Sonra: scripts/build-images-offline.sh (ağ kapalı derleme, Docker paketi) ya da scripts/make-release-package.sh
# (Docker) / deploy/onprem/native/install.sh (native, --network none).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
VENDOR="$ROOT/deploy/onprem/vendor"
PY_IMAGE="python:3.12-slim-bookworm"
NODE_IMAGE="node:22-alpine"
BASE_IMAGES=("$PY_IMAGE" "nginx:1.27-alpine" "postgres:16-alpine")

# Git Bash (Windows) docker bağlama yollarını çevirmesin.
export MSYS_NO_PATHCONV=1

PBS_VERSION="3.12.14+20260924"
PBS_ASSET="cpython-3.12.14%2B20260924-x86_64-unknown-linux-gnu-pgo%2Blto-full.tar.zst"
PBS_SHA256="c88ec70ba0973eb61fcdd595c0dfd8c21e6f46151a4d2b1f9ad18c5058e490d9"
MSODBC_VERSION="18.7.1.1-1"

rm -rf "$VENDOR/wheels" "$VENDOR/debs" "$VENDOR/web-dist" "$VENDOR/requirements.lock" "$VENDOR/base-images.tar" "$VENDOR/native"
mkdir -p "$VENDOR/wheels" "$VENDOR/debs" "$VENDOR/web-dist" \
        "$VENDOR/native/python" "$VENDOR/native/rhel9/postgresql16" "$VENDOR/native/rhel9/nginx" "$VENDOR/native/rhel9/msodbcsql" \
        "$VENDOR/native/ubuntu2204/postgresql16" "$VENDOR/native/ubuntu2204/nginx" "$VENDOR/native/ubuntu2204/msodbcsql"

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
  apt-get install -y --no-install-recommends --download-only msodbcsql18=18.7.1.1-1 unixodbc libpq5
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

echo "==> [native] Taşınabilir Python $PBS_VERSION (python-build-standalone)"
# Hedefte zstd bulunacağı garanti değil (minimal kurulumlarda genelde yok) — burada (bu makinede, internetli)
# .tar.zst indirilip düz .tar.gz'ye yeniden paketleniyor; hedefte yalnızca `tar` (zaten zorunlu araç) yeterli.
docker run --rm -v "$VENDOR/native/python:/out" "$PY_IMAGE" sh -euc "
  apt-get update -qq && apt-get install -y -qq --no-install-recommends zstd curl ca-certificates >/dev/null
  curl -fsSL -o /tmp/py.tar.zst 'https://github.com/astral-sh/python-build-standalone/releases/download/20260924/$PBS_ASSET'
  echo '$PBS_SHA256  /tmp/py.tar.zst' | sha256sum -c -
  mkdir /tmp/py && tar --zstd -xf /tmp/py.tar.zst -C /tmp/py
  tar -czf /out/cpython-3.12.tar.gz -C /tmp/py python
"
sha256sum "$VENDOR/native/python/cpython-3.12.tar.gz" | awk '{print $1}' > "$VENDOR/native/python/cpython-3.12.tar.gz.sha256"
echo "$PBS_VERSION" > "$VENDOR/native/python/VERSION"

echo "==> [native] RHEL9 ailesi: PostgreSQL 16 (PGDG RPM)"
docker run --rm -v "$VENDOR/native/rhel9/postgresql16:/out" rockylinux:9 sh -euc '
  dnf -y -q module disable postgresql >/dev/null 2>&1 || true
  rpm --import https://download.postgresql.org/pub/repos/yum/keys/PGDG-RPM-GPG-KEY-RHEL
  cat > /etc/yum.repos.d/pgdg16.repo <<EOF
[pgdg16]
name=PostgreSQL 16 for RHEL 9 - x86_64
baseurl=https://download.postgresql.org/pub/repos/yum/16/redhat/rhel-9-x86_64
enabled=1
gpgcheck=1
gpgkey=https://download.postgresql.org/pub/repos/yum/keys/PGDG-RPM-GPG-KEY-RHEL
EOF
  dnf install -y -q --downloadonly --downloaddir=/out postgresql16-server postgresql16-contrib
'

echo "==> [native] RHEL9 ailesi: nginx (AppStream)"
docker run --rm -v "$VENDOR/native/rhel9/nginx:/out" rockylinux:9 sh -euc '
  dnf install -y -q --downloadonly --downloaddir=/out nginx
'

echo "==> [native] RHEL9 ailesi: msodbcsql18 + unixODBC (Microsoft resmi RPM deposu, $MSODBC_VERSION)"
docker run --rm -v "$VENDOR/native/rhel9/msodbcsql:/out" rockylinux:9 sh -euc "
  curl -fsSL -o /tmp/ms.rpm https://packages.microsoft.com/config/rhel/9/packages-microsoft-prod.rpm
  rpm -i /tmp/ms.rpm
  ACCEPT_EULA=Y dnf install -y -q --downloadonly --downloaddir=/out msodbcsql18-$MSODBC_VERSION unixODBC
  test \"\$(ls /out/msodbcsql18-*.rpm | wc -l)\" = 1
"

echo "==> [native] Ubuntu 22.04: PostgreSQL 16 (PGDG DEB)"
docker run --rm -v "$VENDOR/native/ubuntu2204/postgresql16:/out" ubuntu:22.04 sh -euc '
  export DEBIAN_FRONTEND=noninteractive
  mkdir -p /out/partial
  apt-get update -qq
  apt-get install -y -qq curl gnupg ca-certificates >/dev/null
  curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/keyrings/pgdg.asc
  echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq --download-only -o Dir::Cache::Archives=/out postgresql-16 postgresql-contrib-16
  rm -rf /out/partial /out/lock
'

echo "==> [native] Ubuntu 22.04: nginx (main)"
docker run --rm -v "$VENDOR/native/ubuntu2204/nginx:/out" ubuntu:22.04 sh -euc '
  export DEBIAN_FRONTEND=noninteractive
  mkdir -p /out/partial
  apt-get update -qq
  apt-get install -y -qq --download-only -o Dir::Cache::Archives=/out nginx
  rm -rf /out/partial /out/lock
'

echo "==> [native] Ubuntu 22.04: msodbcsql18 + unixodbc (Microsoft resmi DEB deposu, $MSODBC_VERSION)"
docker run --rm -v "$VENDOR/native/ubuntu2204/msodbcsql:/out" ubuntu:22.04 sh -euc "
  export DEBIAN_FRONTEND=noninteractive
  mkdir -p /out/partial
  apt-get update -qq
  apt-get install -y -qq curl >/dev/null
  curl -fsSL -O https://packages.microsoft.com/config/ubuntu/22.04/packages-microsoft-prod.deb
  dpkg -i packages-microsoft-prod.deb
  apt-get update -qq
  ACCEPT_EULA=Y apt-get install -y -qq --download-only -o Dir::Cache::Archives=/out msodbcsql18=$MSODBC_VERSION unixodbc
  rm -rf /out/partial /out/lock
  test \"\$(ls /out/msodbcsql18_*.deb | wc -l)\" = 1
"

echo ""
echo "Hazır: $VENDOR"
ls -la "$VENDOR"
echo "wheel: $(ls "$VENDOR/wheels" | wc -l)  deb: $(ls "$VENDOR/debs" | wc -l)  lock: $(wc -l < "$VENDOR/requirements.lock") satır"
echo "[native] python: $(ls "$VENDOR/native/python"/*.tar.gz 2>/dev/null | wc -l)" \
     "rhel9 rpm: $(ls "$VENDOR"/native/rhel9/*/*.rpm 2>/dev/null | wc -l)" \
     "ubuntu2204 deb: $(ls "$VENDOR"/native/ubuntu2204/*/*.deb 2>/dev/null | wc -l)"
