# pgwatch

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
pg_stat_statements.track = all
```

## Roadmap

- [ ] TimescaleDB / Prometheus export for long-term retention
- [ ] Email/Slack/PagerDuty alert channels
- [ ] Query plan capture and index recommendations
- [ ] MySQL, Redis, MongoDB collectors
- [ ] Agent-based deployment model

## License

MIT
