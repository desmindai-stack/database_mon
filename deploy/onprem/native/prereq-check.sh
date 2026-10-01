#!/usr/bin/env bash
# shellcheck disable=SC1091
# Scans the server BEFORE install (and from inside install.sh itself) - Phase 32 Commit 11b
# (docs/ONPREM_NATIVE.md section 8). Reports each item as PASS/WARN/FAIL; exits 1 and blocks
# install.sh if any FAIL is found. READ-ONLY - never changes anything.
set -uo pipefail

STATUS=0
DISTRO_FAMILY=""

pass() { printf '  [PASS] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; STATUS=1; }
info() { printf '  [INFO] %s\n' "$1"; }

echo "== dbace on-prem prerequisite check =="

# --- Distribution + version -------------------------------------------------------------------
if [ ! -r /etc/os-release ]; then
  fail "cannot read /etc/os-release - distribution could not be detected (RHEL9 family / Ubuntu 22.04 expected)"
else
  . /etc/os-release
  case "${ID:-}" in
    rhel|rocky|almalinux)
      major="${VERSION_ID%%.*}"
      if [ "$major" = "9" ]; then
        DISTRO_FAMILY="rhel9"
        pass "distribution: $PRETTY_NAME (RHEL9 family)"
      else
        fail "distribution: $PRETTY_NAME - only RHEL/Rocky/Alma version 9 is supported; install requires the RHEL9 family"
      fi
      ;;
    ubuntu)
      if [ "${VERSION_ID:-}" = "22.04" ]; then
        DISTRO_FAMILY="ubuntu2204"
        pass "distribution: $PRETTY_NAME"
      else
        fail "distribution: $PRETTY_NAME - only Ubuntu 22.04 LTS is supported; install requires Ubuntu 22.04"
      fi
      ;;
    *)
      fail "distribution: ${PRETTY_NAME:-unknown} - not supported (RHEL9 family or Ubuntu 22.04 required)"
      ;;
  esac
fi

# --- Architecture ----------------------------------------------------------------------------
arch="$(uname -m)"
if [ "$arch" = "x86_64" ]; then
  pass "architecture: x86_64"
else
  fail "architecture: $arch - vendor packages are x86_64 only; cannot install on ARM/other architectures"
fi

# --- systemd PID 1 -----------------------------------------------------------------------------
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
  pass "systemd is running as PID 1"
else
  fail "systemd is not running (or /run/systemd/system is missing) - dbace.service cannot be installed, systemd is required"
fi

# --- Required tools --------------------------------------------------------------------------
for tool in tar useradd openssl systemctl awk sha256sum; do
  if command -v "$tool" >/dev/null 2>&1; then
    pass "required tool present: $tool"
  else
    fail "missing: $tool - required for install (package: coreutils/shadow-utils/openssl/systemd, varies by distro)"
  fi
done

# --- Free disk space (informational - see the baseline table in docs/ONPREM_NATIVE.md section 2) ----
if command -v df >/dev/null 2>&1; then
  avail_kb="$(df -Pk /opt 2>/dev/null | awk 'NR==2 {print $4}')"
  if [ -n "${avail_kb:-}" ]; then
    avail_gb=$((avail_kb / 1024 / 1024))
    if [ "$avail_gb" -lt 15 ]; then
      warn "${avail_gb} GB free under /opt - recommended baseline for 10 instances is 15 GB (docs/ONPREM_NATIVE.md section 2)"
    else
      pass "${avail_gb} GB free under /opt"
    fi
  fi
fi

# --- RAM (informational) -----------------------------------------------------------------------
if [ -r /proc/meminfo ]; then
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  mem_gb=$((mem_kb / 1024 / 1024))
  if [ "$mem_gb" -lt 4 ]; then
    warn "RAM ${mem_gb} GB - below the 4 GB minimum documented in KURULUM.md"
  else
    pass "RAM ${mem_gb} GB"
  fi
fi

# --- Port conflicts ----------------------------------------------------------------------------
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
    info "port $port ($label) not checked - dbace is already installed (its own service may already be listening on an upgrade)"
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
      fail "port $port ($label) is already in use - change HTTP_PORT/HTTPS_PORT in .env or stop that service"
    else
      pass "port $port ($label) is free"
    fi
  else
    info "port $port ($label) not checked (ss unavailable) - install.sh will also check this via its own bind attempt"
  fi
}
check_port_free "$HTTP_PORT" "HTTP_PORT"
if [ "${TLS_MODE:-nginx}" = "nginx" ]; then
  check_port_free "$HTTPS_PORT" "HTTPS_PORT"
fi
check_port_free 5432 "meta PostgreSQL, localhost only"

# --- SELinux mode (informational) ---------------------------------------------------------------
if command -v getenforce >/dev/null 2>&1; then
  info "SELinux mode: $(getenforce)"
elif [ "$DISTRO_FAMILY" = "rhel9" ]; then
  info "SELinux mode could not be detected (getenforce missing - libselinux-utils may not be installed)"
fi

# --- glibc version (defensive - the portable Python's baseline) ---------------------------------
if command -v ldd >/dev/null 2>&1; then
  glibc_ver="$(ldd --version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+$' || true)"
  if [ -n "$glibc_ver" ]; then
    major="${glibc_ver%%.*}"; minor="${glibc_ver##*.}"
    if [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 17 ]; }; then
      pass "glibc $glibc_ver (the portable Python's baseline is 2.17+)"
    else
      warn "glibc $glibc_ver - the portable Python targets 2.17+, this version may be older"
    fi
  fi
fi

# --- Already installed? (informational - determines fresh install vs. upgrade) ------------------
if [ -L /opt/dbace/current ] || [ -d /opt/dbace/releases ]; then
  info "dbace already appears to be installed (/opt/dbace) - install.sh will take the UPGRADE path"
else
  info "dbace is not installed - install.sh will take the FRESH INSTALL path"
fi

echo "===================================="
if [ "$STATUS" -ne 0 ]; then
  echo "RESULT: at least one FAIL - install will NOT start. Fix the [FAIL] lines above and run again."
else
  echo "RESULT: no critical prerequisites missing."
fi
exit "$STATUS"
