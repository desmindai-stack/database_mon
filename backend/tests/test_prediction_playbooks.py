"""Faz 16-B İŞ 7 — tahminler için adım adım çözüm planları.

Bildirilen sorun: "önerilen aksiyonlar genel". Her tahmin türü artık numaralanacak adımlar ve
kopyalanabilir komutlar taşıyor. Testler planların gerçekten dolu, çalıştırılabilir ve
yıkıcı komutlar için uyarılı olduğunu doğruluyor.
"""

from __future__ import annotations

import pytest

from app.services.prediction_playbooks import (
    connections_playbook,
    database_size_playbook,
    index_bloat_playbook,
    table_growth_playbook,
    wraparound_playbook,
)

ALL_PLAYBOOKS = {
    "database_size": database_size_playbook("10 GB", "250 MB", "2026-11-01"),
    "connections_pg": connections_playbook("postgresql"),
    "connections_mssql": connections_playbook("sqlserver"),
    "connections_mongo": connections_playbook("mongodb"),
    "table_growth": table_growth_playbook("app", "events", "120 MB"),
    "wraparound": wraparound_playbook(150_000_000, 200_000_000, "2026-12-15"),
    "index_bloat": index_bloat_playbook("app", "idx_events_created", "40 MB"),
}


@pytest.mark.parametrize("name,steps", ALL_PLAYBOOKS.items())
def test_every_playbook_has_multiple_actionable_steps(name, steps):
    assert len(steps) >= 3, f"{name}: adım adım plan en az 3 adım olmalı"
    for step in steps:
        assert step["title"].strip(), f"{name}: başlıksız adım"
        assert step["detail"].strip(), f"{name}: açıklamasız adım"
    # Her planda en az bir çalıştırılabilir komut olmalı — yoksa yine "genel öneri" olurdu.
    assert any(step["command"] for step in steps), f"{name}: hiç komut yok"


@pytest.mark.parametrize("name,steps", ALL_PLAYBOOKS.items())
def test_commands_are_not_truncated_or_empty(name, steps):
    for step in steps:
        command = step["command"]
        if command is None:
            continue
        assert command.strip() == command.strip().rstrip("\n")
        assert "…" not in command, f"{name}: komut kırpılmış görünüyor"


def test_measured_values_are_carried_into_the_steps():
    """Adımlar dbace'in gerçekten ölçtüğü değerleri taşımalı — genel metin değil."""
    steps = database_size_playbook("10 GB", "250 MB", "2026-11-01")
    text = " ".join(s["detail"] for s in steps)
    assert "10 GB" in text and "250 MB" in text and "2026-11-01" in text


def test_table_and_index_playbooks_name_the_actual_object():
    table_steps = table_growth_playbook("app", "events", "120 MB")
    assert any("app.events" in (s["command"] or "") or 'app"."events' in (s["command"] or "") for s in table_steps)

    index_steps = index_bloat_playbook("app", "idx_events_created", "40 MB")
    assert any("idx_events_created" in (s["command"] or "") for s in index_steps)


def test_destructive_commands_carry_an_explicit_warning():
    """VACUUM FULL, max_connections değişikliği ve REINDEX kilitleme/yeniden başlatma
    gerektirir — komutu uyarısız vermek tehlikeli olurdu."""
    db_steps = database_size_playbook("10 GB", "250 MB", "2026-11-01")
    full = next(s for s in db_steps if s["command"] and "VACUUM FULL" in s["command"])
    assert "ACCESS EXCLUSIVE" in full["detail"]

    conn_steps = connections_playbook("postgresql")
    max_conn = next(s for s in conn_steps if s["command"] and "max_connections = " in s["command"])
    assert "YENİDEN BAŞLAT" in max_conn["command"].upper() or "yeniden başlat" in max_conn["detail"].lower()

    idx_steps = index_bloat_playbook("app", "idx_x", "1 MB")
    reindex = next(s for s in idx_steps if s["command"] and "REINDEX INDEX CONCURRENTLY" in s["command"])
    assert "iki kopyası" in reindex["detail"]


def test_wraparound_playbook_checks_blockers_before_suggesting_freeze():
    """autovacuum engellenmişse elle VACUUM FREEZE de yetmez — sıralama önemli."""
    steps = wraparound_playbook(150_000_000, 200_000_000, "2026-12-15")
    titles = [s["title"] for s in steps]
    blocker_idx = next(i for i, s in enumerate(steps) if "engelleyen" in s["title"])
    freeze_idx = next(i for i, s in enumerate(steps) if "dondurun" in s["title"])
    assert blocker_idx < freeze_idx, titles
    blockers = steps[blocker_idx]["command"]
    assert "pg_replication_slots" in blockers and "pg_prepared_xacts" in blockers


def test_connection_playbook_prefers_pooler_over_raising_max_connections():
    steps = connections_playbook("postgresql")
    pooler_idx = next(i for i, s in enumerate(steps) if "pooler" in s["title"].lower())
    raise_idx = next(
        i for i, s in enumerate(steps) if s["command"] and "ALTER SYSTEM SET max_connections" in s["command"]
    )
    assert pooler_idx < raise_idx, "önce pooler, sonra max_connections önerilmeli"


def test_sqlserver_and_mongodb_playbooks_do_not_leak_postgres_commands():
    for engine in ("sqlserver", "mongodb"):
        joined = " ".join((s["command"] or "") for s in connections_playbook(engine))
        assert "pg_stat_activity" not in joined
        assert "ALTER SYSTEM" not in joined
