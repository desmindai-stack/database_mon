"""Sunucunun GERÇEK topolojisi: tek sunucu / cluster (sağlıklı|bozuk) / ölçülemedi (Faz 31 Commit 6).

## Çözdüğü hata (gerçek sunucuda yeniden üretildi)

Tek sunucuda "cluster down" alarmı çıkıyordu. Topoloji hiçbir yerde ÖLÇÜLMÜYORDU; yapılandırma
alanlarından tahmin ediliyordu:

1. Sihirbaz ve düğüm ekleme, standalone grupta da `services=[motor]` (düğüm yolu ayrıca
   `cluster_name=grup adı`) yazıyor; toplama `cluster_name or services` doluysa Patroni yığını
   probunu çalıştırıyor ve cluster alarm kurallarını ekliyordu.
2. Tek sunucuya plan yakalama için kurulan host-agent, sunucuda OLMAYAN patroni/etcd/haproxy/
   keepalived birimleri için `inactive` döndürüyor (systemd'li Debian'da ölçüldü) ve birleştirme
   "skipped" servisleri — açık TCP portunu bile — `down` yapıyordu. Sonuç: 5 alarm ("Patroni down",
   "etcd unhealthy", "HAProxy down", "Keepalived VIP unreachable", "Cluster services down"),
   PostgreSQL 17 ve SQL Server 2022'de aynı.

"Cluster yapılandırılmamış" ile "cluster erişilemiyor" arasındaki ayrım hiç yoktu: probun göremediği
her şey `down` sayılıyordu.

## Ölçülen salt okunur kaynaklar (15.19/16.15/17.11, SQL Server 16.0.4275)

**PostgreSQL** — `pg_is_in_recovery()`, `pg_stat_replication`, `pg_stat_wal_receiver`:
- pg_monitor rolü: birincilde replika satırı `state=streaming`, replikada `status=streaming`.
- İzleme yetkisi OLMAYAN rol: satır görünüyor ama `state`/`status` NULL → ölçülemedi (pg_monitor).
- Replika koparılınca: birincilde satır yok (tek sunucudan AYIRT EDİLEMEZ → aşağıdaki "beklenen
  cluster" kuralı), replikada `pg_stat_wal_receiver` satırı yok.
- Patroni REST: tespitin parçası değil (tek sunucuda 8008 kapalı; yapılandırılmış Patroni yığını
  kendi probunda kalıyor).

**SQL Server** — `SERVERPROPERTY('IsHadrEnabled')` + `sys.dm_hadr_availability_replica_states`:
- VIEW SERVER STATE ile DMV'ler AG'yi ve kopuk replikayı gösteriyor; katalog görünümü
  `sys.availability_groups` ise **0 satır** dönüyor (meta veri görünürlüğü). Tespit yalnızca DMV.
- Yetkisiz login: `IsHadrEnabled` okunuyor; DMV "permission denied" (Msg 297) → ölçülemedi.

## Beklenen cluster

Birincilde replika satırı yoksa bu tek sunucu da olabilir, replikası kopmuş cluster da. Grup
topolojisi patroni/alwayson ise ya da bu sunucuda son 24 saat içinde cluster GÖZLENDİYSE
"cluster, bozuk"; değilse "tek sunucu". 24 saat sonra (replika bilerek kaldırıldıysa) tek sunucu
sayılıyor ve gerekçe bunu söylüyor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import Instance

KIND_STANDALONE = "standalone"
KIND_CLUSTER = "cluster"
KIND_UNMEASURED = "unmeasured"
STATE_HEALTHY = "healthy"
STATE_DEGRADED = "degraded"

CLUSTER_MEMORY = timedelta(hours=24)
#: Son tespit bundan eskiyse durum bilinmiyor sayılır (toplama çalışmıyor).
TOPOLOGY_STALE_AFTER = timedelta(hours=1)
#: Alarm metriği — YALNIZCA topoloji "cluster" iken üretiliyor. Tek sunucu ya da ölçülemedi durumunda
#: metrik hiç yok; alarm motoru değeri olmayan kuralı değerlendirmiyor.
DEGRADED_METRIC = "topology_cluster_degraded"

PG_GRANT = "GRANT pg_monitor TO <izleme_kullanıcısı>;  -- pg_read_all_stats: replikasyon durum kolonları"
MSSQL_GRANT = "GRANT VIEW SERVER STATE TO [<izleme_login>];  -- sys.dm_hadr_* DMV'leri"


@dataclass
class TopologyObservation:
    kind: str
    reason: str
    state: str | None = None
    role: str | None = None
    members: list[dict[str, Any]] = field(default_factory=list)
    required_grant: str | None = None

    def alert_flags(self) -> dict[str, float]:
        if self.kind != KIND_CLUSTER:
            return {}
        return {DEGRADED_METRIC: 1.0 if self.state == STATE_DEGRADED else 0.0}


def _permission_error(text: str) -> bool:
    lowered = text.lower()
    return "permission" in lowered or "yetki" in lowered or "must be" in lowered or "(297)" in lowered


def expected_cluster(instance: Instance, group_topology: str | None, *, now: datetime) -> bool:
    if group_topology in ("patroni", "alwayson"):
        return True
    seen = instance.topology_cluster_seen_at
    if seen is None:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    return now - seen <= CLUSTER_MEMORY


def classify_postgresql(facts: dict[str, Any], *, expected: bool) -> TopologyObservation:
    """`facts`: {"error"} ya da {"in_recovery", "replication": [...], "wal_receiver": [...]}."""
    if facts.get("error"):
        error = str(facts["error"])
        return TopologyObservation(
            KIND_UNMEASURED,
            f"Ölçülemedi: topoloji sorgusu başarısız — {error[:300]}",
            required_grant=PG_GRANT if _permission_error(error) else None,
        )
    if facts["in_recovery"]:
        receivers = facts.get("wal_receiver") or []
        if not receivers:
            return TopologyObservation(
                KIND_CLUSTER, "Replika birincil sunucuya BAĞLI DEĞİL: pg_stat_wal_receiver boş (WAL alınmıyor).",
                state=STATE_DEGRADED, role="replica",
            )
        if any(r.get("status") is None for r in receivers):
            return TopologyObservation(
                KIND_UNMEASURED,
                "Ölçülemedi: sunucu replika (recovery) ama WAL alıcısının durumu görünmüyor — izleme "
                "kullanıcısında pg_read_all_stats yok.",
                role="replica", required_grant=PG_GRANT,
            )
        status = receivers[0].get("status")
        members = [{"name": r.get("sender_host"), "state": r.get("status")} for r in receivers]
        if status == "streaming":
            return TopologyObservation(KIND_CLUSTER, "Replika birincilden akışla WAL alıyor.",
                                       state=STATE_HEALTHY, role="replica", members=members)
        return TopologyObservation(KIND_CLUSTER, f"Replikanın WAL alıcısı akışta değil (durum: {status}).",
                                   state=STATE_DEGRADED, role="replica", members=members)

    replicas = facts.get("replication") or []
    if replicas:
        if any(r.get("state") is None for r in replicas):
            return TopologyObservation(
                KIND_UNMEASURED,
                f"Ölçülemedi: {len(replicas)} replika bağlantısı görünüyor ama durumları gizli — izleme "
                "kullanıcısında pg_read_all_stats yok.",
                role="primary", required_grant=PG_GRANT,
            )
        members = [{"name": r.get("application_name"), "state": r.get("state"), "sync": r.get("sync_state")}
                   for r in replicas]
        bad = [m for m in members if m["state"] != "streaming"]
        if bad:
            return TopologyObservation(
                KIND_CLUSTER, f"{len(bad)}/{len(members)} replika akışta değil: "
                + ", ".join(f"{m['name']}={m['state']}" for m in bad),
                state=STATE_DEGRADED, role="primary", members=members,
            )
        return TopologyObservation(KIND_CLUSTER, f"{len(members)} replika akışta.",
                                   state=STATE_HEALTHY, role="primary", members=members)
    if expected:
        return TopologyObservation(
            KIND_CLUSTER,
            "Beklenen replika BAĞLI DEĞİL: pg_stat_replication boş (bu sunucu cluster olarak yapılandırılmış "
            "ya da son 24 saatte replikası görülmüş).",
            state=STATE_DEGRADED, role="primary",
        )
    return TopologyObservation(
        KIND_STANDALONE,
        "Tek sunucu: recovery modunda değil ve bağlı replika yok (son 24 saatte de görülmedi).",
    )


def classify_sqlserver(facts: dict[str, Any], *, expected: bool) -> TopologyObservation:
    """`facts`: {"hadr_enabled", "replicas": [...] | None, "error"}."""
    if facts.get("hadr_enabled") is None:
        return TopologyObservation(
            KIND_UNMEASURED, f"Ölçülemedi: IsHadrEnabled okunamadı — {str(facts.get('error') or '')[:300]}",
        )
    if not facts["hadr_enabled"]:
        if expected:
            return TopologyObservation(
                KIND_CLUSTER, "Always On bu sunucuda KAPALI (IsHadrEnabled=0) ama grup Always On olarak "
                "yapılandırılmış ya da son 24 saatte AG görülmüş.", state=STATE_DEGRADED,
            )
        return TopologyObservation(KIND_STANDALONE, "Tek sunucu: Always On kapalı (IsHadrEnabled=0).")
    if facts.get("error"):
        error = str(facts["error"])
        return TopologyObservation(
            KIND_UNMEASURED,
            f"Ölçülemedi: Always On açık ama replika durumu okunamadı — {error[:300]}",
            required_grant=MSSQL_GRANT if _permission_error(error) else None,
        )
    replicas = facts.get("replicas") or []
    if not replicas:
        if expected:
            return TopologyObservation(
                KIND_CLUSTER, "Beklenen availability group görünmüyor (sys.dm_hadr_availability_replica_states boş).",
                state=STATE_DEGRADED,
            )
        return TopologyObservation(KIND_STANDALONE, "Tek sunucu: Always On açık ama availability group yok.")
    members = [
        {"name": r.get("replica_server_name"), "role": r.get("role_desc"), "state": r.get("connected_state_desc"),
         "health": r.get("synchronization_health_desc")}
        for r in replicas
    ]
    local = next((r for r in replicas if r.get("is_local")), None)
    role = {"PRIMARY": "primary", "SECONDARY": "replica"}.get(str((local or {}).get("role_desc") or "").upper())
    bad = [m for m in members if m["state"] != "CONNECTED" or m["health"] != "HEALTHY"]
    if bad:
        return TopologyObservation(
            KIND_CLUSTER, f"{len(bad)}/{len(members)} AG replikası sağlıksız: "
            + ", ".join(f"{m['name']}={m['state']}/{m['health']}" for m in bad),
            state=STATE_DEGRADED, role=role, members=members,
        )
    return TopologyObservation(KIND_CLUSTER, f"Availability group: {len(members)} replika bağlı ve sağlıklı.",
                               state=STATE_HEALTHY, role=role, members=members)


def classify(engine: str, facts: dict[str, Any], *, expected: bool) -> TopologyObservation:
    if engine == "postgresql":
        return classify_postgresql(facts, expected=expected)
    if engine == "sqlserver":
        return classify_sqlserver(facts, expected=expected)
    return TopologyObservation(KIND_UNMEASURED, f"Ölçülemedi: {engine} için topoloji tespiti yok.")


def record_topology(instance: Instance, observation: TopologyObservation, *, now: datetime) -> None:
    instance.topology_kind = observation.kind
    instance.topology_state = observation.state
    instance.topology_role = observation.role
    instance.topology_reason = observation.reason
    instance.topology_required_grant = observation.required_grant
    instance.topology_members = observation.members or None
    instance.topology_checked_at = now
    if observation.kind == KIND_CLUSTER:
        instance.topology_cluster_seen_at = now


def topology_status(instance: Instance, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    checked = instance.topology_checked_at
    if checked is not None and checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    if checked is None or now - checked > TOPOLOGY_STALE_AFTER:
        error = instance.last_collect_error
        if error:
            # Toplama tamamen başarısızsa (ör. yetkisiz login'de SQL Server oturum DMV'si bile okunamıyor)
            # topoloji de okunamadı: gerekçe o hata, yetki hatasıysa gereken yetki.
            grant = (PG_GRANT if instance.engine == "postgresql" else MSSQL_GRANT) if _permission_error(error) else None
            return {
                "kind": KIND_UNMEASURED, "state": None, "role": None, "members": [], "checked_at": checked,
                "required_grant": grant, "reason": f"Ölçülemedi: son toplama başarısız — {error}",
            }
        return {
            "kind": KIND_UNMEASURED, "state": None, "role": None, "members": [], "checked_at": checked,
            "required_grant": None,
            "reason": "Ölçülemedi: toplayıcı son bir saatte bu sunucunun topolojisini okumadı (toplama çalışmıyor "
                      "ya da bağlantı başarısız). Tek sunucu da cluster da varsayılmıyor.",
        }
    return {
        "kind": instance.topology_kind, "state": instance.topology_state, "role": instance.topology_role,
        "members": list(instance.topology_members or []), "checked_at": checked,
        "required_grant": instance.topology_required_grant, "reason": instance.topology_reason or "",
    }
