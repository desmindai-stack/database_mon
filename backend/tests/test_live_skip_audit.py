"""Canlı test atlama denetiminin KENDİSİ (Faz 31 Commit 5, tests/conftest.py).

Gerçek pytest alt süreçte, gerçek conftest ile koşuyor; DSN tanımlı (bağlantı kurulmuyor — prob
testleri yalnızca atlıyor). Denetim:
- sürüm dışı atlama → oturum kırmızı (negatif kontrol),
- gerçek sürüm koşulu → yeşil,
- sürüm koşulu gibi YAZILMIŞ ama koşulu tutmayan atlama → kırmızı,
- DSN tanımlıyken canlı OLMAYAN testin atlanması → denetimin konusu değil, yeşil,
- DSN tanımsızken canlı testin atlanması → yeşil (yerel geliştirme).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

LIVE_HEADER = "import pytest\nfrom tests.live_pg import LIVE_DSNS, skip_below_version\n\n"


def _run(tmp_path: Path, body: str, *, dsn: str | None = "postgresql://u:p@127.0.0.1:1/x") -> subprocess.CompletedProcess:
    probe = TESTS / f"test_zz_skip_probe_{uuid.uuid4().hex[:8]}.py"
    probe.write_text(body, encoding="utf-8")
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{(tmp_path / 'probe.db').as_posix()}",
           "PYTHONIOENCODING": "utf-8"}
    env.pop("DBACE_TEST_PG_DSN", None)
    if dsn:
        env["DBACE_TEST_PG_DSN"] = dsn
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider", "-rs"],
            cwd=TESTS.parent, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    finally:
        probe.unlink(missing_ok=True)


def test_negative_control_environment_skip_turns_the_session_red(tmp_path):
    out = _run(tmp_path, LIVE_HEADER + "def test_live():\n    assert LIVE_DSNS\n    pytest.skip('docker CLI yok')\n")
    assert out.returncode == 1, out.stdout
    assert "CANLI TEST ATLANDI" in out.stdout and "docker CLI yok" in out.stdout


def test_version_condition_skip_is_allowed(tmp_path):
    out = _run(tmp_path, LIVE_HEADER + "def test_live():\n    skip_below_version(150019, 160000, 'GENERIC_PLAN')\n")
    assert out.returncode == 0, out.stdout
    assert "1 skipped" in out.stdout


def test_forged_version_text_whose_condition_does_not_hold_is_red(tmp_path):
    out = _run(tmp_path, LIVE_HEADER + "def test_live():\n    pytest.skip('sürüm koşulu: sunucu 170011 < 160000 — uydurma')\n")
    assert out.returncode == 1, out.stdout


def test_skipif_marker_on_a_live_module_is_audited_too(tmp_path):
    out = _run(tmp_path, LIVE_HEADER + "pytestmark = pytest.mark.skipif(True, reason='ortam eksik')\n\ndef test_live():\n    pass\n")
    assert out.returncode == 1, out.stdout


def test_non_live_skip_is_not_audited(tmp_path):
    out = _run(tmp_path, "import pytest\n\ndef test_static():\n    pytest.skip('dinamik hedef')\n")
    assert out.returncode == 0, out.stdout


def test_without_dsn_live_skips_are_fine(tmp_path):
    body = LIVE_HEADER + "pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason='DSN yok')\n\ndef test_live():\n    pass\n"
    out = _run(tmp_path, body, dsn=None)
    assert out.returncode == 0, out.stdout


@pytest.mark.parametrize("reason,disallowed", [
    ("sürüm koşulu: sunucu 150019 < 160000 — GENERIC_PLAN", False),
    ("sürüm koşulu: sunucu 160015 < 160000 — x", True),
    ("Skipped: docker CLI yok", True),
    ("", True),
])
def test_reason_classification(reason, disallowed):
    from tests.live_pg import disallowed_live_skip

    assert disallowed_live_skip(reason) is disallowed
