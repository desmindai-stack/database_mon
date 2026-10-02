"""Topoloji sınıflandırması ve tek sunucuda cluster yığını kuralı (Faz 31 Commit 6).

Girdiler GERÇEK sunucuda ölçülen biçimde (services/server_topology.py açıklaması): pg_monitor'lu
rolün gördüğü satırlar, yetkisiz rolün NULL kolonları, koparılmış replikanın boş wal receiver'ı,
VIEW SERVER STATE'li login'in DMV satırları, yetkisiz login'in Msg 297 hatası. Canlı karşılıkları:
test_topology_live_postgres.py, test_topology_live_mssql.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import Instance
from app.services.cluster_health import merge_agent_into_services
from app.services.collection import runs_cluster_stack_probe
from app.services.server_topology import (
    CLUSTER_MEMORY,
    DEGRADED_CONFIRM_AFTER,
    DEGRADED_METRIC,
    KIND_CLUSTER,
    KIND_STANDALONE,
    KIND_UNMEASURED,
    MSSQL_GRANT,
    PG_GRANT,
    STATE_DEGRADED,
    STATE_HEALTHY,
    classify_postgresql,
    classify_sqlserver,
    expected_cluster,
    record_topology,
    topology_status,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
STREAMING = {"application_name": "dbace_replica", "state": "streaming", "sync_state": "async"}


def _instance(**kw) -> Instance:
    # topology_transient_disconnect_count: SQLAlchemy'nin Python-tarafı `default=0`'ı yalnızca INSERT'te
    # uygulanır — session'a hiç flush edilmeyen bu testlerde elle verilmezse None kalır (DB'den yüklenmiş
    # gerçek bir satırda bu DEĞER HER ZAMAN 0'dır, NOT NULL DEFAULT 0 — bkz. migration).
    kw.setdefault("topology_transient_disconnect_count", 0)
    return Instance(name="x", engine=kw.pop("engine", "postgresql"), host="h", port=5432, database="d",
                    username="u", password="p", **kw)


@pytest.mark.parametrize("facts,expected,kind,state,role,grant", [
    ({"in_recovery": False, "replication": [], "wal_receiver": []}, False, KIND_STANDALONE, None, None, None),
    ({"in_recovery": False, "replication": [STREAMING], "wal_receiver": []}, False, KIND_CLUSTER, STATE_HEALTHY, "primary", None),
    ({"in_recovery": False, "replication": [{**STREAMING, "state": "catchup"}], "wal_receiver": []}, False,
     KIND_CLUSTER, STATE_DEGRADED, "primary", None),
    # koparılmış replika, birincilden bakınca: beklenen cluster ise bozuk, değilse tek sunucu
    ({"in_recovery": False, "replication": [], "wal_receiver": []}, True, KIND_CLUSTER, STATE_DEGRADED, "primary", None),
    ({"in_recovery": True, "replication": [], "wal_receiver": [{"status": "streaming", "sender_host": "p"}]}, False,
     KIND_CLUSTER, STATE_HEALTHY, "replica", None),
    ({"in_recovery": True, "replication": [], "wal_receiver": []}, False, KIND_CLUSTER, STATE_DEGRADED, "replica", None),
    # pg_read_all_stats olmadan ölçülen biçim: satır var, durum NULL
    ({"in_recovery": False, "replication": [{"application_name": "dbace_replica", "state": None, "sync_state": None}],
      "wal_receiver": []}, False, KIND_UNMEASURED, None, "primary", PG_GRANT),
    ({"in_recovery": True, "replication": [], "wal_receiver": [{"status": None, "sender_host": None}]}, False,
     KIND_UNMEASURED, None, "replica", PG_GRANT),
    ({"error": "permission denied for function pg_is_in_recovery"}, False, KIND_UNMEASURED, None, None, PG_GRANT),
    ({"error": "canceling statement due to statement timeout"}, True, KIND_UNMEASURED, None, None, None),
])
def test_postgresql_classification(facts, expected, kind, state, role, grant):
    obs = classify_postgresql(facts, expected=expected)
    assert (obs.kind, obs.state, obs.role, obs.required_grant) == (kind, state, role, grant), obs.reason
    assert obs.reason


AG_OK = [
    {"replica_server_name": "a", "role_desc": "PRIMARY", "connected_state_desc": "CONNECTED",
     "synchronization_health_desc": "HEALTHY", "is_local": True},
    {"replica_server_name": "b", "role_desc": "SECONDARY", "connected_state_desc": "CONNECTED",
     "synchronization_health_desc": "HEALTHY", "is_local": False},
]


@pytest.mark.parametrize("facts,expected,kind,state,grant", [
    ({"hadr_enabled": False, "replicas": None, "error": None}, False, KIND_STANDALONE, None, None),
    ({"hadr_enabled": True, "replicas": [], "error": None}, False, KIND_STANDALONE, None, None),
    ({"hadr_enabled": True, "replicas": AG_OK, "error": None}, False, KIND_CLUSTER, STATE_HEALTHY, None),
    ({"hadr_enabled": True, "replicas": [AG_OK[0], {**AG_OK[1], "connected_state_desc": "DISCONNECTED",
                                                     "synchronization_health_desc": "NOT_HEALTHY"}], "error": None},
     False, KIND_CLUSTER, STATE_DEGRADED, None),
    ({"hadr_enabled": True, "replicas": None,
      "error": "The user does not have permission to perform this action. (297)"}, False, KIND_UNMEASURED, None, MSSQL_GRANT),
    ({"hadr_enabled": None, "replicas": None, "error": "Login timeout expired"}, True, KIND_UNMEASURED, None, None),
    ({"hadr_enabled": False, "replicas": None, "error": None}, True, KIND_CLUSTER, STATE_DEGRADED, None),
])
def test_sqlserver_classification(facts, expected, kind, state, grant):
    obs = classify_sqlserver(facts, expected=expected)
    assert (obs.kind, obs.state, obs.required_grant) == (kind, state, grant), obs.reason


def test_alert_metric_exists_only_for_clusters():
    """record_topology() yalnızca cluster'da metrik döner — standalone/unmeasured'da hiç yok (alarm
    motoru metriği olmayan kuralı zaten değerlendirmiyor, ama burada da doğrulanıyor)."""
    standalone = _instance()
    assert record_topology(standalone, classify_postgresql(
        {"in_recovery": False, "replication": [], "wal_receiver": []}, expected=False), now=NOW) == {}
    unmeasured = _instance()
    assert record_topology(unmeasured, classify_postgresql(
        {"error": "permission denied"}, expected=True), now=NOW) == {}


def test_a_single_degraded_measurement_does_not_alarm_but_is_not_silent_either():
    """Faz 32 Commit 12c — CLAUDE.md: canlı testlerde (ve ÜRÜNDE) süre eşiği yerine mekanizma; TEK
    ölçümle alarm YOK. Gerçek PostgreSQL'de ölçülen (Commit 10g), kendiliğinden-iyileşen 1-4 sn'lik
    wal_receiver boşalmasının YANLIŞLIKLA alarm üretmemesi gerekiyordu — bu, o ürün tarafı garantisi."""
    inst = _instance()
    degraded = classify_postgresql({"in_recovery": True, "replication": [], "wal_receiver": []}, expected=False)
    flags = record_topology(inst, degraded, now=NOW)
    assert flags == {DEGRADED_METRIC: 0.0}, "ilk ölçümde HENÜZ alarm üretilmemeli"
    assert inst.topology_degraded_since == NOW
    assert "Geçici bir kopma olabilir" in inst.topology_reason


def test_negative_control_a_transient_disconnect_that_heals_in_time_never_alarms_but_is_counted():
    """Negatif kontrol (görev 2d, madde 2): eşiğe ulaşmadan iyileşen kopma HİÇBİR ölçümde alarm
    üretmez, ama DBA'nın görebilmesi için SAYILIR (topology_transient_disconnect_count)."""
    inst = _instance()
    degraded = classify_postgresql({"in_recovery": True, "replication": [], "wal_receiver": []}, expected=False)
    healthy = classify_postgresql(
        {"in_recovery": True, "replication": [], "wal_receiver": [{"status": "streaming", "sender_host": "p"}]},
        expected=False,
    )
    assert record_topology(inst, degraded, now=NOW) == {DEGRADED_METRIC: 0.0}
    assert record_topology(inst, degraded, now=NOW + timedelta(seconds=10)) == {DEGRADED_METRIC: 0.0}
    assert inst.topology_transient_disconnect_count == 0, "henüz iyileşmedi — sayılmamalı"
    # DEGRADED_CONFIRM_AFTER (60 sn) dolmadan İYİLEŞTİ.
    flags = record_topology(inst, healthy, now=NOW + timedelta(seconds=30))
    assert flags == {DEGRADED_METRIC: 0.0}
    assert inst.topology_transient_disconnect_count == 1, "eşiğe ulaşmadan iyileşen kopma SAYILMALI"
    assert inst.topology_degraded_since is None


def test_negative_control_a_genuinely_broken_replica_eventually_alarms():
    """Negatif kontrol (görev 2d, madde 1): kopma eşiği AŞARSA (burada: DEGRADED_CONFIRM_AFTER kadar
    sürerse) gerçekten alarm üretilir — hysteresis sonsuza kadar ERTELEMİYOR."""
    inst = _instance()
    degraded = classify_postgresql({"in_recovery": True, "replication": [], "wal_receiver": []}, expected=False)
    assert record_topology(inst, degraded, now=NOW) == {DEGRADED_METRIC: 0.0}
    assert record_topology(inst, degraded, now=NOW + timedelta(seconds=30)) == {DEGRADED_METRIC: 0.0}
    confirmed = record_topology(inst, degraded, now=NOW + DEGRADED_CONFIRM_AFTER)
    assert confirmed == {DEGRADED_METRIC: 1.0}, "eşik AŞILDIKTAN sonra gerçek alarm üretilmeli"
    assert inst.topology_transient_disconnect_count == 0, "bu asla iyileşmedi — geçici SAYILMAMALI"


def test_negative_control_wal_already_removed_alarms_immediately_with_a_distinct_reason():
    """Negatif kontrol (görev 2d, madde 3): birincilin slotu wal_status=lost diyorsa bu KESİN kanıt —
    grace period beklenmeden hemen alarm, ve gerekçe genel 'bağlı değil' mesajından AYRI/belirgin."""
    inst = _instance()
    observation = classify_postgresql(
        {"in_recovery": False, "replication": [], "slots": [{"slot_name": "dbace_replica_slot", "wal_status": "lost"}]},
        expected=True,
    )
    assert observation.confirmed_immediately
    assert "SİLİNMİŞ" in observation.reason and "pg_basebackup" in observation.reason
    flags = record_topology(inst, observation, now=NOW)
    assert flags == {DEGRADED_METRIC: 1.0}, "WAL kesin kanıtla silinmişse İLK ölçümde bile alarm üretilmeli"
    assert "SİLİNMİŞ" in inst.topology_reason, "gerçek gerekçe genel mesajla EZİLMEMELİ"


def test_a_plain_disconnect_without_a_lost_slot_still_waits_for_confirmation():
    """Slot yoksa (ya da wal_status 'lost' değilse) — belirsiz durum, hâlâ grace period bekleniyor."""
    inst = _instance()
    observation = classify_postgresql(
        {"in_recovery": False, "replication": [], "slots": [{"slot_name": "dbace_replica_slot", "wal_status": "reserved"}]},
        expected=True,
    )
    assert not observation.confirmed_immediately
    assert record_topology(inst, observation, now=NOW) == {DEGRADED_METRIC: 0.0}


def test_cluster_memory_window_and_group_topology():
    inst = _instance()
    assert not expected_cluster(inst, None, now=NOW)
    assert expected_cluster(inst, "patroni", now=NOW) and expected_cluster(inst, "alwayson", now=NOW)
    record_topology(inst, classify_postgresql({"in_recovery": False, "replication": [STREAMING], "wal_receiver": []},
                                              expected=False), now=NOW)
    assert expected_cluster(inst, "standalone", now=NOW + CLUSTER_MEMORY - timedelta(minutes=1))
    assert not expected_cluster(inst, "standalone", now=NOW + CLUSTER_MEMORY + timedelta(minutes=1))


def test_stale_topology_is_unmeasured_not_standalone():
    inst = _instance()
    assert topology_status(inst, now=NOW)["kind"] == KIND_UNMEASURED
    record_topology(inst, classify_postgresql({"in_recovery": False, "replication": [], "wal_receiver": []},
                                              expected=False), now=NOW)
    assert topology_status(inst, now=NOW)["kind"] == KIND_STANDALONE
    assert topology_status(inst, now=NOW + timedelta(hours=2))["kind"] == KIND_UNMEASURED


@pytest.mark.parametrize("fields,runs", [
    ({"cluster_name": "", "services": []}, False),                       # form, tek sunucu
    ({"cluster_name": None, "services": ["postgresql"]}, False),         # sihirbaz, tek sunucu
    ({"cluster_name": "grup", "services": ["postgresql"]}, False),       # düğüm ekleme, tek sunucu
    ({"cluster_name": "pg-prod", "services": []}, True),                 # Patroni, varsayılan yığın
    ({"cluster_name": "pg-prod", "services": ["patroni", "etcd"]}, True),
    ({"engine": "sqlserver", "cluster_name": "ag1", "services": ["sqlserver"]}, False),
])
def test_cluster_stack_probe_only_for_configured_patroni_stacks(fields, runs):
    assert runs_cluster_stack_probe(_instance(**fields)) is runs


def test_agent_cannot_turn_a_skipped_or_open_port_service_into_down():
    """Tek sunuculu systemd'de agent'ın GERÇEK yanıtı: olmayan birimler `inactive`."""
    agent = {"services": {name: {"active": "inactive"} for name in ("etcd", "patroni", "postgresql", "keepalived", "haproxy")},
             "keepalived": {}, "agent_ok": True}
    services = [
        {"service": "patroni", "status": "skipped", "detail": "", "source": "probe"},
        {"service": "postgresql", "status": "up", "detail": "TCP open", "source": "probe"},
    ]
    merged = {s["service"]: s["status"] for s in merge_agent_into_services(services, agent)}
    assert merged == {"patroni": "skipped", "postgresql": "up"}
    # Gerçekten yapılandırılmış ve probu da düşmüş servis hâlâ down.
    down = merge_agent_into_services([{"service": "patroni", "status": "down", "detail": "", "source": "probe"}], agent)
    assert down[0]["status"] == "down"
