# Cluster Health (Patroni stack)

Hybrid model:

1. **Remote probe** — pgwatch worker Patroni `:8008`, etcd `:2379`, HAProxy stats, Keepalived VIP TCP kontrolü yapar.
2. **Host agent** — her DB sunucusunda systemd + journalctl + VIP sahipliği okur.

## Instance options

```json
{
  "patroni_port": 8008,
  "etcd_port": 2379,
  "haproxy_stats_port": 8404,
  "haproxy_stats_path": "/stats;csv",
  "keepalived_vip": "10.0.0.50",
  "probe_timeout_sec": 3,
  "agent_url": "http://db-node-1:9105",
  "agent_token": "strong-secret"
}
```

UI: **Instances → düzenle** formunda bu alanlar vardır. `services` checkbox’ları hangi probe’ların çalışacağını seçer.

## API

- `GET /api/instances/{id}/cluster-health`
- `GET /api/instances/{id}/cluster-logs?service=patroni&lines=100` (agent gerekir)

## Alarm metrikleri

Worker her collect turunda şunları `metrics_json` içine yazar:

- `patroni_down`, `etcd_down`, `haproxy_down`, `keepalived_vip_down`
- `cluster_has_leader`, `cluster_services_down`
- `cluster_services` (compact snapshot)

Instance için default alert rule’ları otomatik oluşturulur.

## Host agent kurulumu

Bkz. [agents/host-agent/README.md](../agents/host-agent/README.md) ve [deploy/onprem/docker-compose.host-agent.yml](../deploy/onprem/docker-compose.host-agent.yml).

Firewall: pgwatch worker → agent `:9105`, Patroni `:8008`, etcd `:2379`, HAProxy stats port.
