# dbace

Multi-database DBA monitoring (PostgreSQL, SQL Server, MongoDB) with alerts and trend-based predictions.

**Bulut (Faz 1 — geliştirme/test):** [deploy/cloud/BULUT-KURULUM.md](deploy/cloud/BULUT-KURULUM.md)  
**Yaşam döngüsü (bulut → on-prem paket):** [docs/YASAM-DONGUSU.md](docs/YASAM-DONGUSU.md)  
**On-prem paket kurulumu (Faz 3):** [deploy/onprem/KURULUM.md](deploy/onprem/KURULUM.md)

**Türkçe mimari (bulut):** [docs/MIMARI.md](docs/MIMARI.md)

## Features

- **Multi-instance monitoring** — register many PostgreSQL servers
- **Core metrics** — connections, TPS, cache hit ratio, DB size, replication lag, deadlocks
- **Slow queries** — top queries from `pg_stat_statements`
- **Web dashboard** — React UI with live charts
- **Alerting** — threshold rules with active alert events

## Architecture

```
┌─────────────┐     poll every 15s     ┌──────────────────┐
│ PostgreSQL  │ ◄──────────────────────│  FastAPI backend │
│  instances  │                        │  (collector+API) │
└─────────────┘                        └────────┬─────────┘
                                                │
                                       SQLite (metadata + metrics)
                                                │
                                       ┌────────▼─────────┐
                                       │  React dashboard │
                                       └──────────────────┘
```

## Quick start (local dev)

### 1. Demo PostgreSQL (optional)

```bash
docker compose up -d postgres-demo
```

Connects on `localhost:5433` with user/password `postgres`.

Enable slow query stats (already applied via init script):

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

### 2. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard: http://localhost:5173

### 4. Add an instance

In the UI go to **Instances → Add instance**, or POST to `/api/instances`:

```json
{
  "name": "local-demo",
  "host": "localhost",
  "port": 5433,
  "database": "postgres",
  "username": "postgres",
  "password": "postgres"
}
```

## İlk kurulum — kimlik doğrulama

`/api/health` ve `/api/auth/login` (+`/refresh`) dışında her API ucu bir
oturum (JWT) gerektirir — dashboard'a girmeden önce bir admin hesabıyla
giriş yapmanız gerekir.

### Zorunlu / önerilen `.env` değişkenleri

| Değişken | Zorunlu mu | Açıklama |
|---|---|---|
| `JWT_SECRET` | Prod'da zorunlu (dev'de bir varsayılanı var, kullanılırsa her açılışta uyarı loglanır) | Token imzalama anahtarı — uzun, rastgele bir değer olmalı |
| `ADMIN_USERNAME` | Hayır (varsayılan `admin`) | İlk açılışta oluşturulacak admin kullanıcının adı |
| `ADMIN_PASSWORD` | Hayır ama önerilir | Belirtilmezse rastgele bir şifre üretilip **sadece bir kez** loglanır — kaçırırsanız aşağıdaki "şifre unutuldu" adımına bakın |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Hayır (varsayılan 60) | Access token ömrü |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Hayır (varsayılan 7) | Refresh token ömrü |

Örnek değerler için `.env.example`'a bakın.

### İlk giriş

1. Backend'i `ADMIN_USERNAME`/`ADMIN_PASSWORD` `.env`'de tanımlıyken ilk kez
   başlatın. Açılışta `users` tablosunda bu kullanıcı adıyla kayıt yoksa bir
   admin oluşturulur (log: `Admin oluşturuldu: <kullanıcı adı>`).
   `ADMIN_PASSWORD` tanımlı değilse rastgele bir şifre üretilip **bir kez**
   loglanır (`ADMIN_PASSWORD not set — generated initial admin credentials: ...`)
   — bu satırı kaydedin, bir daha gösterilmez.
2. Dashboard'ı açıp bu kullanıcı adı/şifreyle giriş yapın.
3. İlk girişte şifre değiştirme zorunludur — sistem sizi otomatik olarak
   şifre değiştirme ekranına yönlendirir.

### `.env`'e `ADMIN_PASSWORD`'u sonradan eklediyseniz

Admin kullanıcısı `ADMIN_PASSWORD` `.env`'e eklenmeden ÖNCE bir açılışta
oluşturulmuşsa (rastgele şifre üretilip loglanmış, kaybedilmiş olabilir),
şifreyi `.env`'e eklemek tek başına yeterlidir: kullanıcı henüz ilk şifre
değişikliğini yapmamışsa, bir sonraki backend açılışında şifre otomatik
olarak `.env`'deki değere senkronize edilir (log:
`Admin şifresi .env'den güncellendi: <kullanıcı adı>`). Kullanıcı zaten
kendi şifresini belirlemişse, `.env`'deki değer bir daha ASLA üzerine
yazmaz — bu durumda aşağıdaki CLI script'ini kullanın.

### Şifre unutulursa / hesap kilitlenirse

Veritabanına/sunucuya doğrudan erişiminiz varsa, `backend/` dizininden:

```bash
python scripts/reset_admin_password.py <kullanici_adi> <yeni_sifre>
```

Kullanıcı pasifleştirilmişse (`is_active=false`) aynı anda aktifleştirmek için:

```bash
python scripts/reset_admin_password.py <kullanici_adi> <yeni_sifre> --activate
```

Script şifreyi hemen ayarlar ve "ilk girişte şifre değiştir" zorunluluğunu
kaldırır — script'i çalıştırabilen kişinin zaten sunucuya doğrudan erişimi
olduğundan, web arayüzünde ayrıca bir şifre-değiştir adımına zorlamanın
güvenlik faydası yoktur.

## Docker (all services)

```bash
docker compose up --build
```

- Dashboard: http://localhost:5173
- API: http://localhost:8000
- Demo Postgres: localhost:5433

## API overview

| Endpoint | Description |
|----------|-------------|
| `GET /api/instances/summary` | Dashboard overview |
| `POST /api/instances` | Register instance |
| `POST /api/instances/test` | Test connection |
| `GET /api/metrics/{id}` | Time-series metrics |
| `GET /api/queries/{id}` | Slow queries |
| `GET/POST /api/alerts/rules` | Alert rules |
| `GET /api/alerts/events` | Active alerts |

## Recommended PostgreSQL setup for monitoring

Create a dedicated monitoring user with read-only access:

```sql
CREATE USER pgwatch WITH PASSWORD 'changeme';
GRANT pg_monitor TO pgwatch;
GRANT CONNECT ON DATABASE yourdb TO pgwatch;
```

For `pg_stat_statements`, add to `postgresql.conf`:

```
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = top
```

`track = top` (the default) only records top-level statements — exactly what
dbace's collector reads. `track = all` additionally records every statement
nested inside PL/pgSQL functions/triggers, which multiplies the number of
tracked entries (and the bookkeeping overhead per execution) without adding
anything dbace's slow-query view uses. Only switch to `all` if you're
debugging inside a specific function and plan to switch back afterward.

## Monitoring load (izleme yükü)

dbace is designed so the *cost of being monitored* stays close to zero on a
normal, healthy target server. This section is the audit: every query dbace
runs against a monitored instance, how often, and roughly how expensive it
is — so you can reason about the load on your own production database
before turning monitoring on.

### Collection loop — every `collect_interval_seconds` (default 15s, per-instance override supported)

One connection per cycle (see `services/collection.py` / `collectors/postgresql.py`),
`statement_timeout = 5000ms` on every query:

| Query / source | What it reads | Cost |
|---|---|---|
| `current_setting('server_version_num')`, `version()` | in-memory | negligible |
| `pg_stat_database` (one row) | in-memory cumulative counters | negligible |
| `SHOW max_connections` | GUC read | negligible |
| `pg_database_size(current_database())` | filesystem stat over the DB's relation files | low, scales with object count (not data volume) — a handful of ms even on large schemas |
| `pg_last_wal_receive_lsn()` / `pg_last_wal_replay_lsn()` | function call | negligible |
| `pg_stat_checkpointer` (17+) or `pg_stat_bgwriter` (<17) | in-memory cumulative counters | negligible |
| `pg_stat_io` (16+ only, version-gated — not even queried below 16) | in-memory view | negligible |
| `pg_stat_statements` (top ~20 by mean time) | extension's own in-memory ring buffer | negligible — this is exactly what the extension exists for |

Total per cycle: roughly 8 lightweight queries over one connection, typically
low single-digit milliseconds combined on a normal server. SQL Server's
equivalent collector cycle is the DMV/perf-counter analog of the same list,
guarded with `SET LOCK_TIMEOUT 5000` (SQL Server has no direct client-side
`statement_timeout` equivalent — see `SORULAR.md` for why LOCK_TIMEOUT was
chosen over guessing at an ODBC-driver-specific query-timeout attribute).

### Dashboard refresh loop — every `dashboard_refresh_interval_seconds` (default 60s, as low as 10s)

Per Patroni-topology group, against its target node:

| Source | What it reads | Cost |
|---|---|---|
| `parameter_audit` (`pg_settings`, ~10 named parameters) | in-memory catalog view | negligible; `statement_timeout = 5000ms` |
| `performance_insights` | pure in-memory analysis of already-collected `metrics_json` | zero — never touches the target database |
| cluster health probes (Patroni/etcd/haproxy/keepalived) | TCP socket checks + HTTP to Patroni/agent REST APIs | zero DB load — never opens a PostgreSQL/SQL Server protocol connection at all |

`index_advisor` recommendations used to run automatically here too (a live
catalog scan per instance, every tick) — this was removed; see below.

### On-demand only, user-triggered, cached (5 min TTL) — never in the periodic loop

| Source | Endpoint | Cost | Notes |
|---|---|---|---|
| Index advice | `POST /api/queries/{id}/advice` | catalog scan (`pg_stats`/`pg_indexes`/`pg_class`) + optionally 2× `EXPLAIN (FORMAT JSON)` and a hypothetical index create/drop via `hypopg` | moderate; `statement_timeout = 8000ms`; only runs when a user opens a slow query's advice panel |
| EXPLAIN plan | `POST /api/queries/{id}/explain` (`analyze=false`, the default) | plans only, never executes the query | low; `statement_timeout = 8000ms` |
| EXPLAIN ANALYZE | same endpoint, `analyze=true` | **actually executes the query** | cost = the query's own real cost; the UI requires an explicit confirmation with a warning before sending this |
| Activity / Schema Health tabs | `GET /api/instances/{id}/activity`, `/schema-health` | `pg_stat_activity` (in-memory) / `pg_stat_user_indexes`+`pg_stat_user_tables`+`pg_relation_size` (catalog scan) | low–moderate; only runs while that tab is open |

### Practical takeaways

- Leave `pg_stat_statements.track = top` (default) — see above.
- The periodic collection loop is safe to run against production at the
  default 15s/60s cadence; it's the on-demand tools (index advice, EXPLAIN
  ANALYZE) that carry real cost, and those are gated behind an explicit user
  action plus a cache.
- If a specific server is lower-priority or you want to reduce load further,
  set a longer **collection interval** for just that instance (Instances →
  edit → "Toplama aralığı") instead of lowering the global default for
  everyone.

## Roadmap

- [ ] TimescaleDB / Prometheus export for long-term retention
- [ ] Email/Slack/PagerDuty alert channels
- [ ] Query plan capture and index recommendations
- [ ] MySQL, Redis, MongoDB collectors
- [ ] Agent-based deployment model

## License

MIT
