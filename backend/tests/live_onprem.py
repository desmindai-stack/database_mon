"""On-prem paketinin uçtan uca canlı testleri (Faz 31 Commit 8) — açma anahtarı.

`DBACE_TEST_ONPREM=1` ile açılır: Docker (privileged docker:dind), dolu `deploy/onprem/vendor/`
(`deploy/onprem/scripts/prepare-offline-artifacts.sh`) ve yükseltme testi için eski paketi derlerken internet
gerekir. Tanımlıyken bu testlerin atlanması oturumu KIRMIZI yapar (tests/conftest.py atlama denetimi).
"""

from __future__ import annotations

import os

ONPREM_TARGETS = ["dind"] if os.environ.get("DBACE_TEST_ONPREM", "").strip() == "1" else []
ONPREM_SKIP_REASON = (
    "On-prem paket testi kapalı. `DBACE_TEST_ONPREM=1` ile açın (Docker + dolu deploy/onprem/vendor gerekir)."
)
