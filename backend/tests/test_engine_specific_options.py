"""Proves the engine-specific connection knobs added for wizard İŞ 1 (PostgreSQL ssl_mode,
SQL Server auth_type, MongoDB replica_set/authSource) actually affect the connection built by
the collectors — not just decorative form fields that get stored and ignored.
"""

from __future__ import annotations

from app.collectors.base import ConnectionTarget
from app.collectors.sqlserver_mongodb import MongoDBCollector, build_odbc_connection_string


def test_windows_auth_uses_trusted_connection_and_omits_credentials():
    target = ConnectionTarget(
        host="h", port=1433, database="master", username="u", password="p", options={"auth_type": "windows"}
    )
    dsn = build_odbc_connection_string(target)
    assert "Trusted_Connection=yes" in dsn
    assert "UID=" not in dsn
    assert "PWD=" not in dsn


def test_sql_auth_is_the_default_and_uses_credentials():
    target = ConnectionTarget(host="h", port=1433, database="master", username="u", password="p", options={})
    dsn = build_odbc_connection_string(target)
    assert "UID=u;PWD=p" in dsn
    assert "Trusted_Connection" not in dsn


def test_mongo_uri_includes_replica_set_when_given():
    target = ConnectionTarget(
        host="h", port=27017, database="admin", username="u", password="p",
        options={"authSource": "admin", "replica_set": "rs0"},
    )
    collector = MongoDBCollector(target)
    uri = collector._build_uri()
    assert "authSource=admin" in uri
    assert "replicaSet=rs0" in uri


def test_mongo_uri_omits_replica_set_when_not_given():
    target = ConnectionTarget(
        host="h", port=27017, database="admin", username="u", password="p", options={"authSource": "admin"}
    )
    collector = MongoDBCollector(target)
    uri = collector._build_uri()
    assert "authSource=admin" in uri
    assert "replicaSet" not in uri
