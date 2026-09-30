#!/usr/bin/env bash
# Kurulumdan ÖNCE (ve install.sh'in kendi içinde de) sunucuyu tarar — Faz 32 Commit 11b
# (docs/ONPREM_NATIVE.md §8). Her kalemi PASS/WARN/FAIL olarak raporlar; herhangi bir FAIL varsa
# 1 ile çıkar ve install.sh BAŞLAMAZ. Yalnızca OKUR — hiçbir şeyi değiştirmez.
set -uo pipefail

STATUS=0
DISTRO_FAMILY=""

pass() { printf '  [PASS] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; STATUS=1; }
info() { printf '  [BİLGİ] %s\n' "$1"; }

echo "== dbace on-prem önkoşul denetimi =="

# --- Dağıtım + sürüm -----------------------------------------------------------------------------
if [ ! -r /etc/os-release ]; then
  fail "/etc/os-release okunamadı — dağıtım tespit edilemedi (RHEL9 ailesi / Ubuntu 22.04 bekleniyor)"
else
  . /etc/os-release
  case "${ID:-}" in
    rhel|rocky|almalinux)
      major="${VERSION_ID%%.*}"
      if [ "$major" = "9" ]; then
        DISTRO_FAMILY="rhel9"
        pass "dağıtım: $PRETTY_NAME (RHEL9 ailesi)"
      else
        fail "dağıtım: $PRETTY_NAME — desteklenen RHEL/Rocky/Alma yalnızca sürüm 9; kurulum için RHEL9 ailesi gerekir"
      fi
      ;;
    ubuntu)
      if [ "${VERSION_ID:-}" = "22.04" ]; then
        DISTRO_FAMILY="ubuntu2204"
        pass "dağıtım: $PRETTY_NAME"
      else
        fail "dağıtım: $PRETTY_NAME — desteklenen Ubuntu yalnızca 22.04 LTS; kurulum için Ubuntu 22.04 gerekir"
      fi
      ;;
    *)
      fail "dağıtım: ${PRETTY_NAME:-bilinmiyor} — desteklenmiyor (RHEL9 ailesi ya da Ubuntu 22.04 gerekir)"
      ;;
  esac
fi

# --- Mimari ----------------------------------------------------------------------------------------
arch="$(uname -m)"
if [ "$arch" = "x86_64" ]; then
  pass "mimari: x86_64"
else
  fail "mimari: $arch — vendor paketleri yalnızca x86_64; ARM/diğer mimarilerde kurulamaz"
fi

# --- systemd PID 1 -----------------------------------------------------------------------------
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  pass "systemd PID 1 olarak çalışıyor"
else
  fail "systemd çalışmıyor (ya da /run/systemd/system yok) — dbace.service kurulamaz, systemd gerekir"
fi

# --- Gerekli araçlar -------------------------------------------------------------------------------
for tool in tar useradd openssl systemctl awk sha256sum; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "gerekli araç var: $tool"
  else
    fail "eksik: $tool — kurulum için gerekli (paket: coreutils/shadow-utils/openssl/systemd, dağıtıma göre değişir)"
  fi
done

# --- Disk boş alan (bilgilendirici — docs/ONPREM_NATIVE.md §2'deki tabana göre) -------------------
if command -v df >/dev/null 2>&1; then
  avail_kb="$(df -Pk /opt 2>/dev/null | awk 'NR==2 {print $4}')"
  if [ -n "${avail_kb:-}" ]; then
    avail_gb=$((avail_kb / 1024 / 1024))
    if [ "$avail_gb" -lt 15 ]; then
      warn "/opt altında ${avail_gb} GB boş alan — 10 instance için önerilen taban 15 GB (docs/ONPREM_NATIVE.md §2)"
    else
      pass "/opt altında ${avail_gb} GB boş alan"
    fi
  fi
fi

# --- RAM (bilgilendirici) ---------------------------------------------------------------------------
if [ -r /proc/meminfo ]; then
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  mem_gb=$((mem_kb / 1024 / 1024))
  if [ "$mem_gb" -lt 4 ]; then
    warn "RAM ${mem_gb} GB — KURULUM.md'deki minimum 4 GB'nin altında"
  else
    pass "RAM ${mem_gb} GB"
  fi
fi

# --- Port çakışması ----------------------------------------------------------------------------------
HTTP_PORT="${HTTP_PORT:-8080}"
HTTPS_PORT="${HTTPS_PORT:-443}"
ALREADY_INSTALLED=0
if [ -L /opt/dbace/current ] || [ -d /opt/dbace/releases ]; then
  ALREADY_INSTALLED=1
fi
check_port_free() {
  port="$1"
  label="$2"
  if [ "$ALREADY_INSTALLED" = "1" ]; then
    info "port $port ($label) denetlenmedi — dbace zaten kurulu (yükseltmede kendi servisi zaten dinliyor olabilir)"
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
      fail "port $port ($label) zaten dinleniyor — HTTP_PORT/HTTPS_PORT .env'de değiştirin ya da o servisi durdurun"
    else
      pass "port $port ($label) boş"
    fi
  else
    info "port $port ($label) denetlenemedi (ss yok) — install.sh kendi bağlanma denemesiyle ayrıca denetleyecek"
  fi
}
check_port_free "$HTTP_PORT" "HTTP_PORT"
if [ "${TLS_MODE:-nginx}" = "nginx" ]; then
  check_port_free "$HTTPS_PORT" "HTTPS_PORT"
fi
check_port_free 5432 "meta PostgreSQL, yalnızca localhost"

# --- SELinux modu (bilgilendirici) --------------------------------------------------------------------
if command -v getenforce >/dev/null 2>&1; then
  info "SELinux modu: $(getenforce)"
elif [ "$DISTRO_FAMILY" = "rhel9" ]; then
  info "SELinux modu tespit edilemedi (getenforce yok — libselinux-utils kurulu olmayabilir)"
fi

# --- glibc sürümü (savunma amaçlı — taşınabilir Python'un tabanı) ------------------------------------
if command -v ldd >/dev/null 2>&1; then
  glibc_ver="$(ldd --version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+$' || true)"
  if [ -n "$glibc_ver" ]; then
    major="${glibc_ver%%.*}"; minor="${glibc_ver##*.}"
    if [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 17 ]; }; then
      pass "glibc $glibc_ver (taşınabilir Python'un tabanı 2.17+)"
    else
      warn "glibc $glibc_ver — taşınabilir Python 2.17+ hedefliyor, bu sürüm daha eski olabilir"
    fi
  fi
fi

# --- Zaten kurulu mu (bilgilendirici — kurulum mu yükseltme mi yolunu belirler) ------------------------
if [ -L /opt/dbace/current ] || [ -d /opt/dbace/releases ]; then
  info "dbace zaten kurulu görünüyor (/opt/dbace) — install.sh YÜKSELTME yolunu izleyecek"
else
  info "dbace kurulu değil — install.sh YENİ KURULUM yolunu izleyecek"
fi

echo "===================================="
if [ "$STATUS" -ne 0 ]; then
  echo "SONUÇ: en az bir FAIL var — kurulum BAŞLAMAYACAK. Yukarıdaki [FAIL] satırlarını düzeltip tekrar çalıştırın."
else
  echo "SONUÇ: kritik önkoşul eksiği yok."
fi
exit "$STATUS"
