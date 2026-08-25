# İlerleme — feature/multi-tenant-cluster

Tüm iş `feature/multi-tenant-cluster` dalında, `master`'a dokunulmadan ve
push edilmeden yapıldı. Her faz ayrı commit — `git log master..feature/multi-tenant-cluster`
ile tek tek görülebilir. Karar veremediğim / varsayımla ilerlediğim noktalar
`SORULAR.md`'de, gerekçeleriyle.

## Ne yapıldı

**Faz 0 — Temizlik.** `backend/app/collector/pg_collector.py` hiçbir yerden
import edilmiyordu, silindi. `collector/scheduler.py` → `collectors/scheduler.py`
taşındı, `main.py`/`worker.py` importları güncellendi, boşalan `collector/`
klasörü kaldırıldı.

**Faz 1 — Veri modeli.** Yeni tablolar: `Customer`, `Application`,
`DatabaseGroup`, `Node` (SQLAlchemy modelleri `backend/app/models.py`,
Pydantic şemaları `backend/app/schemas.py`). CRUD router'ları:
`/api/customers`, `/api/applications` (`?customer_id=` filtresi),
`/api/groups` (`?application_id=` filtresi, + `/api/groups/{id}/nodes`),
`/api/nodes`. `Instance`'a nullable `group_id` eklendi — mevcut instance
akışı hiç değişmedi. Supabase migration:
`supabase/migrations/20260825120000_multi_tenant_cluster.sql`; SQLite'ta
aynı kolon `database.py`'deki mevcut auto-migration mekanizmasıyla eklendi.
`backend/scripts/seed_demo.py` X Bank örneğini (boa: SQL Server Always On
4 düğüm/1 DR; aapara: PostgreSQL Patroni 3 düğüm/1 DR) SQLite'a ekler —
elle çalıştırılır, otomatik tetiklenmez, idempotent.

**Faz 2 — Çok düğümlü cluster health.** `services/cluster_health.py`'deki
tek-instance probe fonksiyonları (`probe_postgresql`, `probe_patroni`,
`probe_etcd`, `probe_haproxy`, `probe_keepalived`) host-parametreli
`*_host()` versiyonlarına ayrıldı; eski fonksiyonlar bunların ince
wrapper'ı oldu, davranış birebir korundu. Yeni `probe_node()` +
`collect_group_health()`: gruptaki her düğümü paralel prob'lar, etcd
quorum'unu (çoğunluk) hesaplar, split-brain'i (birden fazla düğümde
`holds_vip=true`) tespit eder, `postgresql` servisi down olan düğümleri
site bilgisiyle listeler. Patroni `/cluster` member satırlarına `lag`
eklendi. Yeni endpoint: `GET /api/groups/{id}/health`.
`alert_engine.GROUP_RULE_SPECS` yeni metrik kataloğu (henüz kalıcı
`AlertEvent`'e yazılmıyor — bkz. SORULAR.md).

**Faz 3 — PostgreSQL parametre denetimi.** `services/parameter_audit.py`:
gruptaki primary (yoksa ilk) düğümden `pg_settings`'i okur
(`shared_buffers`, `work_mem`, `max_connections`, `wal_level`,
`max_wal_senders`, `checkpoint_timeout/completion_target`,
`autovacuum(_max_workers/_naptime/_vacuum_scale_factor/_analyze_scale_factor)`),
bellek/zaman birimlerini normalize eder, best-practice baseline'dan sapmaları
severity ile listeler. Patroni gruplarında `/config`'i de döner. Endpoint:
`GET /api/groups/{id}/parameters`. Node'da DB kimlik bilgisi olmadığından
`node.options.db_username/db_password/db_database` okunuyor (varsayım,
SORULAR.md'de).

**Faz 4 — SQL Server gerçek collector.** `collectors/sqlserver_mongodb.py`
`SqlServerCollector`: `aioodbc` lazy import (sürücü yoksa temiz hata döner).
DMV tabanlı `collect_metrics` (bağlantı sayısı, Batch Requests/sec →
canonical `transactions_per_sec`, buffer cache hit ratio, deadlock sayacı,
db/tempdb boyutu, blocked session sayısı), `collect_slow_queries`
(`sys.dm_exec_query_stats` + `sys.dm_exec_sql_text`), `collect_activity`
(`sys.dm_exec_sessions/requests/connections`, mevcut `ActivityOut` şekline
eşlendi). `requirements.txt`'e `aioodbc` eklendi. Yeni
`services/alwayson_health.py`: `sys.dm_hadr_availability_group_states` /
`_replica_states` / `_database_replica_states`'ten AG durumu, her replikayı
`Node`'a `replica_server_name` ile eşleyip site/DR bilgisini ekliyor,
`failover_ready` hesaplıyor (senkron commit + tüm DB'ler SYNCHRONIZED).
Endpoint: `GET /api/groups/{id}/alwayson`.

**Faz 5 — Frontend.** Yeni sayfalar: `CustomersPage`, `ApplicationsPage`,
`DatabaseGroupsPage`, `GroupDetailPage` (`/customers` → `/customers/:id/applications`
→ `/applications/:id/groups` → `/groups/:id`). Group Detail'de düğüm
kartları (rol, site — DR ayrı etiket/renk), on-demand sağlık kontrolü
(etcd quorum, split-brain uyarısı, Patroni leader/members+lag, servis
pill'leri), PostgreSQL grupları için parametre denetimi tablosu, SQL
Server grupları için Always On replika kartları. Sidebar'a "Müşteri
Grupları" linki eklendi (mevcut `Instance`-tabanlı "Müşteriler" ağacından
bilerek ayrı tutuldu — SORULAR.md). Dashboard/Instances/Alerts/Predictions
sayfaları değişmedi.

**Faz 6 — Node credential şifreleme + grup alarmlarının kalıcılığı.**
`services/credentials.py`'e `encrypt_node_options`/`decrypt_node_options`/
`redact_node_options` eklendi; `node.options.db_password` artık
`Instance.password` ile aynı Fernet mekanizmasıyla şifreleniyor. Node
router'ları (`create`/`get`/`update`/`list_group_nodes`) şifre yazarken
şifreliyor, hiçbir API yanıtında `"***"`'den başka bir şey döndürmüyor;
`parameter_audit.py`/`alwayson_health.py` bağlanmadan önce çözüyor.
`AlertRule`/`AlertEvent`'e nullable `group_id` eklendi (`AlertEvent.
instance_id` de nullable oldu); `alert_engine.ensure_group_alert_rules()`/
`evaluate_group_alerts()` `GET /api/groups/{id}/health`'e wire edildi —
her health çağrısı artık `replication_lag_bytes`/`etcd_quorum_lost`/
`split_brain`/`node_down` flag'lerini kalıcı `AlertEvent` satırlarına
yazıyor, tekrarlı çağrılarda mükerrer event açmıyor. Alerts sayfası
`group_id`'yi "Group #id" olarak gösterip grup detayına linkliyor.

**Faz 7 — İŞ 2: deployment mode + environment ayrımı.** `GET /api/config`
(`deployment_mode`, `default_customer_name`). Private modda backend açılışta
(`services/bootstrap.ensure_default_customer`) hiç müşteri yoksa
`DEFAULT_CUSTOMER_NAME` ile tek müşteriyi otomatik oluşturuyor —
idempotent. Frontend private modda sidebar'ı doğrudan o müşterinin
Applications listesine yönlendiriyor ("Uygulamalar" etiketiyle),
`/customers` sayfası kendini otomatik `/customers/{id}/applications`'a
yönlendiriyor, müşteri oluşturma/silme UI'ı gizleniyor; public modda hiçbir
şey değişmedi. `DatabaseGroup.environment` (`prod|preprod|test|dev`,
varsayılan `prod`) eklendi — migration, şemalar, CRUD, `seed_demo.py`
(boa ve aapara için birer prod + birer test grubu). Grup listesinde ve
Group Detail'de `.env-badge` rozeti; prod kırmızı/danger renkte görsel
olarak ayrışıyor.

**Faz 7 — İŞ 3: Dashboard özet endpoint'i.** `GET /api/dashboard/summary`
(`services/dashboard.py`): tüm gruplar için paralel canlı health probe +
(postgresql gruplarda) parametre denetimi + (gruba `group_id` ile bağlı
`Instance`'lar varsa) `performance_insights`/`index_advisor` çalıştırıp
`{totals, health, top_issues, recommendations}` derliyor. `top_issues`
split-brain/etcd-quorum-kaybı/düğüm-down/no-leader gibi somut problemleri
`prod` önce, sonra severity'ye göre sıralayıp ilk 10'u döndürüyor.
Recommendations üç kaynaktan (parameter_audit, performance_insights,
index_advisor) best-effort derleniyor — biri hata verirse sessizce
atlanıyor, endpoint asla patlamıyor. DashboardPage yeniden kuruldu: üstte
grup bazlı sağlık sayaçları (Kritik/Uyarı/Sağlıklı/Bilinmiyor), altında
tıklanabilir "En kritik sorunlar" listesi (Group Detail'e linkli) ve
"Öneriler" paneli; hiç grup yoksa "İzlenecek grup ekleyin" boş-durum kartı.
Eski Instance-bazlı özet (stat kartları + filtreli tablo) "Instance bazlı
görünüm" başlığı altında altta kalmaya devam ediyor.

**Faz 7 — Test turu düzeltmeleri (3 iş, 3 ayrı commit).**

- **İŞ 1 — Dashboard performansı.** `GET /api/dashboard/summary` artık canlı
  probe atmıyor (~6sn → önbellekten okuyup ms mertebesinde döner). Yeni
  `GroupHealthSnapshot` tablosu grup bazında son health raporu + öneri
  listesi + zaman damgasını tutuyor; `services/dashboard_snapshot.py`'deki
  `refresh_all_group_snapshots()` bunu dolduruyor — collector scheduler'da
  periyodik (`dashboard_refresh_interval_seconds`, varsayılan 60sn, ilk
  çalıştırma anında) ve yeni `POST /api/dashboard/refresh` ile manuel
  tetiklenebiliyor. Yanıt artık `last_checked` taşıyor; DashboardPage'de
  "Son güncelleme: X önce" + Yenile butonu eklendi.
- **İŞ 2 — Sol menü.** Private modda eski Instance-tabanlı ağaç
  ("Müşteriler") ile yeni nav linki ("Uygulamalar") aynı anda görünüp iki
  ayrı "müşteri" girişi gibi duruyordu. Eski ağaç artık sadece public
  modda görünüyor ve adı "Instance Gezgini" oldu (yeni akışla aynı ismi
  paylaşmasın diye); public moddaki üst nav linki "Müşteri Grupları"
  yerine "Müşteriler" oldu.
- **İŞ 3 — Cluster'lar tek satır.** `DatabaseGroup`'a `access_name`
  (listener/VIP adı) eklendi. Grup listesi ve Group Detail zaten grup
  bazında tek satırdı (düğümler sadece Group Detail'de listeleniyor) —
  asıl tekrar `top_issues`'daki down-node satırlarındaydı: bir gruptaki
  tüm down düğümler artık tek "N düğüm erişilemez: ad1 (site1), ad2
  (site2)..." satırında birleşiyor (önceden düğüm başına ayrı satırdı).
  Grup listesi artık `GroupHealthSnapshot` önbelleğinden türetilen bir
  "Durum" sütunu gösteriyor (up/down düğüm sayısı, primary düğüm,
  replikasyon lag özeti, overall rozet) — ekstra canlı prob yapmadan.

## Nasıl test edilir

### Backend
```bash
cd backend
python -m venv .venv && .venv/Scripts/activate  # veya source .venv/bin/activate
pip install -r requirements.txt
python -c "from app.main import app"             # importlar sağlam mı
python scripts/seed_demo.py                      # X Bank örneğini ekler (SQLite: backend/data/dbace.db)
uvicorn app.main:app --reload
```
Sonra:
- `GET /api/customers`, `/api/applications?customer_id=1`,
  `/api/groups?application_id=...`, `/api/groups/{id}/nodes` — X Bank/boa/aapara
  verisini döndürmeli.
- `GET /api/groups/{aapara_id}/health` — düğüm host'ları gerçek olmadığından
  `overall: "critical"`, `etcd_quorum.has_quorum: false` dönmesi beklenir
  (bu doğru davranıştır, demo host'lar DNS'te yok).
- `GET /api/groups/{aapara_id}/parameters` — `node.options.db_username`
  tanımlı olmadığından `400` döner; gerçek bir PostgreSQL düğümü test etmek
  için önce `PATCH /api/nodes/{id}` ile
  `options: {"db_username": "...", "db_password": "...", "db_database": "..."}`
  set edilmeli.
- `GET /api/groups/{boa_id}/alwayson` — aynı şekilde `db_username` gerektirir;
  `aioodbc` + bir ODBC sürücüsü kurulu değilse (`pip install aioodbc` ve
  işletim sisteminde "ODBC Driver 18 for SQL Server") net bir hata mesajıyla
  döner.
- Mevcut uçlar (`/api/instances/...`) hiç değişmeden çalışmalı.
- `GET /api/config` — `deployment_mode`/`default_customer_name` döner.
  `DEPLOYMENT_MODE=private DEFAULT_CUSTOMER_NAME="X Bank" uvicorn ...` ile
  başlatırsanız (temiz bir DB'de) açılışta otomatik bir müşteri oluşur.
- `GET /api/dashboard/summary` — ilk çalıştırmada (henüz snapshot yokken)
  hepsi `0`/`[]`/`last_checked: null` döner, anında (DB read only).
  `POST /api/dashboard/refresh` çağırdıktan sonra (X Bank demo verisiyle,
  tüm host'lar erişilemez) `health.critical: 4`, `top_issues`'da grup
  başına tek satır (ör. "4 düğüm erişilemez: boa-node-1 (primary), ..."),
  `last_checked` dolu döner; sonraki `GET /summary` çağrıları ms
  mertebesinde. `run_mode=worker`/`all` ile başlatılmışsa scheduler bunu
  60sn'de bir kendiliğinden de tazeliyor.
- `GET /api/groups?application_id=...` — her grup artık `access_name`
  (cluster gruplarda listener/VIP adı) ve `status` (refresh sonrası
  `nodes_up`/`nodes_down`/`primary_node`/`overall`) alanlarını taşıyor.

### Frontend
```bash
cd frontend
npm install
npm run build   # tsc -b && vite build, hatasız geçmeli
npm run dev
```
`http://localhost:5173/customers` → X Bank → boa/aapara → grup detay
sayfasına gidip düğüm ekleme formunu ve "Sağlığı kontrol et" /
"Parametreleri denetle" / "Always On durumunu getir" butonlarını deneyin.

## Bilinen sınırlar (detay için SORULAR.md)

- Grup seviyeli alarmlar artık kalıcı (Faz 6) — ama bu, `AlertRule`/
  `AlertEvent` şemasına `group_id` eklemeyi gerektirdi. Var olan bir
  `data/dbace.db` dosyanız varsa **silin**: SQLite `ALTER COLUMN ... DROP
  NOT NULL` desteklemediğinden `alert_events.instance_id`'nin eski NOT
  NULL kısıtı otomatik migration'la kaldırılamıyor; dosya silinip
  yeniden oluşturulduğunda (veya Supabase'de migration çalıştırıldığında)
  düzeliyor.
- `Node.agent_token` hâlâ düz metin (host-agent paylaşımlı sırrı — DB
  kimlik bilgisi değil, bu istekte kapsam dışı bırakıldı).
- Faz 4 gerçek bir SQL Server'a karşı test edilemedi (ortamda yok) — DMV
  sorguları standart Microsoft dokümantasyon örneklerine dayanıyor, bağlantı
  hataları temiz şekilde 502/400 olarak raporlanıyor (doğrulandı).
- Faz 5/7'deki sayfalar gerçek bir tarayıcıda tıklanarak doğrulanmadı (bu
  ortamda tarayıcı otomasyon aracı yoktu); bunun yerine `tsc -b && vite build`
  ve backend+frontend'i gerçekten ayağa kaldırıp `curl` ile uçtan uca API
  şekli karşılaştırması yapıldı.
- `GET /api/dashboard/summary` artık önbellekten okuyor (Faz 7 test turu
  düzeltmesi) — canlı prob sadece scheduler'da (worker/all run_mode,
  60sn'de bir) veya `POST /api/dashboard/refresh`'te çalışıyor.
  `run_mode=api` tek başına çalıştırılan bir deployment'ta scheduler
  çalışmaz, veri manuel refresh'e kadar bayat kalır. `index_advisor`/
  `performance_insights` önerileri yalnızca `Instance.group_id` ile bir
  gruba bağlanmış instance'lar için üretiliyor — `seed_demo.py` hiç
  Instance oluşturmadığı için demo'da bu iki kaynak boş kalır (beklenen
  davranış).
- Grup listesindeki "Durum" özeti (`GroupStatusSummaryOut`) genel amaçlı
  `collect_group_health` prob'undan türetiliyor; Always On grupları için
  `primary_node` ve `replication_lag_bytes` bu yüzden çoğunlukla boş/
  statik kalır — AG'nin gerçek DMV tabanlı primary/lag bilgisi sadece
  Group Detail'in Always On sekmesinde (`GET /api/groups/{id}/alwayson`).

## API uyumluluğu

Mevcut hiçbir endpoint kırılmadı; `Instance` ile ilgili tüm uçlar ve
davranışları (cluster-health dahil) aynı kaldı. Sadece ek, yeni uçlar
eklendi.
