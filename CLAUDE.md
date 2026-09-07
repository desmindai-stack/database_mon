# dbace

## Vizyon

PostgreSQL ve SQL Server için kurumsal DBA izleme, performans analizi ve
troubleshooting platformu. Çok müşterili: public ve private müşteriler,
private'ta uygulama adı → veritabanı grubu eşlemesi (ör. "X Bank" →
uygulama "boa" → SQL Server Always On 4 düğüm; uygulama "aapara" →
PostgreSQL Patroni 3 düğüm).

- **PostgreSQL**: standalone + Patroni cluster (2-3 düğüm + DR düğümü).
  Düğüm başına postgresql/patroni/etcd/keepalived/haproxy servis durumu ve
  logları, lider/replika rolü, replikasyon lag, etcd quorum, VIP sahipliği,
  split-brain tespiti, parametre denetimi, index önerisi.
- **SQL Server**: standalone + Always On AG (DR düğümü dahil). DMV tabanlı
  collector, AG sağlık izleme, wait stats, yavaş sorgular.

Hedef kitle iki ayrı: **DBA** (derin, eyleme dönük, komutlu) ve **müşteri
yöneticisi** (özet, güvence, risk + trend).

## Mevcut durum

Çalışan ana özellikler:

- **Kimlik doğrulama**: JWT (access + refresh), admin/viewer rolleri. Viewer
  salt-okunur — `require_write_access` router seviyesinde her mutasyonu
  admin'e kapatıyor.
- **Çok müşterili yapı**: Customer → Application → DatabaseGroup → Node,
  ayrıca Server kayıtları. Sihirbazla grup/düğüm ekleme.
- **Cluster health**: Patroni/Always On, etcd quorum, split-brain, servis
  durumu, host-agent üzerinden log tail.
- **DPA (instance detay)**: metrik grafikleri (etkileşimli, sürükleyerek
  aralık seçme), yavaş sorgular, EXPLAIN, index önerisi, şema sağlığı,
  aktivite, ön koşul kontrolü, tuning.
- **Parametre denetimi**: `pg_settings` ↔ Patroni `/config` karşılaştırması.
- **Tahminler**: trend tabanlı kapasite/risk öngörüsü + adım adım playbook.
- **Sağlık raporu**: aynı veriden iki rapor — teknik (DBA) ve yönetici
  (müşteri). Zamanlanmış üretim, PDF/CSV dışa aktarma.
- **Bulgu durum makinesi**: açık | yoksayıldı | ertelendi | risk_kabul |
  planlandı | çözüldü_doğrulanacak | çözüldü. Not zorunlu, kapsam seçimi
  (instance/grup/uygulama/müşteri/global), değişiklik geçmişi.
- **Alarmlar**: varsayılan + özel kurallar, olay geçmişi.

### Canlı ortam

| Katman | Yer | Not |
|---|---|---|
| Frontend | Vercel | `frontend/`, Vite build |
| API | Railway | `RUN_MODE` boş/`api` → uvicorn |
| Worker | Railway (ayrı servis) | `RUN_MODE=worker` → `python -m app.worker` |
| Veritabanı | Supabase (Postgres) | `DATABASE_URL` zorunlu |

Yerelde SQLite (`data/dbace.db`), canlıda Postgres. Ortam değişkenlerinin
tam listesi ve migration sırası **DEPLOY.md**'de.

### Yerel çalıştırma

```bash
# backend (http://localhost:8000)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# frontend (http://localhost:5173)
cd frontend && npm install && npm run dev
```

### Testler

600 test (`backend/tests/`, 49 dosya), `pytest-asyncio` auto mode:

```bash
cd backend && .venv/Scripts/python.exe -m pytest tests/ -q   # Windows
```

Frontend'in kendi test koşucusu yok; frontend garantileri backend
tarafındaki statik denetim testleriyle korunuyor:
`test_navigation_integrity.py` (rota ↔ bağlantı eşleşmesi),
`test_frontend_state_handling.py` (boş/hata durumu ayrımı, yutulan hata),
`test_ui_consistency.py` (sayfalama, yetki ekranı, dar ekran),
`test_definition_order.py` (Python sürüm farkı — aşağıya bakın).

## Mimari

```
backend/app/
  main.py            FastAPI app, router bağlama, yetki dependency'leri
  worker.py          Ayrı süreç: APScheduler ile toplama döngüsü
  models.py          SQLAlchemy 2.0 async ORM modelleri
  schemas.py         Pydantic v2 — TANIM SIRASI ÖNEMLİ (aşağıya bakın)
  database.py        Session, init_db, SQLite'a özel kolon migration'ı
  config.py          Ortam değişkenleri (pydantic-settings)
  domain/            Engine/metric/topology sabitleri ve yardımcıları
  collectors/        Hedef veritabanına bağlanan kod
    postgresql.py      asyncpg; statement_timeout ile korunuyor
    sqlserver_mongodb.py  aioodbc / motor
    scheduler.py       APScheduler job tanımları
  routers/           HTTP uçları (instances, queries, reports, alerts, …)
  services/          İş mantığı — router'lar ince, ağırlık burada
```

Servislerin sorumlulukları (yönünü bulmak için):

| Servis | Ne yapar |
|---|---|
| `collection.py` | Toplama döngüsünün orkestrasyonu |
| `cluster_health.py`, `alwayson_health.py` | Cluster/AG sağlık değerlendirmesi |
| `parameter_audit.py` | Parametre sapması denetimi |
| `slow_query_selection.py` | **Yavaş sorgu seçiminin tek gerçeklik kaynağı** — rapor ve DPA ikisi de buradan besleniyor |
| `pgss.py`, `query_history.py`, `query_cache.py` | pg_stat_statements okuma, seri üretimi |
| `explain_service.py`, `index_advisor.py`, `query_diagnostics.py` | Sorgu tanısı ve öneri |
| `health_report.py` | Rapor motoru: bölüm kaydı, FindingDraft → ReportFinding, fingerprint, öncelik |
| `report_sections.py` | 12 rapor bölümünün bulgu üretimi |
| `executive_report.py` | Aynı veriden yönetici anlatımı |
| `report_export.py`, `report_documents.py` | PDF (ReportLab) / CSV |
| `finding_status.py` | Bulgu durum makinesi, kapsam çözümü, geçmiş |
| `advice.py` | Beş parçalı öneri yapısı (aşağıdaki kural) |
| `noise_settings.py` | Gürültü eşikleri (AppSetting tabanlı) |
| `prediction.py`, `forecasting.py`, `prediction_playbooks.py` | Tahmin ve aksiyon planı |
| `prerequisites.py` | Ön koşul kontrolü ve yoksayma |
| `alert_engine.py`, `custom_alert_rules.py` | Alarm değerlendirme |
| `dashboard.py`, `dashboard_snapshot.py` | Dashboard özeti ve derin bağlantıları |
| `auth_deps.py`, `security.py`, `credentials.py` | Kimlik/şifreleme |
| `retention.py`, `rollup.py` | Saklama süresi temizliği, toplulaştırma |

```
frontend/src/
  App.tsx        Rota tablosu + kenar çubuğu gezinme ağacı
  api.ts         Tüm API çağrıları + ApiError (status taşır)
  auth.tsx       Oturum context'i
  pages/         Rota başına bir sayfa
  components/    PageState (yükleniyor/hata/bulunamadı/boş), Pagination,
                 ErrorBoundary, rapor ve DPA panelleri
  hooks/         useUrlTab / useUrlFilter — sekme ve filtre URL'de
```

Diğer: `supabase/migrations/` (29 SQL), `agents/host-agent/` (servis durumu
ve log tail sağlayan ajan), `docs/` (MIMARI, CLUSTER_HEALTH,
YASAM-DONGUSU), `deploy/`, `docker/`.

## Kurallar

Her iş sonunda `python -c "from app.main import app"` ve `npm run build`
yeşil olmalı; kırıksa düzeltmeden commit atma.

- **Her iş ayrı commit.** Commit mesajı Türkçe ve ne yaptığını anlatsın —
  "düzeltmeler" değil, hangi davranışın nasıl değiştiği.
- **Şema değişikliğinde migration zorunlu**: `supabase/migrations/` altına
  ekle **ve** DEPLOY.md tablosuna işle. SQLite'ta `migrate_schema()` ile
  otomatik oluşması yeterli DEĞİL — o fonksiyon Postgres'te no-op, yani
  migration yazılmazsa canlıda kolon hiç oluşmaz.
- **Yerel Python 3.14, canlı 3.12.** Sürüme bağlı davranış farklarına
  dikkat: 3.14 (PEP 649) annotation'ları ertelemeli değerlendirir, 3.12
  hemen. Bu yüzden `schemas.py`'de bir tip, kendisini KULLANAN modelden
  önce tanımlı olmalı — yoksa yerelde sessizce geçer, canlıda import
  anında `NameError` verir. `tests/test_definition_order.py` bunu tarıyor.
- **Öneriler beş parçalı standarda uysun**: neden (iş etkisiyle),
  numaralı adımlar, adım başına komut, dikkat notları (kilit/süre/bakım
  penceresi/geri alma), doğrulama sorgusu. Öneri üretilemiyorsa NEDEN
  üretilemediğini yaz, boş bırakma.
- **Bulgu üretirken kanıt zorunlu.** Veri yetersizse uydurma; "yeterli
  veri yok" de ve neyin eksik olduğunu söyle.
- **Aynı veriyi gösteren yerler tek gerçeklik kaynağından beslensin**
  (rapor ↔ DPA gibi). Ayrı hesaplama = ayrı sonuç = güven kaybı.
- **Yönetici raporunda teknik detay ASLA görünmez**: sorgu metni,
  parametre adı, komut, log satırı, IP/host.
- Mevcut API'yi kırmak zorunda kalırsan `ILERLEME.md`'ye gerekçesiyle yaz.
- Çözemediğin/çözmediğin şeyi `SORULAR.md`'ye nedeniyle yaz.
- **İş büyükse böl ama sırayı bozma.** Yarıda kalırsan nerede kaldığını
  ILERLEME.md'ye yaz.
- `deploy/`, `railway.toml`, `frontend/vercel.json`, Dockerfile'lara
  dokunma.
- `.env`, `data/`, `*.db`, keystore dosyalarını commit etme.
- Kullanıcıya görünen metinler Türkçe, kod ve tanımlayıcılar İngilizce.

## Bilinen sınırlar

Detayları SORULAR.md'de; burada yalnızca "bunu bilerek yapmadık" listesi:

- **Host-agent OS metriği toplamıyor.** Protokol yalnızca servis durumu ve
  log tail sağlıyor; CPU/RAM/disk kullanımı yok. Bu yüzden "kaynak
  artırımıyla çözülür" ayrımı yapılamıyor ve disk dolma tahmini gerçek
  kapasiteye değil, veri büyüme trendine dayanıyor.
- **SQL Server'da yürütme süresi koruması daha zayıf.** PostgreSQL'de
  `statement_timeout` var; SQL Server'da karşılığı olmadığı için yalnızca
  `LOCK_TIMEOUT` uygulanıyor (en yaygın takılma sebebini kapsar, CPU/IO
  bound saf yürütmeyi kapsamaz). Hiçbir engine'de oturum salt-okunur'a
  zorlanmıyor — güvenlik verilen kimlik bilgisinin yetkisine dayanıyor.
- **Şema bölümü günlük fotoğrafa dayanıyor**, arayüzdeki Şema sekmesi
  canlı tarama yapıyor; gün içinde ikisi ayrışabilir.
- **Cluster grup bulguları anlık**, dönemsel değil — `GroupHealthSnapshot`
  grup başına tek satır tutuyor.
- **Sistem sorgusu tespiti desen tabanlı**, kusursuz değil.
- **EXPLAIN uygulanabilirliği sorgu metninden çıkarılıyor**, denenerek
  değil.
- **Sayfalama istemci tarafında**; sunucu hâlâ tüm satırları gönderiyor.
- **Logout stateless** — JWT sunucu tarafında iptal edilmiyor.
- **Rol gizleme frontend'de tam kapsamlı değil**; backend her zaman
  otoriter (yetki kontrolü orada).
