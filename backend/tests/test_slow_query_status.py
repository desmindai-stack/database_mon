"""Faz 16-B İŞ 1 — "yavaş sorgu verisi neden yok?" tek kaynağı.

Bildirilen hata: Ön koşullar paneli pg_stat_statements'ı "var" gösterirken DPA "Yavaş sorgu
verisi yok, eklentiyi kurun" diyordu. Bu testler, iki tarafın da aynı probe'dan beslendiğini ve
her farklı sebebin ayrı bir mesaj ürettiğini kanıtlıyor — özellikle "eklenti var ama veri yok"
durumunda ARTIK "eklentiyi kurun" denmediğini.
"""

from __future__ import annotations

import pytest

from app.services.pgss import PgStatStatementsProbe, probe_pg_stat_statements, qualified_view
from app.services.prerequisites import pg_stat_statements_checks
from app.services.slow_query_status import _pg_status
from tests.fakes import FakeAsyncConnection


def _probe(**over) -> PgStatStatementsProbe:
    base = dict(
        installed=True,
        schema="public",
        reachable=True,
        preloaded=True,
        preload_raw="pg_stat_statements",
        track="top",
        privileged=True,
        total_rows=25,
        redacted_rows=0,
    )
    base.update(over)
    return PgStatStatementsProbe(**base)


def test_extension_present_but_no_rows_does_not_say_install_extension():
    """Asıl bildirilen çelişki: eklenti kuruluyken "CREATE EXTENSION" önerilmesi."""
    status, title, message, fix = _pg_status(_probe(total_rows=0), stored=0)

    assert status == "no_data_yet"
    assert "CREATE EXTENSION" not in (fix or "")
    assert "CREATE EXTENSION" not in message
    assert "henüz" in message.lower() or "henüz" in title.lower()


def test_missing_extension_is_the_only_case_that_says_create_extension():
    status, _title, _message, fix = _pg_status(_probe(installed=False, schema=None, reachable=False), stored=0)

    assert status == "extension_missing"
    assert "CREATE EXTENSION" in fix


def test_restricted_visibility_reported_separately_from_missing():
    status, title, message, fix = _pg_status(
        _probe(privileged=False, total_rows=40, redacted_rows=39), stored=0
    )

    assert status == "restricted_visibility"
    assert "sadece kendi sorgularınızı" in title.lower() or "kendi sorgularınızı" in title
    assert "pg_read_all_stats" in fix
    assert "39" in message


def test_server_has_rows_but_worker_has_not_stored_any():
    status, _title, message, _fix = _pg_status(_probe(total_rows=12), stored=0)

    assert status == "not_collected_yet"
    assert "worker" in message.lower()


def test_not_preloaded_beats_no_data_message():
    """Preload yoksa tablo her zaman boş kalır — "veri birikmedi" demek yanıltıcı olurdu."""
    status, _title, _message, fix = _pg_status(_probe(preloaded=False, total_rows=0), stored=0)

    assert status == "not_preloaded"
    assert "shared_preload_libraries" in fix


def test_track_off_reported_before_empty_data():
    status, _t, _m, fix = _pg_status(_probe(track="none", total_rows=0), stored=0)

    assert status == "track_off"
    assert "pg_stat_statements.track" in fix


def test_ok_with_restricted_visibility_still_warns_about_scope():
    status, title, message, _fix = _pg_status(_probe(privileged=False, redacted_rows=5), stored=100)

    assert status == "ok"
    assert "kısıtlı" in title
    assert "maskeli" in message


@pytest.mark.parametrize(
    "probe_kwargs,expected_prereq_status,expected_availability_status",
    [
        (dict(installed=False, schema=None, reachable=False), "missing", "extension_missing"),
        (dict(privileged=False, total_rows=40, redacted_rows=39), "ok", "restricted_visibility"),
        (dict(total_rows=0), "ok", "no_data_yet"),
    ],
)
def test_prerequisite_panel_and_dpa_message_never_contradict(
    probe_kwargs, expected_prereq_status, expected_availability_status
):
    """İki taraf da aynı probe nesnesinden türüyor: eklenti kontrolü "ok" ise availability asla
    "extension_missing" diyemez, "missing" ise asla "ok" diyemez."""
    probe = _probe(**probe_kwargs)

    prereq = next(c for c in pg_stat_statements_checks(probe) if c.key == "pg_stat_statements")
    status, _t, _m, _f = _pg_status(probe, stored=0)

    assert prereq.status == expected_prereq_status
    assert status == expected_availability_status
    assert (prereq.status == "ok") == (status != "extension_missing")


async def test_probe_qualifies_view_with_extension_schema():
    """Supabase eklentiyi `extensions` şemasına kurar; probe (ve collector) view'ı nitelemeli."""
    conn = FakeAsyncConnection(
        {
            "WHERE e.extname = $1": "extensions",
            "SHOW shared_preload_libraries": "pg_stat_statements",
            "SHOW pg_stat_statements.track": "all",
            "pg_has_role(current_user, 'pg_read_all_stats'": True,
            'FROM "extensions".pg_stat_statements': {"total": 7, "redacted": 0},
        }
    )

    probe = await probe_pg_stat_statements(conn)

    assert probe.installed and probe.reachable
    assert probe.schema == "extensions"
    assert probe.total_rows == 7
    assert any('"extensions".pg_stat_statements' in q for q in conn.queries)


def test_qualified_view_quotes_schema():
    assert qualified_view("extensions", "pg_stat_statements") == '"extensions".pg_stat_statements'
    assert qualified_view(None, "pg_stat_statements") == "pg_stat_statements"


async def test_collector_drops_rows_whose_query_text_is_masked():
    """Ayrıcalıksız rol için pg_stat_statements başka kullanıcıların satırlarını
    '<insufficient privilege>' ile maskeler; bunları saklamak hem faydasız hem de
    SlowQuerySample.query NOT NULL kısıtını zorlar."""
    from app.collectors.base import ConnectionTarget
    from app.collectors.postgresql import PostgreSQLCollector

    conn = FakeAsyncConnection(
        {
            "current_setting('server_version_num')": {"num": 160_000, "txt": "PostgreSQL 16"},
            "WHERE e.extname = $1": "public",
            'FROM "public".pg_stat_statements': [
                {"queryid": "1", "query": "SELECT 1", "calls": 3},
                {"queryid": None, "query": "<insufficient privilege>", "calls": 9},
                {"queryid": "2", "query": None, "calls": 4},
            ],
        }
    )
    collector = PostgreSQLCollector(
        ConnectionTarget(host="h", port=5432, database="d", username="u", password="p")
    )

    async def _connect():
        return conn

    collector._connect = _connect  # type: ignore[method-assign]

    rows = await collector.collect_slow_queries(limit=10)

    assert [r["queryid"] for r in rows] == ["1"]
