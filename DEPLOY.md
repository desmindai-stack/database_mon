# Deploy öncesi kontrol listesi — feature/multi-tenant-cluster

Bu dosya, bu dalda birikmiş şema değişikliklerini Supabase (production Postgres)
ve Railway'e (backend hosting) güvenle uygulamak için hazırlandı. Aşağıdaki
migration sırası ve ortam değişkeni listesi, `backend/app/models.py` ile
`supabase/migrations/` karşılaştırılarak çıkarıldı — eksik bulunan iki migration
(`users` tablosu, `prediction_insights.recommendation`/`.action`) bu kontrol
sırasında yazıldı, bkz. aşağıdaki "Bu kontrolde bulunan eksikler" bölümü.

## Bu kontrolde bulunan eksikler

SQLite'ta `init_db()` her açılışta hem `Base.metadata.create_all` (eksik
TABLOLARI oluşturur) hem de `migrate_schema()` (eksik KOLONLARI ekler) çalıştırıyor.
`migrate_schema()` Postgres için **no-op** — sadece `sqlite://` URL'lerinde
çalışıyor (`backend/app/database.py`). Bu yüzden SQLite'ta "otomatik" görünen
bazı kolonlar Supabase'e hiç uygulanmamıştı:

1. **`users` tablosu — tamamen eksikti.** Faz 15 İŞ 1 (kimlik doğrulama) ile
   eklenen `User` modelinin Supabase karşılığı hiç yazılmamış. `create_all`
   yeni bir tabloyu teorik olarak örtük oluşturabilir (checkfirst=True, tablo
   yoksa) ama bu, DB rolünün `CREATE TABLE` yetkisine ve `init_db()`'nin
   production'da gerçekten çalışmasına bel bağlar — projedeki diğer her
   tablo gibi açık bir migration'la izlenmesi gerekiyordu. Eklendi:
   `supabase/migrations/20260901090000_users_table.sql`.
2. **`prediction_insights.recommendation` / `.action` — eksikti.** Faz 15 İŞ 6
   ile eklendi ama `prediction_insights` tablosu Supabase'de İLK migration'dan
   beri zaten var olduğundan `create_all` bu iki kolonu **atlar** (checkfirst
   sadece tablo var mı bakar, kolon var mı bakmaz). Bu migration olmadan
   predictions okuyan her sorgu Supabase'de `column "recommendation" does not
   exist` ile patlardı. Eklendi:
   `supabase/migrations/20260901100000_prediction_insight_recommendation.sql`.

Kontrol edilen ve migration'ı ZATEN mevcut olduğu doğrulanan her şey: Customer,
Application, DatabaseGroup (environment, access_name, cluster_name,
vip_address, listener_port), Server (ip_address dahil), Node (instance_id,
instance_name, server_id dahil), GroupHealthSnapshot, AlertRule.group_id,
AlertEvent.group_id + nullable instance_id, Instance.group_id/server_version/
unsupported_metrics/collect_interval_seconds, MetricSample, SlowQuerySample
(IO/plan/exec kolonları dahil), AppSetting tablosu. `Instance.options` ve
`Node.options` zaten JSONB olduğundan `uses_pooler` (PgBouncer/Supabase pooler
tespiti, bkz. son commit) ve saklama/yenileme ayarları (`app_settings`
key/value satırları) için **ayrı bir migration gerekmiyor** — bunlar mevcut
genel amaçlı kolonlara/tabloya yeni değerler olarak yazılıyor, şema
değişmiyor.

## Migration sırası

`supabase/migrations/` altındaki dosyalar dosya adındaki zaman damgasına göre
sıralı — bu SIRAYLA, Supabase SQL Editor'e yapıştırılıp **Run** ile ya da
`supabase db push` ile çalıştırılmalı (bkz. `deploy/cloud/BULUT-KURULUM.md`
Ek A.2). Hepsi `IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` kullanıyor, yani
daha önce kısmen çalıştırılmış bir ortamda tekrar çalıştırmak güvenli
(idempotent) — ama sıra önemli, özellikle 8 ve 9. maddeler birbirine bağımlı.

| # | Dosya | Ne yapıyor |
|---|---|---|
| 1 | `20250717120000_dbace_core.sql` | Çekirdek tablolar: instances, metric_samples, slow_query_samples, alert_rules, alert_events, prediction_insights |
| 2 | `20250718180000_add_instance_metadata.sql` | instances: customer_name/environment/application/cluster_name/role/services |
| 3 | `20250718210000_add_slow_query_io_columns.sql` | slow_query_samples: shared/local/temp blk + plan/exec time kolonları |
| 4 | `20250719140000_query_history_index.sql` | Sadece index (queryid bazlı geçmiş sorgular) |
| 5 | `20260825120000_multi_tenant_cluster.sql` | customers, applications, database_groups, nodes tabloları + instances.group_id |
| 6 | `20260825130000_node_credentials_and_group_alerts.sql` | alert_rules.group_id, alert_events.group_id, **alert_events.instance_id NOT NULL kaldırılıyor** |
| 7 | `20260826100000_group_environment.sql` | database_groups.environment |
| 8 | `20260827090000_group_health_snapshots.sql` | group_health_snapshots tablosu |
| 9 | `20260827100000_group_access_name.sql` | database_groups.access_name |
| 10 | `20260828090000_node_instance_link.sql` | nodes.instance_id (+ index) |
| 11 | `20260828100000_group_cluster_fields.sql` | database_groups.cluster_name, vip_address |
| 12 | `20260828110000_app_settings.sql` | app_settings tablosu (saklama/yenileme ayarları buraya yazılır, şema değişikliği gerektirmez) |
| 13 | `20260828120000_alert_rules_custom.sql` | alert_rules: rule_type/is_default/severity/engine/sql_query/interval_seconds/last_run_at |
| 14 | `20260829090000_server_model.sql` | **⚠️ servers tablosu + veri taşıma + kolon SİLME — aşağıya bakın** |
| 15 | `20260830090000_instance_server_version.sql` | instances.server_version, instances.unsupported_metrics |
| 16 | `20260830100000_instance_collect_interval.sql` | instances.collect_interval_seconds |
| 17 | `20260831090000_wizard_model_fields.sql` | database_groups.listener_port, servers.ip_address |
| 18 | `20260901090000_users_table.sql` | **YENİ** — users tablosu (kimlik doğrulama) |
| 19 | `20260901100000_prediction_insight_recommendation.sql` | **YENİ** — prediction_insights.recommendation, .action |
| 20 | `20260903090000_prediction_forecasting.sql` | **YENİ** — prediction_insights.lower_bound/.upper_bound/.seasonality, metric_rollup_daily ve schema_object_daily_samples tabloları |
| 21 | `20260904090000_instance_ignored_prerequisites.sql` | **YENİ** — instances.ignored_prerequisites (yoksayılan ön koşul kontrolleri) |
| 22 | `20260904100000_prediction_playbook.sql` | **YENİ** — prediction_insights.playbook (adım adım çözüm planı) |
| 23 | `20260905090000_health_reports.sql` | **YENİ** — health_reports, report_findings, finding_acknowledgements tabloları (Sağlık Raporu) |
| 24 | `20260905100000_daily_state_snapshots.sql` | **YENİ** — daily_state_snapshots (günlük parametre/ön koşul fotoğrafı) |

`14` numaralı dosya `9` numaralıdan (nodes.instance_id) SONRA çalışıyor olsa
da bağımsızdır — asıl bağımlılığı `5` numaralı dosyadaki `nodes` tablosunun
o anki `host`/`site`/`agent_url`/`agent_token` kolonlarına duyuyor (aşağıya
bakın), bu yüzden sıra listede yazıldığı gibi korunmalı.

## ⚠️ Veri kaybı riski taşıyan değişiklikler

**`20260829090000_server_model.sql` — kolon silme (tek gerçek veri kaybı
riski taşıyan migration bu dalda):**

- `nodes` tablosundaki `host`, `site`, `agent_url`, `agent_token`
  kolonlarını, her (customer, host) çifti için bir `Server` satırı
  oluşturup her `node.server_id`'yi buna bağladıktan SONRA **SİLİYOR**
  (`ALTER TABLE nodes DROP COLUMN ...`).
- Migration bunu bir tek transaction'da (`DO $$ ... END $$`) yapıyor ve
  silmeden önce `WHERE n.server_id IS NULL` guard'ıyla backfill'i çalıştırıyor
  — yani veri kaybetmeden taşıyor, TEORİDE güvenli.
- **Gerçek risk:** backfill sorgusu `database_groups → applications →
  customers` zincirini JOIN ederek `nodes.group_id`'den `customer_id`'ye
  ulaşıyor. Bu zincirde KOPUK bir satır varsa (ör. elle/kısmi bir migration
  sonrası tutarsız veri) o node için `server_id` NULL kalır ve host/site/
  agent bilgisi **sessizce kaybolur** (kolon zaten siliniyor, hata
  vermiyor). **Prod'da çalıştırmadan önce mutlaka:**
  ```sql
  SELECT count(*) FROM nodes WHERE server_id IS NULL;  -- migration SONRASI, 0 olmalı
  ```
  ve migration ÖNCESİ bir yedek alın (aşağıdaki "Geri alma planı"na bakın).
- Bu migration `IF EXISTS (... column_name = 'host')` guard'ı sayesinde
  ikinci kez çalıştırıldığında no-op'tur (kolon zaten silinmişse DO bloğu
  hiç girmez) — tekrar çalıştırmak güvenli, ama GERİ ALINAMAZ (kolonlar
  gitmiş olur).

**`alert_events.instance_id` NOT NULL → nullable (madde 6):** Bu yönde bir
kısıtlama gevşetmesi (NOT NULL EKLEME değil, KALDIRMA) — veri kaybı riski
taşımıyor, mevcut satırlar etkilenmiyor.

**NOT NULL eklemeleri:** Bu daldaki hiçbir migration, var olan bir tabloya
DEFAULT'suz bir NOT NULL kolon eklemiyor — her ADD COLUMN ... NOT NULL bir
DEFAULT ile birlikte geliyor (ör. `database_groups.environment DEFAULT
'prod'`, `alert_rules.rule_type DEFAULT 'metric'`), bu yüzden mevcut
satırlar için değer sorunu oluşmuyor.

**Tip değişiklikleri:** Bu dalda hiçbir `ALTER COLUMN ... TYPE ...` yok —
kontrol edildi, risk yok.

## Railway ortam değişkenleri

`backend/app/config.py::Settings` — her alan `UPPER_SNAKE_CASE` env var'a
karşılık gelir (pydantic-settings, case-insensitive).

| Değişken | Zorunlu mu | Varsayılan | Eksikse ne olur |
|---|---|---|---|
| `DATABASE_URL` | **Zorunlu (prod)** | `sqlite+aiosqlite:///...data/dbace.db` | Eksikse container içindeki geçici SQLite dosyasına düşer — her redeploy'da **veri tamamen kaybolur**, ayrıca `RUN_MODE` ile API/worker ayrı Railway servisleri ise ikisi de KENDİ SQLite dosyasına yazar (paylaşılmaz) → tutarsız/bozuk veri. Supabase connection string'i (`postgresql://...`) olmalı. |
| `CREDENTIALS_MASTER_KEY` | **Zorunlu (prod)** | yok | Eksikse `services/credentials.py::encrypt_secret` instance/node parolalarını **şifrelemeden**, `plain:` önekiyle düz metin olarak DB'ye yazar (bir uyarı logluyor ama engellemiyor) — ciddi güvenlik açığı. |
| `JWT_SECRET` | **Zorunlu (prod)** | `dev-insecure-secret-change-me-in-production` | Eksikse herkese açık, kaynak kodunda görünen bu varsayılan anahtar kullanılır — herkes geçerli bir JWT SAHTELEYEBİLİR (tam kimlik doğrulama bypass'ı). Açılışta uyarı loglanır ama engellenmez. |
| `ADMIN_USERNAME` | Önerilir | `admin` | Eksikse varsayılan `admin` kullanılır — sorun değil, sadece hatırlatma: username'i .env'de override etmediyseniz varsayılan öngörülebilir. |
| `ADMIN_PASSWORD` | Önerilir | yok (rastgele üretilir) | Eksikse ilk açılışta rastgele bir şifre üretilip **sadece Railway loglarına bir kez** yazılır — o log satırını kaçırırsanız `backend/scripts/reset_admin_password.py` ile kurtarma gerekir (bkz. README "İlk kurulum"). |
| `CORS_ORIGINS` | **Zorunlu (prod)** | `["http://localhost:5173","http://127.0.0.1:5173"]` | Eksikse sadece localhost'a izin verilir — production frontend (Vercel) API'ye tarayıcıdan erişemez (CORS hatası). Prod frontend URL'sini (ör. `["https://dbace.vercel.app"]`) eklemek gerekir. |
| `RUN_MODE` | **Zorunlu (çok servisli deploy'da)** | `all` | `railway.toml`'daki başlangıç komutu SADECE `RUN_MODE=worker` iken ayrı `python -m app.worker` sürecini başlatır; başka her değerde (boş, `api`, veya bir YAZIM HATASI) `uvicorn` API süreci çalışır. API süreci İÇİNDE arka plan toplama döngüsü ise `settings.run_mode in ("worker","all")` iken başlar — yani `RUN_MODE=api` API'yi çalıştırır ama **toplama yapmaz** (bilerek), `RUN_MODE`'da bir YAZIM HATASI ise API yine çalışır görünür ama toplama SESSİZCE devre dışı kalır. Tek servisli (all-in-one) bir Railway deploy'da bu değişkeni HİÇ ayarlamayın (varsayılan `all` doğru olan). İki ayrı Railway servisi (biri API, biri worker) kullanıyorsanız worker servisine `RUN_MODE=worker`, API servisine `RUN_MODE=api` (veya boş bırakın) verin — İKİSİNİ DE `all` bırakırsanız toplama döngüsü İKİ KEZ çalışır (izlenen sunuculara çift yük, çift alarm değerlendirmesi). |
| `DEPLOYMENT_MODE` | Opsiyonel | `public` | `private` moda geçmek için `DEFAULT_CUSTOMER_NAME` ile birlikte ayarlanır (tek müşterili izole kurulum) — eksikse çok müşterili varsayılan dashboard davranışı sürer. |
| `DEFAULT_CUSTOMER_NAME` | Sadece `DEPLOYMENT_MODE=private` iken zorunlu | yok | `private` modda bu eksikse ilk açılışta hangi müşteri için bootstrap yapılacağı belirsiz kalır — `ensure_default_customer()` mantığını kontrol edin. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Opsiyonel | `60` | Eksikse 60 dakika. |
| `REFRESH_TOKEN_EXPIRE_DAYS` | Opsiyonel | `7` | Eksikse 7 gün. |
| `COLLECT_INTERVAL_SECONDS` | Opsiyonel | `15` | Global toplama aralığı — instance bazında override edilebilir. |
| `DASHBOARD_REFRESH_INTERVAL_SECONDS` | Opsiyonel | `60` | Dashboard/cluster health yenileme aralığı. |
| `API_HOST` / `API_PORT` | Kullanılmıyor (Railway) | `0.0.0.0` / `8000` | `railway.toml`'daki startCommand Railway'in kendi `$PORT`'unu kullanıyor (`--port ${PORT:-8000}`) — bu iki değişken sadece Docker-dışı/yerel çalıştırmalar için, Railway'de ayarlamaya gerek yok. |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_ROLE_KEY` | **Kullanılmıyor** | yok | `Settings`'te tanımlı ama backend kodunun hiçbir yerinde okunmuyor (grep ile doğrulandı) — muhtemelen ileride Supabase Auth/Storage entegrasyonu için ayrılmış. Şu an ayarlanmasa da hiçbir fark etmez; DATABASE_URL yeterli. |

## Geri alma planı

**Kod (Railway):** Railway'in kendi deploy geçmişinden bir önceki başarılı
deploy'a **Rollback** ile dönün (Railway dashboard → Deployments → önceki
deploy → Redeploy). Bu sadece çalışan imajı değiştirir, DB şemasına
dokunmaz.

**Şema (Supabase) — bu daldaki migration'lar için:**

1. **Genel prensip:** Bu migration'ların hiçbiri "down" migration
   içermiyor (proje bu pratiği kullanmıyor, tek yönlü ileri migration'lar
   var) — geri almanın en güvenli yolu Supabase'in **Point-in-Time
   Recovery**'si (Project Settings → Database → Backups) ile migration'ı
   çalıştırmadan HEMEN ÖNCEki bir zamana dönmek. Bu yüzden her migration
   partisini çalıştırmadan önce mutlaka bir manuel yedek/PITR referans
   noktası alın.
2. **Sadece bu kontrolde eklenen 2 yeni migration'ı geri almak isterseniz**
   (ör. kod tarafı henüz deploy edilmediyse ve şemayı eski haline
   döndürmek istiyorsanız), elle çalıştırın:
   ```sql
   ALTER TABLE prediction_insights DROP COLUMN IF EXISTS recommendation;
   ALTER TABLE prediction_insights DROP COLUMN IF EXISTS action;
   DROP TABLE IF EXISTS users;
   ```
   **Dikkat:** `DROP TABLE users` mevcut kullanıcı hesaplarını (admin dahil)
   kalıcı olarak siler — sadece kod tarafı da geri alınıyorsa (auth
   endpoint'leri devre dışıysa) çalıştırın, aksi halde kimse giriş
   yapamaz hale gelir.
3. **`20260829090000_server_model.sql`'i (nodes → servers taşıması) geri
   almak GERİ ALINAMAZ** — kolonlar silindiği için orijinal `host`/`site`/
   `agent_url`/`agent_token` verisi migration çalıştıktan sonra sadece
   `servers` tablosunda yaşıyor. Bu migration'ı yanlışlıkla çalıştırdıysanız
   TEK yol PITR ile migration öncesine dönmek.
4. Herhangi bir geri alma sonrası, kodun o şema sürümüyle uyumlu bir commit'e
   (Railway rollback'iyle birlikte) döndüğünü doğrulayın — şema ile kod
   sürümü uyuşmazsa (ör. yeni kod eski şemaya karşı çalışırsa) `column ...
   does not exist` hatalarıyla karşılaşırsınız (tam olarak bu kontrolün
   bulduğu türden hatalar).

## Deploy öncesi son kontrol

```bash
cd backend && python -c "from app.main import app"   # yeşil olmalı
cd frontend && npm run build                          # yeşil olmalı
```

Migration'ları Supabase'e uyguladıktan sonra, backend'i Supabase'e karşı bir
kere yerelde çalıştırıp (`DATABASE_URL` prod değerine ayarlanmış, dikkatli
kullanın) `GET /api/health` ve bir `GET /api/predictions` ile en azından yeni
kolonların gerçekten okunabildiğini doğrulayın.
