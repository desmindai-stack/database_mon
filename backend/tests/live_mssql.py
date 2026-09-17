"""Canlı SQL Server testlerinin ortak kurulumu (Faz 31 Commit 6). Konteynerler: scripts/live_mssql.py.

Hedefler tanımlıyken canlı SQL Server testi sürüm koşulu dışında atlanırsa oturum kırmızı
(tests/conftest.py atlama denetimi — PostgreSQL ile aynı kural).
"""

from __future__ import annotations

import os

from app.collectors.base import ConnectionTarget

MSSQL_TARGETS = {
    kind: value.strip()
    for kind, value in (
        ("standalone", os.environ.get("DBACE_TEST_MSSQL_STANDALONE", "")),
        ("ag", os.environ.get("DBACE_TEST_MSSQL_AG", "")),
    )
    if value.strip()
}
MSSQL_SKIP_REASON = (
    "Gerçek SQL Server yok. `python scripts/live_mssql.py up` ile konteynerleri kurup yazdırdığı "
    "DBACE_TEST_MSSQL_* değerlerini tanımlayın."
)
LOGINS = {"ro": ("dbace_ro", "Dbace!Ro_pw1"), "noperm": ("dbace_noperm", "Dbace!No_pw1")}


def odbc_options() -> dict:
    driver = os.environ.get("DBACE_TEST_MSSQL_ODBC_DRIVER", "").strip()
    if not driver or driver == "ODBC Driver 18 for SQL Server":
        return {}
    # Windows'un eski "SQL Server" sürücüsü TLS 1.2 şifrelemeyi desteklemiyor (ölçüldü).
    return {"odbc_driver": driver, "encrypt": False}


def mssql_target(kind: str, login: str) -> ConnectionTarget:
    host, port = MSSQL_TARGETS[kind].split(",")
    user, password = LOGINS[login]
    return ConnectionTarget(host=host, port=int(port), database="master", username=user, password=password,
                            options=odbc_options())
