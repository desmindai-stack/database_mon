# dbace

[![CI](https://github.com/desmindai-stack/database_mon/actions/workflows/ci.yml/badge.svg)](https://github.com/desmindai-stack/database_mon/actions/workflows/ci.yml)

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

### API tipleri (TypeScript)

`frontend/src/api-types.ts` **otomatik üretilir, elle düzenlenmez.** Kaynağı backend'in
OpenAPI şemasıdır. Elle yazılmış tipler API'den sessizce ayrıştığı için canlı çökmeler
yaşandı (`ReportFinding.facts` tipte "her zaman var" diyordu, API onu hiç döndürmüyordu);
bu üretim o sınıfı derleme zamanına taşıyor.

Backend'de bir şema değiştirdiyseniz tipleri yeniden üretin ve sonucu commit'leyin:

```bash
cd frontend
npm run gen:types          # backend'i çalıştırmadan üretir (python gerekir)
npm run gen:types:live     # çalışan bir backend'in /openapi.json ucundan üretir
```

`gen:types:live` varsayılan olarak `http://localhost:8000` adresine bakar;
`DBACE_API_URL` ile değiştirilebilir.

CI, tipleri yeniden üretip commit'lenmiş hâliyle karşılaştırır — farklıysa iş kırmızı olur
("backend değişmiş ama tipler güncellenmemiş" demektir).

### Tarayıcı testleri (Playwright)

Backend testleri API katmanında durur; arayüz çökmelerini (rapor bulgu detayı, silinmiş kayda
gitme, boş form) yalnızca gerçek tarayıcı yakalar. Bu paket o katmanı kapatır.

```bash
cd frontend
npx playwright install chromium   # ilk seferde bir kez
npm run test:e2e                  # tamamı
npm run test:e2e:critical         # yalnızca @critical akışlar (CI her push'ta bunu koşar)
npm run test:e2e:ui               # etkileşimli mod — adım adım izlemek için
```

Backend ve frontend sunucularını Playwright **kendisi başlatır**; elle `npm run dev`
çalıştırmanız gerekmez (çalışıyorsa da sorun olmaz, ayrı portlar kullanılır: API 8001,
web 5174).

**Veri izolasyonu:** e2e kendi SQLite dosyasını (`backend/data/dbace_e2e.db`) kullanır ve her
çalıştırmadan önce siler. Geliştirme (`dbace.db`) ve pytest (`dbace_pytest.db`) veritabanlarına
dokunmaz. Toplayıcı kapalıdır (`RUN_MODE=api`), yani testler sırasında hiçbir hedef veritabanına
bağlanılmaz.

Bir test kırıldığında ekran görüntüsü, video ve iz `frontend/test-results/` altına yazılır:

```bash
npx playwright show-trace test-results/<klasör>/trace.zip
```

CI her push'ta `@critical` akışları, gecelik zamanlamada tam paketi koşar; başarısız testin
kanıtları artifact olarak yüklenir.

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

## PgBouncer / connection pooler arkasında çalışma

dbace'in izlediği bir PostgreSQL sunucusu (veya dbace'in kendi meta veri tabanı — Supabase dahil)
PgBouncer ya da Supabase'in pooler'ı gibi bir bağlantı havuzlayıcısının arkasındaysa ve havuzlayıcı
`transaction` veya `statement` pool_mode'da çalışıyorsa, aşağıdaki gibi bir hata görebilirdiniz:

```
prepared statement "__asyncpg_stmt_21__" already exists
pgbouncer cannot support prepared statements in transaction/statement pooling mode
```

Sebep: asyncpg (dbace'in PostgreSQL sürücüsü) varsayılan olarak sorguları isimli bir "prepared
statement" olarak sunucuda önbelleğe alır. Transaction/statement modundaki bir havuzlayıcı, aynı
istemci bağlantısındaki ardışık sorguları farklı gerçek sunucu bağlantılarına yönlendirebilir —
bu yüzden bir sorgu, kendisini hiç görmemiş bir bağlantıda "EXECUTE" edilmeye çalışılır.

dbace bunu artık her asyncpg bağlantısında (collector, activity, schema health, parametre
denetimi, EXPLAIN, index advisor ve dbace'in kendi meta veri tabanı bağlantısı dahil)
`statement_cache_size=0` ile koşulsuz olarak devre dışı bırakarak çözer — havuzlayıcı olsun ya da
olmasın, ek bir maliyeti yoktur.

Bu hata yine de bir yerden sızarsa (ör. tespit edilemeyen özel bir kurulum), API anlaşılır bir
Türkçe mesaj döner ve pooler'ı işaretlemenizi önerir. Instance/Node ekleme formunda ve sihirbazda
**"Pooler kullanılıyor"** seçeneği bulunur:

- **Otomatik algıla** (varsayılan) — host adı `pooler`/`pgbouncer` içeriyorsa veya port
  `6432`/`6543` ise (Supabase'in pooled portu 6543, PgBouncer'ın paket varsayılanı 6432) pooler
  olarak kabul edilir.
- **Evet / Hayır** — otomatik tespiti geçersiz kılar; standart olmayan bir host/port üzerinde
  çalışan bir PgBouncer için elle işaretleyin.

dbace'in kendi meta veri tabanı için (`DATABASE_URL` bir Supabase/PgBouncer bağlantısıysa) ayrı
bir ayar gerekmez — `postgresql+asyncpg://` şemasını gördüğünde SQLAlchemy motoru otomatik olarak
aynı korumayı uygular.

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

## PostgreSQL sürüm yetenek matrisi

PostgreSQL sistem katalogları sürümden sürüme değişiyor: sütunlar taşınıyor, yeniden
adlandırılıyor, kaldırılıyor. dbace bu farkları **tek yerden** yönetiyor —
`backend/app/domain/pg_capabilities.py`. Sürüm eşiği koda dağılmıyor.

**Kural: bir metriğin alternatif kaynağı varsa "desteklenmiyor" DENMEZ.** Alternatifi
varken öyle demek, kullanıcıyı ekranında bir eksiklikle ve başka bir araca yönlenmekle baş
başa bırakır.

| Metrik | PG 12-16 | PG 17-18 |
|---|---|---|
| `checkpoints_timed`, `checkpoints_req` | `pg_stat_bgwriter` | `pg_stat_checkpointer` (`num_timed`, `num_requested`) |
| `checkpoint_write_time_ms`, `checkpoint_sync_time_ms` | `pg_stat_bgwriter` | `pg_stat_checkpointer` |
| `buffers_checkpoint_per_sec` | `pg_stat_bgwriter` | `pg_stat_checkpointer` (`buffers_written`) |
| `buffers_clean_per_sec`, `buffers_alloc_per_sec` | `pg_stat_bgwriter` | `pg_stat_bgwriter` (taşınmadı) |
| **`buffers_backend_per_sec`** | `pg_stat_bgwriter` | **`pg_stat_io`** (arka plan süreçleri dışındaki yazmalar) |
| **`buffers_backend_fsync_per_sec`** | `pg_stat_bgwriter` | **`pg_stat_io`** (`fsyncs`) |
| `io_reads/writes/extends_per_sec` | 16+ `pg_stat_io`; öncesinde **yok** | `pg_stat_io` |
| `io_op_bytes` | 16-17 `pg_stat_io.op_bytes` | **18'de kaldırıldı** |
| `io_read_bytes/write_bytes_per_sec` | **yok** | 18+ `pg_stat_io` (gerçek bayt sayaçları) |

PostgreSQL 17, `buffers_backend` sütununu `pg_stat_bgwriter`'dan kaldırdı. dbace önceden
"doğrudan bir karşılığı yok" diyordu — yanlıştı. Aynı bilgi `pg_stat_io` içinde duruyor:

```sql
SELECT SUM(writes), SUM(fsyncs)
FROM pg_stat_io
WHERE object = 'relation' AND context = 'normal'
  AND backend_type NOT IN ('checkpointer', 'background writer');
```

PostgreSQL 18 `op_bytes` sütununu kaldırıp yerine gerçek bayt sayaçlarını koydu. Bu kod
tarafında **gerçek bir kırılmaydı**: eski sütunu sormaya devam etmek PG 18'de `pg_stat_io`
sorgusunun tamamını düşürürdü — tek bir sütun yüzünden `io_reads` ve `io_writes` de
kaybolurdu. Sorgu artık sürüme göre farklı sütun seçiyor.

Sorgu tarafında da sürüm dallanması var:

| Özellik | Gereken sürüm | Yoksa ne oluyor |
|---|---|---|
| `pg_stat_activity.query_id` (bekleme → sorgu eşleşmesi) | 14+ | Bekleme kırılımı üretiliyor, sorgu bazında ayrıştırılamıyor |
| `EXPLAIN (GENERIC_PLAN)` (yer tutuculu sorgu planı) | 16+ | Plan alınamıyor; sebebi ve auto_explain alternatifi yazılıyor |
| `pg_stat_statements` `total_exec_time` (vs `total_time`) | 13+ | 13 öncesinde eski sütun adları kullanılıyor |

Desteklenen aralık **PostgreSQL 12-18**. Altındaki sunucuya yine bağlanılıyor (en yakın
davranış + uyarı) — reddetmek, çalışabilecek bir kurulumu boşuna engellerdi. Üstündeki
sürümlerde en yeni dal kullanılıyor: PostgreSQL katalog değişiklikleri neredeyse her zaman
eklemeli olduğu için bu, toplamayı durdurmaktan güvenli.

Arayüzde her metriğin kaynağı **Veritabanı detayı → Metrik kaynakları** altında görünüyor.

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

### Wait-event sampler — every `wait_sample_interval_seconds` (default 1s)

Separate from the collection loop, and much more frequent: cumulative counters
are fine to read every 15s, but "what is the system waiting on *right now*"
needs frequent snapshots of instantaneous state. A 15s cadence would miss a
200ms lock storm entirely.

**One persistent connection per instance**, reused across ticks — reconnecting
every second would cost more (TCP + TLS handshake + backend fork) than the
sample query itself. `statement_timeout = 1000ms` (tighter than the collection
loop's 5000ms: a sampling query that takes over a second is already broken).

| Query / source | What it reads | Cost |
|---|---|---|
| `pg_stat_activity`, filtered to `state='active'` server-side | in-memory view, one row per backend | negligible — no disk access, no catalog scan |
| `pg_blocking_pids(pid)` | lock manager walk | **only called for rows already waiting on a `Lock`** — a `CASE` guard keeps it off every other row; the answer is meaningless anywhere else |
| SQL Server: `dm_exec_requests` + `dm_os_waiting_tasks` + `dm_exec_sql_text` | DMVs / plan-cache lookup | low; active requests are few by definition, and `TOP` caps the worst case |

One query, one round trip, per instance per tick. Instances are sampled
concurrently, so one slow server does not delay the others, and the scheduler
job runs with `max_instances=1` so ticks never overlap.

**Measuring the cost on your own server** (the sampler's query shows up in
`pg_stat_statements` like any other):

```sql
SELECT calls,
       round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 3)  AS mean_ms
FROM pg_stat_statements
WHERE query LIKE '%pg_stat_activity%'
  AND query LIKE '%parallel worker%'
ORDER BY total_exec_time DESC;
```

`mean_ms` is the per-sample cost on *your* hardware and workload. This is the
number to look at before enabling the sampler on a busy production server; the
target-side cost has not been measured on a real production instance by the
project itself (see `SORULAR.md`).

**Storage cost on dbace's own database** (this *is* measured):

Raw samples are never stored. At 1s sampling, one row per active session per
second means ~864,000 rows/day for a server with 10 concurrently active
sessions. Instead, samples are accumulated in memory and written once per
minute as one row per `(minute, queryid, wait category, wait event)`
combination.

Measured at **192 bytes/row including indexes** (SQLite, 200k rows, realistic
queryid cardinality):

| Distinct combinations per minute | Rows/day per instance | Per month (30d retention) |
|---|---|---|
| 5 (quiet, few distinct queries) | 7,200 | ~41 MB |
| 15 (typical mixed workload) | 21,600 | ~124 MB |
| 50 (very diverse workload) | 72,000 | ~415 MB |

That is a ~40× reduction versus storing raw samples, and all three tables are
covered by the retention policy (`services/retention.py`) — unlike
`slow_query_samples`, which accumulated 337k rows for a single instance before
retention was fixed in Faz 21.

Turn the sampler off entirely with `WAIT_SAMPLING_ENABLED=false`: no
connection is opened and no query is sent to the target at all.

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
