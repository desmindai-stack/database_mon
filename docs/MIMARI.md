# dbace — Mimari

Bu doküman **ihtiyaç anında** okunacak bir referanstır: kodda yönünü bulmak, bir
sorumluluğun hangi dosyada olduğunu görmek için. Her oturumda okunması gereken kurallar
`CLAUDE.md`'de.

- Kurulum ve yerel çalıştırma → [README.md](../README.md)
- Ortam değişkenleri, migration sırası, deploy adımları → [DEPLOY.md](../DEPLOY.md)
- Kapalı ortam (on-prem) paketi → [YASAM-DONGUSU.md](YASAM-DONGUSU.md),
  [deploy/onprem/KURULUM.md](../deploy/onprem/KURULUM.md)
- Cluster sağlık değerlendirmesi → [CLUSTER_HEALTH.md](CLUSTER_HEALTH.md)
- auto_explain kurulumu ve yönetilen servisler → [AUTO_EXPLAIN.md](AUTO_EXPLAIN.md)

## Süreç mimarisi

Üç ayrı süreç; ikisi aynı kod tabanını `RUN_MODE` ile farklı modda çalıştırıyor.

```
┌─────────────────┐     HTTPS      ┌──────────────────────┐
│ React Dashboard │ ◄────────────► │ FastAPI (RUN_MODE=api)│
│ (Vercel)        │                │ (Railway)             │
└────────┬────────┘                └────────┬──────────────┘
         │                                   │
         │        Supabase PostgreSQL        │
         └──────────────┬────────────────────┘
                        │
              ┌─────────▼──────────────┐
              │ Worker (RUN_MODE=worker)│
              │ APScheduler döngüleri   │
              └─────────┬──────────────┘
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   PostgreSQL      SQL Server        MongoDB
```

| Bileşen | Nerede | Görev |
|---|---|---|
| **Frontend** | Vercel | Dashboard, veritabanı yönetimi, grafikler |
| **API** | Railway | REST, bağlantı testi, canlı sorgular (EXPLAIN, bloklama) |
| **Worker** | Railway (ayrı servis) | Toplama, örnekleme, alarm, tahmin, rapor |
| **Veritabanı** | Supabase (Postgres) | Metadata + metrik geçmişi |

Yerelde SQLite (`backend/data/dbace.db`), canlıda Postgres.
`RUN_MODE=all` ikisini tek süreçte çalıştırır (yalnızca yerel geliştirme).

### Worker'daki zamanlanmış işler

Aralıklar `backend/app/collectors/scheduler.py` içinde; hepsi ayarlanabilir.

| İş | Aralık | Ne yapıyor |
|---|---|---|
| `collect_all` | 15 sn | Metrik toplama (instance başına kendi aralığı olabilir) |
| `wait_event_sampling` | 1 sn | Aktif oturum örnekleme (AAS + bekleme kırılımı). Kalıcı bağlantı; her 10 sn'de bir bloklama kontrolü de bu bağlantı üzerinde |
| `evaluate_custom_rules` | 10 sn | Özel alarm kuralları (her kuralın kendi periyodu içeride kontrol ediliyor) |
| `refresh_dashboard_snapshots` | 60 sn | Dashboard/cluster health özeti |
| `auto_explain_plan_capture` | 5 dk | Host-agent log'undan gerçek planlar + PostgreSQL deadlock'ları; SQL Server'da `system_health` deadlock'ları |
| `prediction_accuracy` | 1 saat | Hedef tarihi gelmiş tahminleri gerçekleşenle karşılaştırma |
| `retention_cleanup`, `daily_rollup` | cron 03:00 / 03:30 | Saklama temizliği, günlük toplulaştırma |
| `daily_health_report` | cron (ayarlanabilir saat) | Zamanlanmış sağlık raporu |

**Günlük işler `cron` ile, `interval` ile değil.** `interval(days=1)` ilk çalışmasını
scheduler başladıktan 24 saat sonraya planlar; worker günde bir kereden sık yeniden
başlıyorsa (yeniden dağıtım, çökme) sayaç sıfırlanır ve iş **hiç çalışmaz**. Bu canlıda
yaşandı: saklama temizliği çalışmadığı için `slow_query_samples` sınırsız büyüdü.

## Backend yerleşimi

```
backend/app/
  main.py            FastAPI app, router bağlama, yetki dependency'leri
  worker.py          Ayrı süreç: APScheduler ile toplama döngüsü
  models.py          SQLAlchemy 2.0 async ORM modelleri
  schemas.py         Pydantic v2 — TANIM SIRASI ÖNEMLİ (bkz. CLAUDE.md kuralı)
  database.py        Session, init_db, SQLite'a özel kolon migration'ı
  config.py          Ortam değişkenleri (pydantic-settings)
  logging_setup.py   LOG_LEVEL + gürültülü kütüphane logger'larının susturulması
  domain/            Engine/metrik/topoloji sabitleri, bekleme taksonomisi,
                     PostgreSQL sürüm yetenek matrisi
  collectors/        Hedef veritabanına bağlanan kod
    postgresql.py      asyncpg; statement_timeout ile korunuyor
    sqlserver_mongodb.py  aioodbc / motor
    scheduler.py       APScheduler job tanımları
  routers/           HTTP uçları (instances, queries, reports, alerts, …)
  services/          İş mantığı — router'lar ince, ağırlık burada
```

### Servis sorumlulukları

| Servis | Ne yapar |
|---|---|
| `collection.py` | Toplama döngüsünün orkestrasyonu |
| `cluster_health.py`, `alwayson_health.py` | Cluster/AG sağlık değerlendirmesi |
| `parameter_audit.py` | Parametre sapması denetimi |
| `slow_query_selection.py` | **Yavaş sorgu seçiminin tek gerçeklik kaynağı** — rapor ve DPA ikisi de buradan besleniyor |
| `pgss.py`, `query_history.py`, `query_cache.py` | pg_stat_statements okuma, seri üretimi |
| `sql_analysis.py` | **SQL ayrıştırmanın tek yeri** (sqlglot) — CTE/takma ad ayrımı, kesik metin tespiti, EXPLAIN stratejisi |
| `explain_service.py`, `index_advisor.py`, `query_diagnostics.py`, `plan_analysis.py` | Sorgu tanısı, plan analizi, index önerisi |
| `auto_explain.py`, `plan_capture.py` | Gerçek çalıştırma planlarının log'dan yakalanması |
| `wait_sampling.py`, `database_load.py`, `wait_advice.py` | Bekleme örnekleme, AAS, bekleme tabanlı öneri |
| `blocking.py`, `blocking_history.py`, `deadlocks.py` | Bloklama zinciri, geçmiş olaylar, deadlock |
| `health_report.py` | Rapor motoru: bölüm kaydı, FindingDraft → ReportFinding, fingerprint, öncelik |
| `report_sections.py` | Rapor bölümlerinin bulgu üretimi |
| `executive_report.py`, `report_export.py`, `report_documents.py` | Yönetici anlatımı, PDF/CSV |
| `finding_status.py` | Bulgu durum makinesi, kapsam çözümü, geçmiş |
| `advice.py` | Beş parçalı öneri yapısı (bkz. CLAUDE.md kuralı) |
| `prediction.py`, `forecasting.py`, `prediction_playbooks.py`, `prediction_accuracy.py` | Tahmin, aksiyon planı, doğruluk geri beslemesi |
| `prerequisites.py` | Ön koşul kontrolü ve yoksayma |
| `alert_engine.py`, `custom_alert_rules.py` | Alarm değerlendirme |
| `dashboard.py`, `dashboard_snapshot.py` | Dashboard özeti ve derin bağlantıları |
| `auth_deps.py`, `security.py`, `credentials.py` | Kimlik/şifreleme |
| `retention.py`, `rollup.py` | Saklama süresi temizliği, toplulaştırma |
| `noise_settings.py` | Gürültü eşikleri (AppSetting tabanlı) |

## Frontend yerleşimi

```
frontend/src/
  App.tsx           rota tablosu + kenar çubuğu gezinme ağacı
  api.ts            API çağrıları + ApiError (HTTP status taşır)
  api-types.ts      backend OpenAPI şemasından ÜRETİLİYOR (npm run gen:types)
  auth.tsx          oturum context'i
  terminology.ts    kullanıcıya görünen terimler (bkz. CLAUDE.md)
  formFields.ts     engine/topolojiye göre alan görünürlüğü
  pages/            rota başına bir sayfa
  components/       PageState (yükleniyor/hata/bulunamadı/boş), ErrorBoundary, paneller
  hooks/            useUrlTab / useUrlFilter — sekme ve filtre URL'de
e2e/                Playwright tarayıcı testleri
```

**Tipler elle yazılmaz, türetilir.** `api-types.ts` OpenAPI şemasından üretiliyor ve
`api.ts` ondan türetiyor. Elle yazılmış tipler API'den sessizce ayrışıp canlı çökmelere yol
açtı (üç kez: `InstanceDependencies`, `ExplainResult`, `Instance`).
`tests/test_api_contract_alignment.py` bunu koruyor.

## Ortak metrik kataloğu

Tüm motorlar `backend/app/domain/metrics.py` içindeki **canonical** anahtarlara normalize
edilir: `active_connections`, `connection_utilization_pct`, `cache_hit_ratio`,
`transactions_per_sec` / `ops_per_sec`, `database_size_bytes`, `replication_lag_bytes`,
`deadlocks`, `temp_bytes` ve checkpoint/bgwriter/IO sayaçları.

Aynı metrik PostgreSQL sürümüne göre **farklı view'dan** gelebiliyor (ör. `buffers_backend`
17'den itibaren `pg_stat_bgwriter` yerine `pg_stat_io`). Bu eşleme
`backend/app/domain/pg_capabilities.py` içinde tek yerde duruyor; sürüm matrisi
[README](../README.md#postgresql-sürüm-yetenek-matrisi)'de tablo hâlinde.

## Alarm, tahmin ve bulgu — üçü ayrı şey

| Tür | Ne zaman | Nasıl |
|---|---|---|
| **Alarm** | Eşik AŞILDIĞINDA | Kullanıcı tanımlı `alert_rules`, olay geçmişiyle |
| **Tahmin** | Eşik aşılmadan ÖNCE | Trend regresyonu + güven aralığı; tarih tek nokta değil ARALIK. Doğruluk sonradan ölçülüp geri besleniyor (`prediction_outcomes`) |
| **Bulgu** | Rapor üretiminde | Bölümlerin ürettiği, kanıtlı ve durum makinesine bağlı kayıt |

## Güvenlik

- Instance şifreleri `CREDENTIALS_MASTER_KEY` ile Fernet şifreli saklanır (canlıda zorunlu).
- `/api/health` ve auth uçları dışında her uç `Authorization: Bearer` şart koşuyor;
  mutasyonlar ayrıca `role=admin` istiyor (`require_write_access`).
- İzlenen veritabanına verilen kimlik bilgisi **salt-okunur olmalı**: dbace oturumu
  salt-okunura zorlamıyor, güvenlik verilen yetkiye dayanıyor (bkz. CLAUDE.md sınırlar).
- Supabase service role yalnızca worker/API ortam değişkenlerinde; frontend'e verilmez.

## Diğer dizinler

`supabase/migrations/` (37 SQL), `agents/host-agent/` (servis durumu ve log tail sağlayan
ajan), `deploy/`, `docker/`.
