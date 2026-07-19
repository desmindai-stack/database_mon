# pgwatch host-agent

Patroni stack sunucularında `systemctl` + `journalctl` + Keepalived VIP sahipliği okur.

## Endpoints

- `GET /v1/health`
- `GET /v1/services`
- `GET /v1/keepalived`
- `GET /v1/logs?service=patroni&lines=100`

Tüm endpoint'ler `X-Agent-Token` ister.

## Ortam değişkenleri

| Değişken | Default | Açıklama |
|----------|---------|----------|
| `AGENT_TOKEN` | `change-me` | Shared secret |
| `KEEPALIVED_VIP` | — | VIP adresi (holds_vip için) |
| `UNIT_PATRONI` | `patroni` | systemd unit adı |
| `UNIT_ETCD` | `etcd` | |
| `UNIT_POSTGRESQL` | `postgresql` | distro'ya göre `postgresql@16-main` olabilir |
| `UNIT_KEEPALIVED` | `keepalived` | |
| `UNIT_HAPROXY` | `haproxy` | |
| `BIND_PORT` | `9105` | |

## Host network çalıştırma

```bash
docker run -d --name pgwatch-host-agent --network host \
  -e AGENT_TOKEN='strong-secret' \
  -e KEEPALIVED_VIP='10.0.0.50' \
  -v /var/run/dbus/system_bus_socket:/var/run/dbus/system_bus_socket \
  -v /run/systemd:/run/systemd \
  pgwatch-host-agent
```

systemd erişimi için host üzerinde binary/systemd unit olarak çalıştırmak genelde daha güvenilir:

```bash
pip install -r requirements.txt
AGENT_TOKEN=strong-secret KEEPALIVED_VIP=10.0.0.50 \
  uvicorn app:app --host 0.0.0.0 --port 9105
```

## pgwatch bağlama

Instance `options`:

```json
{
  "agent_url": "http://db-node-1:9105",
  "agent_token": "strong-secret",
  "keepalived_vip": "10.0.0.50",
  "patroni_port": 8008,
  "etcd_port": 2379,
  "haproxy_stats_port": 8404
}
```
