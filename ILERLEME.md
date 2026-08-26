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

**Faz 10 — İŞ 1: Düğüme bağlantı bilgisi girme akışı.** Node kartında
`instance_id` yoksa artık "Bağlantı bilgisi gir" butonu var: tıklayınca
açılan formda ya yeni bağlantı (kullanıcı adı/parola/db adı/port) ya da
mevcut bir Instance'a bağlama (açılır liste) seçilebiliyor, kaydetmeden
önce "Bağlantıyı test et" sonucu (`POST /api/instances/test` veya
`POST /api/instances/{id}/test`) gösteriyor. Bağlandıktan sonra kart
instance detay sayfasına (metrikler/yavaş sorgular/index önerileri/
explain/tuning) linkleniyor. Backend'de bu akışın tamamı zaten Faz 9'dan
beri vardı (`PATCH /api/nodes/{id}` `db_username`/`instance_id` ile) —
eksik olan sadece ona erişecek UI'ydı.

**Faz 10 — İŞ 2: Seed'de çoklu instance/farklı AG senaryosu.** Önceki
seed'deki paylaşımlı Windows sunucusu (`boa-shared-winsvr`) iki instance
barındırıyordu ama ikisi de standalone gruba üyeydi — bu, "aynı sunucudaki
instance'lar farklı Always On gruplarına üye olabilir" iddiasını
kanıtlamıyordu. Artık ikisi de gerçek birer 2 düğümlü Always On AG'nin
(`boa-sqlserver-test-ag`: MSSQLSERVER, `boa-reporting-ag`: SQLPROD02)
birincil düğümü; her AG'nin ikinci (replika) düğümü ayrı bir sunucuda.

**Faz 10 — İŞ 3: Müşteriler/Uygulamalar için sabit sol menü linkleri.**
Sol menünün gezinme ağacı kökü artık hem tıklanabilir bir link hem de ayrı
bir ok butonuyla genişletilebilir (branch node'ların zaten kullandığı
link+toggle deseniyle aynı) — public modda "Müşteriler" doğrudan
`/customers`'a, private modda "Uygulamalar" doğrudan tek müşterinin
uygulama sayfasına gider. Public modda ayrıca "seçili müşteri"nin
Uygulamalar sayfasına giden ikinci bir sabit link var (seçili müşteri
URL'den izleniyor; ağaçta bir müşterinin adına tıklamak da artık doğrudan
Uygulamalar sayfasına gidip seçimi günceller). Ağaçtaki genişlet/daralt ve
alt düğümlere tıklama bağımsız çalışmaya devam ediyor.

**Faz 10 — İŞ 4: Önerilerde açıklama/aksiyon ayrımı.**
`DashboardRecommendationOut`'a opsiyonel `action` alanı eklendi (tek
satırlık, kopyalanabilir komut). `dashboard_snapshot.py`'deki dört öneri
kaynağı buna göre ayrıldı: connectivity (action: journalctl/Get-EventLog
log komutu), parameter_audit (action: `SHOW <param>;` — hedef değeri
sunucu RAM/CPU olmadan uydurmak yerine mevcut değeri kontrol etmeye
yönlendiriyor), index_advisor (action: gerçek `CREATE INDEX` DDL'i),
performance_insights (action yok — `insight.recommendation` zaten düzyazı,
`insight.action` bir tab-hint'i, komut değil; bunun yerine recommendation
metni artık `message`'a dahil edildi, önceden hiç gösterilmiyordu).
Frontend'de Dashboard'daki her iki öneri gösterimi de artık açıklama ile
komutu ayrı satırlarda gösteriyor, komut monospace ve "Kopyala" butonuyla
tek tıkla panoya kopyalanabiliyor.

**Faz 10 — SONRA: Ekleme akışlarının eksiklerini tamamlama.** Dört
senaryonun (aşağıdaki bölüm) uçtan uca denenmesi sırasında bulunan
gerçek eksikler kapatıldı:
- "Yeni düğüm" formunda (grup içinde ilk kez düğüm eklerken) da artık
  "Bağlantıyı test et" butonu var — önceden bu buton sadece İŞ 1'in
  post-creation "Bağlantı bilgisi gir" akışındaydı, ilk oluşturma formunda
  yoktu.
- Aynı formda zorunlu alan validasyonu eklendi: "Yeni instance oluştur"
  modunda kullanıcı adı, "Mevcut instance'a bağla" modunda instance seçimi
  artık HTML `required` ile zorunlu — önceden boş bırakılırsa backend
  sessizce `instance_id: null` ile düğüm oluşturuyordu (kullanıcı bağlı
  sanıyor ama değil).
- `Server` seviyesinde agent bilgisi (`agent_url`/`agent_token`) artık
  test edilebiliyor: yeni `POST /api/servers/test-agent` (kayıttan önce,
  Sunucu formundaki "Agent'ı test et" butonu) ve
  `POST /api/servers/{id}/test-agent` (kayıtlı bir sunucu için, liste
  satırındaki "Agent testi" butonu) — ikisi de `fetch_agent_snapshot`'ı
  (Faz 2'den beri var olan agent probe) yeniden kullanıyor.
- Bağlantı test hataları artık anlaşılır Türkçe mesajlara sınıflandırılıyor
  (`collectors/base.py` → `classify_connection_error`, PostgreSQL/SQL
  Server/MongoDB collector'larının `test_connection`'ında kullanılıyor):
  yanlış şifre/kimlik doğrulama, host çözümlenemiyor (DNS), port
  kapalı/reddedildi, zaman aşımı, veritabanı bulunamadı ayrı ayrı
  mesajlarla ayırt ediliyor — orijinal driver mesajı parantez içinde hâlâ
  korunuyor.

## Doğrulanmış senaryo akışları (motor/topoloji bazında)

Yukarıdaki genel 8 adımlık akışa ek olarak, Faz 10 sonunda dört somut
senaryo — her biri UI'ın kullandığı gerçek uçlarla, sıfırdan bir DB'de,
httpx/ASGITransport üzerinden gerçek HTTP istekleriyle — ayrı ayrı
doğrulandı:

**a) PostgreSQL standalone.** Sunucu ekle (os=linux) → grup oluştur
(engine=postgresql, topology=standalone) → Group Detail'de "Yeni düğüm":
sunucu seç, "Yeni instance oluştur" + kullanıcı adı/parola/db → önce
"Bağlantıyı test et" (erişilemeyen demo host için temiz, sınıflandırılmış
hata) → Ekle. *Doğrulandı:* `POST /api/nodes` → 201, `instance_id` dolu;
oluşan instance `enabled=true` ve `GET /api/instances/summary`'de görünüyor
(yani "izlemeye başlamak" için ekstra bir adım yok — collector scheduler
zaten enabled instance'ları topluyor).

**b) PostgreSQL Patroni cluster.** Grup oluştur (topology=patroni,
access_name=VIP/listener adı, cluster_name, vip_address) → 3 sunucu ekle
(2'si primary site, 1'i site=disaster + her birine agent_url/agent_token)
→ her sunucuya bir düğüm/instance ekle → DR sunucusundan oluşan düğümün
`site` alanı `"disaster"` dönüyor (Server'dan türetilen salt-okunur alan,
ayrıca bir "DR işaretle" adımı yok — DR'yi seçmek = site=disaster olan
sunucuyu seçmek). Agent bilgisi hem kayıttan önce
(`POST /api/servers/test-agent`) hem kayıtlı bir sunucu için
(`POST /api/servers/{id}/test-agent`) test edildi (erişilemeyen demo
host'lar için temiz `ok: false`). *Doğrulandı:* `GET /api/groups/{id}/health`
→ 200 (host'lar erişilemez olduğundan `overall: "critical"`, beklenen).

**c) SQL Server standalone (named instance).** Sunucu ekle (os=windows) →
grup oluştur (engine=sqlserver, topology=standalone) → "Yeni düğüm"
formunda SQL Server için görünen `instance_name` alanına named instance adı
(`SQLPROD01`) + port gir, kullanıcı adı/parola → Ekle. *Doğrulandı:*
`node.instance_name == "SQLPROD01"`; `POST /api/instances/test` (engine=
sqlserver) 200 dönüyor, bu ortamda ODBC sürücüsü kurulu olmadığından
sınıflandırılmış hata "Bağlantı başarısız." (driver mesajı parantez
içinde) — gerçek bir Windows/SQL Server ortamında aynı yol kimlik
doğrulama/host/port hatalarını ayrı ayrı sınıflandırır.

**d) SQL Server Always On.** Grup oluştur (topology=alwayson, access_name=
listener adı, cluster_name) → 2 sunucu (biri site=disaster) → her birine
`instance_name=MSSQLSERVER` ile birer düğüm/instance → DR replikası =
site=disaster sunucusundan gelen düğüm (yine Server'dan türetilen alan).
*Doğrulandı:* `GET /api/groups/{id}/alwayson` çökmeden yanıt veriyor — bu
uç gerçek bir DMV bağlantısı gerektirdiğinden (TCP-probe'lu `/health`'in
aksine) bu sandbox'ta ODBC sürücüsü olmadan `502` + net `detail` mesajıyla
dönüyor; gerçek bir SQL Server + ODBC sürücüsüyle `200` + AG durumu döner.

Ayrıca doğrulandı: var olmayan bir `server_id`/`customer_id` referansıyla
düğüm/sunucu oluşturma girişimi temiz `404` ile reddediliyor (backend
seviyesinde savunma — frontend'deki `required` alanlar zaten bu durumu
normalde engelliyor).

**Faz 11 — PostgreSQL sürüm-uyumlu collector (raporlanan hata: PG17'de
"column checkpoints_timed does not exist").** Kök neden: PostgreSQL 17
checkpoint istatistiklerini `pg_stat_bgwriter`'dan yeni bir
`pg_stat_checkpointer` view'ına taşıdı (`checkpoints_timed` →
`num_timed`, `checkpoints_req` → `num_requested`,
`checkpoint_write_time` → `write_time`, `checkpoint_sync_time` →
`sync_time`, `buffers_checkpoint` → `buffers_written`) ve
`buffers_backend`/`buffers_backend_fsync`'i tamamen kaldırdı (yeniden
adlandırma değil, gerçek kaldırma — doğrudan bir karşılığı yok).
`collectors/postgresql.py`'ye `_detect_version()` eklendi
(`current_setting('server_version_num')` + `version()`, tek round-trip);
`collect_metrics()` artık `server_version_num >= 170000` ise
`pg_stat_checkpointer` + trimmed `pg_stat_bgwriter` (sadece
`buffers_clean`/`buffers_alloc`), altındaysa eski tam `pg_stat_bgwriter`
sorgusunu kullanıyor. `buffers_backend_per_sec`/`buffers_backend_fsync_per_sec`
PG17+'de metriklerden tamamen çıkarılıyor (null değil, yok) ve
`Instance.unsupported_metrics`'e nedeniyle birlikte yazılıyor. Aynı
prensip `pg_stat_io`'ya da uygulandı (16+ gerekiyor — önceden sessiz bir
try/except'ti, artık açık bir sürüm kontrolü + loglanan bir "desteklenmiyor"
nedeni). `collect_slow_queries()` artık `pg_stat_statements`'ın PG13'te
`total_time`/`mean_time`'dan `total_exec_time`/`mean_exec_time`'a
yeniden adlandırılmasını da sürüme göre seçiyor (12.x hâlâ eski adları
kullanıyor). Checkpoint/bgwriter sorgusu beklenmedik bir hatayla
başarısız olursa (ör. yetki reddi) tüm collect_metrics çökmüyor — sadece
o metrik grubu `unsupported_metrics`'e düşüyor, geri kalanı toplanmaya
devam ediyor (doğrulandı: birim testte). `pg_stat_activity`/
`pg_stat_replication` de tarandı — dbace'nin kullandığı kolonlar
(`pid`, `usename`, `state`, `wait_event_type/event`, `backend_type`,
`query_start` vb.; replikasyon lag'i `pg_last_wal_receive_lsn`/
`pg_last_wal_replay_lsn` fonksiyonlarından, view'dan değil) PG12-17
arası stabil — kod değişikliği gerekmedi (bkz. SORULAR.md).

Yeni `Instance.server_version` (insan-okunabilir sürüm metni) ve
`Instance.unsupported_metrics` (metrik adı → Türkçe neden) alanları
her başarılı toplama döngüsünde `services/collection.py` tarafından
güncelleniyor; `InstanceOut`'a eklendi.

**Test:** Bu ortamda gerçek bir PostgreSQL 17 (Supabase veya başka)
erişilebilir değildi, bu yüzden sürüm tespiti/dallanma/nazik bozulma
mantığı `backend/tests/test_postgresql_version_adapt.py`'de gerçek bir
ağ bağlantısı kurmayan sahte bir `asyncpg.Connection` ile (7 test)
kanıtlandı: PG17 → checkpointer view kullanılıyor ve
`buffers_backend_per_sec` doğru şekilde "desteklenmiyor" işaretleniyor;
PG16 → eski bgwriter + pg_stat_io hâlâ destekleniyor; PG12 → bgwriter
fallback çalışıyor ve pg_stat_io sorgusu hiç denenmiyor (sürüm koşulu
sorgudan önce); checkpoint sorgusu hata fırlatınca `collect_metrics`
çökmeden devam ediyor; `collect_slow_queries` sürüme göre doğru
`pg_stat_statements` kolon adını kullanıyor (12/13/17 parametrize).
Ayrıca `services/collection.py` → SQLite üzerinden uçtan uca da
doğrulandı: sahte bir PG17 collector'ıyla `collect_instance()`
çalıştırılıp `Instance.server_version`/`unsupported_metrics`'in gerçekten
veritabanına yazıldığı teyit edildi. `backend/requirements-dev.txt` +
`pytest.ini` eklendi (`pytest`/`pytest-asyncio` — prod
`requirements.txt`'e dokunulmadı, deploy image'larını etkilemiyor).

**Faz 11 — Instance detayında tespit edilen sürüm gösterimi.** Instance
Detail sayfasının başlık alt satırına `instance.server_version` eklendi
(`host:port/database · engine · uygulama · sürüm`). Bu sürümde
desteklenmeyen metrikler varsa (`instance.unsupported_metrics` boş
değilse) Özet sekmesinde, istatistik kartlarının hemen üstünde, her
metrik adı + Türkçe nedeniyle listelenen uyarı-renkli bir bilgi kartı
görünüyor. `GET /api/instances/{id}` yanıtında bu iki alanın gerçekten
döndüğü doğrulandı (`server_version`/`unsupported_metrics` — henüz
toplama yapılmamış taze bir instance'da ikisi de `null`, ilk
`collect_metrics()` sonrası dolar).

**Faz 11 — SQL Server collector'ı da sürüm-uyumlu hale getirildi.**
`collectors/sqlserver_mongodb.py`'ye `_detect_version()` eklendi
(`SERVERPROPERTY('ProductMajorVersion')` + `@@VERSION` — 2016=13,
2017=14, 2019=15, 2022=16, minimum desteklenen 13). PostgreSQL'in
aksine SQL Server'da DMV kolon farklılıkları temiz bir major-version
sınırına oturmuyor (SP/CU'ya bağlı) — bu yüzden version-number gate
yerine dene/hata-al/geri-çekil deseni kullanıldı:
`sys.dm_exec_query_stats.total_rows` sorgusu başarısız olursa
(`collect_slow_queries`) `NULL AS rows` ile otomatik yeniden deneniyor,
tüm yavaş sorgu verisi kaybolmuyor. `collect_metrics`'teki perf counter
(Buffer cache hit ratio/Deadlocks — Azure SQL Database gibi edition'larda
farklı davranabiliyor), veritabanı boyutu ve tempdb boyutu sorguları
artık bağımsız try/except'lerle korunuyor — biri başarısız olursa sadece
o metrikler `unsupported_metrics`'e düşüyor (nedeniyle birlikte,
loglanarak), geri kalan metrikler toplanmaya devam ediyor. Aynı
`Instance.server_version`/`unsupported_metrics` alanları (PostgreSQL'le
paylaşılan, engine-agnostik) burada da doluyor.

**Test:** `backend/tests/test_sqlserver_version_adapt.py`'de sahte bir
aioodbc-tarzı connection/cursor ile (4 test, gerçek ODBC sürücüsü/ağ
gerekmez) kanıtlandı: sürüm doğru tespit ediliyor ve tam metrik seti
toplanıyor; perf counter sorgusu patlarsa ilgili 3 metrik `unsupported`a
düşüyor ama `active_connections` gibi bağımsız metrikler etkilenmiyor;
tempdb sorgusu patlarsa sadece `temp_bytes` etkileniyor; `total_rows`
kolonu yoksa yavaş sorgu toplama `NULL AS rows` ile otomatik geri
çekiliyor ve sonuç kaybolmuyor. Toplam 11 test (7 PostgreSQL + 4 SQL
Server) `python -m pytest` ile yeşil.

**Faz 12 — Toplama yükü denetimi: index_advisor otomatik döngüden çıkarıldı.**
`services/dashboard_snapshot.py`'nin `_instance_recommendations()`'ı her
dashboard refresh tick'inde (varsayılan 60sn, kullanıcı 10sn'ye kadar
düşürebiliyor — `ALLOWED_REFRESH_INTERVALS`) her grubun her instance'ının
en yavaş sorgusu için `PostgreSQLIndexAdvisor.advise()`'ı ÇAĞIRIYORDU —
bu, izlenen veritabanına karşı gerçek bir katalog taraması
(`pg_stats`/`pg_indexes`/`pg_class`) ve bazen `hypopg` ile iki kez
`EXPLAIN (FORMAT JSON)` + hipotetik index oluşturma/silme demekti. Bu
artık tamamen kaldırıldı — index önerisi sadece kullanıcı bir sorgunun
"Index önerisi" panelini açtığında çalışıyor (`POST
/api/queries/{id}/advice`, zaten vardı, sadece otomatik döngüden ayrık
hale getirildi). `_load_instance_snapshots()` de artık sadece
`analyze_metrics()`'in ihtiyaç duyduğu alanları (`name`/`engine`/
`metrics_json`/`collected_at`) çekiyor — host/port/database/kullanıcı
adı/şifre/options ve gereksiz bir `SlowQuerySample` sorgusu (N+1, her
instance için bir tane) artık hiç çekilmiyor. `performance_insights`
kaynağı (tamamen bellek içi, `metrics_json` üzerinde çalışıyor, hedef
DB'ye hiç bağlanmıyor) değişmeden kalıyor.

**Faz 12 — EXPLAIN/index önerisi sonuçları önbelleğe alındı + statement_timeout.**
Yeni `services/query_cache.py`: küçük, process-local, TTL'li bir bellek
içi önbellek (5 dakika). `POST /api/queries/{id}/explain` ve `POST
/api/queries/{id}/advice` artık (instance_id, sorgu metni, [explain için
+ analyze bayrağı]) anahtarıyla önbellekten okuyor/yazıyor — aynı panel
tekrar açılırsa (tab değişimi, re-render, çift tıklama) pahalı/yürüten
işlem tekrarlanmıyor. `PostgreSQLIndexAdvisor._connect()`'e de
`explain_service`'in zaten sahip olduğu `statement_timeout` (8sn)
eklendi — katalog taraması + hypopg re-plan artık sınırsız süre
bağlantı açık tutamıyor. httpx ile doğrulandı: `advise`/`explain`
sahte implementasyonları çağrı sayacıyla sarmalanıp aynı sorgu 3 kez
istendi — gerçek çağrı sayısı 1'de kaldı; farklı bir sorgu metni ayrı
bir önbellek girdisi olarak doğru şekilde yeni bir çağrı tetikledi.
`backend/tests/test_query_cache.py`'de get/set/TTL expiry ayrıca birim
testle de kanıtlandı (3 test).

**Faz 12 — EXPLAIN ANALYZE artık ayrı, açıkça onaylanan bir eylem.**
Backend zaten varsayılan olarak `analyze=false` kullanıyordu
(`ExplainRequest.analyze: bool = False`) — asıl eksik olan UI'da hiç
ANALYZE seçeneği olmamasıydı (tek buton hep `analyze=false`
gönderiyordu). Instance Detail'in Yavaş Sorgular sekmesinde artık iki
ayrı buton var: "EXPLAIN plan" (sorguyu çalıştırmaz, sadece planlar) ve
kırmızı "EXPLAIN ANALYZE ⚠" (sorguyu gerçekten çalıştırır) — ikinciye
tıklayınca "bu sorguyu GERÇEKTEN ÇALIŞTIRIR ... devam edilsin mi?"
uyarısıyla `confirm()` çıkıyor, onaylanmadan istek gönderilmiyor.
Sonuç panelinde zaten var olan `ExplainPlanTree`'nin mod etiketi
("EXPLAIN"/"ANALYZE") hangi sonucun hangi modda üretildiğini gösteriyor.

**Faz 12 — Her toplama sorgusuna statement_timeout.**
`PostgreSQLCollector._connect()` artık her bağlantıda
`SET statement_timeout = '5000ms'` çalıştırıyor — `collect_metrics`/
`collect_slow_queries`'in kullandığı her sorgu (in-memory view'lar,
`pg_stat_statements`) zaten milisaniyeler sürmeli, bu sadece kilitli/
aşırı yüklü bir hedefte bağlantının 15sn'lik toplama döngüsü boyunca
sınırsız açık kalmasını engelleyen bir güvenlik ağı. Aynısı
`services/parameter_audit.py::_pg_connect()`'e de eklendi (dashboard
refresh tick'inde, 10sn'ye kadar sık çalışabiliyor).
`PostgreSQLIndexAdvisor` zaten bir önceki commit'te aldı,
`PostgreSQLExplainService`'te de zaten vardı (8sn) — artık hedef
veritabanına dokunan her PostgreSQL sorgusu (periyodik veya on-demand)
sınırlı.

SQL Server'da PostgreSQL'in `statement_timeout`'una birebir karşılık
gelen, düz SQL ile ayarlanabilen bir mekanizma yok —
`SqlServerCollector._connect()` artık `SET LOCK_TIMEOUT 5000` çalıştırıyor
(en yaygın gerçek "toplama sorgusu takıldı" sebebi olan kilit beklemesini
sınırlıyor), ama bu ham CPU/IO-bound yürütme süresini SINIRLAMIYOR — bu
bilinen bir fark, SORULAR.md'de gerekçesiyle not edildi.

**Test:** İki yeni test (`test_connect_applies_statement_timeout`,
`test_connect_applies_lock_timeout`) önceki testlerin aksine `_connect()`
metodunu MONKEYPATCH'lemiyor — gerçek `_connect()` gövdesini
(`asyncpg.connect`/`aioodbc.connect`'i sahteleyerek) çalıştırıp
gönderilen `SET` ifadesinin gerçekten içeride olduğunu doğruluyor.
Toplam 16 test yeşil.

**Faz 12 — Metrik toplamada tek bağlantı (N+1 azaltıldı).**
`collect_instance()` her 15sn'lik döngüde `collect_metrics()` ve
`collect_slow_queries()` için AYRI AYRI bağlantı açıyordu (her ikisi de
kendi `_connect()`'ini çağırıyordu) — hedef sunucuya cycle başına en az
2 ayrı TCP+auth el sıkışması demekti. `BaseCollector`'a
`open_connection()`/`close_connection()` eklendi (varsayılan `None`
döner — desteklemeyen collector'lar eskisi gibi kendi bağlantısını açar/
kapatır); `PostgreSQLCollector`/`SqlServerCollector` bunu `_connect()`'e
yönlendiriyor. `collect_metrics`/`collect_slow_queries` artık opsiyonel
bir `conn` parametresi kabul ediyor — verilirse onu kullanıp
kapatmıyor (`owns_conn` bayrağı), verilmezse eskisi gibi kendi
bağlantısını açıp kapatıyor (geriye dönük uyumlu — mevcut testler hiç
değişmeden geçiyor). `collect_instance()` artık BİR bağlantı açıp
ikisine de veriyor. MongoDB `open_connection()`'ı override etmiyor
(motor zaten kendi içinde connection pooling yapıyor) — `conn=None`
alıp yok sayıyor, davranışı değişmedi.

**Test:** Yeni `test_collection_connection_sharing.py`, gerçek
`collect_instance()`'ı sahte bir hedef bağlantısıyla (`FakeAsyncConnection`)
ve GERÇEK bir SQLite app-DB'ye karşı çalıştırıp `_connect()`'in
tam olarak 1 kez çağrıldığını (önceden 2 olurdu) doğruluyor — bu
öncekilerden farklı olarak collector'ı izole test etmiyor, uçtan uca
`collect_instance()` akışını kanıtlıyor. Yeni bir `tests/conftest.py`
eklendi (test DB yolunu `app.config.Settings()` ilk import edilmeden
önce ayarlıyor — dosya sırasına bağlı kırılgan bir env-var hilesi
yerine pytest'in standart mekanizması). Toplam 17 test yeşil.

**Faz 12 — Toplama aralığı instance başına ayarlanabilir.** Yeni
`Instance.collect_interval_seconds` (nullable — boş = uygulama genel
`settings.collect_interval_seconds` varsayılanı, 15sn). Scheduler tick'i
sabit kalıyor (`collect_all_instances()` hâlâ her `collect_interval_seconds`
saniyede bir tetikleniyor — `evaluate_custom_alert_rules`'ın custom alarm
kuralları için zaten kullandığı "sabit tick + öğe başına due-check"
desenini birebir tekrarlıyor): her instance için son toplamadan bu yana
geçen gerçek süre kendi (override edilmiş veya varsayılan) aralığını
geçmediyse o cycle'da atlanıyor. Bunu mümkün kılmak için `collect_instance`
artık `*_per_sec` metriklerin `delta_time`'ını nominal yapılandırılmış
aralık yerine GERÇEK geçen süreden hesaplıyor (yan fayda: scheduler
gecikmesi/backlog durumunda oran metrikleri artık daha doğru).
`InstancesPage`'in formuna "Toplama aralığı (saniye, opsiyonel)" alanı
eklendi (5-3600sn arası, boş = varsayılan). httpx ile doğrulandı:
`collect_interval_seconds=300` ile oluştur → döner; `null`'a resetle →
döner; `2` (min altı) → `422`.

**Test:** Yeni `test_scheduler_due_check.py` (2 test): 20 saniye önce
toplanmış iki instance'tan (biri varsayılan 15sn aralıkla, biri 3600sn
override ile) sadece varsayılan olanın gerçekten toplandığını, hiç
toplanmamış bir instance'ın override'dan bağımsız her zaman due
olduğunu kanıtlıyor. Toplam 19 test yeşil.

**Faz 12 — README'ye "Monitoring load" bölümü.** Kök `README.md`'ye
(mevcut dosya İngilizce, tutarlılık için bu bölüm de İngilizce yazıldı)
üç tabloluk bir denetim eklendi: toplama döngüsü (her sorgu, sıklık,
maliyet), dashboard refresh döngüsü, ve on-demand/cache'li araçlar
(index advice, EXPLAIN/EXPLAIN ANALYZE, Activity/Schema Health). Ayrıca
mevcut `pg_stat_statements.track = all` önerisi `track = top`'a
düzeltildi (gerekçesiyle birlikte — `all` fonksiyon içi her ifadeyi de
izler, dbace'nin okuduğu hiçbir şeye katkısı yok, sadece ek yük).

**Faz 13 — İŞ 1: Sihirbaz için model eksikleri.** `DatabaseGroup.listener_port`
eklendi (SQL Server Always On listener portu / PostgreSQL HAProxy-VIP
portu — bir node'un instance portundan bağımsız, ör. HAProxy 5000'de
dinlerken arkadaki PostgreSQL 5432'de olabilir). `Server.ip_address`
eklendi (hostname'e ek olarak, DNS henüz kurulu değilken de sunucuya
işaret edilebilsin diye). `NodeOut.ip_address` da host/site gibi
`node.server`'dan türetilen salt-okunur bir alan olarak eklendi.
Migration (SQLite auto-migration + Supabase), şema (`ServerCreate/
Update/Out`, `DatabaseGroupCreate/Update/Out`) ve `seed_demo.py`
güncellendi (`boa-sqlserver-ag` → `listener_port=1433`,
`aapara-patroni` → `listener_port=5000`, birkaç sunucuya örnek
`ip_address`). httpx ile doğrulandı: seed sonrası hem grup hem sunucu
hem de türetilen node alanları API üzerinden doğru dönüyor.

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
- `classify_connection_error` (Faz 10) driver'ın ham hata metnini anahtar
  kelime eşleştirmeyle Türkçe bir kategoriye ayırıyor (kimlik doğrulama /
  DNS / port / timeout / veritabanı yok) — bu bir sezgisel eşleme, driver
  sürümüne göre farklı ifadeler kullanılırsa (asyncpg/pyodbc/motor
  arasında veya sürümler arası) sınıflandırma "Bağlantı başarısız."
  genel mesajına düşebilir; orijinal driver mesajı her zaman parantez
  içinde korunduğundan bilgi kaybı yok, sadece kategori tahmini
  başarısız olabilir.

## API uyumluluğu

Mevcut hiçbir endpoint kırılmadı; `Instance` ile ilgili tüm uçlar ve
davranışları (cluster-health dahil) aynı kaldı. Sadece ek, yeni uçlar
eklendi.
