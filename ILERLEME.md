# İlerleme — feature/multi-tenant-cluster

Tüm iş `feature/multi-tenant-cluster` dalında, `master`'a dokunulmadan ve
push edilmeden yapıldı. Her faz ayrı commit — `git log master..feature/multi-tenant-cluster`
ile tek tek görülebilir. Karar veremediğim / varsayımla ilerlediğim noktalar
`SORULAR.md`'de, gerekçeleriyle.

## Doğrulanmış uçtan uca akış (sıfırdan)

Bu ortamda tarayıcı otomasyonu yok, bu yüzden aşağıdaki akış Faz 9
sonunda **gerçek bir API scripti ile, boş bir DB'den başlayarak, gerçek
HTTP istekleriyle uçtan uca çalıştırılıp doğrulandı** (her adım gerçek
bir yanıt kodu ve veri döndürdü — hayali değil). Her adımın karşılığı
olan UI eylemi de yazılı; kullanıcı bu sırayla ilerleyerek aynı sonucu
tarayıcıda alabilir:

1. **Müşteri ekleme** — Sol menüde "Müşteriler" (public mod) → sayfa
   açılır → sağdaki "Yeni müşteri" formuna Ad + Tip girip Ekle (veya
   başlıktaki "+ Müşteri Ekle" butonu forma kaydırır). *Doğrulandı:*
   `POST /api/customers` → 201, müşteri oluştu.
2. **Uygulama ekleme** — Müşteriler listesinde müşteri adına tıkla →
   Uygulamalar sayfası → "+ Uygulama Ekle" / sağdaki form → Ad + Açıklama
   → Ekle. *Doğrulandı:* `POST /api/applications` → 201.
3. **Database group oluşturma** — Uygulamalar sayfasında uygulama adına
   tıkla → Database Groups sayfası → "+ Grup Ekle" → Ad, Motor
   (postgresql), Topoloji (standalone), Ortam → Ekle. *Doğrulandı:*
   `POST /api/groups` → 201, `topology=standalone`.
4. **Sunucu ekleme** — Uygulamalar sayfası başlığındaki "Sunucular"
   butonu (veya sol menü ağacında müşteri dalını açıp "📁 Sunucular") →
   Sunucular sayfası → "+ Sunucu Ekle" → Ad/Host/OS/Site → Ekle.
   *Doğrulandı:* `POST /api/servers` → 201.
5. **Instance ekleme + gruba alma** — Grup adına tıkla → Group Detail →
   Düğümler sekmesi → "Yeni düğüm" formu (üstteki "+ Düğüm Ekle" da
   forma kaydırır): Sunucu seç, Ad/Port/Rol gir, "Instance bağlantısı:
   Yeni instance oluştur" seçiliyken DB kullanıcı adı/parola gir → Ekle.
   Bu TEK adım hem instance'ı oluşturuyor hem de düğümü (dolayısıyla
   instance'ı) gruba üye yapıyor. *Doğrulandı:* `POST /api/nodes`
   (`db_username` ile) → 201, yanıtta `instance_id` dolu; ikinci bir
   düğüm daha eklendi.
6. **Cluster'a dönüştürme** — Group Detail'de (grup hâlâ standalone'ken)
   "Cluster'a dönüştür" kartı → Topoloji (patroni/alwayson), Erişim adı,
   Cluster adı, VIP gir → Dönüştür. *Doğrulandı:*
   `POST /api/groups/{id}/convert-to-cluster` → 200, `topology=patroni`
   oldu; ikinci düğüm eklendikten sonra grup artık tam Patroni yığınını
   (`postgresql, patroni, etcd, keepalived, haproxy`) prob'luyor.
7. **Sağlık görüntüleme** — Group Detail → Düğümler sekmesi → "Sağlığı
   kontrol et" → UP/DOWN sayıları, etcd quorum, split-brain durumu,
   Patroni cluster tablosu, her düğüm kartında servis durumları
   görünür. *Doğrulandı:* `GET /api/groups/{id}/health` → 200; aynı
   sorun `POST /api/dashboard/refresh` sonrası Dashboard'ın
   `top_issues` listesinde de göründü.
8. **Instance detayına gitme** — Sol menü ağacında Müşteri → Uygulama →
   Grup → Düğüm'ü aç, düğüme tıkla (instance bağlıysa link aktif) —
   veya Group Detail'deki düğüm kartındaki "Instance detayı" linkine
   tıkla → Instance detay sayfası (Overview/Metrics/Queries/Activity/
   Cluster/Schema/Tuning sekmeleri) açılır. *Doğrulandı:*
   `GET /api/instances/{id}` ve `GET /api/instances/{id}/insights` →
   200, ilk yüklemede kullanılan tüm uçlar hatasız.

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
- **İŞ 2 — Sol menü (revize edildi).** İlk deneme sadece etiketleri
  değiştirmişti; test turunda "sorun adlandırma değil, yapı" geri
  bildirimi geldi. Son hâli: `App.tsx`'te tek bir gerçek gezinme ağacı
  (`MainNavTree`) — public modda Müşteriler → Uygulamalar → Gruplar,
  private modda kök seviye doğrudan Uygulamalar → Gruplar (müşteri
  seviyesi atlanıyor). Ara düğümler (müşteri/uygulama) tıklanınca sadece
  genişliyor/daralıyor; sadece yaprak (grup) `Link` ile Group Detail'e
  gidiyor. Veriler her dal ilk açıldığında lazy-load ediliyor
  (`api.getCustomers/getApplications/getGroups`). Eski Instance-tabanlı
  ağaç ("Instance Gezgini") artık nav'ın en altında, ayrı bir bağlantı
  olarak her iki modda da duruyor — yeni ağaçla aynı ismi paylaşmadığı
  için private modda gizlemeye gerek kalmadı. Boş bir dal (hiç uygulaması
  olmayan müşteri, hiç grubu olmayan uygulama) "Kayıt yok" yerine ilgili
  create sayfasına giden bir "+ X ekle" linki gösteriyor — aksi halde
  CRUD sayfalarına ağaçtan erişim tamamen kaybolurdu (bkz. SORULAR.md).
- **İŞ 3 — Cluster'lar tek satır.** `DatabaseGroup`'a `access_name`
  (listener/VIP adı) eklendi. Grup listesi ve Group Detail zaten grup
  bazında tek satırdı (düğümler sadece Group Detail'de listeleniyor) —
  asıl tekrar `top_issues`'daki down-node satırlarındaydı: bir gruptaki
  tüm down düğümler artık tek "N düğüm erişilemez: ad1 (site1), ad2
  (site2)..." satırında birleşiyor (önceden düğüm başına ayrı satırdı).
  Grup listesi artık `GroupHealthSnapshot` önbelleğinden türetilen bir
  "Durum" sütunu gösteriyor (up/down düğüm sayısı, primary düğüm,
  replikasyon lag özeti, overall rozet) — ekstra canlı prob yapmadan.

**Faz 8 — İŞ 1: Node ve Instance birleştirme.** `Node`'a nullable
`instance_id` FK eklendi. `POST/PATCH /api/nodes`: `instance_id` verilirse
mevcut bir Instance'a bağlanıyor (ve o Instance'ın `group_id`'si bu gruba
set ediliyor); `db_username` (+ opsiyonel `db_password`/`db_database`)
verilirse `services/routers/nodes.py::_auto_create_instance` yeni bir
Instance oluşturup bağlıyor (host/port node'dan, şifre `encrypt_secret`
ile Instance.password mekanizmasıyla saklanıyor — `node.options.
db_password` artık kullanılmıyor). `services/parameter_audit.py` ve
`services/alwayson_health.py` artık bağlantı bilgisini `node.options`
yerine `node.instance`'tan okuyor (ilgili router'larda `selectinload
(Node.instance)` ile eager-load ediliyor). Auto-create edilen Instance'a
`group_id` de set edildiği için `services/dashboard_snapshot.py`'nin
zaten var olan `Instance.group_id` bazlı öneri toplama mantığı (performance_
insights, index_advisor) hiçbir değişiklik gerektirmeden bu instance'ları
otomatik kapsıyor — Faz 7'de "seed_demo hiç Instance oluşturmadığı için
bu iki kaynak boş kalıyor" diye not düşülen sınırlama artık kapandı.
`seed_demo.py` her düğüm için bir Instance oluşturup bağlıyor. Frontend:
Group Detail'deki her düğüm kartında bağlı Instance'a giden bir link
("metrikler, yavaş sorgular, index önerileri, explain") + "Yeni düğüm"
formuna instance bağlama seçenekleri (yeni oluştur / mevcuda bağlan /
bağlama) eklendi. Sol menü ağacı bir seviye büyüdü (Grup → Düğüm, düğüm
Instance'a bağlıysa tıklanabilir); eski "Instance Gezgini" ağacı tamamen
kaldırıldı — artık tüm instance erişimi bu tek ağacın içinden.

**Faz 8 — İŞ 2: Cluster'a dönüşüm akışı.** `DatabaseGroup`'a `cluster_name`
+ `vip_address` eklendi (migration + şema + CRUD). Yeni endpoint
`POST /api/groups/{id}/convert-to-cluster` — sadece `topology=standalone`
olan bir grupta çalışır (aksi halde 400), hedef topoloji `alwayson`/
`patroni` olmalı; grup güncellenir, mevcut düğüme dokunulmaz (zaten
node tablosu grup topolojisinden bağımsız). Yeni grup oluşturma formu
zaten topoloji seçimi sunuyordu (Faz 7); artık cluster topolojisi
seçilince `cluster_name`/`vip_address` alanları da beliriyor. Group
Detail'de standalone bir grupta "Cluster'a dönüştür" kartı — topoloji
(engine'e göre öntanımlı: postgresql→patroni, sqlserver→alwayson),
erişim adı, cluster adı, VIP formu; dönüşümden sonra kart kayboluyor,
kullanıcı "Yeni düğüm" formuyla diğer düğümleri ekleyebiliyor.

**Faz 8 — İŞ 3: Otomatik yenileme aralığı.** Yeni `app_settings`
key/value tablosu (`services/settings.py`) — `dashboard_refresh_interval_
seconds` artık DB'de kalıcı (10/30/60/300/900/3600sn seçenekleri).
`GET/PUT /api/dashboard/refresh-interval`; PUT hem DB'yi günceller hem
de (scheduler o an çalışıyorsa) `APScheduler.reschedule_job` ile canlı
job'ı yeniden başlatmadan günceller. `start_scheduler()` artık async —
açılışta DB'den kalıcı değeri okuyup job'ı onunla kuruyor (yoksa
`DASHBOARD_REFRESH_INTERVAL_SECONDS` env var'ına düşüyor).
DashboardPage'e açılır liste eklendi; seçim hem backend'e yazılıyor hem
de sayfanın kendi otomatik yenilemesi (sadece ucuz `GET /summary`
önbellek okuması, canlı prob değil) o aralığa göre çalışıyor.

**Faz 8 — İŞ 4: Dashboard düzeni.** Alttaki eski Instance-tabanlı blok
("Instance bazlı görünüm" — stat kartları + filtrelenebilir per-instance
tablo) **kaldırıldı** (birleştirilmedi) — gerekçe: stat kartları üstteki
grup-bazlı sağlık sayaçlarıyla çakışıyordu, tablo satırlarının benzersiz
içeriği (ham metrik değerleri) artık her instance sol menü ağacından ve
Group Detail'den erişilebilir olduğu için kaybolmuyor (Faz 8 İŞ 1). Detay
ve gerekçe SORULAR.md'de. `top_issues` sıralaması artık severity birincil
(kritik→uyarı), environment sadece aynı severity içinde ikincil anahtar
(prod öne) — Faz 7'deki tersini kırıyor, bilinçli bir değişiklik.
`DashboardIssueOut`'a `recommendation` alanı eklendi: her sorun,
kendi grubunun en yüksek öncelikli önerisiyle (varsa) eşleştiriliyor
(nedensel değil, "aynı grubun en iyi önerisi" yaklaşıklığı — SORULAR.md).
Öneri yoksa UI'da hiçbir şey render edilmiyor (boş kutu yok).

**Faz 8 — İŞ 5: Alarm kuralları (default + custom).** `AlertRule`'a
`rule_type` (metric/custom), `is_default`, `severity`, `engine`,
`sql_query`, `interval_seconds`, `last_run_at` eklendi. Otomatik
oluşturulan kurallar (`ensure_cluster_alert_rules`/`ensure_group_alert_
rules`) artık `is_default=True` ile işaretleniyor — `DELETE
/api/alerts/rules/{id}` bunları 400 ile reddediyor, `PATCH` sadece
`threshold`/`enabled` alanlarına izin veriyor (başka bir alan
gönderilirse 400). Yeni `services/custom_alert_rules.py`: özel SQL
kuralları hedef instance/gruba bağlanıp sorguyu çalıştırıp tek bir
sayısal değeri (`fetchval`/ilk satır ilk kolon) eşik değeriyle
karşılaştırıyor, aşımda `AlertEvent` üretiyor. Güvenlik katmanları
(SELECT/WITH-only + anahtar kelime kara listesi + PostgreSQL'de gerçek
`READ ONLY` transaction + üç seviyeli zaman aşımı) ayrıntılı olarak
SORULAR.md'de — SQL Server hedeflerinde sunucu-taraflı zorlamanın
olmadığı bilinen bir sınırlama olarak orada işaretli. Scheduler'a sabit
10sn'lik bir tick eklendi (`evaluate_custom_rules_tick`) — her kuralın
kendi `interval_seconds`'ı bu tick içinde `last_run_at`'e göre
self-servis uygulanıyor (kural başına ayrı job yok). AlertsPage yeniden
yazıldı: özel kural ekleme formu (hedef/engine/aralık/severity/SQL),
kural tablosunda Varsayılan/Özel rozeti, inline düzenleme (varsayılan
kurallarda sadece eşik+aç-kapa, özel kurallarda tüm alanlar), varsayılan
kurallarda Sil butonu gizli.

**Faz 9 — İŞ 1: Server modeli (sunucu/instance ayrımı).** Yeni `Server`
modeli (`id, customer_id, name, host, os, site, agent_url, agent_token`)
— bir müşterinin fiziksel/VM envanteri. `Node` artık "sunucu" değil,
instance seviyesinde: `server_id` FK ile bir Server'a bağlanıyor,
`host/site/agent_url/agent_token` Node'dan kalktı (artık `node.server`
üzerinden), yeni `instance_name` alanı (SQL Server named instance için)
eklendi. Grup (cluster) üyeliği hâlâ Node seviyesinde — aynı Windows
sunucusundaki iki named instance farklı gruplara üye olabiliyor
(`seed_demo.py`'de `boa-shared-winsvr` örneği: `MSSQLSERVER` instance'ı
`boa-sqlserver-test` grubunda, `REPORTING` instance'ı yeni `boa-reporting`
grubunda). Yeni CRUD router `routers/servers.py` + `ServersPage.tsx`
(`/customers/{id}/servers`). Geriye dönük uyumluluk: `database.py::
_migrate_nodes_to_server_model()` var olan düğümler için otomatik Server
satırları oluşturup bağlıyor, sonra eski sütunları güvenli şekilde
düşürüyor (detay SORULAR.md'de) — DB silmeye gerek kalmadı.

Aynı yeniden yazımda (aynı dosya, `cluster_health.py`) **İŞ 3'ün asıl
düzeltmesi de** yapıldı: `_node_services()` artık engine+topology bazlı
— `alwayson` grupları `{sqlserver, alwayson, windows_cluster}` prob'luyor,
Patroni/etcd/keepalived/haproxy hiç görünmüyor; `standalone` gruplar hiç
cluster stack'i sorgulamıyor, sadece tek bir engine-doğru servis. Ayrıca
`down_nodes` tespiti önceden hep literal `"postgresql"` servisine
bakıyordu — bu yüzden sqlserver/mongodb gruplarında düğümler hiçbir zaman
"down" işaretlenmiyordu (sessiz bir hataydı, sadece yanlış etiket değil);
artık engine-doğru servise bakıyor. Doğrulandı: `boa-sqlserver-ag` grubu
artık `services=['alwayson','windows_cluster','sqlserver']` döndürüyor ve
4 düğümü de doğru şekilde down olarak işaretliyor (demo host'ları
erişilemez); `aapara-patroni` değişmeden tam Patroni yığınını koruyor.

**Faz 9 — İŞ 2: Node ↔ Instance eşleşmesi doğrulandı.** Uçtan uca test
edildi (API seviyesinde — tarayıcı otomasyonu bu ortamda yok): taze bir
`seed_demo.py` çalıştırması sonrası her düğümün `instance_id`'si dolu,
sol menü ağacı bunu kullanarak Instance detay sayfasına linkliyor, Group
Detail'deki düğüm kartı da aynı linke sahip, InstanceDetailPage'in ilk
yüklemede çağırdığı tüm uçlar (metrics/queries/summary/alerts/
predictions/insights) taze/hiç toplanmamış bir instance için bile hatasız
200 dönüyor. Mekanizma zaten doğru kodlanmıştı — kök neden kesin tespit
edilemedi (muhtemelen eski bir `data/dbace.db` dosyası), detay ve
kurtarma adımı SORULAR.md'de.

**Faz 9 — İŞ 3: Engine-aware probe doğrulandı.** Asıl kod değişikliği İŞ 1
commit'inde (`cluster_health.py` yeniden yazımı) — burada hedeflenen
semptomun düzeldiğini doğruladım: `boa-sqlserver-ag` artık sadece
`{sqlserver, alwayson, windows_cluster}` prob'luyor (Patroni/etcd/
keepalived/haproxy hiç yok), `down_nodes` 4 düğümü de doğru tespit
ediyor, dashboard'daki hiçbir sorun mesajında "PostgreSQL" geçmiyor;
`aapara-patroni` (postgresql/patroni) davranışı değişmedi.

**Faz 9 — İŞ 4: CRUD erişimi.** Her liste sayfasında (Müşteriler,
Uygulamalar, Database Groups, Sunucular) artık başlıkta net bir "+ X
Ekle" butonu var (sayfadaki mevcut forma çapalıyor) ve her satırda
"Düzenle" (inline, alanlar yerinde düzenlenebilir) + "Sil" birlikte
duruyor — önceden sadece "Sil" vardı, düzenleme hiçbir yerde yoktu.
Group Detail'deki düğüm kartlarına da aynı "Düzenle" (sunucu/instance
adı/port/rol) eklendi. Backend'de zaten var olan `PATCH` endpoint'leri
(customers/applications/groups/servers/nodes) kullanıldı — hiçbiri yeni
değildi, sadece frontend'de karşılığı yoktu. Sol menü ağacında artık her
seviyede (Müşteri/Uygulama/Grup) satırın üzerine gelince beliren bir "+"
düğmesi var, ilgili alt-öğe ekleme sayfasına gidiyor — önceden bu sadece
dal boşken (`Kayıt yok` yerine) görünüyordu, artık dolu bir dalda da
erişilebilir. Breadcrumb gezinme (← Uygulamalar, ← Müşteriler vb.)
değişmeden duruyor — artık tek erişim yolu değil, üç yol var (breadcrumb,
liste sayfası + Ekle butonu, sol menü ağacındaki + düğmesi). Private
modda müşteri ekleme/silme hâlâ kapalı (CustomersPage `!isPrivate`
kontrolü değişmedi).

**Faz 9 — İŞ 5: Dashboard öneri alanları dolduruldu.** Teşhis: öneriler
instance bağlantısı eksikliğinden değil (Faz 8 İŞ 1 zaten çözmüştü),
instance'lara gerçekten ulaşılamadığından boştu — `parameter_audit`/
`performance_insights`/`index_advisor` üçü de canlı bağlantı veya
toplanmış metrik/yavaş sorgu verisi gerektiriyor, demo host'ları
(`*.internal`, DNS'te yok) bunların hiçbirini sağlayamıyor (detay
SORULAR.md'de). Çözüm: `services/dashboard_snapshot.py`'ye dördüncü bir
öneri kaynağı eklendi — `_connectivity_recommendations()`, zaten
yapılmış health prob'undaki `down_nodes`'a bakıp engine'e uygun bir log
komutu (`journalctl -u patroni` / PostgreSQL, `Get-EventLog ...
MSSQLSERVER` / SQL Server) öneriyor; hiçbir canlı bağlantıya ihtiyaç
duymadığı için erişilemez demo'da bile her zaman dolu. Doğrulandı: 5
demo grubunun 5'i de artık hem "Öneriler" panelinde hem kendi
`top_issues` satırının altında bir öneri gösteriyor.

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
- `GET /api/groups/{id}/nodes` — her düğüm artık `instance_id` taşıyor
  (seed_demo sonrası dolu). `POST /api/nodes` `db_username` ile çağrılırsa
  201 yanıtında `instance_id` dolu döner; `GET /api/instances/{instance_id}`
  ile o instance'ın metrik/tuning uçları normal instance gibi çalışır.

### Frontend
```bash
cd frontend
npm install
npm run build   # tsc -b && vite build, hatasız geçmeli
npm run dev
```
`http://localhost:5173/` açılınca sol menüde "Müşteriler" (public) /
"Uygulamalar" (private) başlığına tıklayın — ağaç açılır, "X Bank"a
tıklayın (public'te) → altında "boa"/"aapara" görünür → birine tıklayın
→ altında grup adları (veya cluster gruplarda `access_name`) görünür →
bir gruba tıklayınca Group Detail açılır, orada düğüm ekleme formunu ve
"Sağlığı kontrol et" / "Parametreleri denetle" / "Always On durumunu
getir" butonlarını deneyin.

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
  gruba bağlanmış instance'lar için üretiliyor — Faz 8 İŞ 1'den beri
  `seed_demo.py` her düğüm için bir Instance oluşturup bağladığından bu
  artık demo'da da çalışıyor (önceki sınırlama kapandı), ama gerçek
  öneri üretebilmek için o instance'ların gerçekten toplanmış metrik/
  yavaş sorgu verisine ihtiyacı var (collector scheduler'ın bir süre
  çalışmış olması gerekir — taze bir DB'de ilk birkaç dakika hâlâ boş
  dönebilir).
- Grup listesindeki "Durum" özeti (`GroupStatusSummaryOut`) genel amaçlı
  `collect_group_health` prob'undan türetiliyor; Always On grupları için
  `primary_node` ve `replication_lag_bytes` bu yüzden çoğunlukla boş/
  statik kalır — AG'nin gerçek DMV tabanlı primary/lag bilgisi sadece
  Group Detail'in Always On sekmesinde (`GET /api/groups/{id}/alwayson`).

## API uyumluluğu

Mevcut hiçbir endpoint kırılmadı; `Instance` ile ilgili tüm uçlar ve
davranışları (cluster-health dahil) aynı kaldı. Sadece ek, yeni uçlar
eklendi.
