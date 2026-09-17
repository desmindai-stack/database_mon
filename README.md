# dbace

[![CI](https://github.com/desmindai-stack/database_mon/actions/workflows/ci.yml/badge.svg)](https://github.com/desmindai-stack/database_mon/actions/workflows/ci.yml)

PostgreSQL ve SQL Server için kurumsal DBA izleme, performans analizi ve troubleshooting
platformu. Çok müşterili: bir müşterinin uygulamaları, uygulamaların veritabanı grupları
(standalone, Patroni cluster, Always On AG) ve bu grupların düğümleri tek yerden izlenir.

Aynı veriden iki ayrı çıktı üretir: **DBA** için derin, komutlu, eyleme dönük analiz;
**müşteri yöneticisi** için özet, risk ve trend.

## İçindekiler

- [Ne yapıyor](#ne-yapıyor)
- [Mimari](#mimari)
- [Hızlı başlangıç (yerel geliştirme)](#hızlı-başlangıç-yerel-geliştirme)
- [İlk kurulum — kimlik doğrulama](#i̇lk-kurulum--kimlik-doğrulama)
- [İzlenecek veritabanını hazırlama](#i̇zlenecek-veritabanını-hazırlama)
- [Testler](#testler)
- [API tipleri (TypeScript)](#api-tipleri-typescript)
- [PgBouncer / connection pooler arkasında çalışma](#pgbouncer--connection-pooler-arkasında-çalışma)
- [PostgreSQL sürüm yetenek matrisi](#postgresql-sürüm-yetenek-matrisi)
- [İzleme yükü](#i̇zleme-yükü)
- [Dokümanlar](#dokümanlar)

## Ne yapıyor

| Alan | İçerik |
|---|---|
| **Desteklenen motorlar** | PostgreSQL 12-18 (standalone, Patroni 2-3 düğüm + DR), SQL Server (standalone, Always On AG + DR). MongoDB için temel bir collector var, yalnızca standalone. |
| **Çok müşterili yapı** | Müşteri → Uygulama → Veritabanı grubu → Düğüm + Sunucu. `public` (çok müşteri) ve `private` (tek müşteri, izole) dağıtım modu. |
| **Veritabanı detayı (DPA)** | Metrik grafikleri, veritabanı yükü (AAS) + bekleme kırılımı, yavaş sorgular (`pg_stat_statements` / DMV), EXPLAIN ve auto_explain ile yakalanan gerçek planlar, tahmini/gerçek satır sapması, bloklama zinciri ve geçmişi, deadlock, index önerisi, şema sağlığı, ön koşul kontrolü, tuning. |
| **Cluster sağlığı** | Patroni/Always On durumu, etcd quorum, split-brain tespiti, parametre sapması, host-agent üzerinden servis durumu ve log tail. |
| **Tahminler** | Trend tabanlı öngörü (tarih tek nokta değil aralık), adım adım playbook, doğruluk geri beslemesi. |
| **Sağlık raporu** | Teknik (DBA) ve yönetici raporu, zamanlanmış üretim, PDF/CSV. Bulgular kanıtlı ve durum makinesine bağlı. |
| **Alarmlar** | Varsayılan + özel (SQL tabanlı) kurallar, olay geçmişi. |
| **Diğer** | Yedek izleme, bakım pencereleri, SLA hedefleri, JWT kimlik doğrulama (admin / salt-okunur viewer). |

Bilerek yapılmamış işler ve açık varsayımlar (ör. host-agent'ın CPU/RAM/disk toplamaması)
[CLAUDE.md](CLAUDE.md#bilinen-sınırlar) ve [SORULAR.md](SORULAR.md)'de.

## Mimari

```
┌─────────────────┐              ┌──────────────────────────┐
│ React dashboard │ ◄──  REST ──►│ FastAPI  (RUN_MODE=api)   │
└─────────────────┘              └────────────┬─────────────┘
                                              │
                          metadata + metrik geçmişi
                   (yerelde SQLite, canlıda PostgreSQL/Supabase)
                                              │
                                 ┌────────────▼─────────────┐
                                 │ Worker (RUN_MODE=worker)  │
                                 │ APScheduler döngüleri     │
                                 └────────────┬─────────────┘
                     ┌────────────────────────┼──────────────────┐
                     ▼                        ▼                  ▼
                PostgreSQL               SQL Server         host-agent
            (asyncpg, salt-okuma)     (aioodbc, DMV)    (systemd + log tail)
```

API ve worker aynı kod tabanıdır; `RUN_MODE` hangi rolde çalışacağını seçer. Yerelde
`RUN_MODE=all` (varsayılan) ikisini tek süreçte çalıştırır. Canlıda **ikisini ayrı servis
olarak** çalıştırın — ikisi de `all` kalırsa toplama döngüsü iki kez çalışır.

Kodda yön bulma, servis sorumlulukları ve zamanlanmış işlerin listesi:
**[docs/MIMARI.md](docs/MIMARI.md)**.

| Katman | Teknoloji |
|---|---|
| Backend | Python 3.12 (canlı), FastAPI, SQLAlchemy 2.0 async, Pydantic v2, APScheduler, sqlglot, reportlab |
| Frontend | React 19, React Router 7, Recharts, Vite 6, TypeScript 5.7 |
| Testler | pytest (backend + frontend statik denetimleri), Playwright (tarayıcı) |

## Hızlı başlangıç (yerel geliştirme)

**Gereksinimler:** Python 3.12+, Node 20+, isteğe bağlı Docker (demo PostgreSQL için).
SQL Server izleyecekseniz makinede **ODBC Driver 18 for SQL Server** kurulu olmalı.

> **Python sürümü:** Canlı ortam ve CI 3.12 kullanıyor. Yerelde daha yeni bir sürüm
> (ör. 3.14) çalışır, ama annotation değerlendirmesi farklı olduğu için bazı hatalar yalnızca
> 3.12'de görünür — ayrıntı için [CLAUDE.md](CLAUDE.md#kurallar).

### 1. Demo PostgreSQL (isteğe bağlı)

```bash
docker compose up -d postgres-demo
```

`localhost:5433`, kullanıcı/şifre `postgres`. `pg_stat_statements` yüklü gelir
(`docker/init-demo.sql`).

> Demo konteyner `pg_stat_statements.track=all` ile başlıyor; bu, her şeyi görmek isteyen
> geliştirme ortamı için. Canlı sunucularda `top` önerilir — bkz.
> [İzlenecek veritabanını hazırlama](#i̇zlenecek-veritabanını-hazırlama).

### 2. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp ../.env.example .env            # yerelde DATABASE_URL satırını silin → SQLite kullanılır
uvicorn app.main:app --reload --port 8000
```

- API dokümantasyonu: http://localhost:8000/docs
- Sağlık kontrolü: http://localhost:8000/api/health
- Yerel veritabanı: `backend/data/dbace.db` (ilk açılışta oluşur)

`.env.example` canlı (Supabase) değerleriyle geliyor. Yerelde `DATABASE_URL` tanımlı
değilse SQLite kullanılır; `SUPABASE_*` değişkenleri boş kalabilir. Tüm ortam değişkenleri
ve açıklamaları: **[DEPLOY.md](DEPLOY.md)**.

Demo verisi (örnek müşteri/uygulama/grup hiyerarşisi) için:

```bash
python scripts/seed_demo.py        # idempotent, tekrar çalıştırılabilir
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Dashboard: http://localhost:5173 — Vite, `/api` isteklerini `127.0.0.1:8000`'e yönlendirir.

### 4. İlk veritabanını ekleme

1. Admin hesabıyla giriş yapın (bkz. [İlk kurulum](#i̇lk-kurulum--kimlik-doğrulama)).
2. **Müşteriler** sayfasında **+ Veritabanı ekle** — sihirbaz müşteri, uygulama ve
   veritabanı grubunu (standalone / Patroni / Always On) adım adım oluşturur.
3. Sihirbazdaki **Bağlantıyı test et** adımı, kaydetmeden önce kimlik bilgisini doğrular.

Aynı işlem API ile de yapılabilir: `POST /api/instances` (bkz. http://localhost:8000/docs).

### Docker ile tüm yığın

Kapalı ortam (on-prem) paketi tam yığını — PostgreSQL metadata veritabanı, API/worker ve
nginx arkasında dashboard — tek komutla kurar:
**[deploy/onprem/KURULUM.md](deploy/onprem/KURULUM.md)**.

Kök dizindeki `docker-compose.yml` de tam yığını (demo PostgreSQL + API/worker + nginx
arkasında dashboard) ayağa kaldırır:

```bash
docker compose up -d --build     # dashboard: http://localhost:8080
```

Dashboard 8080'de, API aynı origin üzerinden `/api/` altında. Doğrudan API'ye
http://localhost:8000 adresinden de erişilir.

## İlk kurulum — kimlik doğrulama

`/api/health` ve `/api/auth/login` (+`/refresh`) dışında her API ucu bir oturum (JWT)
gerektirir. Değişiklik yapan uçlar ayrıca `admin` rolü ister; `viewer` salt-okunurdur.

### Kimlik doğrulama değişkenleri

| Değişken | Zorunlu mu | Açıklama |
|---|---|---|
| `JWT_SECRET` | Canlıda zorunlu | Token imzalama anahtarı — uzun, rastgele. Yerelde bir varsayılanı var; kullanılırsa her açılışta uyarı loglanır. |
| `CREDENTIALS_MASTER_KEY` | Canlıda zorunlu | İzlenen veritabanı şifrelerini (Fernet) şifreler. Değiştirilirse kayıtlı şifreler çözülemez. |
| `ADMIN_USERNAME` | Hayır (varsayılan `admin`) | İlk açılışta oluşturulacak admin kullanıcı adı |
| `ADMIN_PASSWORD` | Hayır, ama önerilir | Boşsa rastgele bir şifre üretilip **yalnızca bir kez** loglanır |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Hayır (varsayılan 60) | Access token ömrü |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Hayır (varsayılan 7) | Refresh token ömrü |

### İlk giriş

1. Backend'i ilk kez başlatın. `users` tablosunda bu kullanıcı adıyla kayıt yoksa bir admin
   oluşturulur (log: `Admin oluşturuldu: <kullanıcı adı>`). `ADMIN_PASSWORD` tanımlı
   değilse üretilen şifre **bir kez** loglanır
   (`ADMIN_PASSWORD not set — generated initial admin credentials: ...`) — bu satırı
   kaydedin, bir daha gösterilmez.
2. Dashboard'a bu kullanıcı adı/şifreyle giriş yapın.
3. İlk girişte şifre değiştirme zorunludur; sistem sizi şifre değiştirme ekranına yönlendirir.

### `ADMIN_PASSWORD`'u `.env`'e sonradan eklediyseniz

Kullanıcı henüz ilk şifre değişikliğini yapmamışsa, bir sonraki açılışta şifre `.env`'deki
değere senkronize edilir (log: `Admin şifresi .env'den güncellendi: <kullanıcı adı>`).
Kullanıcı kendi şifresini zaten belirlemişse `.env` bir daha **asla** üzerine yazmaz — bu
durumda aşağıdaki betiği kullanın.

### Şifre unutulursa / hesap kilitlenirse

Sunucuya doğrudan erişiminiz varsa, `backend/` dizininden:

```bash
python scripts/reset_admin_password.py <kullanici_adi> <yeni_sifre>
python scripts/reset_admin_password.py <kullanici_adi> <yeni_sifre> --activate   # pasif kullanıcıyı da aktifleştirir
```

Betik şifreyi hemen ayarlar ve "ilk girişte şifre değiştir" zorunluluğunu kaldırır — betiği
çalıştırabilen kişinin zaten sunucuya erişimi olduğundan, arayüzde ayrıca zorlamanın bir
güvenlik faydası yok.

> Logout stateless'tır: JWT sunucu tarafında iptal edilmez, süresi dolana kadar geçerlidir.

## İzlenecek veritabanını hazırlama

dbace izlenen oturumu salt-okunura **zorlamaz**; güvenlik, verdiğiniz kimlik bilgisinin
yetkisine dayanır. Bu yüzden izleme için ayrı, yalnızca okuma yetkili bir kullanıcı açın.

### PostgreSQL

```sql
CREATE USER dbace_monitor WITH PASSWORD 'changeme';
GRANT pg_monitor TO dbace_monitor;
GRANT CONNECT ON DATABASE yourdb TO dbace_monitor;
```

`postgresql.conf` (yeniden başlatma gerektirir):

```
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = top
```

`track = top` (varsayılan) yalnızca en üst seviye ifadeleri kaydeder — dbace'in okuduğu tam
olarak budur. `track = all`, PL/pgSQL fonksiyon/tetikleyici içindeki her ifadeyi de kaydeder;
izlenen kayıt sayısını ve çalıştırma başına yükü artırır, ama dbace'in yavaş sorgu ekranına
bir şey katmaz. Yalnızca belirli bir fonksiyonun içini ayıklarken geçici olarak açın.

Gerçek çalıştırma planlarının yakalanması (auto_explain) ve yönetilen servislerdeki
(RDS, Azure, Cloud SQL) farklar: **[docs/AUTO_EXPLAIN.md](docs/AUTO_EXPLAIN.md)**.

### SQL Server

Bağlantı `aioodbc` ile, varsayılan olarak **ODBC Driver 18 for SQL Server** üzerinden kurulur
(bağlantı seçeneklerinde `odbc_driver` ile değiştirilebilir). DMV'leri okuyabilmek için izleme
kullanıcısına en az `VIEW SERVER STATE` yetkisi gerekir; yedek izleme `msdb` geçmişini okur.

SQL Server'da `statement_timeout` karşılığı yoktur; sorgular `SET LOCK_TIMEOUT 5000` ile
korunur. Bu, PostgreSQL'e göre daha zayıf bir korumadır (gerekçe SORULAR.md'de).

### Patroni cluster: host-agent

Patroni/etcd/HAProxy/Keepalived servis durumu, VIP sahipliği ve log tail için her düğüme
küçük bir ajan kurulur: **[agents/host-agent/README.md](agents/host-agent/README.md)**.
Host-agent işletim sistemi metriği (CPU/RAM/disk) **toplamaz**. Cluster değerlendirmesinin
nasıl yapıldığı: **[docs/CLUSTER_HEALTH.md](docs/CLUSTER_HEALTH.md)**.

## Testler

Her değişiklikten sonra en az şu ikisi yeşil olmalı:

```bash
cd backend && python -c "from app.main import app"
cd frontend && npm run build
```

### Backend (pytest)

```bash
cd backend
.venv/Scripts/python.exe -m pytest tests/ -q     # Windows
.venv/bin/python -m pytest tests/ -q             # Linux/macOS
python scripts/check_model_integrity.py          # import + Pydantic + OpenAPI bütünlüğü (CI'daki ilk adım)
```

pytest kendi veritabanını (`backend/data/dbace_pytest.db`) kullanır; geliştirme veritabanına
dokunmaz.

Frontend'in kendi birim test koşucusu **yok**. Arayüz garantileri backend'deki statik
denetim testleriyle korunuyor: `test_navigation_integrity.py`,
`test_frontend_state_handling.py`, `test_ui_consistency.py`, `test_ui_terminology.py`,
`test_definition_order.py`.

### Gerçek PostgreSQL'e karşı testler

Bazı davranışlar sahte bağlantıyla **doğrulanamaz**, çünkü kırılan şey sorgunun sunucuya
hangi protokolle gittiği, katalogun gerçek cevabı ya da pg_stat_statements'ın gerçekte ne
sakladığı. EXPLAIN özelliği bu yüzden üç tur boyunca "testler yeşil" görünürken canlıda çalışmadı.

Canlı testler (`tests/*_live_postgres.py`, `tests/test_real_value_cleanup_migration_live.py`)
`DBACE_TEST_PG_DSN` tanımlı değilse **atlanır**. Konteynerler elle değil betikle kurulur —
sürümler aynı ayarlarla, hypopg dahil; roller ve test verisi testlerin kendisi tarafından
idempotent kuruluyor (`tests/live_pg.py`):

```bash
cd backend
python scripts/live_pg.py up                 # PG 15, 16, 17 — eksiği tamamlar
python scripts/live_pg.py up --recreate      # silip sıfırdan kurar
# yazdırdığı DSN'i kullanın:
DBACE_TEST_PG_DSN=postgresql://postgres:dbace@127.0.0.1:55433/dbace,postgresql://postgres:dbace@127.0.0.1:55434/dbace,postgresql://postgres:dbace@127.0.0.1:55432/dbace \
    .venv/Scripts/python.exe -m pytest tests/ -q
```

Her sürümde `dbace_nohypopg` adlı, hypopg'SUZ ikinci bir veritabanı da kuruluyor — hypopg'nin
olmayacağı ortamlar (banka) bu yoldan geçiyor ve ayrı test ediliyor.

### Tarayıcı testleri (Playwright)

Backend testleri API katmanında durur; arayüz çökmelerini (rapor bulgu detayı, silinmiş kayda
gitme, boş form) yalnızca gerçek tarayıcı yakalar.

```bash
cd frontend
npx playwright install chromium   # ilk seferde bir kez
npm run test:e2e                  # tamamı
npm run test:e2e:critical         # yalnızca @critical akışlar
npm run test:e2e:ui               # etkileşimli mod
```

Playwright backend ve frontend'i **kendisi başlatır** (API 8001, web 5174); çalışan bir
`npm run dev` ile çakışmaz. E2E kendi SQLite dosyasını (`backend/data/dbace_e2e.db`) kullanır
ve her çalıştırmadan önce siler; toplayıcı kapalıdır (`RUN_MODE=api`), yani testler hiçbir
hedef veritabanına bağlanmaz.

Kırılan testin ekran görüntüsü, videosu ve izi `frontend/test-results/` altına yazılır:

```bash
npx playwright show-trace test-results/<klasör>/trace.zip
```

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) her push'ta şunları koşar:

| İş | Ne kontrol ediyor |
|---|---|
| Backend (Python 3.12) | Model bütünlüğü + pytest — canlıyla aynı Python sürümünde |
| Frontend (Node 20) | `tsc -b` + `npm run build` |
| Tip sürüklenmesi | OpenAPI'den tipleri yeniden üretir, commit'lenmiş hâliyle karşılaştırır |
| Tarayıcı testleri | Push'ta `@critical` akışlar; gecelik ve elle tetiklemede tam paket. Hata kanıtları artifact olarak yüklenir. |

**CI canlı PostgreSQL testlerini koşuyor (Faz 31 Commit 5):** `live-postgres` işi 15/16/17 matrisiyle `scripts/live_pg.py` kurulumunu kullanıyor; DSN tanımlıyken sürüm koşulu dışında atlanan canlı test oturumu kırmızıya çeviriyor (`tests/conftest.py`). Faz 31 Commit 6'dan beri her sürüme streaming replika da kuruluyor (`DBACE_TEST_PG_REPLICA_DSN`); SQL Server topoloji testleri `scripts/live_mssql.py` ile yalnızca yerelde. Yerel eşdeğer koşu (Commit 7): PG 15 1931 geçti / 2 atlandı / 1 xfail, PG 16 ve 17 1932 / 1 / 1; DSN'siz 1827 geçti / 107 atlandı. Gerçek veriyle e2e: `DBACE_TEST_PG_DSN=... npx playwright test e2e/live-counts.spec.ts`. Yerelde: `python scripts/live_pg.py up`, sonra yazdırdığı `DBACE_TEST_PG_DSN` ile `pytest`.

## API tipleri (TypeScript)

`frontend/src/api-types.ts` **otomatik üretilir, elle düzenlenmez.** Kaynağı backend'in
OpenAPI şemasıdır. Elle yazılmış tipler API'den sessizce ayrıştığı için canlı çökmeler
yaşandı; bu üretim o hata sınıfını derleme zamanına taşıyor.

Backend'de bir şema değiştirdiyseniz tipleri yeniden üretin ve sonucu commit'leyin:

```bash
cd frontend
npm run gen:types          # backend'i çalıştırmadan üretir (python gerekir)
npm run gen:types:live     # çalışan bir backend'in /openapi.json ucundan üretir
```

`gen:types:live` varsayılan olarak `http://localhost:8000` adresine bakar; `DBACE_API_URL`
ile değiştirilebilir. CI tipler güncel değilse kırmızı olur.

## PgBouncer / connection pooler arkasında çalışma

İzlenen bir PostgreSQL sunucusu (ya da dbace'in kendi metadata veritabanı — Supabase dahil)
`transaction` veya `statement` pool_mode'daki bir havuzlayıcının arkasındaysa şu hata
görülebilirdi:

```
prepared statement "__asyncpg_stmt_21__" already exists
pgbouncer cannot support prepared statements in transaction/statement pooling mode
```

**Sebep:** asyncpg sorguları varsayılan olarak isimli prepared statement olarak önbelleğe
alır; havuzlayıcı ardışık sorguları farklı sunucu bağlantılarına yönlendirince sorgu, onu hiç
görmemiş bir bağlantıda çalıştırılmaya çalışılır.

**Çözüm:** dbace her asyncpg bağlantısında (collector, aktivite, şema sağlığı, parametre
denetimi, EXPLAIN, index önerisi ve kendi metadata bağlantısı) `statement_cache_size=0`
kullanır — havuzlayıcı olsun ya da olmasın, ek maliyeti yoktur. `DATABASE_URL`
`postgresql+asyncpg://` ise aynı koruma otomatik uygulanır.

Hata yine de sızarsa API anlaşılır bir Türkçe mesaj döner. Veritabanı/düğüm ekleme formunda
ve sihirbazda **"Pooler kullanılıyor"** seçeneği vardır:

- **Otomatik algıla** (varsayılan) — host adı `pooler`/`pgbouncer` içeriyorsa ya da port
  `6432`/`6543` ise pooler kabul edilir.
- **Evet / Hayır** — standart olmayan host/port üzerindeki bir PgBouncer için elle işaretleyin.

## PostgreSQL sürüm yetenek matrisi

PostgreSQL sistem katalogları sürümden sürüme değişiyor: sütunlar taşınıyor, yeniden
adlandırılıyor, kaldırılıyor. dbace bu farkları **tek yerden** yönetiyor —
`backend/app/domain/pg_capabilities.py`. Sürüm eşiği koda dağılmıyor.

**Kural: bir metriğin alternatif kaynağı varsa "desteklenmiyor" denmez.**

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

PostgreSQL 17, `buffers_backend` sütununu `pg_stat_bgwriter`'dan kaldırdı; aynı bilgi
`pg_stat_io` içinde duruyor:

```sql
SELECT SUM(writes), SUM(fsyncs)
FROM pg_stat_io
WHERE object = 'relation' AND context = 'normal'
  AND backend_type NOT IN ('checkpointer', 'background writer');
```

PostgreSQL 18 `op_bytes` sütununu kaldırıp yerine gerçek bayt sayaçlarını koydu. Eski sütunu
sormaya devam etmek PG 18'de `pg_stat_io` sorgusunun tamamını düşürürdü (tek sütun yüzünden
`io_reads` ve `io_writes` de kaybolurdu); sorgu artık sürüme göre farklı sütun seçiyor.

Sorgu tarafındaki sürüm dallanmaları:

| Özellik | Gereken sürüm | Yoksa ne oluyor |
|---|---|---|
| `pg_stat_activity.query_id` (bekleme → sorgu eşleşmesi) | 14+ | Bekleme kırılımı üretiliyor, sorgu bazında ayrıştırılamıyor |
| `EXPLAIN (GENERIC_PLAN)` (yer tutuculu sorgu planı) | 16+ | Plan alınamıyor; sebebi ve auto_explain alternatifi yazılıyor |
| `pg_stat_statements` `total_exec_time` (vs `total_time`) | 13+ | 13 öncesinde eski sütun adları kullanılıyor |

Desteklenen aralık **PostgreSQL 12-18**. Altındaki sunucuya yine bağlanılır (en yakın
davranış + uyarı); üstündeki sürümlerde en yeni dal kullanılır, çünkü katalog değişiklikleri
neredeyse her zaman eklemelidir.

Arayüzde her metriğin kaynağı **Veritabanı detayı → Metrik kaynakları** altında görünür.

> Bu matris gerçek sunucuların tamamında doğrulanmadı; testler sürüm sahteleyerek hangi
> sorgunun gönderildiğini kanıtlıyor. Canlı doğrulama yalnızca 15 ve 17'de yapıldı.

## İzleme yükü

dbace, sağlıklı bir hedef sunucuda *izlenmenin maliyeti* sıfıra yakın kalacak şekilde
tasarlandı. Bu bölüm bir denetim listesi: dbace'in izlenen veritabanına gönderdiği her sorgu,
ne sıklıkla ve kabaca ne maliyetle — canlı sunucunuzda izlemeyi açmadan önce yükü
değerlendirebilmeniz için.

### Toplama döngüsü — her `COLLECT_INTERVAL_SECONDS` (varsayılan 15 sn, veritabanı başına değiştirilebilir)

Tur başına tek bağlantı (`services/collection.py`, `collectors/postgresql.py`), her sorguda
`statement_timeout = 5000ms`:

| Sorgu / kaynak | Ne okuyor | Maliyet |
|---|---|---|
| `current_setting('server_version_num')`, `version()` | bellek | ihmal edilebilir |
| `pg_stat_database` (tek satır) | bellekteki kümülatif sayaçlar | ihmal edilebilir |
| `SHOW max_connections` | GUC okuması | ihmal edilebilir |
| `pg_database_size(current_database())` | veritabanının ilişki dosyaları üzerinde dosya sistemi stat'ı | düşük; veri hacmiyle değil nesne sayısıyla ölçeklenir — büyük şemalarda bile birkaç ms |
| `pg_last_wal_receive_lsn()` / `pg_last_wal_replay_lsn()` | fonksiyon çağrısı | ihmal edilebilir |
| `pg_stat_checkpointer` (17+) ya da `pg_stat_bgwriter` (<17) | bellekteki kümülatif sayaçlar | ihmal edilebilir |
| `pg_stat_io` (yalnızca 16+, altında hiç sorulmaz) | bellekteki view | ihmal edilebilir |

Toplam: tek bağlantı üzerinden yaklaşık 7-8 hafif sorgu, normal bir sunucuda toplamda
genellikle birkaç milisaniye. SQL Server'daki karşılığı aynı listenin DMV/performans sayacı
eşdeğeridir ve `SET LOCK_TIMEOUT 5000` ile korunur.

### Yavaş sorgu toplama — her `SLOW_QUERY_INTERVAL_SECONDS` (varsayılan 300 sn)

| Sorgu / kaynak | Ne okuyor | Maliyet |
|---|---|---|
| `pg_stat_statements` (ortalama süreye göre ilk ~20) | eklentinin kendi bellek içi yapısı | ihmal edilebilir — eklenti tam olarak bunun için var |

Metriklerden seyrek toplanmasının sebebi hedef yükü değil, **dbace'in kendi depolaması**:
15 sn'de her tur 20 satır yazmak veritabanı başına ayda ~3,5 milyon satır demek.
`pg_stat_statements` kümülatif olduğu için 5 dakikalık örnekler aynı pencere farklarını verir;
aylık satır sayısı ~173 bine iner.

### Bekleme örnekleyicisi — her `WAIT_SAMPLE_INTERVAL_SECONDS` (varsayılan 1 sn)

Toplama döngüsünden ayrı ve çok daha sık: kümülatif sayaçları 15 sn'de okumak yeterli, ama
"sistem şu an ne bekliyor" sorusu anlık durumun sık fotoğrafını ister. 15 sn aralık 200 ms'lik
bir kilit fırtınasını tamamen kaçırır.

**Veritabanı başına tek kalıcı bağlantı**, turlar arasında yeniden kullanılır — her saniye
yeniden bağlanmak (TCP + TLS el sıkışması + backend fork) örnekleme sorgusundan pahalıya
patlardı. `statement_timeout = 1000ms` (bir saniyeden uzun süren örnekleme sorgusu zaten
bozuktur).

| Sorgu / kaynak | Ne okuyor | Maliyet |
|---|---|---|
| `pg_stat_activity`, sunucu tarafında `state='active'` filtreli | bellekteki view, backend başına bir satır | ihmal edilebilir — disk erişimi yok, katalog taraması yok |
| `pg_blocking_pids(pid)` | kilit yöneticisi taraması | **yalnızca zaten `Lock` bekleyen satırlarda** çağrılır — bir `CASE` koruması onu diğer satırlardan uzak tutar |
| SQL Server: `dm_exec_requests` + `dm_os_waiting_tasks` + `dm_exec_sql_text` | DMV'ler / plan önbelleği | düşük; aktif istek sayısı tanımı gereği azdır ve `TOP` en kötü durumu sınırlar |

Veritabanı başına tur başına tek sorgu, tek gidiş-dönüş. Veritabanları eşzamanlı örneklenir
(yavaş bir sunucu diğerlerini geciktirmez) ve iş `max_instances=1` ile çalışır (turlar üst
üste binmez).

**Maliyeti kendi sunucunuzda ölçmek** (örnekleyicinin sorgusu `pg_stat_statements`'ta
görünür):

```sql
SELECT calls,
       round(total_exec_time::numeric, 1) AS total_ms,
       round(mean_exec_time::numeric, 3)  AS mean_ms
FROM pg_stat_statements
WHERE query LIKE '%pg_stat_activity%'
  AND query LIKE '%parallel worker%'
ORDER BY total_exec_time DESC;
```

`mean_ms`, *sizin* donanımınız ve iş yükünüzdeki örnek başına maliyettir. Yoğun bir canlı
sunucuda örnekleyiciyi açmadan önce bakılacak sayı budur; hedef taraftaki maliyet proje
tarafından gerçek bir canlı sunucuda **ölçülmedi** (bkz. SORULAR.md).

**dbace'in kendi veritabanındaki depolama maliyeti** (bu ölçüldü):

Ham örnekler saklanmaz. 1 sn örneklemede, 10 eşzamanlı aktif oturumlu bir sunucu için satır
başına bir örnek günde ~864.000 satır demek. Bunun yerine örnekler bellekte biriktirilir ve
dakikada bir, `(dakika, queryid, bekleme kategorisi, bekleme olayı)` birleşimi başına tek satır
olarak yazılır.

Ölçülen: **indeksler dahil satır başına 192 bayt** (SQLite, 200 bin satır, gerçekçi queryid
çeşitliliği):

| Dakika başına farklı birleşim | Veritabanı başına günlük satır | Aylık (30 gün saklama) |
|---|---|---|
| 5 (sakin, az sayıda farklı sorgu) | 7.200 | ~41 MB |
| 15 (tipik karışık iş yükü) | 21.600 | ~124 MB |
| 50 (çok çeşitli iş yükü) | 72.000 | ~415 MB |

Bu, ham örnek saklamaya göre ~40 kat azalma; üç tablo da saklama politikasının
(`services/retention.py`) kapsamında.

Örnekleyiciyi tamamen kapatmak için `WAIT_SAMPLING_ENABLED=false`: hiçbir bağlantı açılmaz,
hedefe hiçbir sorgu gönderilmez.

### Dashboard yenileme — her `DASHBOARD_REFRESH_INTERVAL_SECONDS` (varsayılan 60 sn, en az 10 sn)

Patroni topolojili grup başına, hedef düğüme karşı:

| Kaynak | Ne okuyor | Maliyet |
|---|---|---|
| `parameter_audit` (`pg_settings`, ~10 adlı parametre) | bellekteki katalog view'ı | ihmal edilebilir; `statement_timeout = 5000ms` |
| `performance_insights` | zaten toplanmış `metrics_json` üzerinde bellek içi analiz | sıfır — hedefe hiç dokunmaz |
| cluster sağlık probları (Patroni/etcd/HAProxy/Keepalived) | TCP soket kontrolleri + Patroni/ajan REST API'lerine HTTP | sıfır veritabanı yükü — PostgreSQL/SQL Server protokol bağlantısı hiç açılmaz |

Index önerileri eskiden burada da her turda otomatik çalışıyordu (veritabanı başına canlı
katalog taraması); kaldırıldı, aşağıya bakın.

### Diğer periyodik işler

| İş | Aralık | Hedefe etkisi |
|---|---|---|
| Plan yakalama (auto_explain) | `PLAN_CAPTURE_INTERVAL_SECONDS` (300 sn) | Log host-agent üzerinden HTTP ile çekilir; PostgreSQL'e bağlanılmaz. SQL Server'da `system_health` deadlock'ları okunur. |
| Yedek izleme | `BACKUP_CHECK_INTERVAL_SECONDS` (900 sn) | Yedek geçmişi okuması (SQL Server'da `msdb`); `BACKUP_MONITORING_ENABLED=false` ile kapatılır |

### Yalnızca istek üzerine, kullanıcı tetikli, önbellekli (5 dk) — periyodik döngüde asla

| Kaynak | Uç | Maliyet | Not |
|---|---|---|---|
| Index önerisi | `POST /api/queries/{id}/advice` | katalog taraması (`pg_stats`/`pg_indexes`/`pg_class`) + isteğe bağlı 2× `EXPLAIN (FORMAT JSON)` ve `hypopg` ile varsayımsal index | orta; `statement_timeout = 8000ms`; yalnızca kullanıcı bir yavaş sorgunun öneri panelini açınca |
| EXPLAIN planı | `POST /api/queries/{id}/explain` (`analyze=false`, varsayılan) | yalnızca planlar, sorguyu çalıştırmaz | düşük; `statement_timeout = 8000ms` |
| EXPLAIN ANALYZE | aynı uç, `analyze=true` | **sorguyu gerçekten çalıştırır** | maliyet = sorgunun kendi gerçek maliyeti; arayüz göndermeden önce uyarılı açık onay ister |
| Aktivite / Şema sağlığı sekmeleri | `GET /api/instances/{id}/activity`, `/schema-health` | `pg_stat_activity` (bellek) / `pg_stat_user_indexes` + `pg_stat_user_tables` + `pg_relation_size` (katalog taraması) | düşük–orta; yalnızca sekme açıkken |

### Pratik sonuçlar

- `pg_stat_statements.track = top` (varsayılan) bırakın.
- Periyodik toplama döngüsü varsayılan 15 sn / 60 sn aralıkla canlıda güvenle çalışır; gerçek
  maliyet taşıyan istek üzerine araçlardır (index önerisi, EXPLAIN ANALYZE) ve bunlar açık bir
  kullanıcı eylemi + önbellek arkasındadır.
- Belirli bir sunucu düşük öncelikliyse ya da yükü daha da azaltmak istiyorsanız, genel
  varsayılanı herkes için düşürmek yerine yalnızca o veritabanının **Toplama aralığı**
  ayarını uzatın (veritabanını düzenle → "Toplama aralığı").

## Dokümanlar

| Ne arıyorsan | Nereye bak |
|---|---|
| Kodda yön bulma, servis sorumlulukları, süreç mimarisi | [docs/MIMARI.md](docs/MIMARI.md) |
| Ortam değişkenleri, migration sırası, deploy adımları | [DEPLOY.md](DEPLOY.md) |
| Bulut kurulumu (geliştirme/test) | [deploy/cloud/BULUT-KURULUM.md](deploy/cloud/BULUT-KURULUM.md) |
| Kapalı ortam (on-prem) paketi | [deploy/onprem/KURULUM.md](deploy/onprem/KURULUM.md), [docs/YASAM-DONGUSU.md](docs/YASAM-DONGUSU.md) |
| auto_explain kurulumu, yönetilen servisler | [docs/AUTO_EXPLAIN.md](docs/AUTO_EXPLAIN.md) |
| Cluster sağlık değerlendirmesi | [docs/CLUSTER_HEALTH.md](docs/CLUSTER_HEALTH.md) |
| Host-agent | [agents/host-agent/README.md](agents/host-agent/README.md) |
| Ne yapıldı, hangi karar neden verildi | [ILERLEME.md](ILERLEME.md) |
| Çözülmemiş / bilerek yapılmamış işler | [SORULAR.md](SORULAR.md) |
| Geliştirme kuralları (commit, migration, terminoloji) | [CLAUDE.md](CLAUDE.md) |
