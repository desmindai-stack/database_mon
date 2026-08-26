"""Runs before any test module is collected/imported — guarantees DATABASE_URL points at a
dedicated test SQLite file (not the real dev data/dbace.db) before app.config.Settings() gets
instantiated by whichever test module happens to import app.database/app.config first."""

from __future__ import annotations

import os
from pathlib import Path

_TEST_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "dbace_pytest.db"
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_PATH}")
