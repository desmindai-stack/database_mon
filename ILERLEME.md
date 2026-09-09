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

**Faz 13 — İŞ 2 (backend): Tek işlemli sihirbaz oluşturma uç noktası.**
Yeni `POST /api/wizard/database-groups` (`routers/wizard.py`): tek bir
istekte grup + her düğüm için yeni bir Server + Instance + Node
oluşturuyor, hepsi TEK bir DB transaction'ında — `db.commit()` sadece
en sonda, döngü içinde herhangi bir adım (ör. tekrar eden sunucu adı)
`HTTPException` fırlatırsa `db.rollback()` çağrılıyor ve o ana kadar
`flush()` edilmiş hiçbir şey (grup dahil) kalıcı olmuyor. `WizardCreateGroupRequest`
şeması (`schemas.py`) bir `model_validator` ile: standalone tam 1 düğüm,
cluster 2-8 düğüm; `patroni` sadece `postgresql`, `alwayson` sadece
`sqlserver`; cluster gruplarında `access_name`/`cluster_name` zorunlu —
hepsini istek DB'ye hiç dokunmadan (422 ile) reddediyor. Patroni
`cluster_options` (patroni/etcd/haproxy portları + keepalived VIP) tek
seferde girilip her düğümün `Node.options`'ına kopyalanıyor (mevcut
`services/cluster_health.py::probe_node()` zaten `Node.options`'tan
okuyor, değişiklik gerekmedi). Her zaman YENİ sunucu oluşturuyor —
var olan bir sunucuya (ör. paylaşımlı bir Windows kutusuna ikinci named
instance) bağlama senaryosu bilinçli olarak kapsam dışı, mevcut
sayfa-bazlı akışlar hâlâ o işi yapıyor (bkz. SORULAR.md).

**Test:** `backend/tests/test_wizard_atomicity.py` (3 test, gerçek
httpx/ASGITransport ile): standalone akışı grup+sunucu+instance+node'u
gerçekten birlikte oluşturuyor; listenin ikinci düğümünde tekrar eden
bir sunucu adı TÜM isteği (ilk düğüm için zaten oluşturulmuş sunucu
dahil) geri alıyor — hiçbir şey sızmıyor; engine/topology uyumsuzluğu
DB'ye hiç dokunmadan reddediliyor. Ayrıca dört topolojinin hepsi
(standalone, 2 düğümlü AG, 3 düğümlü Patroni + DR, 5 düğümlü özel
Patroni) ve üç ek doğrulama senaryosu (1 düğümlü cluster reddi, 9
düğümlü cluster reddi, engine/topology uyumsuzluğu reddi) ayrıca elle
bir doğrulama scriptiyle de çalıştırıldı. Toplam 22 test yeşil.

**Faz 13 — İŞ 2 (frontend): Tek ekranlı ekleme sihirbazı.** Yeni
`DatabaseWizardPage.tsx` (`/applications/:applicationId/groups/wizard`),
Database Groups sayfasındaki yeni birincil "+ Veritabanı Ekle (Sihirbaz)"
butonundan açılıyor (eski "+ Grup Ekle" formu "Manuel grup ekle" olarak
kalıyor — ana yol artık sihirbaz, ama tek tek ekleme akışları
kaldırılmadı). Adımlar:
1. **Topoloji** — motor (postgresql|sqlserver) + 4 topoloji kartından
   biri (standalone / 2 düğüm aynı DC / 3 düğüm 2DC+1DR / özel 2-8
   düğüm). Kart seçimi düğüm listesini doğru site/rol ön-dolduruyla
   otomatik kuruyor (ör. 3 düğüm preseti: 2. düğüm aynı DC replica, 3.
   düğüm disaster site replica).
2. **Cluster bilgileri** (sadece cluster topolojilerinde) — grup adı,
   cluster adı, erişim adı/listener IP/portu, ortam, PostgreSQL için
   ayrıca Patroni/etcd/HAProxy portları + keepalived VIP.
3. **Düğümler** (standalone'da tek bir "Sunucu ve instance" bloğu) —
   her düğüm için sunucu adı/hostname/ip/OS/site(+cluster'da rol),
   SQL Server'da instance adı, port/veritabanı/kullanıcı/şifre, opsiyonel
   agent bilgisi; her düğümde ayrı "Bağlantıyı test et" (+ agent varsa
   "Agent'ı test et") ve üstte "Tümünü test et"; özel topolojide "+
   Düğüm ekle"/"Sil" (2-8 sınırı içinde).
4. **Özet ve onay** — her alanın ve her düğümün (son test sonucuyla
   birlikte) özeti, "Kaydet" tıklanınca tek bir `POST
   /api/wizard/database-groups` çağrısı (Faz 13 İŞ 2 backend) ile
   oluşturuluyor, başarılıysa yeni grubun detay sayfasına
   yönlendiriliyor.

Zorunlu alanlar `*` ile işaretli; "İleri"ye basıldığında o adımın
zorunlu alanları eksikse geçiş engellenip alan bazında kırmızı hata
metni gösteriliyor (aynı doğrulama Kaydet'te de tekrar çalışıyor, geri
gidip düzeltmeden ilerlenemiyor). Bağlantı testi mevcut ve teşvik
ediliyor ama Kaydet'i kilitlemiyor — mevcut sayfa-bazlı düğüm ekleme
akışlarıyla aynı davranış (bkz. SORULAR.md).

**Faz 13 — İŞ 3: Mevcut formlarda zorunlu alan işaretlemesi + doğrulama.**
`ServersPage.tsx`, `InstancesPage.tsx`, `DatabaseGroupsPage.tsx`'in
oluşturma formlarına sihirbazla aynı desen uygulandı: zorunlu alanlar
`*` ile işaretli, gönderimde alan bazlı `fieldErrors` state'i ile
kırmızı hata metni (`.field-error`/`.field-invalid`), önceden sadece
HTML5 `required`'a (tarayıcıya göre değişen, stilsiz tooltip) güvenen
yerler artık uygulama genelinde tutarlı görünüyor. İki gerçek eksik de
kapandı: `ServersPage`/`DatabaseGroupsPage`'in tablo-içi satır düzenleme
formları (`saveEdit`) `<form>` DIŞINDAki bir butona bağlı olduğundan
HTML5 `required`'ın hiç etkisi yoktu — boş ad/host ile sessizce
kaydediliyordu; artık `saveEdit` içinde açık bir kontrol var. Aynı
geçişte `ServersPage`'e `ip_address`, `DatabaseGroupsPage`'e
`listener_port` alanları da eklendi (Faz 13 İŞ 1'den — önceden sadece
backend/şemada vardı, hiçbir formda giriş alanı yoktu).

**Faz 13 — Kapanış: sihirbaz dört topoloji için canlı sunucuya karşı
doğrulandı.** Bu ortamda tarayıcı otomasyonu yok, bu yüzden şu üç
katmanla doğrulandı:
1. **Gerçek uvicorn + gerçek SQLite'a karşı, curl ile** (in-process
   ASGITransport değil — `uvicorn app.main:app --port 8123` gerçekten
   ayağa kaldırılıp dıştan HTTP isteğiyle vuruldu): dört senaryonun
   hepsi (a: standalone, b: 2 düğüm AG, c: 3 düğüm Patroni+DR, d: 5
   düğüm özel AG) `POST /api/wizard/database-groups` ile başarıyla
   oluşturuldu; her grubun düğüm listesi (`GET /api/groups/{id}/nodes`)
   doğru site/rol/instance_name/ip_address ve her düğümün kendi
   instance'ına bağlı (`instance_id` dolu) olduğu teyit edildi; `GET
   /api/groups/{id}/health` dördü için de `200` döndü;
   `/parameters`/`/alwayson` (canlı DB bağlantısı gerektiren, demo
   host'ları sahte olduğundan beklenen) `502` ile temiz şekilde
   başarısız oldu, çökmedi.
2. **Gerçek Vite dev server'a karşı, curl ile**: `npm run dev` gerçekten
   ayağa kaldırılıp `/applications/1/groups/wizard` (SPA shell) ve
   `DatabaseWizardPage.tsx`'in Vite'ın kendi transform ucundan servis
   edilen hâli `200` döndü ve beklenen `export default function
   DatabaseWizardPage` işaretini içeriyordu — yani component gerçekten
   derleniyor/transform ediliyor, sadece `tsc` seviyesinde değil.
3. **Statik**: `tsc -b && vite build` (tip güvenliği) + her formun
   `buildPayload()`/`WizardCreateGroupRequest` şekli (1)'de doğrulanan
   uç noktanın kabul ettiği şekille birebir aynı.

**Doğrulanamayan şey, açıkça belirtiliyor:** Gerçek bir tarayıcıda
adım adım tıklayarak (buton tıklamaları, alan doldurma, "İleri"/"Geri"
geçişleri, hata mesajlarının görsel olarak doğru yerde çıkması) test
edilmedi — bu ortamda tarayıcı otomasyon aracı yok. Yukarıdaki üç katman
"sihirbazın ürettiği istekler backend tarafından doğru işleniyor" ve
"component hatasız derleniyor/render ediliyor" iddialarını kanıtlıyor,
"buton X'e tıklayınca Y oluyor" iddiasını değil.

**Faz 14 — İŞ 1: Engine'e göre alan gösterimi.** Sihirbazın düğüm
kartlarında ve `InstancesPage`'in ekleme/düzenleme formunda artık
SADECE seçili engine'in alanları render ediliyor (gizli/disabled değil,
DOM'da hiç yok): PostgreSQL → SSL modu (yeni); SQL Server → kimlik
doğrulama tipi (SQL|Windows, yeni — Windows seçilince kullanıcı adı/
şifre alanları TAMAMEN kayboluyor, gerçekten gerekmediği için); MongoDB
→ replica set adı + authSource (yeni, `InstancesPage`'de zaten vardı,
sihirbaza da eklendi) — engine=mongodb seçilince cluster topoloji
kartları devre dışı kalıyor (dbace'de MongoDB replica-set topolojisi
henüz modellenmiyor, sadece standalone anlamlı).

Yeni alanlar dekoratif değil, gerçekten toplama döngüsünü etkiliyor:
- `collectors/postgresql.py::_connect()` artık `options.ssl_mode ==
  "require"` ise `asyncpg.connect(ssl=True)` çağırıyor (asyncpg'nin
  `ssl` parametresi libpq'nun 6 değerli sslmode'u değil bool/SSLContext
  — UI de sadece disable/require sunuyor, gerekçesi SORULAR.md'de).
- `collectors/sqlserver_mongodb.py::build_odbc_connection_string()`
  artık `options.auth_type == "windows"` ise `UID=`/`PWD=` yerine
  `Trusted_Connection=yes` üretiyor.
- `MongoDBCollector._build_uri()` artık `options.replica_set` varsa
  bağlantı URI'sine `&replicaSet=<ad>` ekliyor.

Sihirbazın backend'i (`WizardCreateGroupRequest`) artık MongoDB'yi de
kabul ediyor (önceden tamamen reddediyordu) — ama sadece
`topology=standalone` ile; `replica_set` girilirse grup seviyeli
`cluster_name` boşsa onun yerine geçiyor (mongo standalone'da ayrı bir
cluster-bilgileri adımı yok, replica set adı en yakın karşılığı).

**Test:** Yeni `test_engine_specific_options.py` (4 test): Windows auth
DSN'de `Trusted_Connection=yes` üretip UID/PWD'yi tamamen çıkarıyor; SQL
auth (varsayılan) DSN'de kimlik bilgilerini kullanıyor; Mongo URI
`replica_set` verildiğinde `replicaSet=` içeriyor, verilmediğinde
içermiyor. Ayrıca httpx ile uçtan uca doğrulandı: MongoDB standalone
sihirbazla oluşturuldu (`instance.options.authSource`/`replica_set` ve
`instance.cluster_name` doğru); MongoDB + cluster topoloji denemesi
`422` ile reddedildi; PostgreSQL `ssl_mode=require` ve SQL Server
`auth_type=windows` ile oluşturulan instance'ların `options`'ı doğru
şekilde saklandı. Toplam 26 test yeşil.

**Faz 14 — İŞ 2: Tek ekleme giriş noktası.** `DatabaseWizardPage` artık
iki modda çalışan tek bir bileşen: route `/applications/:applicationId/
groups/wizard` (mevcut "create-group" modu, değişmedi) ve yeni route
`/groups/:groupId/wizard` ("add-node" modu — mevcut bir cluster grubuna
düğüm eklemek için). Mod, `useParams`'tan `groupId` gelip gelmediğine
göre türetiliyor (`groupId` varsa `add-node`). Add-node modunda:
- Engine/topoloji sihirbaza sorulmuyor, grup zaten belirliyor — "Topoloji"
  ve "Cluster bilgileri" adımları listeden tamamen çıkarılıyor (`steps`
  hesaplamasında), sihirbaz doğrudan "Yeni düğümler" adımıyla açılıyor.
- Kayıt, `POST /api/wizard/database-groups` yerine yeni `POST /api/
  wizard/groups/{group_id}/nodes` (bkz. aşağıdaki backend bölümü) çağırıyor.
- Standalone bir grup için bu moda hiç girilemiyor: `GroupDetailPage`'in
  "+ Düğüm Ekle" butonu `group.topology !== "standalone"` olmadıkça hiç
  render edilmiyor, sol menüdeki ağaçtaki "+" düğme aynı şekilde
  standalone gruplarda gizli (`App.tsx::groupNode`) — backend'in kendi
  400 reddiyle (bkz. İŞ 2 backend) iki katmanlı koruma.

**Backend (yeni uç nokta):** `POST /api/wizard/groups/{group_id}/nodes`
(`routers/wizard.py::wizard_add_nodes`) — `wizard_create_group` ile aynı
atomiklik garantisi (hepsi ya da hiçbiri; ortadaki bir düğüm ismi
çakışırsa öncekiler de rollback olur). Sunucu/instance/node oluşturma
mantığı iki uç nokta arasında `_create_server_instance_node()` helper'ına
çıkarıldı (davranış-koruyan refactor). Grup zaten standalone ise `400`;
8 düğüm sınırı aşılırsa `400`; `cluster_options` verilmezse kardeş
düğümlerden birinin `options`'ı miras alınıyor (Patroni portlarını her
düğüm eklemede yeniden girmemek için).

Mükerrer ekleme formları kaldırıldı: `DatabaseGroupsPage`'in inline
"Manuel grup ekle" formu (`api.createGroup` ile düğümsüz, çıplak bir
grup oluşturuyordu — sihirbazla üretilenden farklı/eksik bir yol) ve
`GroupDetailPage`'in inline "Yeni düğüm" formu (var olan bir sunucuya
manuel node bağlıyordu) silindi; her ikisinin "+" aksiyonu artık
sihirbaza yönlendiriyor. Düzenleme formları (`DatabaseGroupsPage`'in
satır-içi grup düzenleme, `GroupDetailPage`'in düğüm düzenleme ve "var
olan instance'a bağlan" akışı) korundu — İŞ 2 sadece ekleme yollarını
tekilleştiriyor, düzenlemeyi değil.

**Test:** Backend'e 3 yeni test eklendi
(`test_wizard_add_nodes.py`): kardeş `cluster_options` miras alma,
standalone grup reddi, mükerrer sunucu adında tam rollback (2. düğümün
ismi ilk düğümle çakışınca, o istekte flush edilmiş 1. düğümün sunucusu
da geri alınıyor — sızmadığı sunucu sayısıyla doğrulandı). Toplam 29
test yeşil. `tsc -b && vite build` yeşil.

**Faz 14 — İŞ 3: Bağımsız sunucu ekleme kalktı.** `ServersPage`'in
inline "Yeni sunucu" formu ve "+ Sunucu Ekle" giriş noktası kaldırıldı
(düzenleme/silme kaldı) — yerine sayfanın üstünde sihirbaza yönlendiren
bir not var. Sunucu bilgisi artık sadece instance eklerken giriliyor:
sihirbazın her düğüm kartında "Sunucu" seçimi (`servers.length > 0`
olduğunda görünür) — **Yeni sunucu** (eskisi gibi ad/host/ip/os/site/
agent alanları) veya **Mevcut sunucu** (müşterinin kayıtlı
sunucularından bir açılır liste; aynı fiziksel kutuda ikinci bir SQL
Server named instance'ı senaryosu için). Sihirbaz artık hem
create-group hem add-node modunda `GET /api/servers?customer_id=...`
çekip node kartlarında sunuyor.

**Backend:** `WizardNodeInput`'a `existing_server_id: int | None`
eklendi; `server_name`/`host` artık zorunlu değil, bir
`model_validator` ikisinden birinin (server_name+host YA DA
existing_server_id) verilmiş olmasını zorluyor.
`_create_server_instance_node()` artık `existing_server_id` varsa yeni
bir Server oluşturmak yerine var olanı (customer_id eşleşmesi
doğrulanarak — başka müşterinin sunucusu `404`) yeniden kullanıyor;
Instance/Node'un host'u her zaman `server.host`'tan okunuyor (tek
kaynak). Bunu yazarken gerçek bir bug bulundu ve test onu yakaladı:
`Node.name`'in `(group_id, name)` üzerinde unique kısıtı var — var olan
bir sunucuyu AYNI gruba ikinci kez eklerken (`server.name`'i doğrudan
node adı olarak kullanmak) çakışıyordu; `_unique_node_name()` helper'ı
eklendi (aynı `_unique_instance_name()` deseni, `-2`/`-3` soneki ekliyor).
Düğüm listesindeki "tekrar eden sunucu adı" reddi artık sadece yeni
sunucu oluşturan node'ları karşılaştırıyor (existing_server_id'li
node'ların `server_name`'i `None`, hepsini "aynı" sayıp yanlışlıkla
reddetmemesi için).

**Sunucu ne olacak (kullanıcıya soruluyor, otomatik silinmiyor):**
Kararın gerekçesi SORULAR.md'de — kısaca, bir Server artık birden fazla
Node barındırabildiğinden (yukarıdaki existing_server_id senaryosu),
"son düğüm silindi" anında kullanıcının niyeti belirsiz. Yeni uç nokta
`GET /api/servers/{id}/node-count`; `GroupDetailPage::onDeleteNode` bir
düğüm sildikten sonra bunu çağırıp sunucu sahipsiz kaldıysa ikinci bir
`confirm()` ile soruyor, evetse `DELETE /api/servers/{id}` çağırıyor.

**Test:** 2 yeni backend testi (`test_wizard_existing_server.py`):
`existing_server_id` ile eklenen düğümün gerçekten aynı Server row'unu
(yeni satır oluşturmadan) kullandığı ve host'unun o sunucudan doğru
okunduğu; başka bir müşterinin `existing_server_id`'sini kullanmaya
çalışmanın `404` ile reddedildiği. Toplam 31 test yeşil. `tsc -b &&
vite build` yeşil.

**Faz 14 — İŞ 4: Form yerleşimi (yalnızca sihirbaz, kapsam notu
aşağıda).** Backend/test değişikliği yok — sadece `DatabaseWizardPage`
ve `index.css`.

- **Sabit adım göstergesi + kendi içinde kayan içerik + sabit eylem
  çubuğu:** `.wizard-steps` artık `position: sticky; top: 0` (yeni
  `.sticky` class); adım içeriğini saran `.card` artık
  `.wizard-scroll-body` da taşıyor (`max-height: calc(100vh - 320px);
  overflow-y: auto` — sayfanın geri kalanı değil, sadece bu blok
  kayıyor); her adımın kendi "İleri"/"Geri"/"Kaydet" satırı artık
  `.wizard-form-actions` da taşıyor (`position: sticky; bottom: 0` —
  scroll bölgesinin altına yapışık kalıyor, JSX'i adım başına
  değiştirmeden salt CSS ile).
- **Çok düğümlü kartlar katlanabilir:** `mode === "add-node" ||
  isCluster` iken her düğüm kartı başlığa tıklanarak katlanıp
  açılabiliyor (`NodeFormState.collapsed`); kapalıyken tek satırlık özet
  gösteriliyor — sunucu adı (ya da seçili mevcut sunucunun adı) · site
  (Ana DC/DR) · son bağlantı testi durumu. Varsayılan: bir preset/özel
  sayı seçildiğinde ilk düğüm açık, geri kalanı kapalı başlıyor
  (`collapseAllButFirst()`); "+ Düğüm ekle" ile sonradan eklenenler de
  kapalı başlıyor (kullanıcı zaten kaç tane olduğunu biliyor, hepsini
  açık tutmak yine uzun bir liste demek).
- **Katlanabilir bölümler:** Her düğüm kartının içi üçe ayrıldı —
  "Sunucu bilgileri" (yeni/mevcut sunucu seçimi + ilgili alanlar),
  "Veritabanı bağlantısı" (port/database/kullanıcı/şifre + engine'e
  özel SSL/auth/replica-set alanları), "Agent" (sadece yeni sunucu
  modunda — mevcut sunucunun agent'ı zaten kendi kaydında). Tek seferde
  sadece biri açık (basit accordion, `NodeFormState.openSection`),
  varsayılan "Sunucu bilgileri". Rol seçimi bölümlerin dışında, hep
  görünür (tek bir alan, katlamaya değmez).
- **Kapsam notu:** Bu iş sadece sihirbaza uygulandı —
  `InstancesPage`'in düz ekleme/düzenleme formu ve `ServersPage`'in
  düzenleme satırı bilerek dokunulmadı (görev metni "ekleme paneli"
  diyordu, tekil; en uzun/en çok alan içeren panel sihirbazın çok
  düğümlü hâli, oradaki kazanç en büyük). Gerekçe SORULAR.md'de.

**Doğrulama notu:** Bu ortamda tarayıcı otomasyon aracı yok — sticky/
scroll/accordion davranışı görsel olarak tıklanarak doğrulanmadı; `tsc
-b && vite build` (tip/derleme doğruluğu) ve Vite dev sunucusunun
component'i hatasız transform ettiği (bkz. aşağıdaki dört-topoloji
doğrulama bölümü) kontrol edildi. Kullanıcı `npm run dev` ile fırsat
bulduğunda görsel olarak kontrol etmeli.

## Faz 14 sonu — dört topoloji + yeni akışlar gerçekten çalıştırılarak doğrulandı

Dört İŞ de bitince, gerçek bir uvicorn (`--port 8123`, temiz bir SQLite
dosyasına karşı) + gerçek bir Vite dev sunucusuna karşı, curl ile
uçtan uca çalıştırıldı (in-process test client değil):

1. **(a) Standalone** — PostgreSQL, 1 düğüm:
   `POST /api/wizard/database-groups` → `201`, `GET /api/groups/{id}/nodes`
   doğru server/host/instance eşlemesini döndürdü.
2. **(b) 2 düğümlü Always On** — SQL Server, biri `auth_type: windows`:
   `201`; `GET /api/instances/{id}` ile Windows auth düğümünün
   `options.auth_type == "windows"` olarak gerçekten saklandığı
   (dekoratif değil) doğrulandı.
3. **(c) 3 düğümlü Patroni + DR** — 2 ana DC + 1 disaster site, biri
   `ssl_mode: require`, `cluster_options` (patroni/etcd portları) her
   düğümün `options`'ına doğru yayıldı; `GET /api/groups/{id}/health`
   `200` döndü (host'lar sahte olduğundan `down` bekleniyor, öyle de
   oldu).
4. **(d) Özel 5 düğümlü Always On** — oluşturuldu, sonra üstüne İŞ 2/3'ün
   yeni akışları bu grupta canlı test edildi:
   - `POST /api/wizard/groups/{id}/nodes` ile 6. düğüm eklendi (`201`) —
     tek başına grup oluşturmadan sonradan ekleme gerçekten çalışıyor.
   - Aynı grupta, `winsvr-d1`'in `server_id`'siyle `existing_server_id`
     kullanılarak 2. bir named instance (`NAMEDINST`, port 1434)
     eklendi (`201`) — `GET /api/servers?customer_id=1` sunucu sayısının
     ARTMADIĞINI doğruladı (aynı Server row'u yeniden kullanıldı), yeni
     düğümün adı `Node.name` çakışmasını önlemek için otomatik
     `winsvr-d1-NAMEDINST` oldu (`_unique_node_name` fix'i canlı
     doğrulandı), host'u doğru şekilde `winsvr-d1.internal`'dan okundu.
   - O iki düğüm (`winsvr-d1` + `winsvr-d1-NAMEDINST`) silindi,
     `GET /api/servers/7/node-count` `0` döndü (sahiplenme kontrolü
     çalışıyor), `DELETE /api/servers/7` `204` ile temizlendi — İŞ 3'ün
     "son düğüm silinince ne olacak" akışının backend yarısı uçtan uca
     doğrulandı (frontend'deki `confirm()` diyaloğu görsel olarak
     denenmedi, ama çağırdığı iki uç nokta gerçekten böyle davranıyor).
   - Standalone bir gruba (`a`) `POST /api/wizard/groups/{id}/nodes`
     denendi → `400` (beklenen ret).

**Frontend (Vite dev sunucusu, gerçek `npm run dev`, curl ile):**
`/groups/4/wizard` (yeni add-node route) ve
`/applications/1/groups/wizard` (create-group route) SPA shell'i `200`
döndürdü; `DatabaseWizardPage.tsx`'in Vite transform çıktısı
`export default function DatabaseWizardPage` işaretini ve 6 adet
`WizardSection` kullanımını içeriyordu (İŞ 4'ün bölümlere ayırma
mantığı gerçekten derleniyor); `ServersPage.tsx`/`DatabaseGroupsPage.tsx`/
`GroupDetailPage.tsx`'in transform çıktısında kaldırılan eski form
id'leri (`new-server-form`/`new-group-form`/`new-node-form`) artık HİÇ
geçmiyor (grep 0 sonuç) — mükerrer formların gerçekten kaldırıldığı
kaynak/derlenmiş kod seviyesinde doğrulandı, sadece statik okumayla
değil.

**Doğrulanamayan şey, açıkça belirtiliyor:** Sihirbazın 4 adımını
gerçek bir tarayıcıda tıklayarak (İleri/Geri, katlanabilir bölüm/düğüm
kartı açma-kapama, sticky çubukların gerçekten yapışık kaldığını
görme, "Mevcut sunucu" dropdown'unun UI'da doğru dolduğunu görme) test
etmek bu ortamda mümkün değildi — tarayıcı otomasyon aracı yok. Yukarıdaki
doğrulama "sihirbazın ürettiği istekler backend tarafından doğru
işleniyor" ve "yeni component'ler hatasız derleniyor/transform ediliyor"
iddialarını kanıtlıyor, "kullanıcı X'e tıklayınca ekranda Y görünüyor"
iddiasını değil. Kullanıcı `npm run dev` ile fırsat bulduğunda dört
akışı da (özellikle "Mevcut sunucu" seçimi ve katlanabilir kartlar)
görsel olarak denemeli.

## Faz 14 sonrası düzeltme — kullanıcı geri bildirimiyle bulunan kalıntı eski ekleme yolları

Yukarıdaki curl/Vite-transform doğrulaması "yeni yollar çalışıyor mu"yu
kanıtladı ama "eski yollar gerçekten kayboldu mu"yu tarayıcıda
denemeden yakalayamadı — kullanıcı tarayıcıda hâlâ görünen iki eski
giriş noktası bildirdi, ikisi de bu fazın kapsamındaydı ama gözden
kaçmıştı:

- **`InstancesPage`'in "+ Yeni Instance" butonu ve açtığı eski
  formu.** Bu, Faz 1 öncesinden kalma "çıplak Instance" oluşturma
  yolu — Customer/Application/Group/Node modelinden tamamen bağımsız,
  `POST /api/instances`'a doğrudan gidiyordu (sihirbazın her zaman
  Server+Instance+Node'u birlikte oluşturduğu akıştan tamamen ayrı).
  Buton kaldırıldı, form artık SADECE düzenleme modunda açılıyor
  (`startAdd()` silindi, `onSubmit`'in create dalı kaldırıldı,
  `validate()`'in "editingId===null" şart dalı kaldırıldı — form artık
  tek moda indirgendi). Boş durumda gösterilen metin ve buton artık
  `/customers`'a (sihirbaz hiyerarşisinin köküne) yönlendiriyor.
  `DashboardPage`'in "+ Yeni instance" CTA'sı da aynı sebeple
  `/instances`'tan `/customers`'a çevrildi (eskiden bu ölü add-butonuna
  gidiyordu).
- **`ServersPage`'in "Sunucu ekle" butonu.** Kaynak kodda zaten İŞ 3
  commit'inde (0b23836) kaldırılmıştı — bu, muhtemelen tarayıcının
  eski bir build'i (stale dev server / cache) göstermesinden kaynaklı
  bir yanlış pozitifti. Dosya yeniden okunup teyit edildi: buton/form
  yok, sadece sihirbaza yönlendiren bir not var. Kod değişikliği
  gerekmedi.

**Tüm UI tarandı** (`Ekle|Yeni |Oluştur|+ [harf]` deseniyle grep) —
bulunan her "ekle" işlevli buton/link aşağıdaki "Kalan ekleme yolları"
listesinde. Veritabanı/instance/sunucu/düğüm/grup domain'i dışında
kalanlar (Müşteri, Uygulama, Alert kuralı ekleme) bilerek dokunulmadı —
bunların hiçbiri sihirbazın kapsamına girmiyor (motor/topoloji kavramı
yok, tekil basit CRUD formları, mükerrer bir yolları da yok).

**Kaldırılan buton/form:**
- `InstancesPage.tsx` — "+ Yeni Instance" butonu + formun create modu.

**Düzeltilen ölü yönlendirme (buton kalktı ama hedef güncellenmedi):**
- `DashboardPage.tsx` — "+ Yeni instance" artık `/customers`'a gidiyor
  (`/instances`'a değil).

**Kalan ekleme yolları (tam liste):**
| Yer | Buton/link | Hedef |
|---|---|---|
| Sol menü, uygulama satırı "+" | + Grup ekle | sihirbaz (`/applications/{id}/groups/wizard`) |
| Sol menü, grup satırı "+" (sadece cluster) | + Düğüm ekle | sihirbaz (`/groups/{id}/wizard`) |
| `DatabaseGroupsPage` üst bar | + Veritabanı Ekle | sihirbaz (`/applications/{id}/groups/wizard`) |
| `GroupDetailPage` üst bar (sadece cluster) | + Düğüm Ekle | sihirbaz (`/groups/{id}/wizard`) |
| `InstancesPage` üst bar | + Veritabanı Ekle | `/customers` (sihirbaz hiyerarşisinin kökü) |
| `DashboardPage` üst bar | + Yeni instance | `/customers` |
| Sihirbazın kendi içi | + Düğüm ekle (cluster-custom/add-node) | aynı sihirbaz formunda yeni satır |
| Sol menü kökü / `CustomersPage` | + Müşteri Ekle | inline form (sihirbaz kapsamı dışı — Customer'ın motoru/topolojisi yok) |
| Sol menü, müşteri altı / `ApplicationsPage` | + Uygulama Ekle | inline form (aynı gerekçe) |
| `AlertsPage` | Kural ekle | inline form (ayrı domain — alert rule, veritabanı/sunucu değil) |

`ServersPage` ve `GroupDetailPage`'in eski "sunucu ekle"/"düğüm ekle"
inline formları (İŞ 2/İŞ 3) hâlâ kaldırılmış durumda — listede yok.

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

## Faz 15 — İŞ 1: Kimlik doğrulama

**Bu, API uyumluluğunu bilerek kırıyor** (aşağıdaki "API uyumluluğu"
bölümüne bakın) — görevin kendisi "şu an API'de hiç kimlik doğrulama
yok" tespitiyle başlıyordu ve bunu düzeltmek doğası gereği önceden
anonim erişilebilen her uca artık bir token şartı koymak demek.

**Backend:**
- Yeni `User` modeli: `username` (unique), `email` (opsiyonel),
  `password_hash` (bcrypt), `role` (`admin`|`viewer`), `is_active`,
  `must_change_password`, `last_login_at`.
- `services/security.py`: bcrypt hash/verify + PyJWT access/refresh
  token üretimi ve çözümü. `JWT_SECRET` ayarlanmazsa dev-only bir
  varsayılanla çalışır ve her başlangıçta uyarı loglar (mevcut
  `CREDENTIALS_MASTER_KEY` uyarı desenini tekrarlıyor).
- `services/auth_deps.py`: `get_current_user` (Bearer token doğrulama),
  `require_admin`, ve `require_write_access` — sonuncusu `request.method`
  GET değilse (`POST/PUT/PATCH/DELETE`) admin şartı koyuyor, GET'lerde
  sadece oturum yeterli. Bu TEK dependency, `main.py`'de her router'ın
  `include_router(..., dependencies=[Depends(require_write_access)])`
  ile sarılmasıyla hem "tüm /api/* korunsun" hem "viewer salt-okunur"
  kuralını tek seferde karşılıyor — 9 router dosyasının hiçbirine tek
  tek dokunmadan (SORULAR.md'de bu tasarım kararının gerekçesi var).
- `routers/auth.py`: `POST /login`, `POST /refresh`, `POST /logout`
  (stateless JWT — sunucu tarafında iptal yok, bkz. SORULAR.md),
  `GET /me`, `POST /change-password`.
- `services/bootstrap.py::ensure_default_admin()`: `users` tablosu
  boşsa `ADMIN_USERNAME`/`ADMIN_PASSWORD`'dan bir admin oluşturuyor;
  `ADMIN_PASSWORD` verilmezse rastgele bir şifre üretilip BİR KEZ log'a
  yazılıyor (tahmin edilebilir bir varsayılan yerine). Yeni admin her
  zaman `must_change_password=True` ile oluşuyor.
- `/api/health` ve `/api/auth/login` (+`/refresh`) hariç HER `/api/*`
  ucu artık oturum gerektiriyor; `/api/config` de dahil (plain
  `@app.get`, router'a dahil değildi — ayrıca `Depends(get_current_user)`
  eklendi).

**Frontend:**
- `auth.tsx`: `AuthProvider`/`useAuth()` — token'ları `localStorage`'da
  (`dbace_auth` anahtarı) saklıyor, `api.ts`'in modül-seviyesi
  `authToken`/`refreshTokenValue` değişkenlerini güncelliyor (React
  state değil — `api.ts` bir component değil, her isteğin senkron token
  okuması gerekiyor). `api.ts`'in `request()` fonksiyonu artık 401
  aldığında (login/refresh/health hariç) otomatik olarak refresh
  token'la yeni bir access token almayı deniyor, başarısızsa
  `onUnauthorized` callback'ini tetikleyip oturumu temizliyor.
- `App.tsx`: `AuthProvider` + `AuthGate` ile sarıldı —
  `loading` → yükleniyor ekranı, `!user` → `LoginPage`,
  `user.must_change_password` → `ForcedPasswordChangePage` (şifre
  değiştirmeden asıl uygulamaya geçilemiyor), aksi halde eski `App`
  gövdesi (`AppShell` olarak yeniden adlandırıldı). Sidebar'a
  kullanıcı adı + rol rozeti + "Çıkış yap" eklendi.
- **Viewer için UI gizleme:** Sol menü ağacındaki "+" giriş noktaları
  (`canWrite` prop'u ile `NavTreeBranch`/`MainNavTree`'ye kadar
  taşındı) ve ana CRUD sayfalarındaki (Customers, Applications,
  DatabaseGroups, GroupDetail, Servers, Instances, Dashboard, Alerts,
  Predictions) ekle/düzenle/sil/onayla butonları `useAuth().user?.role
  === "admin"` koşuluyla gizlendi; sihirbaz artık admin olmayan bir
  kullanıcıya "Bu işlem için admin yetkisi gerekiyor" gösterip formu
  hiç render etmiyor. Bu gizleme UX içindir — gerçek yetki sınırı her
  zaman backend'de (`require_write_access`, 403).

**Test:** Yeni `tests/test_auth.py` (10 test): health/login herkese
açık, korumalı uç token'sız 401, login başarı/başarısız, viewer
GET-200/POST-403, admin POST-201, refresh token akışı, access token
refresh endpoint'inde reddediliyor (tip karışıklığı önleniyor),
change-password akışı (yanlış mevcut şifre 400, doğrusu 200, eski şifre
artık çalışmıyor), pasif kullanıcı login edemiyor. Mevcut 3 test
dosyası (`test_wizard_add_nodes.py`, `test_wizard_atomicity.py`,
`test_wizard_existing_server.py`) artık korumalı uçlara gittiğinden
yeni paylaşılan `tests/auth_helper.py::authed_client()` ile
güncellendi (tek satırlık değişiklik, her dosyanın `_client()`
helper'ı artık login olup token ekliyor). Toplam 41 test yeşil.

**Canlı doğrulama:** Gerçek uvicorn'a (`ADMIN_USERNAME`/`ADMIN_PASSWORD`
/`JWT_SECRET` env'leriyle) karşı curl ile: `/api/health` token'sız 200;
`/api/customers` token'sız 401; login ile admin bootstrap doğru
kimlik bilgileriyle çalışıyor ve `must_change_password: true`
dönüyor; token'lı istek 200; yanlış mevcut şifreyle change-password
400, doğrusuyla 200 ve `must_change_password: false`'a dönüyor; admin
token'ıyla `POST /api/customers` 201.

## Faz 15 — İŞ 2: Veri saklama ve admin ekranı

**Backend:**
- `services/retention.py` — `AppSetting` tabanlı basit anahtar/değer
  deposu (mevcut `dashboard_refresh_interval_seconds` deseniyle aynı):
  `metrics_retention_days` (7/14/30/60/90, varsayılan 30 — "1 ay"),
  `retention_last_run_at`, `retention_last_deleted_count`.
  `run_retention_cleanup()` `MetricSample.collected_at`,
  `SlowQuerySample.collected_at`, `AlertEvent.triggered_at`,
  `PredictionInsight.created_at` sütunlarına göre kesim tarihinden eski
  satırları tek seferde `DELETE ... WHERE` ile siliyor (ORM nesnesi
  nesne yükleyip tek tek silmiyor — büyük tablolarda ölçeklenir),
  silinen toplam satır sayısını ve zamanı `AppSetting`'e yazıyor.
- `collectors/scheduler.py`'ye günlük (`interval, days=1`) yeni bir iş
  eklendi (`retention_cleanup_tick`) — mevcut tek `AsyncIOScheduler`
  örneğine katıldı, ayrı bir scheduler açılmadı.
- Yeni `routers/admin.py` — `/api/admin/retention` (GET/PUT),
  `/api/admin/retention/run` (POST, elle tetikleme),
  `/api/admin/users` (GET liste, POST oluştur),
  `/api/admin/users/{id}` (PATCH rol/aktiflik, DELETE),
  `/api/admin/users/{id}/reset-password` (POST — rastgele geçici şifre
  üretip bir kerelik döndürür, `must_change_password=True` set eder).
  Bu router `main.py`'de `dependencies=[Depends(require_admin)]` ile
  dahil edildi — İŞ 1'in `require_write_access`'inden farklı olarak
  GET dahil HER metod admin gerektiriyor (kullanıcı listesi/saklama
  ayarı viewer'a bile görünmüyor).
- Kendi kendini pasifleştirme/silme/yetki düşürme engellendi
  (`update_user`/`delete_user` içinde `current.id == user_id` kontrolü)
  — son admin'in kazara kendini kilitleyip dışarıda kalması önlendi.

**Frontend:**
- Yeni `AdminPage.tsx` (`/admin`, sadece admin — sidebar linki
  `canWrite` ile gizli, sayfanın kendisi de `currentUser.role !==
  "admin"` ise "admin yetkisi gerekiyor" gösteriyor) — üç sekme:
  **Veri saklama** (süre seçimi + son çalışma zamanı/silinen kayıt +
  "Şimdi temizle"), **Kullanıcılar** (liste + rol değiştir/pasifleştir/
  şifre sıfırla/sil + yeni kullanıcı formu), **Genel ayarlar**
  (dashboard otomatik yenileme aralığı — DashboardPage'den buraya
  taşındı).
- `DashboardPage.tsx`'in eski "Otomatik yenileme" `<select>`'i
  kaldırıldı; sayfa hâlâ `GET /api/dashboard/refresh-interval`'i okuyup
  kendi otomatik yenileme timer'ında kullanıyor, sadece DEĞİŞTİRME
  kontrolü artık Admin'de. Elle "Yenile" butonu (aksiyon, ayar değil)
  Dashboard'da kaldı.

**Test:** Yeni `tests/test_admin.py` (4 test): viewer admin uçlarına GET
dahil giremiyor (403), saklama süresi GET/PUT (geçersiz değer 400),
elle temizlik tetikleme son-çalışma alanlarını dolduruyor, kullanıcı
CRUD + kendi kendini pasifleştirme/silmenin reddedildiği. Toplam 45
test yeşil.

**Canlı doğrulama:** Gerçek uvicorn'a karşı curl ile: retention GET
(varsayılan 30) → PUT 60 → POST /run (`last_run_at`/`last_deleted_count`
doluyor) → kullanıcı oluşturma (`must_change_password: true` ile) →
liste iki kullanıcıyı da gösteriyor.

## Faz 15 — İŞ 3: Dashboard sayaç kartları tıklanabilir

**Bulgu:** `top_issues` (dashboard'un mevcut tek itemize listesi)
sadece aktif sorunu olan grupları içeriyor — "Sağlıklı" veya
"Bilinmiyor" kartına tıklayınca filtrelenecek hiçbir satır yoktu (o
gruplar zaten `top_issues`'a hiç girmiyor). Bunun için backend'e
`GroupStatusRowOut` eklendi: `collect_dashboard_summary()` artık HER
grup için (durumu ne olursa olsun) bir satır üretiyor
(`DashboardSummaryOut.groups`) — snapshot yoksa `status: "unknown"`
olarak, varsa snapshot'ın `overall`'ı olarak.

**Frontend:** `StatCard` artık `status` prop'u verilen dört sağlık
kartı (Kritik/Uyarı/Sağlıklı/Bilinmiyor) için tıklanabilir — mevcut ama
kullanılmayan `.stat-card.clickable`/`.stat-card.active`/`.stat-card-btn`
CSS'i (kod tabanında zaten duruyordu, hiçbir yerde referans edilmiyordu)
kullanıldı. Tıklamak `statusFilter` state'ini set ediyor (tekrar
tıklamak temizliyor); aktif filtre kartın etrafında bir highlight
(`.active` — `box-shadow`) olarak görünüyor. Filtre aktifken sayaç
kartlarının hemen altında yeni bir "{Durum} gruplar" kartı açılıyor —
`groupSummary.groups`'u `statusFilter`'a göre filtreleyip listeliyor
(grup adı → `/groups/{id}` linki, müşteri/uygulama, ortam rozeti), boşsa
"Bu durumda grup yok" gösteriyor; "Filtreyi temizle" butonu filtreyi
sıfırlıyor. "Database groups"/"Müşteriler" toplam kartları `status`
almıyor, tıklanabilir değil (bir "durum" temsil etmiyorlar).

**Test:** Yeni `tests/test_dashboard_summary.py` — taze oluşturulan bir
grubun (henüz hiç probe çalışmamış, `GroupHealthSnapshot` yok)
`groups` listesinde `status: "unknown"` ile göründüğünü kanıtlıyor
(önceki `top_issues`-only şekilde bu grup hiçbir yerde görünmezdi).
Toplam 46 test yeşil.

## Faz 15 — İŞ 4: Dashboard bilgi düzeni

"En kritik sorunlar" ve "Öneriler" iki ayrı, birbirine gevşekçe bağlı
liste kartıydı (bir sorunun altında rastgele "grubun en iyi önerisi"
gösteriliyordu, öneriler listesi ayrıca tekrar aynı bilgiyi içeriyordu).
Tek bir "Sorunlar ve öneriler" kartına, kart-başına-satır düzenine
indirgendi.

**Backend zenginleştirme (İŞ 4'ün gerektirdiği):**
- `DashboardIssueOut`'a `node: str | None` (down_nodes/split-brain
  olaylarında etkilenen düğüm adları) ve `checked_at: datetime | None`
  (o grubun health snapshot'ının alındığı an) eklendi.
- `DashboardRecommendationOut`'a `steps: list[str]` (katlanabilir
  bölüm açıldığında numaralı liste olarak gösterilecek adımlar) ve
  `customer`/`application`/`environment`/`link_hint`/`checked_at`
  eklendi — önceden bir öneri sadece `group` (ad, string) taşıyordu,
  bağımsız bir öneri kartı olarak (bir "issue"ya iliştirilmeden)
  gösterilebilmesi için tam kaynak bilgisine ihtiyaç vardı.
  `services/dashboard_snapshot.py`'nin üç öneri üreticisi
  (`_connectivity_recommendations`, `_parameter_recommendations`,
  `_instance_recommendations`) artık gerçek, birden fazla adımlı
  `steps` listeleri dolduruyor (tek cümlelik `message`'ı olduğu gibi
  bırakıp yanına ekliyor, uydurma adım eklemiyor — performance_insights
  kaynaklı öneriler hâlâ tek adımlı, çünkü altta yatan veri (bir
  `PerformanceInsight`'ın `recommendation`'ı) zaten tek bir düz-yazı
  cümlesi).
- Bunu yazarken gerçek bir bug bulundu ve yeni test onu yakaladı:
  `min(checked_ats)` bazen "can't compare offset-naive and
  offset-aware datetimes" ile patlıyordu — SQLite tzinfo'yu kalıcı
  olarak saklamıyor, aynı session'da yeni yazılmış bir satırın Python
  nesnesi (identity map'te, aware) ile başka bir session'da yazılıp bu
  session'da taze sorgulanan satırlar (naive) karışınca `min()`
  karşılaştıramıyordu. `collect_dashboard_summary`'de `checked_ats`
  listesine eklerken artık `tzinfo` varsa çıkarılıyor (hepsi zaten UTC
  anı, sadece etiket farkı).

**Frontend:** `DashboardPage.tsx`'e `buildProblemCards()` —
`top_issues` + `recommendations`'ı TEK bir kart listesine birleştirip
tekrarları eliyor (bir issue'ya iliştirilmiş öneri, ayrıca kendi
kartı olarak tekrar gösterilmiyor — `group|message` anahtarıyla
dedup). Her kart: kapalıyken sadece severity rozeti + başlık (üstte,
tıklanabilir toggle) + kaynak satırı (grup linki, müşteri/uygulama,
düğüm varsa, ortam rozeti, "X dakika önce") görünüyor; açılınca
katlanabilir gövdede numaralı adım listesi (`<ol>`) ve varsa komut
(`CopyableAction` ile ayrı satırda, kopyala butonlu) çıkıyor. Adımı/
komutu olmayan kartların toggle'ı devre dışı (`·` işareti, katlanacak
bir şey yok).

**Test:** Yeni `tests/test_dashboard_issue_enrichment.py` — canlı ağ
prob'u gerektirmeden (sahte host'lara bağlanmayı beklemek yerine)
doğrudan bir `GroupHealthSnapshot` satırı yazıp `collect_dashboard_
summary()`'yi çağırarak `checked_at`/`node`/`steps`/kaynak alanlarının
hem issue hem recommendation tarafında doğru dolduğunu kanıtlıyor. Bu
testi yazarken bir de test-izolasyonu sorunu bulundu: `top_issues`/
`recommendations` `[:10]`'a kırpılıyor, ve `tests/conftest.py`'nin
SQLite dosyası pytest çalıştırmaları arasında KALICI (temizlenmiyor) —
testi tekrar tekrar çalıştırırken (debug sırasında) biriken "critical"
snapshot'lar top 10'u doldurup testin kendi satırını dışarı itti.
Test artık kendi oluşturduğu `GroupHealthSnapshot`'ı sonunda siliyor;
kalıcı dosyadaki eski birikim de bir kere temizlendi
(`data/dbace_pytest.db` silinip yeniden oluşturulmaya bırakıldı —
gerçek `data/dbace.db` değil, sadece test fixture'ı). Toplam 47 test
yeşil, art arda çalıştırıldı, kararlı.

**Doğrulama notu:** Kart açma/kapama ve "Kopyala" butonunun görsel
davranışı tarayıcıda tıklanarak denenmedi (bu ortamda tarayıcı
otomasyonu yok) — Vite dev sunucusunun component'i hatasız transform
ettiği ve `tsc -b && vite build`'in geçtiği doğrulandı.

## Faz 15 — İŞ 5: Kopyala butonu taşıyor

**Bulgu:** Kod tabanında komut-kutusu + kopyala-butonu deseni tek bir
yerde var — `DashboardPage.tsx`'in `CopyableAction` component'i (Faz
15 İŞ 4'te de bu component kullanıldı). Eski düzen: `.rec-action-row`
flex satırında kod kutusu (`flex: 1`) ile "Kopyala" metin butonu YAN
YANA sibling'lerdi — uzun bir komutta kutu `overflow-x: auto` ile
kendi içinde kaymak yerine (bazı tarayıcı/konteyner genişlik
kombinasyonlarında) satırın tamamı taşıp butonu ekran dışına itiyordu.

**Düzeltme:** Buton artık kutunun İÇİNDE, sağ üst köşesine
`position: absolute` ile sabitlenmiş küçük bir ikon (`.rec-action-copy-btn`,
22×22px, metin yok — inline SVG kopyala/onay ikonları). Kod kutusu
(`<pre className="rec-action-code">`) `max-width: 100%` ve
`overflow-x: auto` ile kendi içinde yatay kayıyor, sağdan
`padding: 2rem` ile ikonun altında metin kalmıyor. Kopyalanınca ikon
1.5 saniyeliğine bir onay (✓) ikonuna dönüşüyor (`title`/`aria-label`
de "Kopyalandı" oluyor) — önceki "Kopyalandı" metin değişimiyle aynı
süre, sadece görsel olarak ikon.

**Kapsam notu:** Yazıldığı anda bu component `DashboardPage.tsx`
dışında hiçbir yerde kullanılmıyordu (grep ile doğrulandı). İŞ 6'da
`PredictionsPage`'in de aynı deseni kullanması gerekince
`components/CopyableAction.tsx`'e taşındı (bkz. İŞ 6) — o zaman tek
dosyaya özeldi, artık paylaşılan bir component.

**Doğrulama:** `tsc -b && vite build` yeşil; backend değişmedi (saf
frontend/CSS değişikliği), 47 test yeşil kaldı. Görsel taşma/kayma
davranışı tarayıcıda tıklanarak denenmedi (otomasyon yok).

## Faz 15 — İŞ 6: Predictions çözüm önerisi versin

**Backend:** `PredictionInsight`'a iki yeni sütun —
`recommendation: str | None` (öneri metni) ve `action: str | None`
(varsa kopyalanabilir tek satırlık komut, Dashboard'un öneri
kartlarıyla aynı desen). `services/prediction.py`:
- Bağlantı sayısı artış tahminlerine (`connection_utilization_pct`/
  `active_connections`) artık engine'e göre değişen bir öneri
  iliştiriliyor: PostgreSQL → `max_connections`/pooler (PgBouncer/
  pgpool-II) + `SHOW max_connections;` komutu; SQL Server → bağlantı
  havuzlama ayarları; MongoDB → sürücü `maxPoolSize`.
- **Yeni bir tahmin türü eklendi:** `database_size_bytes` artık trend
  izleniyor (öncesinde hiç tahmin edilmiyordu). Büyüme hızından
  ("iki katına çıkma tarihi") bir tahmin üretiyor, önerisi arşivleme/
  partitioning/VACUUM/disk büyütme kombinasyonu — İŞ 6'nın istediği
  "disk dolma tahmini" ve "tablo büyüme trendi" maddelerinin ikisini
  de tek sinyalle (dbace'in gerçekten topladığı tek büyüme verisi)
  karşılıyor; kapsam sınırı SORULAR.md'de.
- Cache hit oranı düşüşü ve replication lag tahminlerine de genel
  (parametre-spesifik OLMAYAN) öneri metinleri eklendi — "Öneriler
  mevcut parameter_audit ve index_advisor çıktılarıyla çelişmesin"
  şartı gereği, cache hit önerisi spesifik bir `shared_buffers` değeri
  önermek yerine "Parametreler sekmesine bakın" diyerek
  parameter_audit'in kendi (canlı değer bilen, daha kesin) bulgusuna
  yönlendiriyor — aynı ayarı iki farklı sayıyla önermenin çelişki
  riski böylece yok ediliyor.

**Frontend:** `CopyableAction`, `DashboardPage.tsx`'ten
`components/CopyableAction.tsx`'e taşınıp paylaşılan bir component
oldu. `PredictionsPage.tsx`'in tablosuna yeni bir "Önerilen aksiyon"
sütunu eklendi — öneri metni + varsa kopyalanabilir komut kutusu.

**Test:** Yeni `tests/test_prediction.py` (4 test, `run_predictions()`
çağrılarak doğrudan test edildi — canlı bir hedef veritabanına
bağlanmayı gerektirmez): PostgreSQL bağlantı artışı önerisinde
"pooler"/"max_connections" geçiyor + `action="SHOW max_connections;"`;
SQL Server'da farklı (havuzlama) öneri, `action=None`; veritabanı
boyutu büyüme trendinde mesajda "iki katına" ve önerisinde "arşiv"/
"partit" + "vacuum" geçiyor; düz/sabit boyutta hiç tahmin
üretilmiyor. Toplam 51 test yeşil.

**Doğrulanamayan şey:** Gerçek bir hedef veritabanına karşı uçtan uca
(collector → gerçek metrik toplama → tahmin → API → UI) canlı doğrulama
yapılmadı — bu ortamda gerçek bir PostgreSQL/SQL Server/MongoDB yok
(önceki fazlarda da aynı sınır not edildi). `run_predictions()`'ın
kendisi doğrudan, gerçekçi girdilerle test edildi; eksik olan sadece
"gerçek bir collector döngüsünün bu fonksiyonu doğru çağırdığı" ucu
(kod okunarak doğrulandı: `services/collection.py` artık
`engine=instance.engine` geçiyor).

## Faz 15 — İŞ 7: Alerts sayfası düzenlemesi

**Backend:** `services/custom_alert_rules.py::_resolve_target_instance`
artık bir `AlertRule` nesnesi yerine doğrudan `instance_id`/`group_id`
alıyor (tek çağrı yeri — `evaluate_custom_alert_rules` — güncellendi) —
bu, kural henüz KAYDEDİLMEDEN test edilebilmesini sağlıyor. Yeni
`test_custom_query()`: read-only doğrulama → hedef instance çözümleme
→ `_run_query()` (mevcut, kuralların gerçek çalıştırma yolu) ile
çalıştırma → `classify_connection_error` ile Türkçe hata sınıflandırma.
Yeni uç nokta `POST /api/alerts/rules/test-query` (mevcut
`ConnectionTestResult` şemasını yeniden kullanıyor — yeni bir şema
icat etmeye gerek kalmadı).

**Frontend:**
- **Ayrı sayfa:** Yeni `CustomAlertRuleFormPage.tsx` (`/alerts/new`) —
  eski satır-içi "Özel kural ekle" kartı kaldırıldı, `AlertsPage`'in
  sağ üstündeki "+ Özel kural ekle" butonu artık buraya yönlendiriyor.
  SQL editörü artık tam sayfa genişliğinde, 10 satır
  (`.sql-editor` — monospace, `min-height: 220px`), yanında "Sorguyu
  test et" butonu sonucu (`ok`/hata mesajı) gösteriyor. Bunu yazarken
  fark edildi: kod tabanında `textarea` elementleri hiç
  stillendirilmemiş (sadece `font: inherit`) — karanlık temada beyaz
  arka planla render oluyorlardı. `.form-grid textarea` artık
  input/select ile aynı temel stili alıyor (bu, SQL editörüne özel
  değil, uygulamadaki HER textarea'yı düzeltiyor — ör. "Notlar"
  alanları).
- **Sekmeler:** `AlertsPage.tsx` artık "Aktif alarmlar" (aktif rozet
  sayısıyla) | "Kural listesi" | "Geçmiş" (çözülmüş event'ler,
  `resolved_at`'e göre) üç sekme. Tek bir `Promise.all` ile hem aktif
  (`active_only=true`) hem tüm (`active_only=false`, client-side
  `resolved_at != null` filtresiyle geçmişe ayrılıyor) event'ler
  çekiliyor.
- **Kural listesi:** Ad araması (`input`), önem derecesi ve engine
  filtre `<select>`'leri (`useMemo` ile client-side filtreleniyor —
  kural sayısı küçük, ayrı bir backend filtre ucu gerekmedi). Varsayılan/
  özel ayrımı artık renkli rozetle net (`tag public` = Varsayılan,
  `tag private` = Özel — mevcut `.tag.public`/`.tag.private` CSS'i
  yeniden kullanıldı).

**Test:** Yeni `tests/test_alert_rule_query_test.py` (3 test): tam
olarak bir hedef gerektiği (ikisi de/hiçbiri 400), DELETE gibi
salt-okunur-olmayan bir sorgunun `200` ama `ok:false` ile reddedildiği,
erişilemeyen bir hedefin çökme yerine `ok:false` + anlamlı mesajla
döndüğü. Toplam 54 test yeşil.

**Canlı doğrulama:** Gerçek uvicorn'a karşı curl ile: hedefsiz
test-query `400`; `DELETE FROM foo` → `ok:false`, "salt-okunur"
mesajı; sahte host'a `SELECT 1` → `ok:false`, DNS hatası Türkçe
sınıflandırılmış; özel kural gerçekten oluşturuluyor ve listede
`is_default:false` ile görünüyor; `active_only=true`/`false` ikisi de
çalışıyor.

## Faz 15 — İŞ 8: DPA sayfası — grafik-sorgu ilişkilendirmesi

**Backend değişikliği yok** — `GET /api/queries/{id}/history` (mevcut,
`QueryHistorySeries.points`'te `collected_at`/`calls`/`total_time_ms`/
`interval_mean_ms`/`calls_delta` zaten vardı) bu özelliğe yetiyordu;
`InstanceDetailPage`'in "Sorgu geçmişi (trend)" bölümü için zaten
çekilen `queryHistoryTop` verisi yeniden kullanıldı.

**Yeni "Sorgu yükü zaman çizelgesi"** (Yavaş Sorgular sekmesi, "Sorgu
geçmişi (trend)" mini-kartlarının altında, "Yavaş sorgu dağılımı" bar
grafiğinin üstünde):
- `loadTimeline`: `queryHistoryTop`'taki tüm sorguların noktalarını
  zaman damgasına (`HH:MM`) göre grupluyor, her sorgunun o andaki
  `interval_mean_ms`'ini (mevcut `QueryHistoryChart`'ın kullandığı
  aynı alan) toplayıp tek bir "toplam yük" değeri üretiyor —
  gerçek bir dalgalanma grafiği.
- **Sıçrama tespiti:** zaman çizelgesinin ortalama + standart sapması
  hesaplanıp `ortalama + 1.5×std` üzerindeki noktalar kırmızı
  `ReferenceDot` ile işaretleniyor (en az 4 nokta yoksa hiç
  işaretlemiyor — yanlış pozitif riski almıyor).
- **Tıklama:** Grafiğin `onClick`'i recharts'ın `activeLabel`'ini
  kullanıp `selectedTime` state'ini set ediyor; hiç tıklanmamışsa
  varsayılan olarak en büyük sıçrama (yoksa en yüksek nokta) otomatik
  seçili geliyor — bölüm hiçbir zaman boş başlamıyor.
- **İlişkilendirilmiş sorgular:** Seçili zaman noktasındaki tüm
  katkıda bulunan sorgular (o andaki ortalama süreye göre sıralı)
  katlanabilir kartlarda listeleniyor — kapalıyken sorgu özeti + çağrı
  sayısı/toplam süre/o andaki ortalama süre; açılınca tam sorgu metni,
  "Olası nedenler" (aşağıya bakın), ve EXPLAIN/Index önerisi butonları
  (mevcut `loadExplain`/`loadAdvice`/`ExplainPlanTree`/advice-card
  render mantığı yeniden kullanıldı — yeni bir kopya yazılmadı). Sorgu
  artık güncel yavaş-sorgu snapshot'ında yoksa (queryid eşleşmiyor)
  EXPLAIN/advice butonları yerine bir açıklama notu gösteriliyor.
- **Olası nedenler:** dürüst, veriye dayalı iki sinyal — çağrı sayısı
  bu aralıkta arttıysa, ve/veya o andaki ortalama süre yüksekse
  (>100ms) — hiçbiri yoksa genel bir "toplam yüke katkı yaptı" notu.
  Wait-event/lock verisi dbace'de yok, bu yüzden "kilit bekliyordu"
  gibi kesin bir neden İDDİA EDİLMİYOR, sadece "olabilir" deniyor
  (gerekçe SORULAR.md'de).

**Sayfa dağılmadı:** Grafik + ilişkilendirilmiş sorgu listesi TEK bir
kartta, tek bölümde; detaylar (sorgu metni, nedenler, EXPLAIN, advice)
katlanabilir kartların içinde — sayfanın başka bir yerine dağılmıyor.

**Doğrulama:** `tsc -b && vite build` yeşil (recharts'ın `onClick`
event tipiyle ilk denemede sorunsuz derlendi); backend değişmedi, 54
test yeşil kaldı; Vite dev sunucusu component'i hatasız transform etti
(`loadTimeline`/`possibleCauses` işaretleri servis edilen kaynakta
mevcut). Tıklama/sıçrama-işaretleme davranışının görsel doğrulaması
tarayıcıda yapılmadı (bu ortamda tarayıcı otomasyonu yok).

## Faz 15 sonrası düzeltme — admin login kilitlenmesi (ADMIN_PASSWORD geç eklendiğinde)

**Teşhis:** `ensure_default_admin()` sadece `users` tablosu TAMAMEN
BOŞKEN çalışıyordu (`if existing: return`). Senaryo: backend
`ADMIN_PASSWORD` `.env`'de tanımlı OLMADAN bir kere açılmış → admin
rastgele bir şifreyle oluşturulmuş (ve bir kez loglanmış, sonra
kaybedilmiş) → kullanıcı sonradan `.env`'e `ADMIN_PASSWORD` eklemiş →
her sonraki açılışta fonksiyon "tablo boş değil" deyip hiçbir şey
yapmadan dönüyordu — `.env`'deki şifre HİÇBİR ZAMAN uygulanmıyordu.
`users` tablosunda `('admin','admin',1)` benzeri bir kayıt varken
"Kullanıcı adı veya şifre hatalı" hatası bu yüzden oluşuyordu.

**Düzeltme (`services/bootstrap.py::ensure_default_admin`):** Artık
HER açılışta çalışıyor, tablo boş olma şartı kaldırıldı:
- `ADMIN_USERNAME` ile eşleşen kullanıcı yoksa → eskisi gibi oluştur.
- Kullanıcı var ve `must_change_password=True` (yani hiç giriş
  yapmamış/kendi şifresini hiç belirlememiş) ve `.env`'de
  `ADMIN_PASSWORD` tanımlıysa → şifre `.env`'deki değere senkronize
  edilir. Bu, "ADMIN_PASSWORD'u sonradan ekledim" senaryosunu
  kurtarıyor.
- Kullanıcı `must_change_password=False` (kendi şifresini gerçekten
  değiştirmiş) ise → `.env` bir daha ASLA dokunmuyor, kullanıcının
  kendi seçtiği şifre her zaman kazanıyor.
- Kullanıcı `must_change_password=True` ama `.env`'de `ADMIN_PASSWORD`
  de yoksa → senkronize edilecek bir şey yok, mevcut (muhtemelen
  rastgele üretilmiş) şifreye dokunulmuyor, çökmüyor.
- Her dal açıkça logluyor: `"Admin oluşturuldu: ..."` /
  `"Admin şifresi .env'den güncellendi: ..."` /
  `"Admin mevcut, şifre değiştirilmiş, dokunulmadı: ..."` / (4.
  durum için) `"Admin mevcut, ilk şifresini henüz değiştirmemiş ve
  .env'de ADMIN_PASSWORD tanımlı değil..."`.

**Yeni CLI script — `backend/scripts/reset_admin_password.py`:**
Kullanıcı adı + yeni şifre alıp hash'leyip güncelliyor;
`must_change_password`'ü otomatik `False`'a çekiyor (script'i
çalıştırabilen zaten sunucuya doğrudan erişimli); `--activate` ile
pasifleştirilmiş bir kullanıcıyı da aynı anda aktifleştirebiliyor —
"kullanıcı kilitli kalırsa" durumunun çıkış yolu. Kullanıcı adı
bulunamazsa kayıtlı kullanıcı adlarını listeleyip anlamlı bir hata ile
çıkıyor (sessizce yeni bir kullanıcı OLUŞTURMUYOR — bu, bir yazım
hatasıyla kazara ikinci bir admin açmayı önlüyor).

**Test:** Yeni `tests/test_admin_bootstrap.py` (5 test, `monkeypatch`
ile `settings.admin_username`/`admin_password` kontrol edilerek):
ADMIN_PASSWORD'suz ilk açılışta rastgele şifre üretiliyor;
ADMIN_PASSWORD'lu ilk açılışta o şifre kullanılıyor; **asıl regresyon
senaryosu** — ADMIN_PASSWORD'suz ilk açılıştan sonra `.env`'e
sonradan eklenen ADMIN_PASSWORD bir sonraki açılışta gerçekten
uygulanıyor; kullanıcı kendi şifresini değiştirdikten sonra `.env`
bir daha asla üzerine yazmıyor; ADMIN_PASSWORD hiç tanımlanmamışsa
mevcut şifreye dokunulmuyor ve çökmüyor. Toplam 59 test yeşil.

**Canlı doğrulama:** Gerçek senaryo elle simüle edildi (ayrı ayrı
Python çağrılarıyla, gerçek SQLite dosyasına karşı): (1) ADMIN_PASSWORD
olmadan ilk açılış → rastgele şifre üretildi ve loglandı; (2)
ADMIN_PASSWORD `.env`'e eklenip yeniden açılış → şifre gerçekten o
değere güncellendi, login artık çalışıyor (`verify_password` ile
doğrulandı); (3) kullanıcının `must_change_password`'ü elle `False`'a
çekilip üçüncü açılış → farklı bir ADMIN_PASSWORD verilmesine rağmen
şifre DEĞİŞMEDİ (self-chosen password kazandı); (4)
`reset_admin_password.py` tam akışı — boş tabloda anlamlı hata,
başarılı sıfırlamada yeni şifre çalışıyor ve `must_change_password`
temizleniyor.

**README:** Yeni "İlk kurulum — kimlik doğrulama" bölümü: zorunlu/
önerilen `.env` değişkenleri tablosu, ilk giriş adımları, "ADMIN_PASSWORD'u
sonradan eklediyseniz" senaryosu, ve `reset_admin_password.py` ile
kurtarma adımları.

## Faz 15 sonrası düzeltme — PgBouncer/Supabase pooler prepared statement hatası (DPA/Activity/Schema)

**Teşhis:** DPA sayfasındaki Activity ve Schema Health panellerinde
`prepared statement "__asyncpg_stmt_21__" already exists` hatası —
asyncpg varsayılan olarak sorguları isimli bir prepared statement
olarak sunucuya önbelleğe alıyor; PgBouncer (Supabase'in pooler'ı
dahil) `transaction`/`statement` pool_mode'da aynı istemci
bağlantısındaki ardışık sorguları farklı gerçek sunucu bağlantılarına
yönlendirebiliyor, bu yüzden bir EXECUTE hiç görmediği bir bağlantıda
çalıştırılmaya çalışılıyor.

**Düzeltme — tüm asyncpg bağlantılarında `statement_cache_size=0`
(koşulsuz, tek bir merkezi fabrika yoktu, tek tek bulunup düzeltildi):**
`collectors/postgresql.py` (collector + activity + schema health, hepsi
aynı `_connect()`'i paylaşıyor), `services/parameter_audit.py`,
`services/index_advisor.py`, `services/explain_service.py`,
`services/custom_alert_rules.py`. Ayrıca `index_advisor.py`'deki hypopg
tahmini (`_hypopg_estimate`) artık `async with conn.transaction():` ile
sarmalanıyor — hypopg'nin hipotetik indeksi sadece onu oluşturan
backend oturumunda yaşıyor, açık bir transaction olmadan havuzlayıcı
"CREATE" ile "EXPLAIN"i farklı backend'lere yönlendirebilirdi.

**SQLAlchemy tarafı (`database.py`):** dbace'in kendi meta veri tabanı
da (Supabase dahil) aynı soruna açık — `_engine_kwargs_for()` artık
`postgresql+asyncpg://` URL'lerinde `connect_args={"statement_cache_size": 0}`
ekliyor (SQLite/aiosqlite yoluna dokunmuyor).

**Pooler tespiti (`collectors/base.py::detect_pooler`/`resolve_uses_pooler`):**
Host adı `pooler`/`pgbouncer` içeriyorsa veya port `6432`/`6543` ise
otomatik pooler kabul ediliyor; Instance/Node `options.uses_pooler`
(sihirbaz + doğrudan instance formunda "Pooler kullanılıyor" seçimi:
Otomatik/Evet/Hayır) her zaman otomatik tespiti geçersiz kılıyor.
Bağlantı testi sonucuna (`ConnectionTestResult.details.pooler_detected`)
da yansıtılıyor, UI'da "(pooler algılandı)" notu olarak görünüyor.

**Hata mesajı:** `classify_connection_error` artık "prepared statement
... already exists/does not exist" metnini tanıyıp anlaşılır Türkçe bir
mesaja çeviriyor (ham asyncpg metni yerine); bu, activity/schema-health/
parametre denetimi/EXPLAIN/index advisor uçlarının hepsinde kullanılıyor
— `advise_indexes` ucunda daha önce hiç try/except yoktu, o da eklendi.

**Test:** Yeni `tests/test_pgbouncer_compat.py` (13 test) — 5 bağlantı
fabrikasının hepsinin `statement_cache_size=0` gönderdiğini (asyncpg.connect
monkeypatch'lenip kwargs yakalanarak), SQLAlchemy motorunun asyncpg
URL'lerinde `connect_args` eklediğini/SQLite'a dokunmadığını, pooler
tespitinin host/port sezgisini ve açık override'ın kazandığını,
`classify_connection_error`'ın prepared-statement mesajını çevirdiğini
kanıtlıyor. Toplam 72 test yeşil.

**README:** Yeni "PgBouncer / connection pooler arkasında çalışma"
bölümü: hatanın sebebi, dbace'in koşulsuz çözümü, ve "Pooler kullanılıyor"
seçeneğinin nasıl çalıştığı.

## Faz 16 — İŞ 1: Ön koşul denetimi

**Sorun:** Yavaş sorgu listesi/EXPLAIN/index önerisi bazen sessizce boş
dönüyordu ve kullanıcı sebebini (uzantı eksik mi, yetki mi yok, ayar mı
kapalı) göremiyordu.

**Yeni `services/prerequisites.py`:** PostgreSQL için 9 kontrol
(pg_stat_statements kurulu mu + `shared_preload_libraries`'de mi +
`.track` ayarı + okuma yetkisi, pg_monitor rolü, hypopg, pg_qualstats,
pg_buffercache, track_io_timing) ve SQL Server için 3 kontrol (VIEW
SERVER STATE yetkisi, gerçek bir DMV sorgusuyla fonksiyonel doğrulama,
Query Store durumu) — her biri tek bir bağlantı üzerinden art arda
çalışıyor, biri başarısız olursa (extension yok → ona bağımlı kontroller)
`unknown` dönüyor, çökmüyor. Her kontrol `{ad, durum (ok|eksik|yetkisiz|
bilinmiyor), önem (high|medium), etki, düzeltme komutu}` taşıyor;
"opsiyonel" olanlar (pg_qualstats, pg_buffercache, hypopg) `medium`,
temel özellikleri tamamen bloke edenler (`pg_stat_statements`,
`shared_preload_libraries`, okuma yetkisi, pg_monitor, VIEW SERVER
STATE, Query Store) `high`.

**Yeni uç: `GET /api/instances/{id}/prerequisites`** — PostgreSQL/SQL
Server dışında 400 döner (MongoDB henüz kapsam dışı). Hata yine de
oluşursa `classify_connection_error` ile Türkçe mesaja çevriliyor.

**Instance detay sayfası:** "Tuning" sekmesinde, mevcut `TuningPanel`'in
hemen üstünde yeni `PrerequisitesPanel` — her satır mevcut
`.checklist-row` görsel dilini kullanıyor (ok/eksik/yetkisiz/bilinmiyor),
eksik olan her kontrolün düzeltme komutu `CopyableAction` ile kopyalanabilir
kutu içinde. Sekme açıldığında bir kez yükleniyor (activity gibi her 10sn
yenilenmiyor — canlı bağlantı açan bir kontrol olduğu için).

**Grup sayfası:** Tam panel yerine her düğüm kartına instance detayının
tuning sekmesine (`?tab=tuning`) giden bir "Ön koşullar" linki eklendi —
tekrar aynı UI'ı inşa etmek yerine tek tıkla var olan panele yönlendiriyor.

**Dashboard uyarısı:** `dashboard_snapshot.py`'ye yeni
`_prerequisite_recommendations` — `parameter_audit` ile AYNI cadence'te
(dashboard-refresh tick, worker/all run_mode + `POST /api/dashboard/refresh`)
grubun hedef düğümü üzerinden bir kez kontrol çalıştırıp `missing`/
`unauthorized` bulunanları mevcut `GroupHealthSnapshot.recommendations_json`
akışına ekliyor — dashboard'un "top_issues"/"recommendations" render
mantığı zaten kaynak-agnostik olduğundan (`source` alanı özel işlenmiyor)
frontend'de EK bir değişiklik gerekmedi, öneri kartı otomatik göründü.
Standalone (grupsuz) instance'lar için bu dashboard entegrasyonu
çalışmıyor — `parameter_audit`/`performance_insights`'ın dashboard
entegrasyonuyla AYNI, önceden var olan sınır (bkz. SORULAR.md).

**Test:** Yeni `tests/test_prerequisites.py` (10 test) —
`FakeAsyncConnection`/`FakeSqlServerConnection` ile her durumun (ok,
eksik uzantı → bağımlı kontroller unknown, izin reddi → unauthorized,
opsiyonel uzantılar → medium severity, Query Store kapalı, VIEW SERVER
STATE eksik) doğru raporlandığı kanıtlanıyor. Toplam 82 test yeşil.

## Faz 16 — İŞ 2: Öneri başlıkları belirgin olsun

**Sorun:** Öneriler metin içinde kayboluyordu — Dashboard'da genel
"Çözüm önerisi" etiketi vardı ama kısa bir eylem başlığı yoktu; DPA'daki
index önerisi kartında hiç başlık/kopyalama yoktu (DDL düz `<code>`
içindeydi, kopyalanamıyordu); parametre denetimi tablosunda öneri düz
metin bir hücreydi; tahminler sayfasında öneri `.muted-note` (soluk,
ikincil metin) sınıfıyla render ediliyordu — yani GERÇEKTEN görsel
olarak bastırılıyordu.

**Yeni paylaşılan bileşen `components/RecommendationHeader.tsx`:**
Tek satır — `Öneri: {title}` başlığı, `.recommendation-title` (kalın,
accent renk) ile. 4 yerde de aynı bileşen kullanılıyor:

- **Dashboard** (`DashboardPage.tsx`): `dashboard_snapshot.py`'nin 4
  öneri üreticisi (`_connectivity_recommendations`,
  `_parameter_recommendations`, `_prerequisite_recommendations`,
  `_instance_recommendations`) artık her biri kısa bir `title` alanı da
  dolduruyor (yeni `DashboardRecommendationOut.title`, opsiyonel — eski
  kayıtlarda yoksa kart eskisi gibi başlıksız düşer). Kart açıldığında
  artık sabit "Çözüm önerisi" yerine `Öneri: {title}` görünüyor, adımlar
  ve komut aynı şekilde altında kalıyor.
- **DPA** (`InstanceDetailPage.tsx`, 2 yer — tekli sorgu + toplu index
  önerisi): index advice kartı artık `Öneri: {schema}.{table} için index
  ekleyin` başlığı + gerekçe (`a.reason`, artık `.recommendation-reason`
  ile okunabilir, soluk değil) + **DDL artık `CopyableAction` ile
  kopyalanabilir** (önceden düz `<code>`, kopyalanamıyordu — gerçek bir
  eksiklik giderildi).
- **Parametre denetimi** (`GroupDetailPage.tsx`): "Öneri" tablo hücresi
  artık `ok` olmayan bulgular için başlık + `SHOW {parametre};` komutunu
  (dashboard'daki aynı güvenli, uydurulmamış kontrol komutu) kopyalanabilir
  gösteriyor.
- **Tahminler** (`PredictionsPage.tsx`): `.muted-note` (soluk metin)
  kaldırıldı, `RecommendationHeader` ile değiştirildi.

**Test:** Yeni `tests/test_dashboard_recommendation_titles.py` (4 test)
— dashboard_snapshot.py'nin 4 öneri üreticisinin hepsinin `title`
doldurduğunu kanıtlıyor (canlı bağlantı gerekmeden, `collect_parameter_audit`/
`run_prerequisite_checks` monkeypatch'lenerek). Toplam 86 test yeşil.

## Faz 16 — İŞ 3: Performans tuning sayfası, kaynak bazlı analiz

**Yeni `services/query_diagnostics.py`:** Her yavaş sorgu için darboğazın
I/O, CPU, bellek veya kilit/bekleme olduğunu, EK bir canlı sorgu
çalıştırmadan — sadece `SlowQuerySample`'ın zaten topladığı sütunlardan
(shared/temp blk sayaçları, exec_user_time/exec_sys_time) türetir:
- **Bellek** — `temp_blks_read/written > 0` (work_mem yetersiz, disk'e
  taştı). Her zaman `observed` (doğrudan ölçülen bir sayaç).
- **I/O** — okunan blokların >%10'u diskten geldi (cache'te değildi) —
  eşik `performance_insights.py`'nin "Disk okuma oranı yüksek" eşiğiyle
  AYNI (iki modülün aynı sinyali farklı yorumlaması tutarsızlık
  yaratmasın diye).
- **CPU** — yürütme süresinin ≥%70'i `exec_user_time+exec_sys_time`'da.
- **Kilit/Bekleme** — CPU ve I/O ile açıklanamayan büyük bir süre farkı
  varsa. **Her zaman `confidence="inferred"`** — dbace sorgu başına kilit
  bekleme SÜRESİ toplamıyor (sadece anlık Activity görüntüsü var,
  geçmişe dönük değil), bu yüzden bu sınıf asla kesin bir teşhis olarak
  sunulmuyor, "Activity sekmesinden kontrol edin" notuyla geliyor.
- **Bilinmiyor** — `exec_user_time`/`exec_sys_time` hiç yoksa (PostgreSQL
  sürümü/pg_stat_statements ayarı desteklemiyor olabilir) — uydurma
  yapmak yerine açıkça "veri yok" deniyor, Ön koşullar paneline (İŞ 1)
  yönlendiriyor.

**Yeni uç: `GET /api/queries/{id}/diagnostics?limit=5|10|20|50`** — en son
toplanan snapshot'tan Top-N sorguyu (toplam süreye göre) sınıflandırıp
döndürür, `by_resource` sayaçları ve bir `server_resource_note` ile.

**Sunucu kaynağı ayrımı — dürüstlük notu:** Görev "CPU/RAM/disk
metrikleri varsa (agent'tan) kaynak mı sorgu mu ayrımı yapılsın, agent
yoksa bunu söyleyip kurulumu önerin" diyordu. dbace'in host-agent
protokolü (`v1/services`, `v1/logs`) bugün CPU/RAM/disk KULLANIMI hiç
toplamıyor — agent yapılandırılmış olsa bile bu ayrım yapılamıyor. Var
olmayan bir özelliği varmış gibi göstermek yerine `server_resource_note`
alanı iki durumu da açıkça söylüyor: agent yoksa "host-agent tanımlayın"
önerisi, agent VARSA "agent yapılandırılmış ama protokol CPU/RAM/disk
toplamıyor, bu yüzden ayrım yapılamıyor" notu (bkz. SORULAR.md).

**Instance detay sayfası:** Tuning sekmesinde, `PrerequisitesPanel`'in
altında yeni `QueryDiagnosticsPanel` — Top N seçici (5/10/20/50),
kaynak türüne göre sekmeler (I/O/CPU/Bellek/Kilit-Bekleme/Bilinmiyor +
sayaçlarla "Toplam etki"), her sorgu satırında gerekçe ve
"inferred" olanlarda "çıkarım (kesin ölçüm değil)" notu.

**Test:** Yeni `tests/test_query_diagnostics.py` (6 test) — her dal için
(temp file → memory, yüksek disk okuma oranı → io, CPU baskın → cpu,
açıklanamayan süre farkı → lock+inferred, exec_time yok → unknown+inferred)
doğru sınıflandırıldığı kanıtlanıyor. Toplam 92 test yeşil.

## Faz 16 — İŞ 4: Index önerisi neden gelmediğini açıkla

**Sorun:** `advise()` önerecek bir şey bulamadığında sessizce `[]`
döndürüyordu, frontend de bunu düz "Index önerisi bulunamadı." metniyle
gösteriyordu — sebep yoktu.

**`index_advisor.py`'nin `advise()` imzası değişti:** artık
`(list[IndexAdvice], list[NoAdviceReason])` döndürüyor. Her
`NoAdviceReason` `{code, message, what_to_do}` taşıyor — "öneri yok"
yerine "şu yüzden öneremiyorum, şunu yaparsan önerebilirim". Tespit
edilen sebepler:

- **`no_query_data`** — sorgudan hiç tablo/FROM çıkarılamadı.
- **`no_filter_columns`** — tablo bulundu ama WHERE/JOIN/ORDER BY/GROUP
  BY'da filtrelenen bir kolon yok (sorgu zaten filtresiz olabilir, ya da
  ayrıştırıcı tanımadı).
- **`table_not_found`** — tablo bağlı veritabanında/şemada yok.
- **`already_indexed`** — **"sorgu zaten index kullanıyor, sorun başka"**
  durumu: bu kolonları zaten kapsayan bir index var, `what_to_do` EXPLAIN
  ANALYZE'a yönlendiriyor (sıralama/join/veri hacmi olabilir).
- **`insufficient_samples`** — **"yeterli örnek birikmemiş"**: çağıran
  `calls` sayısını verirse (artık `IndexAdviceRequest.calls`, frontend
  `SlowQuery.calls`'ı gönderiyor) ve bu < `MIN_SAMPLE_CALLS` (5) ise, kaç
  çağrı olduğu ve minimum kaçının önerildiği mesajda açık açık yazıyor.

**"yetki yetersiz" ayrı bir NoAdviceReason DEĞİL — gerçek bir hata:**
Katalog sorgularında (`pg_stats`/`pg_indexes`/`pg_class`) bir izin hatası
olursa bu zaten bir exception olarak fırlıyor ve router 502 döndürüyor —
sessizce boş sonuç DEĞİL. `classify_connection_error`'a yeni bir "permission
denied" dalı eklendi (GRANT pg_monitor / VIEW SERVER STATE önerisiyle) —
bu, index advisor dışında activity/schema-health/parametre denetimi gibi
diğer tüm uçlarda da aynı anlamlı mesajı veriyor.

**"pg_stat_statements yok/veri yok" ve "pg_qualstats yok" NEDEN
NoAdviceReason DEĞİL:** bkz. SORULAR.md — ikisi de index_advisor'ı
gerçekte BLOKE etmiyor (birincisi zaten yavaş sorgu listesinin kendisini
boşaltır, oraya hiç gelinmez — Ön koşullar paneli İŞ 1'de zaten var;
ikincisi dbace tarafından hiç kullanılmıyor), bu yüzden fabrikasyon
yapılmadı.

**Frontend:** `advice` state artık `IndexAdviceReport` (`{advice,
no_advice_reasons}`) tutuyor; yeni `NoAdviceReasons` bileşeni "Öneri yok"
etiketiyle her sebebi + "ne yapmalı"yı `.checklist-row` görsel diliyle
(İŞ 1/3 ile aynı) gösteriyor.

**Test:** Yeni `tests/test_index_advisor_reasons.py` (7 test) — her
sebep dalı + başarılı öneri durumunda `reasons=[]` olduğu kanıtlanıyor.
`test_pgbouncer_compat.py`'ye yeni bir test (permission-denied çevirisi).
Toplam 100 test yeşil.

## Faz 16 — İŞ 5: Akış ve kullanılabilirlik

**Tek tıkla ilerleme — dashboard önerileri artık isabetli hedefe iniyor:**
`dashboard_snapshot.py`'nin 3 öneri üreticisi (`_parameter_recommendations`,
`_prerequisite_recommendations`, `_instance_recommendations`) artık kendi
`link_hint`'ini taşıyor; `dashboard.py`'nin enrichment'ı bunu koruyor
(`rec.get("link_hint") or f"/groups/{group_id}"` — sadece daha isabetli
bir hedefi olmayan kaynaklar grup sayfasına düşüyor):
- Parametre denetimi → `/groups/{id}?tab=parameters` (grubun kendi
  Parametreler sekmesi — bu yüzden GroupDetailPage'e de InstanceDetailPage'de
  zaten var olan `?tab=` URL desteği eklendi, önceden GroupDetailPage
  bunu okumuyordu).
- Ön koşul eksikliği (İŞ 1) → doğrudan `/instances/{id}?tab=tuning`.
- Performans içgörüsü (İŞ 2'nin `_instance_recommendations`'ı) → içgörünün
  kendi `insight.action`'ı geçerli bir sekme adıysa (`queries`/`metrics`/
  `alerts` — zaten TuningPanel'in "İlgili sekmeye git" butonunun kullandığı
  AYNI değer) doğrudan o sekmeye, yoksa Tuning'e düşer.

**Geri dönüş yolu:** Instance detay sayfasının üstündeki "← Dashboard"
linki artık instance bir gruba bağlıysa "← Grup" oluyor (`instance.group_id`
— backend'de zaten vardı ama frontend `Instance` tipinde eksikti, eklendi)
— dashboard → grup → instance → geri grup → geri dashboard zinciri artık
her adımda tutarlı.

**Karmaşık teknik çıktı katlandı:** Parametre denetimi sekmesindeki
Patroni `/config` ham JSON çıktısı artık `<details>` içinde — sayfa
ilk açıldığında sade kalıyor, isteyen genişletebiliyor.

**Terminoloji taraması:** "Çözüm önerisi"/"tavsiye"/"gereksinim" gibi
eşanlamlı terimlerin karışık kullanılmadığı doğrulandı — İŞ 2'nin
`RecommendationHeader`'ı ("Öneri: X") tüm sayfalarda tek terim; eski
"Çözüm önerisi" etiketi hiçbir yerde kalmadı.

**Test:** `test_dashboard_recommendation_titles.py`'ye her 3 üreticinin
doğru `link_hint`'i ürettiğini kanıtlayan assertion'lar eklendi;
`test_dashboard_issue_enrichment.py`'ye yeni bir test — bir önerinin
kendi `link_hint`'i varsa grup varsayılanının onu EZMEDİĞİNİ kanıtlıyor.
Toplam 101 test yeşil.

## Faz 16 — İŞ 6: Tahmin (predictions) için veri gereksinimleri

**Yeni tahmin motoru `services/forecasting.py`:** Sadece Python stdlib
(`statistics`, `math`) — makine öğrenmesi kütüphanesi veya LLM
KULLANILMADI:
- `linear_regression_with_ci` — en küçük kareler doğrusal regresyonu +
  kalıntı (residual) varyansından türetilen klasik bir %90 tahmin
  aralığı formülü (istatistik ders kitabı formülü, uydurma bir sayı
  değil).
- `deseasonalize`/`forecast_with_seasonality` — additive mevsimsel
  ayrıştırma: "haftaiçi/haftasonu" (günlük rollup'lara uygulanıyor) veya
  "saat bazlı" (ham örneklere uygulanıyor) — en az 8 nokta VE en az 2
  farklı mevsim bucket'ı yoksa mevsimsellik uygulanmıyor (yetersiz
  veriyle "mevsimsel desen" uydurulmuyor, düz regresyona düşüyor).
- `PREDICTION_REQUIREMENTS`/`check_sufficiency` — 5 tahmin türünün her
  biri için minimum gün/örnek sayısı + "kaç gün daha gerekli" hesabı.

**Yeni günlük rollup — `services/rollup.py` + `MetricRollupDaily`/
`SchemaObjectDailySample` tabloları:** Saklama süresi 1 ay olduğundan
(retention.py ham `MetricSample` satırlarını siler) uzun vadeli
tahminler için DÜNÜN verisi her gün (scheduler'a yeni `daily_rollup_tick`,
retention ile aynı `days=1` cadence) tek satıra özetleniyor:
`database_size_bytes`/`active_connections`/`connection_utilization_pct`/
`transaction_id_age` metrikleri (avg/min/max/last/sample_count) VE
(sadece PostgreSQL) günde bir kez `collect_table_sizes`/
`collect_schema_health` ile tablo/index boyutlarının anlık görüntüsü.
**Bu rollup'lar retention temizliğinden MUAF** — ham örneklerden çok
daha küçük hacimli, aylar/yıllar boyunca birikmesi sorun değil (bkz.
SORULAR.md).

**Yeni metrik: `transaction_id_age`** — PostgreSQL collector'a tek bir
ucuz katalog sorgusu eklendi (`age(datfrozenxid)`), her 15s'lik toplama
döngüsünde. Wraparound riski tahmininin temeli.

**Yeni collector metodu: `collect_table_sizes`** — `collect_schema_health`'in
`bloated_tables`'ından KASITLI olarak ayrı: o liste sadece ZATEN bloat
eşiğini geçmiş tabloları içeriyor, sağlıklı ama hızlı büyüyen bir tabloyu
kaçırırdı. Bu yeni metod TÜM tabloları boyuta göre sıralıyor.

**5 tahmin türü** (`services/prediction.py`), hepsi
`check_sufficiency` ile yetersiz veride SESSİZCE HİÇBİR ŞEY ÜRETMİYOR
(uydurma yok):
1. **Disk/veritabanı boyutu dolma tarihi** — günlük rollup, haftaiçi/
   haftasonu mevsimselliği, ≥7 gün gerektiriyor. Artık ham örneklerin
   son birkaç saatine değil, günlük trende dayanıyor (önceki halinden
   daha sağlam).
2. **Bağlantı sayısı trendi** — ham örnekler + saat-bazlı mevsimsellik,
   ≥12 saat (mevcut kısa-vadeli eşik-ihlali tahmininin YERİNE geçmedi,
   onu güven aralığı + mevsimsellikle güçlendirdi).
3. **Tablo büyüme hızı** — `SchemaObjectDailySample` (object_kind=table),
   en hızlı büyüyen ilk 5 tablo, ≥7 gün.
4. **Transaction ID wraparound riski** — günlük rollup, PostgreSQL'in
   kendi sabit eşiği `autovacuum_freeze_max_age` (200M) baz alınıyor.
   VACUUM FREEZE'den kaynaklı ani bir düşüş (yaşın yarıya inmesi)
   tespit edilirse trend "henüz yeniden kurulmadı" sayılıp tahmin
   ATLANIYOR — reset sonrası eski trendle uydurma yapılmıyor.
5. **Index şişmesi** — `SchemaObjectDailySample` (object_kind=index),
   sadece KULLANILMAYAN indexler (dbace'de genel index bloat tahmini
   için "tüm indexlerin boyutu" toplayan bir sorgu yok — bkz. SORULAR.md
   kapsam kararı), en hızlı büyüyen ilk 5, ≥7 gün.

**Yeni uç: `GET /api/instances/{id}/prediction-readiness`** — 5 türün
(PostgreSQL değilse sadece disk + bağlantı) hepsi için `{kaç gün/örnek
gerekli, şu an ne kadar var, hazır mı, kaç gün kaldı}` — tahmin
üretilmiş olsun olmasın HER ZAMAN dönüyor, "neden tahmin yok" sorusu
asla cevapsız kalmıyor.

**Güven aralığı:** `PredictionInsight`'a `lower_bound`/`upper_bound`/
`seasonality` kolonları eklendi — her tahmin artık %90 tahmin aralığıyla
birlikte geliyor. Instance detay sayfasının Tahminler sekmesinde yeni
`PredictionReadinessPanel` (veri yeterliliği listesi) + tablo artık
aralığı ve mevsimsellik türünü gösteriyor; global Tahminler sayfasına da
aynı aralık bilgisi eklendi.

**Test:** Yeni `tests/test_forecasting.py` (7 test — regresyon doğruluğu,
CI genişlemesi, mevsimsellik tespiti/reddi, yeterlilik hesabı),
`tests/test_rollup.py` (2 test — özetleme + idempotency),
`tests/test_prediction_capacity.py` (8 test — wraparound/tablo/index
tahminleri + reset tespiti + readiness endpoint). `test_prediction.py`
rollup-tabanlı yeni mimariye güncellendi (+1 yeni "yetersiz veri" testi).
Toplam 119 test yeşil.

**Yeni Supabase migration:** `20260903090000_prediction_forecasting.sql`
— DEPLOY.md'nin migration tablosuna 20. sıra olarak eklendi (bu oturumun
kendi DEPLOY.md denetiminden çıkarılan derse sadık kalınarak: yeni
kolon/tablo eklenen HER değişiklik için migration dosyası + DEPLOY.md
güncellemesi birlikte yapıldı).

## Faz 16-B — İŞ 1: Çelişkili pg_stat_statements raporu düzeltildi

**Bildirilen sorun:** Ön koşullar paneli `pg_stat_statements`'ı "var"
gösterirken DPA'daki "Yavaş sorgu detayları" bölümü "Yavaş sorgu verisi
yok. Eklentiyi aktif edin: `CREATE EXTENSION pg_stat_statements`"
diyordu. İki taraf birbirini yalanlıyordu.

**Kök neden (üç ayrı hata birden):**

1. **Panel yanlış kontrol yapıyordu.** Ön koşul kontrolü sadece
   `pg_extension` kataloğuna bakıyordu (`extname = 'pg_stat_statements'`
   var mı). Katalogda kayıtlı olmak view'ın OKUNABİLİR olduğu anlamına
   gelmiyor: Supabase/RDS gibi yönetilen servisler eklentiyi `extensions`
   şemasına kurar; bağlanan rolün `search_path`'inde o şema yoksa
   `SELECT ... FROM pg_stat_statements` "relation does not exist" verir.
   Panel yeşil, veri yok.
2. **Collector de aynı tuzağa düşüyordu.** `collect_slow_queries()`
   view'ı çıplak adıyla (`FROM pg_stat_statements`) sorguluyordu — yani
   eklenti search_path dışındaysa toplama gerçekten çalışmıyordu. Artık
   şema `pg_extension`/`pg_namespace` join'iyle çözülüp view
   `"<şema>".pg_stat_statements` olarak niteleniyor. **Bu, semptomun
   asıl düzeltmesi:** eklenti nerede kurulu olursa olsun veri toplanıyor.
3. **Mesajlar iki ayrı yerden geliyordu.** DPA'nın boş-liste metni
   frontend'de sabit yazılmıştı ve ön koşul sonucundan haberi yoktu.

**Yeni `services/pgss.py` — tek probe.** `probe_pg_stat_statements(conn)`
tek bağlantı üzerinden şu alanları çıkarır: `installed`, `schema`,
`reachable`, `read_error`/`read_error_code`, `preloaded`, `track`,
`privileged`, `total_rows`, `redacted_rows`. Hem ön koşul paneli hem
"veri neden yok" mesajı artık AYNI probe nesnesinden türüyor — ikisinin
çelişmesi yapısal olarak imkânsız.

**Yeni ön koşul durumu `partial`.** Yönetilen servislerde en sık görülen
durum artık ayrı raporlanıyor: eklenti kurulu, okunabiliyor, AMA rol
superuser/`pg_read_all_stats` üyesi olmadığı için başka kullanıcıların
sorgu metni `<insufficient privilege>` olarak maskeli geliyor. Yeni
kontrol `pg_stat_statements_visibility` bunu "Kurulu ama sadece kendi
sorgularınızı görebiliyorsunuz" diye söylüyor, düzeltme komutuyla
(`GRANT pg_read_all_stats`). Panelde sarı (uyarı) olarak görünüyor —
yeşil göstermek yanıltıcı, kırmızı göstermek yanlış olurdu.

**Yeni `services/slow_query_status.py` + `GET /api/queries/{id}/availability`.**
"Yavaş sorgu verisi neden yok" sorusunun tek cevabı; 11 ayrı durum ayırt
ediyor: `ok`, `extension_missing`, `extension_unreachable`,
`unauthorized`, `not_preloaded`, `track_off`, `restricted_visibility`,
`no_data_yet`, `not_collected_yet`, `probe_failed`, `engine_unsupported`.
`CREATE EXTENSION` önerisi artık YALNIZCA eklenti gerçekten kurulu
değilken çıkıyor; eklenti varken mesaj "veri henüz birikmedi" ya da
"worker henüz kaydetmedi" diyor.

**Frontend:** yeni `SlowQueryAvailabilityNote` bileşeni her iki boş-liste
yerinde de (yavaş sorgu dağılımı grafiği + yavaş sorgu detayları) aynı
notu gösteriyor; komut kutusu kopyalanabilir. Probe canlı bağlantı
gerektirdiği için 15 sn'lik yenileme döngüsüne DEĞİL, sadece sekme
açıldığında ve liste boşken yükleniyor.

**Worker gerçekten topluyor mu — doğrulama.** Toplama akışı okundu:
`services/collection.py` metrikleri ve yavaş sorguları aynı döngüde, tek
bağlantı üzerinden alıyor; `collect_slow_queries()` bir istisna atarsa
tüm döngü hata veriyor (instance "error" durumuna düşer), yani "sessizce
kaydetmiyor" senaryosu yalnızca iki yolla oluşabiliyordu ve ikisi de
düzeltildi:

- View search_path dışındaysa sorgu hiç çalışmıyordu → şema nitelemesiyle
  çözüldü.
- Maskelenmiş satırlar (`query = '<insufficient privilege>'`, `queryid`
  NULL) saklanmaya çalışılıyordu; `SlowQuerySample.query` NOT NULL
  olduğundan metni NULL gelen satırlar kaydı bozabilirdi. Artık bu
  satırlar toplamadan çıkarılıyor ve eksikliğin sebebi görünürlük
  kontrolünde açıklanıyor.

**Diğer çelişkili metinler ayıklandı:** `performance_insights.py`'nin
"pg_stat_statements verisi henüz gelmemiş olabilir / Extension'ın yüklü
olduğunu doğrulayın" tahmini kaldırıldı — o fonksiyonun canlı bağlantısı
yok, sebebi tahmin etmesi yanlıştı; artık kullanıcıyı tek kaynağa
yönlendiriyor. Dashboard önerileri de `partial` durumunu artık eyleme
dönük bir eksiklik olarak gösteriyor.

**Testler:** `tests/test_slow_query_status.py` (13 test) — "eklenti
varken CREATE EXTENSION denmiyor", "kısıtlı görünürlük ayrı raporlanıyor",
"panel ve DPA mesajı asla çelişmiyor" (parametrik), "probe/collector
view'ı şemayla niteliyor", "maskeli satırlar saklanmıyor".
`tests/test_prerequisites.py`'ye 2 yeni test (+search_path dışı eklenti,
+partial görünürlük). Toplam: 134 test yeşil.

## Faz 16-B — İŞ 2: Instance yönetimi eksikleri

**Silme neden çalışmıyordu.** `DELETE /api/instances/{id}` düz
`db.delete(instance)` çağırıyordu. Instance'a işaret eden 7 tablo
(`metric_samples`, `slow_query_samples`, `alert_rules`, `alert_events`,
`prediction_insights`, `metric_rollup_daily`,
`schema_object_daily_samples`) ve `nodes.instance_id` foreign key kısıtı
var — hiç kullanılmamış görünen bir instance'ta bile (ör. birkaç metrik
örneği toplanmışsa) silme veritabanı hatasıyla düşüyordu.

**Yeni akış:**

1. `GET /api/instances/{id}/dependencies` — bağlı kayıtların sayımı +
   bağlı düğüm listesi.
2. `DELETE /api/instances/{id}` — bağlı kayıt yoksa siler; varsa **409**
   döner ve neyin bağlı olduğunu sayılarıyla söyler.
3. `DELETE /api/instances/{id}?cascade=true` — bağlı kayıtları da siler.

Düğümler (Node) cascade'de **silinmiyor**, sadece `instance_id = NULL`
yapılıyor: düğüm cluster topolojisinin parçası, veritabanı kaydının
değil — silmek topolojiyi bozardı, sonradan yeniden bağlanabilir.

Frontend'de "Instance ve tüm metrikleri silinsin mi?" tarzı bir
`confirm()` yerine artık bir onay paneli var: hangi kayıttan kaç tane
silineceğini ve hangi düğümlerin bağlantısının kopacağını listeliyor.

**Bağlantı testi düzenleme sırasında hep başarısızdı.** Düzenleme formu
var olan şifreyi (haklı olarak) göstermiyor, ama "Bağlantı testi" butonu
formu olduğu gibi `POST /api/instances/test`'e gönderiyordu — yani boş
şifreyle. Yeni `POST /api/instances/{id}/test-config`: gönderilmeyen
alanlar kayıtlı değerlerden tamamlanıyor (şifre boşsa saklanan şifre,
doluysa yeni şifre denenir), hiçbir şey kaydedilmiyor. Form, var olan bir
instance düzenlenirken bu ucu kullanıyor.

**Düzenleme ekranı bulunabilir hale geldi.** Tam düzenleme formu
(host, port, veritabanı, kullanıcı, şifre, SSL modu, pooler ayarı,
Patroni/etcd/HAProxy portları, agent URL/token, toplama aralığı) zaten
Instances sayfasında vardı ama kullanıcılar oraya ulaşamıyordu. Artık iki
yeni giriş noktası var, ikisi de `?edit=<id>` ile formu doğrudan açıyor:

- Instance detay sayfası başlığında "Bağlantı ayarlarını düzenle".
- Grup detayındaki düğüm kartında, instance bağlıysa aynı bağlantı.

**Sunucu (Server) düzenleme tamamlandı.** Satır içi düzenleme formuna
`agent_url` ve `agent_token` eklendi — daha önce yalnızca sihirbazda
girilebiliyor, sonradan değiştirmek için sunucuyu silmek gerekiyordu.
Tabloya bir "Agent" sütunu da eklendi.

**Düğüm (Node) düzenleme** grup detayında zaten vardı (ad, SQL Server
instance adı, port, rol, sunucu) — bu iş kapsamında ona dokunulmadı,
üzerine instance bağlantı ayarlarına giden yol eklendi.

**Testler:** `tests/test_instance_delete.py` (5 test) — bağlantısız
instance temiz siliniyor, bağlı kayıt varken 409 + reddedilen silme
yarım iş bırakmıyor, cascade siliyor, cascade düğümü silmeyip
bağlantısını koparıyor, test-config şifreyi kayıtlıdan tamamlıyor /
verilen şifreyi tercih ediyor. Toplam: 139 test yeşil.

## Faz 16-B — İŞ 3: DPA metrik grafikleri etkileşimli

**Sürükleyerek aralık seçme + yakınlaştırma.** Yeni
`components/useChartRangeSelection.tsx` hook'u Recharts grafiklerine
mouse-down/move/up ile aralık seçimi kazandırıyor: sürükleme sırasında
seçilen bölge `ReferenceArea` ile gölgeleniyor, bırakıldığında aralık
kesinleşiyor. Sürükleme değil de tek tıklama yapılırsa (başlangıç ==
bitiş) eski "o anı seç" davranışı korunuyor — grafik–sorgu
ilişkilendirmesi (Faz 15 İŞ 8) bu davranışa dayanıyordu.

Metrikler sekmesindeki 9 grafiğin hepsi aynı `chartData`'yı paylaştığı
için seçim hepsinde birden geçerli: bir grafikte 14:10–14:25 arası
seçildiğinde bağlantılar, cache hit, I/O, tuple, checkpoint — hepsi aynı
pencereye yakınlaştırılıyor. Üstte "Seçili aralık: … · N örnek" çubuğu ve
"Yakınlaştırmayı sıfırla" butonu çıkıyor.

**Zaman aralığı seçici genişletildi.** Hazır 1 saat / 6 saat / 24 saat /
7 gün butonlarının yanına **Özel** eklendi: başlangıç/bitiş
`datetime-local` girdileriyle kesin bir pencere seçiliyor. Backend
tarafında `GET /api/metrics/{id}` artık `start`/`end` (ISO-8601)
parametrelerini kabul ediyor; verilmezse eski `hours` davranışı aynen
korunuyor. Özel aralık aktifken 15 saniyelik otomatik yenileme
metrikleri değiştirmiyor (sabit bir pencereye bakılıyor).

**Etiket belirsizliği düzeltildi.** Grafik X ekseni kategorik ve etiket
`HH:MM` idi — 7 günlük aralıkta "14:29" yedi kez tekrar ediyor ve iki
farklı an aynı noktaya düşüyordu. 24 saatten uzun pencerelerde etiket
artık `DD.MM HH:MM`.

**Aralık seçimi sorgu listesini gerçekten filtreliyor.** "Sorgu yükü
zaman çizelgesi" grafiğinde de sürükleyerek aralık seçilebiliyor; altta
listelenen sorgular artık tek bir zaman noktası yerine seçilen aralığın
TAMAMINDAN toplanıyor. Aynı queryid aralıkta birden çok noktada
görünüyorsa: `total_time_ms`/`calls` kümülatif sayaç olduğu için en
yüksek değer alınıyor, `calls_delta` toplanıyor, ortalama süre aralık
ortalaması olarak hesaplanıyor. Başlık da buna göre değişiyor
("14:10 – 14:25 aralığında öne çıkan sorgular").

**Testler:** `tests/test_metrics_custom_range.py` (3 test) — özel aralık
sadece penceredeki örnekleri döndürüyor, `hours` davranışı bozulmamış,
`start` tek başına açık uçlu çalışıyor. Toplam: 142 test yeşil.

## Faz 16-B — İŞ 4: Yavaş sorgu listesi mantığı

**Sorun.** Sorgular sekmesinde iki ayrı liste vardı ve varsayılan olan
tek bir zaman noktasına bağlıydı ("14:29 civarında öne çıkan sorgular").
O liste sorgu geçmişinden geliyordu, güncel yavaş sorgu listesiyle
kesişmediğinde de "bu sorgu artık güncel yavaş sorgu listesinde değil"
diyordu — yani kullanıcıya önce sorunlu olmayan bir sorgu gösterilip
sonra üzerinde işlem yapılamayacağı söyleniyordu.

**Tek liste, "en sorunlu N" mantığı.** Artık tek bir liste var: *En
sorunlu sorgular*. Ayrı "an'a bağlı" liste kaldırıldı; sorgu yükü
çizelgesi duruyor ama artık kendi listesini değil bu listeyi besliyor.

`GET /api/queries/{id}` genişletildi:

- `limit` (UI'da 5/10/20 seçici) ve `sort` (`total`/`mean`/`calls`).
- `start`/`end` — aralık modu.

**Aralık modunda sıralama kümülatif toplama değil DEĞİŞİME göre.**
pg_stat_statements sayaçları sıfırlanana kadar birikir; "14:00–14:30
arasında en çok süre harcayan sorgu" sorusunun cevabı son değerin
kendisi değil, pencerenin başı ile sonu arasındaki farktır. Aksi halde
haftalardır biriken dev bir sayaç, pencerede hiç çalışmasa bile listenin
başında kalırdı — kullanıcının şikâyet ettiği "sorunlu olmayan sorgular"
tam olarak buydu. Sayaç pencerede sıfırlanmışsa (son < ilk) son değer
olduğu gibi alınıyor. Pencerede toplam 1 ms'den az iş yapmış sorgular
listeye hiç girmiyor.

**Aralık seçimi tek yerden yönetiliyor.** Liste şu önceliğe göre bir
aralığa bağlanıyor: sorgu yükü çizelgesinde seçilen aralık → metrik
grafiklerinde yakınlaştırılan aralık → üstteki "Özel" aralık. Hiçbiri
yoksa liste en son toplama döngüsünün anlık görüntüsü. Liste başlığının
altında hangi modda olduğu ve neye göre sıralandığı açıkça yazıyor.
Sabit bir aralığa bakılırken 15 saniyelik otomatik yenileme durdurulmuş
(sonuç değişmeyeceği için).

**"Olası nedenler" ayıklandı.** Eski hali `+1 çağrı arttı` gibi önemsiz
gözlemleri ve hiçbir sinyal yokken bile bir dolgu cümlesini öneri gibi
sunuyordu. Yeni kurallar sadece eşiği geçen gerçek sinyalleri yazıyor:
ortalama süre > 100 ms, `temp_blks_written > 0` (work_mem taşması),
diskten okunan blok sayısı cache isabetinden fazla (I/O ağırlıklı).
Hiçbiri yoksa "Olası nedenler" başlığı hiç görünmüyor.

**Kaldırılan kafa karıştırıcı metinler:** "bu sorgu artık güncel yavaş
sorgu listesinde değil", "Sorgu seçin", "Bu zaman noktasında
ilişkilendirilecek sorgu verisi yok". Listedeki her sorgu artık gerçek
bir `SlowQuerySample` satırına karşılık geliyor, dolayısıyla
EXPLAIN/index önerisi her zaman çalışıyor.

**Testler:** `tests/test_slow_query_ranking.py` (5 test) — varsayılan
görünüm istenen ölçüte göre Top N, aralık modu pencere içi değişime göre
sıralıyor (kümülatif dev sorgu listeye girmiyor), sayaç sıfırlaması
negatif fark üretmiyor, önemsiz sorgular eleniyor, geçersiz `sort` 422.
Toplam: 147 test yeşil.

## Faz 16-B — İŞ 5: Schema Health

**"DROP INDEX komutu yarım üretiliyor" — aslında kırpılıyordu.** Komut
backend'de baştan beri tamdı (`DROP INDEX CONCURRENTLY IF EXISTS
"şema"."index";`). Sorun arayüzdeydi: komut `.ddl-code` sınıfıyla tek
satırlık bir `<code>` içinde gösteriliyordu ve o sınıf
`max-width: 360px; white-space: nowrap; text-overflow: ellipsis`
taşıdığı için uzun komutlar "…" ile kesiliyordu — kullanıcı kestiği
yerden kopyalayınca çalışmayan bir komut elde ediyordu. Artık üç tablo da
DPA'daki `CopyableAction` bileşenini kullanıyor: komut tam metin
görünüyor ve kopyala butonu var.

**Bloat ve vacuum satırlarına da komut eklendi.** Daha önce sadece
kullanılmayan indexlerde komut vardı; "şu tablo şişmiş" deyip ne
yapılacağını söylememek eksikti.

- Bloat riski: `VACUUM (ANALYZE) "şema"."tablo";`. `VACUUM FULL` bilerek
  çalıştırılabilir komut olarak sunulmuyor — tabloyu ACCESS EXCLUSIVE
  kilitler ve tablo boyutu kadar geçici disk ister; ölü satırları
  temizlemek için normal VACUUM yeterli. Diski gerçekten geri vermek
  gerekiyorsa yorum satırı olarak, uyarısıyla birlikte duruyor.
- Vacuum lag: `freeze_age > 100M` ise `VACUUM (FREEZE, ANALYZE)`
  (asıl mesele wraparound), değilse `VACUUM (ANALYZE)`.

**Önem derecesi filtresi.** Üç listeye birden uygulanan çoklu seçim
(checkbox) filtresi eklendi: **Kritik / Uyarı / Bilgi** (backend'deki
`critical` / `high` / `medium` karşılıkları). Varsayılan olarak üçü de
açık — filtre bir daraltma aracı, veriyi gizleyerek başlamamalı. Kaç
kaydın filtrelendiği başlıkta yazıyor, filtre yüzünden boşalan bir liste
"kayıt yok" yerine "seçili önem derecelerinde kayıt yok (filtreyi
genişletin)" diyor.

Filtrenin üç listede de çalışabilmesi için kullanılmayan indexlere de bir
önem derecesi verildi; ölçüt boşa harcanan disk (≥1 GB kritik, ≥100 MB
uyarı, altı bilgi).

**Testler:** `tests/test_schema_health_commands.py` (6 test) — DROP
komutu tam ve şema nitelikli, kullanılmayan index önem derecesi
alıyor, bloat komutu düz VACUUM (FULL sadece yorum), yüksek freeze_age'de
FREEZE, düşükte düz VACUUM, ve üç listedeki her komut şema adını içeriyor
+ noktalı virgülle bitiyor. Toplam: 153 test yeşil.

## Faz 16-B — İŞ 6: Ön koşul kontrollerini yoksayma

**Sorun.** Ortamda hiç kullanılmayacak bir uzantı (ör. `pg_buffercache`,
`pg_qualstats`) yüzünden ön koşul listesi sonsuza kadar kırmızı kalıyor
ve dashboard aynı uyarıyı tekrarlayıp duruyordu. Kullanıcı "bunu
kurmayacağım" diyemiyordu.

**Kalıcı, instance bazında yoksayma.** `Instance.ignored_prerequisites`
(JSON) kolonu eklendi + Supabase migration
(`20260904090000_instance_ignored_prerequisites.sql`, DEPLOY.md tablosuna
21 numaralı satır olarak işlendi). Yeni uç:

```
PUT /api/instances/{id}/prerequisites/ignored   {"keys": [...]}
```

Tam liste gönderiliyor (idempotent) — yoksaymak da geri almak da aynı
uçtan yapılıyor; anahtarlar tekilleştirilip sıralı saklanıyor.

**Yoksaymak kontrolü yeşile boyamıyor.** Kontrolün `status` alanı gerçek
sonucu göstermeye devam ediyor; sadece `ignored: true` işaretleniyor ve
sayımın dışında kalıyor. Rapor artık `ignored_count` ve `completion_pct`
de döndürüyor: yüzde yalnızca yoksayılmayan kontroller üzerinden
hesaplanıyor, yani kalan zorunlu kontroller tamamlandığında **%100**
görünüyor. Hepsi yoksayılmışsa yüzde %100 (kullanıcı bilinçli olarak
"burada denetlenecek bir şey yok" demiş oluyor).

**Dashboard uyarıları da susuyor.** `_prerequisite_recommendations`
yoksayılan anahtarları atlıyor.

**Etkilenen özellik nedenini söylüyor.** Yavaş sorgu kullanılabilirlik
notu (İŞ 1'de eklenen tek kaynak) artık durum → ön koşul eşlemesi
tutuyor: `extension_missing`/`unauthorized` → `pg_stat_statements`,
`not_preloaded` → `shared_preload_libraries`, `track_off` →
`pg_stat_statements_track`, `restricted_visibility` →
`pg_stat_statements_visibility`. İlgili kontrol yoksayılmışsa mesaj
"Bu özellik çalışmıyor çünkü '<kontrol>' ön koşulu eksik ve siz bu
kontrolü yoksaydınız" diye başlıyor ve özgün sebebi de taşıyor.

**Arayüz.** Her kontrol satırında "Yoksay" (yoksayılmışsa "Geri al")
butonu — sadece admin için. Yoksayılanlar listenin altında ayrı bir
**Yoksayılan kontroller (N)** bölümünde, soluk gösteriliyor; gerçek
durumları ve "bu kontrol yoksayıldı, etkilediği özellikler çalışmamaya
devam eder" notu görünüyor. Başlıkta `X/Y kontrol tamam (%Z) · N
yoksayıldı` yazıyor.

**Testler:** `tests/test_prerequisite_ignore.py` (7 test) — yoksayılan
kontrol yüzdeden düşüyor ama durumunu koruyor, yüzde hesabı, hepsi
yoksayılınca %100, `partial` durumu yoksayılmadıkça sorun sayılıyor,
durum→kontrol eşlemesi gerçek anahtarlara denk geliyor, liste kalıcı ve
geri alınabilir, yoksayılan kontrol dashboard önerisi üretmiyor.
Toplam: 160 test yeşil.

## Faz 16-B — İŞ 7: Tahminler için adım adım aksiyon planı

**Sorun.** Tahminlerin önerisi tek cümlelik ve genel geçerdi
("arşivleme/partitioning değerlendirin", "bağlantı havuzunu gözden
geçirin"). Kullanıcı ne çalıştıracağını bilmiyordu.

**Yeni `services/prediction_playbooks.py`.** Beş tahmin türü için
numaralı adımlar; her adım `{title, detail, command}`. Plan tahmin
kaydedildiği anda üretilip `PredictionInsight.playbook` (JSON) kolonunda
saklanıyor — böylece geçmiş tahminler kendi planlarını taşıyor.
Supabase migration: `20260904100000_prediction_playbook.sql`
(DEPLOY.md tablosunda 22 numara).

**Disk/veritabanı dolma (6 adım):** en çok yer kaplayan tabloları çıkaran
sorgu → ölü satır oranını ölçen sorgu → rutin `VACUUM (ANALYZE, VERBOSE)`
→ `VACUUM FULL` (ACCESS EXCLUSIVE kilit ve ek disk uyarısıyla, pg_repack
alternatifiyle) → partition'a geçiş ve `DETACH PARTITION` ile arşivleme →
disk büyütme kararı için eşik. Eşik uydurulmuyor: dbace gerçek disk
kapasitesini ölçmediği için "iki katına çıkma tarihindeki doluluk %85'i
geçecekse şimdi planla" diye kullanıcının kendi kapasitesine bağlanıyor.

**Bağlantı artışı (PostgreSQL 5 adım):** mevcut/azami + idle /
idle-in-transaction dağılımı → bağlantıyı kim tutuyor → **önce PgBouncer**
(örnek `pgbouncer.ini` ile) → `idle_in_transaction_session_timeout` →
en sonda `max_connections` artırımı, "reload ile GİRMEZ, yeniden
başlatma gerekir" ve bellek uyarısıyla. Sıra bilinçli: max_connections'ı
büyütmek bağlantı sorununu bellek sorununa çevirir. SQL Server ve MongoDB
için ayrı, kendi komutlarıyla planlar var (Postgres komutu sızmıyor).

**Tablo büyümesi (5 adım):** büyüme gerçek veri mi şişme mi ayrımı →
tablo/index boyut dağılımı → `VACUUM (ANALYZE, VERBOSE)` → partiler
halinde arşivleme (tek seferde milyonlarca satır silmenin WAL'i şişirdiği
ve VACUUM'u engellediği uyarısıyla) → kalıcı çözüm olarak partitioning.
Komutlar ilgili şema/tablo adıyla üretiliyor.

**Transaction ID wraparound (5 adım):** veritabanı bazında yaş → yaşı
hangi tablo taşıyor → **önce engelleyicileri bul** (uzun transaction,
`pg_replication_slots`, `pg_prepared_xacts`) → `VACUUM (FREEZE, VERBOSE,
ANALYZE)` → autovacuum ayarları (`autovacuum_max_workers` yeniden
başlatma ister, diğer ikisi reload ile geçer notuyla). Engelleyici adımı
freeze adımından ÖNCE: autovacuum engellenmişse elle FREEZE de yetmez.

**Index şişmesi (5 adım):** boyut ve `idx_scan` kontrolü → `pgstattuple`
ile gerçek şişme oranı → hiç kullanılmıyorsa `DROP INDEX CONCURRENTLY` →
kullanılıyorsa `REINDEX INDEX CONCURRENTLY` (iki kopyanın birden diskte
duracağı ve yarıda kalırsa INVALID index kalacağı uyarısıyla) →
tekrarlamaması için `fillfactor`.

**Arayüz.** Yeni `PredictionPlaybook` bileşeni: "Adım adım çözüm (N adım)"
katlanabilir başlığı, açılınca numaralı liste; her adımın komutu ayrı
satırda `CopyableAction` kutusunda (çok satırlı komutlar `white-space:
pre` ile olduğu gibi kopyalanıyor). Hem Tahminler sayfasında hem instance
detayının Tahminler sekmesinde. Varsayılan katlı — üstte sade özet
kalıyor.

**Testler:** `tests/test_prediction_playbooks.py` (20 test) — her planda
en az 3 adım ve en az bir komut, komutlar kırpılmamış, ölçülen değerler
adım metnine geçiyor, tablo/index planları gerçek nesne adını yazıyor,
yıkıcı komutlar uyarı taşıyor, wraparound planında engelleyici adımı
freeze'den önce, bağlantı planında pooler max_connections'tan önce,
SQL Server/MongoDB planlarına Postgres komutu sızmıyor.
Toplam: 180 test yeşil.

## Faz 17 — İŞ 1: Sağlık Raporu motoru ve veri modeli

Yeni özellik: aynı toplanmış veriden iki farklı rapor (teknik/DBA ve
yönetici/müşteri) üretecek ortak üretim hattı. Bu iş hattın kendisini
kuruyor; bölümler İŞ 2-3'te dolduruluyor.

**Veri modeli (3 yeni tablo + Supabase migration
`20260905090000_health_reports.sql`, DEPLOY.md'de 23 numara):**

- `HealthReport` — kapsam (global/customer/application/group/instance),
  dönem, üretim şekli (schedule/manual), genel durum, bölüm gövdeleri
  (`sections` JSON), önceki rapor bağı, süre. Ayrıca arka plan üretimi
  için `status` (queued/running/done/failed), `progress_pct`,
  `progress_label`, `error`. Kapsamın o anki adı (`scope_label`) rapora
  KOPYALANIYOR: kapsam sonradan silinse/yeniden adlandırılsa bile rapor
  kendi başlığını taşısın (rapor geçmişi bir arşivdir, canlı görünüm değil).
- `ReportFinding` — bölüm, ciddiyet, başlık, detay, **kanıt** (evidence),
  öneri, komutlar, ilgili nesne, fingerprint. Ek olarak motorun
  hesapladığı `priority`, `open_since_days`, `change_state`,
  `acknowledged`.
- `FindingAcknowledgement` — fingerprint + kapsam bazında kabul, kim/ne
  zaman/not, süreli (`expires_at`).

**Fingerprint kararlılığı.** Hash'e yalnızca *bulgu tipi* ve *hedef
nesne* girer; ÖLÇÜLEN DEĞER bilerek dışarıda. Değer hash'e girseydi
bulgu her gün "yeni" görünür, "kaç gündür açık" sayacı hiç ilerlemez ve
gürültü kontrolü çalışmazdı.

**Gün-gün karşılaştırma.** Motor her raporda önceki raporun bulgularını
fingerprint ile eşleştirip `change_state` üretiyor: `new`, `ongoing`
(+ `open_since_days` bir artar), `regressed` (ciddiyet yükselmiş),
`resolved` (önceki raporda vardı, artık yok). Kapanan bulgular rapora
`severity="ok"` satır olarak YAZILIYOR — "yapılan işler" ve "düzelenler"
bölümleri (İŞ 2/3) gerçek veriye dayansın diye.

**Kabul (acknowledgement) davranışı.** Kabul edilen bulgu rapordan
silinmiyor, `acknowledged=true` işaretleniyor; raporun genel durumu ve
kritik/uyarı sayıları yalnızca KABUL EDİLMEMİŞ ve kapanmamış bulgulardan
hesaplanıyor. Süresi dolan kabul kaydı silinmiyor (kim ne zaman neyi
kabul etmişti bilgisi kalsın) ama etkisiz sayılıyor — bulgu kendiliğinden
öne çıkıyor. Uç idempotent: aynı fingerprint tekrar kabul edilirse yeni
kayıt açılmaz, süre uzatılır.

**Öncelik (etki × aciliyet).** `priority = ciddiyet × ortam × değişim ×
yaş`. Ciddiyet ana ağırlık (kritik 100, uyarı 40, bilgi 10); **prod ortam
preprod/test'ten önce** (×1.6 / ×1.1 / ×0.9); kötüleşen bulgu yeniden,
yeni bulgu süregelenden önce; 30 güne kadar yaş en fazla %25 ek ağırlık
verir (yaş tek başına ciddiyeti geçemesin). Rapor detayı bulguları bu
puana göre sıralı döndürüyor — alfabetik değil.

**Canlı probe yok.** Rapor yalnızca saklanmış veriden üretiliyor. Rapor
06:00'da onlarca instance için çalışıyor; her biri için canlı bağlantı
açmak hem toplama döngüsüyle yarışır hem de raporu "dün ne oldu"dan
"şu an ne oluyor"a çevirirdi.

**Arka plan üretimi ve ilerleme.** `enqueue_report()` satırı `queued`
olarak yazıp `asyncio.create_task` ile kendi oturumunda üretime
başlatıyor; her bölüm bitince `progress_pct`/`progress_label`
güncelleniyor. Bir bölüm çökerse rapor tamamen düşmüyor — o bölüm
`status="unknown"` + sebebiyle işaretleniyor (sessizce "sorunsuz"
göstermek dürüstlük kuralına aykırı olurdu). Üretim hatası raporun
kendi `error` alanına yazılıyor.

**Kanıt zorunluluğu kodda.** `evidence` boş bırakan bir bölüm üretim
sırasında `ValueError` alıyor ve rapor `failed` işaretleniyor — kural
yorum satırı değil, çalışan bir kısıt.

**Zamanlama.** APScheduler'a cron job eklendi (varsayılan 06:00, saat
ayarlanabilir). `scope_mode` ayarı hangi kapsamların otomatik
üretileceğini belirliyor: `global`, `customers` (her müşteri için ayrı)
veya `both` (varsayılan). Saat değişince `reschedule_health_report()`
canlı scheduler'a uyguluyor (dashboard yenileme aralığındaki desenin
aynısı). Kapsamlar SIRAYLA üretiliyor — paralel çalıştırmak toplama
döngüsüyle yarışırdı.

**Uçlar** (`/api/reports`): liste, detay (bulgular önceliğe göre sıralı),
`latest` (dashboard kartı için), `run` (elle tetikleme, 202 + queued),
silme, `schedule` GET/PUT, `acknowledgements` GET/POST/DELETE. Yetki
main.py'deki `require_write_access` ile geliyor: viewer GET yapabiliyor,
POST/DELETE yapamıyor — İŞ 5'te istenen davranış ek kod olmadan sağlanmış
oluyor.

**İlk bölüm — Erişilebilirlik.** Hattı uçtan uca kanıtlamak için bir
gerçek bölüm eklendi: toplanan metrik örnekleri arasındaki boşluklardan
kesinti pencereleri türetiliyor (toplama aralığının 3 katından uzun
boşluk, en az 60 sn). Yüzde, dönemin tamamı için değil instance'ın
İZLENEBİLDİĞİ süre için hesaplanıyor — dönemin ortasında eklenmiş bir
instance'ın öncesini "kesinti" saymak yanlış olurdu. Bulgu metni ölçümün
sınırını açıkça söylüyor: bu "dbace veri toplayamadı" demektir,
veritabanının kapalı olduğunu tek başına kanıtlamaz.

**Testler:** `tests/test_health_report_engine.py` (11) +
`tests/test_health_report_api.py` (13). Toplam: 204 test yeşil.

## Faz 17 — İŞ 2: Teknik rapor bölümleri (DBA)

12 bölümün tamamı `services/report_sections.py` içinde. Her bölüm
`SectionResult` döndürüyor: durum, özet, serbest veri (tablolar) ve
bulgular. Her bulgu kanıt (hangi metrik, hangi değer, hangi eşik, ne
zaman ölçüldü), öneri ve mümkün olduğunda çalıştırılacak komut taşıyor.

**Motor eklentisi — özet bölümleri.** Yönetici özeti, "dünden beri
değişenler" ve "bilinen konular" diğer bölümlerin ÇIKTISINA bakmak
zorunda. Motora `register_summary_section` eklendi: bu bölümler normal
bölümlerden SONRA çalışıyor ama raporda ÖNDE görünüyor (özet,
özetlediği şeyin üstünde durmalı).

**1. Yönetici özeti.** En yüksek öncelikli 3-5 kritik/uyarı bulgusunu
listeler. Kendi bulgusunu ÜRETMEZ — aksi halde aynı sorun hem özette hem
asıl bölümde sayılır, kritik sayısı şişerdi. Değerlendirilemeyen
bölümler (`unknown`) de özette görünür; "bulgu yok" ile "bakamadık"
karıştırılmaz.

**2. Dünden beri değişenler.** Motorla AYNI `make_fingerprint`
fonksiyonunu kullanır — bu bölümde "yeni" görünen bir bulgu, kaydedilen
satırda da `change_state="new"` olur; iki yerin farklı cevap vermesi
mümkün değil. Yeni / kötüleşen / kapanan / süregelen olarak dört liste,
süregelenler "kaç gündür açık" bilgisiyle sıralı.

**3. Erişilebilirlik.** (İŞ 1'de eklendi.) Toplama boşluklarından kesinti
pencereleri; ölçümün sınırı bulgu metninde yazılı.

**4. Cluster sağlığı.** Lider değişimleri `MetricSample.metrics_json`
içine gömülü cluster anlık görüntülerinden geriye dönük okunuyor
(canlı probe yok). Ayrıca: lidersiz kalınan ölçüm sayısı, servis bazında
"down" görülen ölçüm sayısı, replikasyon lag zirvesi ve zirvenin
yarısının üstünde geçirilen süre. Grup seviyesinde
`GroupHealthSnapshot`'tan etcd quorum kaybı, split-brain şüphesi ve DR
düğümü olmayan gruplar. Cluster yapılandırılmamış instance'lar için bölüm
`ok` döner (yokluğu sorun değil); cluster tanımlı ama veri yoksa
`unknown`.

**5. Performans.** En pahalı sorgular, Faz 16-B İŞ 4'teki mantıkla
DÖNEM FARKINDAN sıralanıyor (kümülatif toplamdan değil). Her sorgu bir
önceki eşit uzunluktaki dönemle karşılaştırılıp `new` / `worse` /
`better` / `stable` etiketleniyor. Darboğaz sınıfı (I/O, CPU, bellek,
kilit) mevcut `diagnose_query` ile — bu fonksiyon zaten saklanan
sütunlardan çalışıyor, ek sorgu çalıştırmıyor. Bulgu yalnızca YENİ ya da
BELİRGİN KÖTÜLEŞEN ve ortalaması ≥50 ms olan sorgular için üretiliyor;
"en pahalı 10"un tamamını bulguya çevirmek her gün 10 bulgu demek olurdu.

**6. Kaynak kullanımı.** Bağlantı zirvesi (saatiyle), cache hit
ortalaması/en düşüğü, geçici dosya zirvesi, checkpoint davranışı.
Checkpoint bulgusu, istek üzerine checkpoint'lerin zamanlanmışlardan
baskın olması durumunda çıkıyor (max_wal_size baskısı). Bölüm notunda
sunucu seviyesi CPU/RAM/disk metriklerinin dbace tarafından
TOPLANMADIĞI açıkça yazıyor.

**7. Şema sağlığı.** Günlük şema anlık görüntülerinden (Faz 16 İŞ 6'da
eklenen `SchemaObjectDailySample`) büyüyen nesneler ve kullanılmayan
indexler. Notta, anlık ölçülen ama geçmişe dönük saklanmayan şeylerin
(autovacuum gecikmesi, dead tuple oranı) bu bölümde raporlanamadığı
belirtiliyor.

**8. Parametre denetimi.** Baseline sapmaları ve **DÜN'E GÖRE DEĞİŞEN
parametreler** — bu bölümün asıl değeri. Rapor canlı probe yapmadığı için
bu bilgi başka türlü elde edilemezdi; bkz. aşağıdaki günlük durum
fotoğrafı.

**9. Alarmlar.** Dönemdeki tetiklemeler kural bazında gruplanıp
10'dan fazla tetikleyen kurallar "gürültü yapan" olarak işaretleniyor
(alarm körlüğü yaratır). Dönemden ÖNCE açılıp hâlâ kapanmamış olaylar
"uzun süredir açık" bulgusu üretiyor. `AlertEvent` kural adını/eşiğini
taşımadığı için okunabilir bir rapor adına `AlertRule` ile join ediliyor.

**10. Kapasite.** Açık `PredictionInsight` kayıtları, %90 güven
aralığıyla birlikte. Öneri ve komut tahminin kendi alanlarından geliyor
(Faz 16-B İŞ 7'de eklenen adım adım plan bu bulgunun devamı).

**11. Ön koşullar.** Eksik eklenti/yetki ve bunların engellediği
analizler. Yoksayılan (ignored) kontroller bulgu ÜRETMEZ ama ayrı
listede görünür — "bu analiz neden yok?" sorusunun cevabı orada.
`unknown` durumundaki kontroller eksiklik sayılmıyor.

**12. Bilinen konular.** Kabul edilmiş bulgular; kim, ne zaman, hangi
notla kabul etmiş, kaç gündür açık, kabul süresi dolmuş mu, bulgu bu
raporda hâlâ var mı.

**Yeni: günlük durum fotoğrafı (`DailyStateSnapshot`).** 8 ve 11
numaralı bölümler geçmişe dönük veri istiyor; `pg_settings` okuması ve ön
koşul denetimi 15 saniyelik toplama döngüsüne konulamayacak kadar pahalı.
Bunun yerine zaten günde bir kez çalışan rollup işine eklendi (şema
taramasıyla aynı desen): her gün bir parametre ve bir ön koşul fotoğrafı
saklanıyor. İki probe birbirinden bağımsız — biri başarısız olursa
diğeri yine kaydediliyor ve başarısızlığın kendisi `error` olarak
saklanıyor, böylece rapor "ölçülemedi" diyebiliyor (sessizce "sorunsuz"
göstermiyor). Supabase migration:
`20260905100000_daily_state_snapshots.sql` (DEPLOY.md 24 numara).

`parameter_audit.py`'ye `collect_instance_parameters()` eklendi: aynı
`CRITICAL_PARAMETERS` baseline'ını ve aynı `_evaluate_parameter`
değerlendirmesini kullanır ama Node/Server yerine Instance üzerinden
çalışır — gruba bağlanmamış standalone sunucular da kapsansın diye.

**Testler:** `tests/test_report_sections.py` (28 test) — her bölüm için
doğru bulgu, kanıttaki gerçek sayılar, veri yokken "unknown" davranışı ve
gürültü üretmeme. Ayrıca `tests/test_rollup.py` yeni probe'lara karşı
izole edildi. Toplam: 232 test yeşil.

## Faz 17 — İŞ 3: Yönetici raporu (müşteri görünümü)

Yeni `services/executive_report.py` + `GET /api/reports/{id}/executive`.
Aynı `HealthReport`/`ReportFinding` satırlarından türeyen, teknik terim
içermeyen ikinci bir görünüm.

**"ASLA teknik detay" kuralı kodda zorlanıyor — iki katman.**

1. *Şablon yaklaşımı:* yönetici görünümü bulguların `title`, `detail`,
   `commands` ve `evidence` alanlarını **kopyalamaz**. Her cümle bölüm
   türüne göre sabit bir şablondan üretilir; bulgudan yalnızca beyaz
   listeye alınmış SAYISAL alanlar okunur (ör. `horizon_days`,
   `uptime_pct`). Yani sorgu metninin ya da parametre adının yönetici
   metnine ulaşacağı bir yol yok.
2. *Tarama katmanı:* üretilen tüm serbest metin `assert_no_technical_leak()`
   ile taranıyor — SQL anahtar kelimeleri, `pg_*` tanımlayıcıları, IPv4,
   `host:port`, dosya yolları ve bilinen parametre adları. Bulunursa
   `TechnicalLeakError`. Bu, ileride biri şablonlara teknik bir alan
   eklediğinde sessizce sızmasını önler.

Testler bunu kasten teknik bulgularla kanıtlıyor: sorgu metni, komut,
`work_mem`, `pg_settings`, `10.20.30.40`, `postgresql.conf` içeren
bulgular verilip yönetici çıktısının tamamı bu dizeler için taranıyor.

**Hedef adı sunucu adı değil.** Risk cümlelerindeki `{target}`,
instance/host adı değil **uygulama adı** (yoksa veritabanı grubu adı).
Instance adları çoğu kurulumda sunucu adını içerdiği için son çare olarak
bile kullanılmıyor. Erişilebilirlik özeti de uygulama bazında toplanıyor;
teknik bölümün instance satırlarındaki sunucu adları dışarı çıkmıyor.

**Kötü adlandırılmış uygulama raporu düşürmüyor.** Bir uygulama IP gibi
adlandırılmışsa (`10.0.0.1`) etiket "İzlenen sistem"e düşürülüyor — rapor
hata vermiyor. Müşterinin kötü adlandırma tercihi, yöneticinin raporu hiç
görememesine yol açmamalı.

**Bölümler:**

- **Kapak:** kapsam adı, dönem (Günlük/Haftalık/Aylık — pencere
  uzunluğundan türetiliyor), genel sağlık notu **Sağlıklı / Dikkat /
  Riskli** ve notun gerekçesi. Kritik bulgu VEYA %99'un altında
  erişilebilirlik → Riskli.
- **Erişilebilirlik:** yüzde uptime, kesinti sayısı, toplam ve en uzun
  kesinti süresi; uygulama bazında ayrı satırlar.
- **Sistem envanteri:** veritabanı ve küme sayısı, ortam ve topoloji
  dağılımı, DR kapsamı ("2/3 kümenin ikinci merkez kapsamı var").
- **Risk özeti:** yüksek/orta/düşük, alan bazında (Erişilebilirlik,
  Performans, Kapasite, Yapılandırma…), her madde iş etkisiyle. Aynı
  alan + aynı uygulama için tek madde — 10 yavaş sorgu bulgusu yöneticiye
  tek satır olarak iniyor.
- **Trend:** önceki dönem raporuyla karşılaştırma (iyileşti / kötüleşti /
  değişmedi), kritik-uyarı sayıları ve uptime yan yana.
- **Yapılan işler:** dönemde kapatılan bulgu sayısı (motorun `resolved`
  kayıtlarından — uydurma değil, gerçek veri).
- **Öneriler:** yalnızca yüksek/orta riskler için; her biri "bu
  yapılmazsa şu risk" cümlesiyle.

Kabul edilmiş (acknowledged) ve kapanmış bulgular risk listesine
girmiyor; kapanmışlar "yapılan işler" sayısını besliyor.

**Testler:** `tests/test_executive_report.py` (14) +
`tests/test_health_report_api.py`'ye 3 uç testi (viewer da yönetici
raporunu görebiliyor; tamamlanmamış rapor 409). Toplam: 249 test yeşil.

## Faz 17 — İŞ 4: Rapor dışa aktarma (PDF / HTML / Markdown)

**Mimari: tek içerik, üç renderer.** Rapor önce biçimden bağımsız bir
"blok belgesine" (`Document`: başlık, paragraf, madde listesi, tablo,
anahtar-değer, not, kod) çevriliyor; PDF, HTML ve Markdown bu AYNI
belgeden üretiliyor. Üç ayrı şablon yazmak, zamanla üçünün ayrışması
demekti (PDF'de olan bir bölümün HTML'de olmaması gibi); bu yapıda bir
bölüm eklendiğinde üç çıktıda da otomatik görünüyor.

- `services/report_export.py` — belge modeli ve üç renderer.
- `services/report_documents.py` — raporu bloklara çeviren iki builder
  (teknik ve yönetici).

**PDF motoru: ReportLab** (gerekçe SORULAR.md'de). Özetle: WeasyPrint
HTML/CSS render ettiği için tek şablonla çalışmayı mümkün kılardı ama
cairo/pango gibi işletim sistemi kütüphaneleri istiyor; bunları kurmak
`deploy/onprem/Dockerfile.backend` dosyasını değiştirmeyi gerektirirdi ve
deploy dosyalarına dokunulmaması kuralı bunu kapatıyor. ReportLab saf
Python — `requirements.txt`'e tek satır yetti.

**Türkçe karakter sorunu çözüldü.** ReportLab'ın varsayılan Helvetica'sı
WinAnsi kodlamasıyla sınırlı ve ğ/ş/ı/İ basamıyor. ReportLab'ın kendi
paketiyle gelen Bitstream Vera TTF'leri gömülü font olarak kaydediliyor;
ek dosya ya da sistem fontu gerekmiyor. Fontun tüm Türkçe karakterleri ve
tipografik tırnakları içerdiği doğrulandı, PDF üretimi testle kanıtlandı.

**Bölüm seçimi.** `GET /api/reports/{id}/export?sections=a,b,c` ile
yalnızca istenen bölümler dahil ediliyor — müşteriye gönderilecek
çıktıdan teknik bölümler çıkarılabiliyor. Seçici için
`GET /api/reports/{id}/export-sections` mevcut bölümleri (teknik ya da
yönetici görünümü için ayrı ayrı) döndürüyor.

**Kurumsal görünüm.** HTML çıktısı kendi kendine yeten tek dosya (stil
gömülü, dışarıdan hiçbir kaynak çekmiyor), `@media print` kurallarıyla
yazdırılabilir; tablolar ve notlar sayfa ortasından bölünmüyor. PDF A4,
başlık bloğu + dönem/durum meta satırı, çizgili tablolar, renkli kenarlı
not kutuları, her sayfada alt bilgi ve sayfa numarası. Markdown çıktısı
e-posta/Slack'e yapıştırmak için — tablolar GFM biçiminde, komutlar
` ```sql ` bloklarında.

**Anlamlı dosya adı.** `x-bank_rapor_2026-09-04.pdf`, yönetici görünümü
için `x-bank_rapor-yonetici_2026-09-04.pdf`. Türkçe harfler ASCII'ye elle
eşleniyor: NFKD normalizasyonu tek başına 'ı' ve 'ğ' harflerini tamamen
düşürüp "Iğdır"ı "gdr" yapardı.

**Yetki.** Export uçları GET olduğu için viewer rolü de dışa
aktarabiliyor (İŞ 5'te istenen davranış); elle tetikleme ve bulgu kabulü
hâlâ admin'e kapalı.

**Testler:** `tests/test_report_export.py` (17 test) — üç renderer'ın her
blok tipini basması, HTML'in kendi kendine yetmesi ve içeriği kaçırması
(XSS), PDF'in gerçek PDF olması ve Türkçe basması, boş tablonun üç
biçimde de atlanması, dosya adı üretimi, bölüm filtresi, yönetici
çıktısına teknik detay sızmaması, viewer'ın export alabilmesi,
tamamlanmamış raporun 409 dönmesi. Toplam: 266 test yeşil.

## Faz 17 — İŞ 5: Rapor arayüzü

**Sol menüde üst seviye "Raporlar"** öğesi ve ayrı bir sayfa
(`/reports`).

**Üst çubuk:** kapsam seçici (Tüm sistem / Müşteri / Uygulama /
Veritabanı grubu / Instance — seçime göre hedef listesi doldurulur),
dönem seçici (Gün / Hafta / Ay / Özel aralık), rapor tipi seçici
(**Teknik | Yönetici**), "Şimdi çalıştır" ve "Dışa aktar" butonları.

**Rapor geçmişi** sol sütunda: tarih, genel durum ve kritik/uyarı
sayılarıyla liste. Herhangi bir satırdaki "Karşılaştır" butonu o raporu
seçili raporla **yan yana** koyuyor (genel durum, kritik, uyarı ve bulgu
sayıları tablo halinde).

**Üretim ilerlemesi.** Rapor arka planda üretildiği için sayfa
`queued`/`running` durumundayken ilerleme çubuğu ve o an çalışan bölümün
adını gösteriyor, 1,5 saniyede bir tazeliyor; bitince listeyi
yeniliyor. Hata durumunda raporun kendi `error` alanı gösteriliyor.

**Bulgu kartları.** Her bölüm altında, öncelik sırasına göre. Kart
kapalıyken ciddiyet, başlık, değişim etiketi (Yeni/Kötüleşti/Kapandı) ve
"N gündür açık" rozeti görünüyor — gürültü kontrolü arayüze de yansıyor.
Açıldığında detay, **kanıt satırı** (metrik · ölçülen · eşik · ölçüm
zamanı), öneri ve kopyalanabilir komutlar. Öneri üretilememişse bunun
kendisi yazılı, boş bırakılmıyor.

**"Kabul et"** butonu not ve süre (gün) alarak bulguyu susturuyor;
formun altında ne olacağı açıkça yazıyor ("rapordan silinmez, Bilinen
konular bölümüne düşer, süre dolunca tekrar öne çıkar"). Kabul sonrası
rapor ve geçmiş anında tazeleniyor.

**Derin bağlantı.** Her bulgu, bölümüne göre doğru sekmeye gidiyor:
performans → Yavaş Sorgular, kapasite → Tahminler, şema → Şema, cluster →
Cluster, parametre/ön koşul → Tuning, alarm kuralı → Alarmlar sayfası.
Genel bir sayfaya değil, sorunun görüleceği yere.

**Dışa aktarma paneli.** Biçim (PDF/HTML/Markdown) ve bölüm seçimi
(checkbox). Seçili görünüme göre bölüm listesi backend'den geliyor.
Panel, teknik görünüm dışa aktarılırken müşteriye gönderim için
Yönetici görünümüne geçmeyi hatırlatıyor. İndirme, dosya adını
`Content-Disposition` başlığından alıyor.

**Yönetici görünümü** ayrı bir bileşen (`ExecutiveReportView`) ve
backend'in teknik detaydan arındırdığı yapıdan besleniyor — teknik
bulgulara hiç erişmiyor, böylece sızıntı riski tek yerde kalıyor. Kapak
kartı (sağlık notu ve gerekçesi), erişilebilirlik istatistikleri ve
uygulama bazında tablo, envanter, risk listesi, basit trend çubukları,
yapılan işler ve öneri tablosu.

**Dashboard kartı.** Üstte "Bugünün sağlık raporu" kartı: kapsam, üretim
zamanı ve **N kritik bulgu** sayısı; tıklayınca `/reports?report=<id>`
ile doğrudan o rapora gidiyor.

**Rapor zamanlaması** Yönetim → Ayarlar sekmesine eklendi: açık/kapalı,
saat (00:00–23:00) ve kapsam modu (sadece tüm sistem / her müşteri için
ayrı / ikisi birden). Diğer operasyonel ayarların yanında duruyor.

**Viewer rolü.** Raporu görüntüleyebiliyor ve dışa aktarabiliyor;
"Şimdi çalıştır" butonu ve "Kabul et" butonu görünmüyor. Sayfada bunun
nedeni de yazılı. Backend tarafında zaten `require_write_access` ile
korunuyor (export uçları GET olduğu için viewer'a açık).

## Faz 17 — İŞ 6: Kalite kuralları (raporun işe yarar olmasını belirleyen kurallar)

Beş kural. Üçü İŞ 1–5 boyunca zaten uygulanmıştı; bu iş eksikleri
tamamladı ve **hepsini kodda zorlanan / testle korunan** hale getirdi.
Yeni `tests/test_report_quality_rules.py` tek tek bölümleri değil, TÜM
bölümlerin uymak zorunda olduğu değişmezleri test ediyor — yeni bir bölüm
eklendiğinde kuralı çiğnerse test kırılıyor, kimsenin kuralları
hatırlamasına gerek kalmıyor.

**1. Gürültü kontrolü.** Aynı fingerprint'e sahip bulgu her raporda
`open_since_days` bir artırılarak `ongoing` işaretleniyor; arayüzde
"N gündür açık" rozeti çıkıyor, "yeni" gibi sunulmuyor. Test üç ardışık
rapor üretip sayacın gerçekten ilerlediğini doğruluyor.

*Bu test gerçek bir hata yakaladı:* "önceki rapor" sorgusu yalnızca
`generated_at DESC` ile sıralanıyordu. SQLite'ın `CURRENT_TIMESTAMP`'i
SANİYE hassasiyetinde olduğu için aynı saniyede üretilen iki rapor
eşitleniyor, zincir kopuyor ve sayaç ilerlemiyordu. Sıralamaya `id DESC`
eklendi.

**2. Kanıt zorunluluğu.** `evidence` boş bırakan bir bölüm üretim
sırasında `ValueError` alıyor (İŞ 1'den beri). Yeni test bunu genişletti:
tüm bölümleri gerçek veriyle çalıştırıp üretilen HER bulgunun kanıt
taşıdığını ve kanıtın ölçüm zamanını (`measured_at`/`triggered_at`)
içerdiğini doğruluyor.

**3. Dürüstlük.**

- Yeni `needs_more_days()` yardımcısı: "yeterli veri yok" yerine
  "en az 2 günlük veri gerekiyor; şu an 1 gün var — yaklaşık 1 gün daha
  gerekli". Şema, parametre ve ön koşul bölümleri bunu kullanıyor.
- Şema bölümü artık **iki günlük** fotoğraf istiyor: tek fotoğraftan
  büyüme çıkarılamaz, "büyüme yok" demek yanlış olurdu.
- Ölçmediğimiz şey açık bir alan olarak "bilinmiyor" işaretleniyor:
  kaynak bölümünün verisine `os_metrics: {status: "unknown", reason: …}`
  eklendi. Dipnot değil, veri yapısında bir alan — "bu bölümdeki 'sorun
  yok' değerlendirmesi YALNIZCA veritabanı içi göstergeler içindir".
- Test, veri olmayan bölümlerin (`cluster`, `performance`, `schema`,
  `parameters`, `prerequisites`) `ok` değil `unknown` döndürdüğünü ve
  nedenini yazdığını doğruluyor.

**4. Aksiyon edilebilirlik.** Yeni kural motora eklendi: kritik/uyarı
bulgusunun önerisi yoksa bulgu **atılmıyor**, yerine neden öneri
verilemediği yazılıyor (`NO_RECOMMENDATION_EXPLANATION`) ve durum loga
düşüyor. Bulguyu atmak gerçek bir sorunu gizlemek olurdu; hata vermek de
tek bir bölümün eksiği yüzünden tüm raporu düşürürdü. Bilgi (`info`)
bulguları bu zorunluluğun dışında — oraya zorla öneri uydurmuyoruz.

**5. Öncelik.** `priority = ciddiyet × ortam × değişim × yaş` (İŞ 1).
Test, kaydedilen bulgular arasında en yüksek önceliklinin kritik
olduğunu ve hiçbir `info` bulgusunun herhangi bir kritik bulgunun önüne
geçemediğini doğruluyor.

**Ek olarak** özet bölümlerinin (yönetici özeti, dünden beri değişenler,
bilinen konular) kendi bulgularını üretmediği testle sabitlendi — aksi
halde aynı sorun iki kez sayılır ve kritik sayısı şişerdi.

**Testler:** `tests/test_report_quality_rules.py` (11 test) + şema
bölümüne 1 yeni test. Toplam: 278 test yeşil.

## Faz 17 — Ek İŞ A (arka uç): Bulgu durum makinesi

Tek "kabul edildi" bayrağı gerçek bir DBA akışını taşıyamıyordu: "şimdi
bakamıyorum, salıya ertele", "bu riski bilerek kabul ediyoruz",
"değişiklik talebi açtık", "düzelttim ama doğrulanmalı" birbirinden çok
farklı kararlar. Yeni `services/finding_status.py` bunları yedi durumlu
bir makineye bağlıyor: **açık / yoksayıldı / ertelendi / risk_kabul /
planlandı / çözüldü_doğrulanacak / çözüldü**.

**Model.** `FindingAcknowledgement` genişletildi (`status`,
`finding_type`, `reference`); tablo adı korundu — yeniden adlandırmak var
olan kurulumlarda veri taşıma gerektirirdi. Yeni `FindingStatusHistory`
tablosu her geçişi saklıyor: kim, ne zaman, hangi durumdan hangisine,
hangi notla. `ReportFinding`'e `status`, `finding_type`,
`verification_failed`, `decision_note/reference/until` eklendi.
Supabase migration `20260906090000_finding_status_machine.sql`
(DEPLOY.md 25), **geriye dönük doldurma dahil**: eski `acknowledged=true`
satırları `status='ignored'`e çevriliyor — yoksa yükseltmeden sonra daha
önce susturulmuş bütün konular topluca kritik olarak geri dönerdi.

**Not zorunlu.** Durum değişikliği notsuz kabul edilmiyor (uçta 422).
Notsuz bir susturma kaydı altı ay sonra "bunu neden kapattık?" sorusunu
cevapsız bırakır.

**Kapsam seçimi.** Karar beş seviyeden birine uygulanabiliyor:
`instance` (varsayılan, en dar) | `group` | `application` | `customer` |
`global`. En dar kapsam tekil bulgunun fingerprint'iyle eşleşiyor; daha
geniş kapsamlar bulgu TİPİYLE (`<bölüm>:<tip>`, hedef nesne kimliği
içermez) eşleşiyor. Yeni `finding_type` alanı bunun için gerekliydi:
fingerprint hedef nesneyi zaten içerdiğinden, onunla "bu tipi her yerde
yoksay" demek imkânsızdı.

Birden fazla karar eşleşirse **en dar olan kazanır** — bir sunucu için
verilmiş özel karar, aynı tip için verilmiş küresel kararı ezer; istisna
yönetimi böyle çalışmalı.

**Üç otomatik geçiş** (rapor üretimi sırasında, hepsi geçmişe
`changed_by="sistem"` olarak yazılıyor):

1. `ertelendi` + süre doldu → `açık`.
2. `çözüldü_doğrulanacak` + bulgu HÂLÂ tespit ediliyor → `açık` +
   **"çözüm doğrulanamadı"** işareti. Yanlış kapatmaları yakalayan asıl
   mekanizma; rapor bu bulguyu tekrar kritik sayıyor.
3. Bulgu artık tespit edilmiyor → `çözüldü`, "düzelenler" bölümünde.

Elle `çözüldü` denmiş ama bulgu duruyorsa o da doğrulanamamış sayılıyor —
`çözüldü` yalnızca bulgunun gerçekten kaybolmasıyla kalıcı olabiliyor.

**Rapora etkisi.**

- Kritik/uyarı sayaçları ve genel durum **yalnızca "açık"** bulguları
  sayıyor.
- "Bilinen konular" bölümü artık yalnızca kabul edilenleri değil, açık
  olmayan TÜM durumları kararlarıyla (kim, ne zaman, not, referans,
  bitiş tarihi, kaç gün sonra geri açılacak) listeliyor. Süresi dolmuş
  bir karar bu listeden düşüyor — artık bilinen konu değil, açık bulgu.
- Yönetici raporuna yeni bir bölüm: **"Planlanan çalışmalar ve kabul
  edilen riskler"**. `planlandı` ekibin çalıştığını, `risk_kabul`
  bilinçli kararı gösteriyor. **`yoksayıldı` bu bölüme hiç girmiyor** —
  o, ekibin kendi iç gürültü yönetimi kararı, müşteriye rapor edilecek
  bir şey değil. Referans (ticket/CR no) yöneticinin takip edebilmesi
  için taşınıyor ve o da sızıntı taramasından geçiyor.
- Dışa aktarmada bölüm seçicisine eklendi.

**Uçlar.** `POST /api/reports/findings/status` (tekil ve toplu aynı uçtan
— iki ayrı uç iki ayrı hata yolu demek olurdu),
`GET /api/reports/findings/{fingerprint}/history`. Eski
`/acknowledgements` ucu geriye dönük uyumluluk için duruyor ve artık aynı
duruma (`yoksayıldı`) yazıyor; iki mekanizmanın ayrışması kaçınılmaz bir
tutarsızlık olurdu.

**Testler:** `tests/test_finding_status.py` (24 test). Bu testler ikinci
bir saniye-hassasiyeti hatası yakaladı: durum geçmişi sorgusu yalnızca
`changed_at` ile sıralanıyordu ve aynı saniyedeki iki değişiklik rastgele
sıralanıyordu (`_previous_report`'takiyle aynı düzeltme uygulandı).
Toplam: 303 test yeşil.

## Faz 17 — Ek İŞ A (arayüz): Durum seçici, toplu işlem ve filtre

**Bulgu kartında durum seçici.** Ayrı bir sayfaya gitmeye gerek yok:
"Durum değiştir" tek tıkla açılan bir panel getiriyor — durum, kapsam,
not ve duruma göre tarih/referans alanları. Yeni
`components/FindingStatusControl.tsx`.

- Tarih alanı yalnızca "ertelendi" ve "yoksayıldı" için görünüyor;
  referans alanı yalnızca "planlandı" için. Backend de o durumlarda
  saklamıyor — arayüzün anlamsız alan göstermesi kullanıcıyı yanıltırdı.
- Not zorunlu: boşken "Uygula" butonu kapalı. Sunucu da reddediyor ama
  kullanıcıyı hata mesajıyla karşılaştırmak yerine engelliyoruz.
- Kapsam seçenekleri bulgunun gerçek zincirinden üretiliyor: bu instance
  → grubu → uygulaması → müşterisi → bu bulgu tipi (tüm sistem).
  Varsayılan en dar olan. Daha geniş bir kapsam seçildiğinde kartın
  altında "bu karar seçtiğiniz kapsamdaki TÜM aynı tip bulgulara
  uygulanır" uyarısı çıkıyor.
- "Ertelendi" ve "çözüldü_doğrulanacak" seçildiğinde ne olacağı panelde
  yazıyor (tarih gelince açılır / doğrulanamazsa açılır).

**Durum geçmişi.** Her kartta "Durum geçmişi" butonu: kim, ne zaman,
hangi durumdan hangisine, hangi notla. Otomatik geçişler "sistem" olarak
görünüyor.

**Toplu işlem.** Kartların yanındaki onay kutularıyla birden çok bulgu
seçilip tek seferde aynı duruma alınabiliyor. Seçim yapıldığında alt
çubuk beliriyor (durum + not + uygula). Toplu işlemde karar bilinçli
olarak **en dar kapsama** uygulanıyor; daha geniş kapsam tek tek ve
bilinçli seçilmeli — bu da çubukta yazılı.

**Durum filtresi.** Varsayılan olarak yalnızca **"açık"** bulgular ana
bölümlerde. Filtre çipleriyle diğer durumlar da açılabiliyor. Filtre
dışında kalan bulgular kaybolmuyor: üstte **"N bulgu gizlendi — göster"**
butonu duruyor ve açıldığında hepsi kararlarıyla birlikte listeleniyor.

**Yanlış kapatma görünürlüğü.** `verification_failed` işaretli bulgu
kırmızı kenarlı ve en görünür etiketle ("Çözüm doğrulanamadı") çıkıyor.

**Karar özeti kartta.** Açık olmayan bir bulgunun altında tek satırlık
özet: durum, not, referans ve varsa "şu tarihte geri açılır".

**Düzelenler bölümü.** Bu dönemde kapanan bulgular ayrı bir kartta
listeleniyor.

**Yönetici görünümü.** Yeni "Planlanan çalışmalar ve kabul edilen
riskler" bölümü; her madde iş etkisi ve (varsa) referansla. Yoksayılanlar
bu görünümde hiç yok.

Yetki davranışı değişmedi: viewer raporu görüntüleyip dışa aktarabiliyor,
durum değiştiremiyor (onay kutuları ve butonlar görünmüyor; backend de
403 döndürüyor).

## Faz 17 — Ek İŞ B: Öneri ve çözüm adımları standardı

dbace'te öneriler dört ayrı yerde üretiliyordu ve her biri kendi
şeklindeydi: rapor bulguları düz metin + komut listesi, dashboard kartları
başlık + adımlar + tek komut, DPA index önerisi gerekçe + DDL, tahminler
adım adım playbook. Aynı ürünün dört farklı "öneri" kavramı olması hem
kullanıcı için kafa karıştırıcıydı hem de her yerde ayrı ayrı eksik
kalıyordu — kiminde doğrulama sorgusu vardı kiminde yoktu.

**Tek yapı: `services/advice.py`.**

    Öneri: <kısa eylem>       → title (durum tespiti değil EYLEM)
    Neden                     → why (iş etkisi dahil, "yapılmazsa ne olur")
    Adımlar (numaralı)        → steps[].action
      └ komut                 → steps[].command (tam, çalıştırılabilir, kopyalanabilir)
    Dikkat                    → cautions[] (kilitleme, süre, bakım penceresi)
    Tahmini süre / Geri alma  → estimated_duration, rollback
    Doğrulama                 → verification ("düzeldi mi" sorgusu)

Öneri üretilemiyorsa `unavailable_reason` doldurulup yapı yine dönüyor —
boş bırakmak yerine NEDEN üretilemediği yazılıyor.

**Dört üretici de aynı yapıya bağlandı:**

- **Rapor bulguları** — `FindingDraft.advice` eklendi ve
  `ReportFinding.advice` (JSON) olarak saklanıyor (Supabase migration
  `20260906100000_finding_advice.sql`, DEPLOY.md 26). Bölüm yapılandırılmış
  öneri vermediyse motor mevcut `recommendation` + `commands`
  alanlarından asgari yapıyı kuruyor — böylece bölümler kademeli olarak
  zenginleşebiliyor, arayüz bu arada iki farklı şekille uğraşmıyor.
  Erişilebilirlik, bağlantı doluluğu ve cache hit bulguları tam
  (neden/adım/komut/dikkat/süre/geri alma/doğrulama) önerilerle yazıldı.
- **Dashboard** — dört öneri üreticisinin (bağlanamayan düğüm, parametre
  denetimi, ön koşullar, performans içgörüleri) hepsi `advice` alanı
  taşıyor.
- **DPA index önerisi** — `CREATE INDEX CONCURRENTLY` (kilitsiz) adımı,
  önce "aynı index zaten var mı" kontrolü, sonra `ANALYZE`; dikkat
  notlarında işlem bloğunda çalışmaması, yarıda kalırsa INVALID index
  bırakması ve disk gereksinimi; geri alma `DROP INDEX CONCURRENTLY`;
  doğrulama olarak EXPLAIN + `idx_scan` sorgusu.
- **Tahminler** — mevcut playbook sunum anında standart yapıya
  çevriliyor (veritabanına ikinci kopya yazılmıyor; iki kopya zamanla
  ayrışırdı). Her tahmin türü için ayrı doğrulama sorgusu var (disk
  boyutu, XID yaşı, bağlantı sayısı, tablo/index boyutu).

**Arayüz: tek bileşen.** Yeni `components/AdviceCard.tsx` dört yerde de
kullanılıyor. Başlık "Öneri: …" ön ekiyle, neden altında, adımlar
numaralı ve her adımın komutu kendi kopyalanabilir kutusunda, dikkat
notları ayrı blokta (tahmini süre ve geri alma dahil), doğrulama sorgusu
"uyguladıktan sonra çalıştırın" başlığıyla en altta. Öneri
üretilememişse kart nedeni gösteriyor, boş kutu değil.

Eski alanlar (`recommendation`, `commands`, `steps`, `action`, `playbook`)
API'de duruyor ve arayüz `advice` yoksa onlara düşüyor — bu değişiklikten
önce üretilmiş rapor ve snapshot kayıtları bozulmadan görüntüleniyor.

**Testler:** `tests/test_advice_standard.py` (15 test). Testler tek tek
üreticileri değil hepsinin uyduğu ORTAK sözleşmeyi kontrol ediyor
(`assert_valid_advice`): başlık boş olamaz, ya adım/neden ya da
üretilememe gerekçesi bulunmalı, adımlar eylemsiz veya boş komutlu
olamaz. Toplam: 318 test yeşil.

## Faz 17 sonrası düzeltme — canlıda `NameError: name 'AdviceOut' is not defined`

**Belirti.** Ek İŞ B commit'inden sonra canlı API import anında çöktü:
`backend/app/schemas.py` satır 668, `PredictionOut` sınıfı `AdviceOut`
tipini kullanıyor ama tanım dosyada 1344. satırda geliyordu. Aynı ileri
referans üç yerde vardı: `PredictionOut` (668), `DashboardRecommendationOut`
(815), `IndexAdviceOut` (951).

### Neden yerelde yakalanmadı — doğrulama zincirindeki boşluk

Kontrol çalıştı ama **yanlış Python sürümüyle** çalıştı:

| | Yerel | Canlı |
|---|---|---|
| Python | 3.14.6 (`backend/.venv`) | 3.12 (`deploy/onprem/Dockerfile.backend`: `FROM python:3.12-slim`) |

Python 3.14 ile gelen **PEP 649**, annotation'ları *ertelemeli*
değerlendiriyor: sınıf gövdesi çalışırken `advice: AdviceOut | None`
ifadesi hiç çalıştırılmıyor. Python ≤ 3.13 ise annotation'ı sınıf
gövdesinde **hemen** değerlendirip tanımsız isimde `NameError` atıyor.

İki kontrol de bu yüzden sessiz kaldı:

1. **`python -c "from app.main import app"`** — 3.14'te import gerçekten
   başarılı oluyor. Pydantic, çözemediği tipi görünce modeli `incomplete`
   işaretleyip hatayı yutuyor.
2. **318 test** — pydantic, eksik modeli ilk kullanımda modül
   ad-uzayından yeniden kuruyor (`AdviceOut` o an artık tanımlı), bu
   yüzden `advice` testleri dahil hepsi yeşil geçiyordu.

Ölçülen kanıt: düzeltmeden önce, import biter bitmez tam olarak
`AdviceOut`'a bakan modeller eksik kalıyordu —
`['PredictionOut', 'DashboardRecommendationOut', 'DashboardIssueOut',
'DashboardSummaryOut', 'IndexAdviceOut', 'IndexAdviceReportOut']`.

Hata, indirilen **gerçek Python 3.12.9** yorumlayıcısıyla birebir yeniden
üretildi (`NameError: name 'AdviceOut' is not defined`); aynı kaynak
3.14'te o noktaya hiç gelmiyordu.

### Düzeltme

`AdviceStepOut` ve `AdviceOut`, ilk kullanımlarından (satır 648,
`PredictionOut`) önceye taşındı ve başlarına neden orada durmaları
gerektiğini açıklayan bir yorum konuldu. Dosyanın tamamı — ve `app/`
altındaki 66 modülün hepsi — tanım sırası açısından tarandı; başka ileri
referans yok.

### Bir daha kaçmaması için: `tests/test_definition_order.py`

Mevcut duman kontrolü yanlış değildi, **yetersizdi**: yorumlayıcı
sürümüne bağımlıydı. Yeni testler sürümden bağımsız çalışıyor:

1. **AST taraması** — `app/` altındaki her dosyada, her sınıf/fonksiyon
   annotation'ı için "bu isim dosyada daha sonra mı tanımlanıyor?"
   sorusu soruluyor. Tırnaklı ileri referanslar (`"AdviceOut"`) kasıtlı
   olarak muaf; onlar zaten değerlendirilmiyor.
2. **`__pydantic_complete__` kontrolü** — import sonrası hiçbir model
   eksik kalmamalı. Bu, 3.14'te ileri referansın bıraktığı izi yakalar:
   `NameError` görünmese bile model eksik işaretlenir.
3. **Denetleyicinin kendi testi** — canlıyı düşüren kalıbın aynısı geçici
   bir dosyaya yazılıp taranıyor. Bu olmadan tarayıcı sessizce hiçbir şey
   bulmayan bir no-op'a dönüşse fark edilmezdi.

Her iki koruma da düzeltmeden önce kırmızıydı, sonra yeşil. Tarayıcı
ayrıca gerçek Python 3.12.9 ile de çalıştırılıp aynı sonucu verdiği
doğrulandı.

### Kalan risk

Yerel geliştirme (3.14) ile canlı (3.12) arasında iki minör sürüm fark
var. Yeni test bu bug sınıfını kapatıyor ama sürüm farkı başka
uyumsuzluklar da doğurabilir — kalıcı çözüm yerel sanal ortamı 3.12'ye
almak ya da CI'yı `python:3.12-slim` üzerinde koşturmak. Bu düzeltmenin
kapsamı dışında bırakıldı, notu SORULAR.md'de.

**Testler:** 387 yeşil (69'u yeni tanım sırası denetimi). Frontend build
temiz.

## Faz 18 — İŞ 1: Rapor ve DPA tek gerçeklik kaynağına bağlandı

**Bildirilen hata.** Rapor "en pahalı sorgular"da bir sorgu gösteriyor
(Supabase iç sorgusu `pg_walfile_name_offset…`, 1 çağrı, 206 ms,
"%+229"), aynı sorgu DPA'da hiç yok. Öneri "DPA'da EXPLAIN'e bakın"
diyor ama tıklanınca sorgu orada bulunamıyor.

### Kök nedenler (üç ayrı kusur)

**1. İki ayrı seçim mantığı.** Rapor dönemin TAMAMI üzerinde kümülatif
sayaç farkına bakıyordu; DPA'nın varsayılan görünümü ise yalnızca EN SON
toplama döngüsünün anlık görüntüsüydü. Collector her döngüde
pg_stat_statements'ın ilk 20 satırını sakladığı için, dönem içinde bir
kez öne çıkıp sonra listeden düşen bir sorgu raporda görünüyor ama DPA'da
görünmüyordu. Aynı veriden iki farklı gerçek.

**2. Kimlik parçalanması.** Gruplama `queryid or query` ile yapılıyordu.
pg_stat_statements ayrıcalıksız rollerde bazı satırların `queryid`
alanını NULL döndürür (bkz. Faz 16-B İŞ 1); aynı sorgunun bir örneği
queryid'li, diğeri queryid'siz gelince tek sorgu İKİ gruba bölünüyor ve
raporda **aynı sorgu iki kez** listeleniyordu — kullanıcının fark ettiği
tekrar buydu.

**3. Bulgu fingerprint'i çakışıyordu.** `str(queryid or "")[:32]`
kullanıldığı için queryid'siz TÜM sorgular aynı fingerprint'e düşüyor,
motorun tekilleştirmesi bunları birbirini eleyerek kaybediyordu.

### Yeni `services/slow_query_selection.py`

Seçim tek yere toplandı; rapor da DPA da buradan besleniyor, dolayısıyla
ayrışmaları yapısal olarak mümkün değil.

- Sıralama her zaman pencere içindeki değişime göre.
- Kimlik: `queryid` esas, ama aynı sorgu metni daha önce queryid'siz
  görülmüşse gruplar **birleştiriliyor** (metinden türeyen kararlı parmak
  izi üzerinden). Bulgu fingerprint'i de artık bu `key` ile üretiliyor.
- Pencerede tek toplama döngüsü varsa fark alınamaz; liste boş
  bırakılmıyor, kümülatif değerler `mode="snapshot"` etiketiyle
  gösteriliyor ve arayüz bunu açıkça yazıyor.

**API davranış değişikliği (bilinçli).** `GET /api/queries/{id}` aralıksız
çağrıldığında artık "son toplama döngüsü" değil **son 24 saatlik pencere**
döndürüyor ve yanıt düz liste yerine zarflanmış
(`items` + `mode` + `window_start/end` + filtrelenen sayıları). Bu, iki
görünümü tek kaynağa bağlamanın gereğiydi; kural gereği burada yazılı.

### Derin bağlantı artık hedefe iniyor

Bulgular `link_hint` taşıyor (yeni kolon + migration
`20260907090000_report_finding_link_hint.sql`, DEPLOY.md 27). Sorgu
bulgusunun bağlantısı sorgu anahtarını VE raporun kullandığı pencereyi
taşıyor; DPA o bağlantıyla açıldığında aynı pencereyi kuruyor, sorguyu
vurguluyor ve gerekiyorsa sistem sorgusu filtresini açıyor. Rapor bir
sorgudan bahsediyorsa o sorgu DPA'da mutlaka bulunuyor.

### Tüm bulgu hedeflerinin denetimi

Dokuz bölümün hedefleri tek tek kontrol edildi; **iki yanlış hedef**
bulundu ve düzeltildi:

1. **Parametre bulguları** `/instances/{id}?tab=tuning`'e gidiyordu, oysa
   parametre denetimi arayüzü GRUP sayfasında
   (`/groups/{id}?tab=parameters`); tuning sekmesinde ön koşullar ve tanı
   var, parametre yok. Artık doğru sayfaya gidiyor. Gruba bağlı olmayan
   bir instance için parametre denetimi sayfası olmadığından bağlantı
   üretilmiyor (var olmayan bir sayfaya söz vermek yerine).
2. **Kullanılmayan index bulgusu** kapsam seviyesinde tek bir bulgu
   olarak üretiliyordu ve hiçbir nesneye bağlı olmadığı için hiçbir
   sayfaya bağlantı veremiyordu. Artık instance başına üretiliyor.

Ayrıca bir **veri yeterliliği hatası** bulundu: şema bölümü, trend
GEREKTİRMEYEN "kullanılmayan index" bulgusunu da büyüme eşiğinin
(≥2 gün) arkasında tutuyordu; ilk iki gün boyunca bölüm tamamen
"bilinmiyor" dönüyor ve kullanılmayan indexler hiç raporlanmıyordu. İki
bulgu tipinin veri ihtiyacı ayrıştırıldı.

### Gürültü filtresinin temeli

Seçim servisi sistem/platform sorgularını tanıyor (pg_catalog, pg_stat_*,
pg_walfile_*, information_schema, Supabase/RDS/Cloud SQL/Azure iç
sorguları ve **dbace'in kendi toplama sorguları**) ve varsayılan olarak
eliyor; kaç sorgunun elendiği yanıtta görünüyor, arayüzden
"Sistem sorgularını göster" ile açılabiliyor. Bulgu üretimi için ayrıca
mutlak eşikler kondu (en az 5 çağrı, en az 1000 ms) — bildirilen
örnekteki tek çağrılık 206 ms'lik sorgu artık bulgu üretmiyor. (Eşiklerin
ayarlanabilir hale gelmesi İŞ 2'de.)

**Testler:** `tests/test_report_dpa_consistency.py` (17 test) — bildirilen
senaryonun birebir regresyonu, her bulgu bağlantısının gerçekten bir
sorguya inmesi, rapor ve DPA'nın aynı pencerede aynı sırayı vermesi,
kimlik parçalanmasının giderilmesi, sistem sorgusu sınıflandırması ve tek
çağrılık sorgunun bulgu üretmemesi. Toplam: 407 test yeşil.

## Faz 18 — İŞ 2: Gürültü filtresi ayarlanabilir hale getirildi

İŞ 1'de filtre mekanizması kuruldu ama eşikler koda gömülüydü. Gömülü bir
eşik her ortam için doğru olamaz: OLTP bir veritabanında 1 saniyelik bir
sorgu ciddi, raporlama veritabanında sıradan.

**Yeni `services/noise_settings.py`.** Beş ayar, `AppSetting` üzerinde
saklanıyor:

| Ayar | Varsayılan | Ne yapar |
|---|---|---|
| `list_min_total_ms` | 100 ms | Altındaki sorgu listede görünmez |
| `list_min_calls` | 1 | Aynı, çağrı sayısı için |
| `finding_min_total_ms` | 1000 ms | Altındaki sorgu **bulgu üretmez** |
| `finding_min_calls` | 5 | Tek çağrılık sorgu trend bulgusu üretmez |
| `show_system_queries` | kapalı | Sistem/platform sorgularının görünürlüğü |

**İki ayrı eşik kümesi olması bilinçli.** Liste ve bulgu farklı sorular
soruyor: liste "bu sorgu en pahalı N'de görünmeye değer mi?" (keşif
aracı, düşük eşik), bulgu ise "DBA'nın bugün buna bakması gerekir mi?"
(dikkat talebi, yüksek eşik). Bildirilen örnekteki 1 çağrılık 206 ms'lik
sorgu artık listede görünebilir ama bulgu üretmez.

**Rapor ve DPA aynı ayarı okuyor.** İkisinin farklı eşik kullanması,
İŞ 1'de düzelttiğimiz tutarsızlığın aynısını geri getirirdi — bunu bir
test doğruluyor.

**Sistem sorgusu görünürlüğü iki katmanlı.** Yönetim ayarı varsayılanı
belirliyor; `?include_system=true` sorgu parametresi tek seferlik
geçersiz kılıyor. Rapordan gelen derin bağlantı bir sistem sorgusuna
işaret ediyorsa DPA filtreyi kendiliğinden açıyor — aksi halde bağlantı
gene boş sayfaya çıkardı.

**Filtrelenen sorgu sayısı hep görünür.** Yanıt zarfı
`filtered_system` ve `filtered_insignificant` taşıyor; DPA'da
"N sistem/platform sorgusu · M eşik altı sorgu filtrelendi" yazıyor ve
yanında "Sistem sorgularını göster" onay kutusu var. Bir şeyin gizlendiği
asla gizli değil.

**Bozuk ayar raporu düşürmüyor.** Okunamayan/geçersiz bir kayıt sessizce
varsayılana düşüyor: ayar hatası yüzünden rapor üretiminin çökmesi,
yanlış eşikle çalışmaktan daha kötü olurdu. Kaydetme tarafında ise
değerler doğrulanıyor (negatif ya da saçma büyük değer kabul edilmiyor).

**Uçlar:** `GET/PUT /api/admin/noise-settings` (viewer değiştiremez).
Yönetim ekranının Ayarlar sekmesine "Gürültü filtresi" paneli eklendi;
her alanın altında ne işe yaradığı yazılı.

**Testler:** `tests/test_noise_settings.py` (8 test) — varsayılanlar,
liste eşiğinin gerçekten filtrelemesi, bulgu eşiği yükseltilince bulgunun
susup sorgunun listede kalması, rapor ve DPA'nın aynı ayarı okuması,
sistem sorgusu ayarının varsayılan görünümü değiştirmesi, sorgu
parametresinin ayarı geçersiz kılması, uç doğrulaması ve viewer yetkisi.
Toplam: 416 test yeşil.

## Faz 18 — İŞ 3: Öneri geçerliliği

Rapor "DPA'da EXPLAIN'e bakın" diyordu ama EXPLAIN'in o sorgu için
alınabilir olup olmadığını hiç kontrol etmiyordu. Bir UPDATE/INSERT ya da
çoklu statement için EXPLAIN reddedilir — kullanıcı bağlantıya tıklayıp
boşa gidiyordu.

**Yönlendirmeden önce doğrulama.** `validate_explainable` (DPA'nın EXPLAIN
ucunun kullandığı kontrolün aynısı) canlı bağlantı gerektirmiyor; rapor
kendi kuralını çiğnemeden (canlı probe yok) yönlendirmenin geçerli olup
olmadığını bilebiliyor. Geçerliyse öneri EXPLAIN adımlarını veriyor;
değilse **"şu yüzden öneremiyorum"** biçiminde nedeni yazıp yerine
uygulanabilir bir alternatif sunuyor (çağrı sıklığını azaltmak, hedef
tablodaki index sayısını gözden geçirmek). Boş yönlendirme yok.

**Sınırlılıklar artık ayrı ve kısa bir not.** "Darboğaz belirlenemedi
çünkü exec_user_time verisi yok" gibi cümleler bulgu metninin içine
gömülüydü ve asıl bulgunun önüne geçiyordu. Yeni `note` alanı
(kolon + migration `20260907100000_report_finding_note.sql`, DEPLOY.md 28)
bunları ayırıyor; arayüzde sönük, italik ve kenar çizgili küçük bir satır
olarak, bulgunun altında duruyor. Nota taşınanlar:

- Darboğaz sınıfının kesin olmaması (ölçüm verisi eksikse).
- Pencerede tek toplama döngüsü olması (değerler fark değil kümülatif).
- EXPLAIN'in o sorgu için alınamaması.

Bulgu metni böylece kısa ve yapılandırılmış kalıyor; bir test detay
metninin 240 karakteri geçmediğini ve sınırlılık cümlelerinin oraya
sızmadığını doğruluyor.

**Testler:** `tests/test_advice_validity.py` (10 test) — EXPLAIN
uygulanabilirliği tespiti, uygulanabilirken doğru yönlendirme,
uygulanamazken gerekçe + alternatif, sınırlılığın nota taşınması, ve
"bir sayfaya yönlendiren her bulgunun orada ne yapılacağını da söylemesi"
kuralı. Toplam: 426 test yeşil.

## Faz 18 — İŞ 4: Rapor okunabilirliği

**Bulgu metni parçalara ayrıldı.** Eskiden tek uzun paragraftı ve sayılar
cümlenin içinde kayboluyordu. Yeni `facts` alanı (kolon + migration
`20260907110000_report_finding_facts.sql`, DEPLOY.md 29) dört parçalı
yapıyı kuruyor:

| Parça | Nerede |
|---|---|
| Ne oldu | `detail` — tek kısa cümle |
| Ne kadar / neye göre | `facts` — etiketli satırlar |
| Sınırlılık | `note` (İŞ 3) — sönük, ayrı |
| Ne yapmalı | `advice` (Ek İŞ B) |

Yavaş sorgu, erişilebilirlik, bağlantı doluluğu ve cache hit bulguları bu
yapıya geçti. Örnek: eskiden "Dönemde 40 çağrı, toplam 8000 ms, ortalama
200 ms (önceki döneme göre %+229). Darboğaz: …" tek cümlesiydi; şimdi
"Pahalı sorgu kötüleşti." cümlesi + Toplam süre / Çağrı / Ortalama /
Darboğaz / Önceki döneme göre etiketli satırları.

**Önemli sayılar vurgulanıyor.** Her `fact` bir `tone` taşıyor
(neutral/good/bad); arayüzde büyük, kalın ve `tabular-nums` ile hizalı
gösteriliyor, kötü yönde olanlar kırmızıya çalıyor.

**Sorgu metni kesme düzeltildi.** `_short_query` artık kelime sınırında
kesiyor — eskiden tanımlayıcının ortasından kesip okunmaz hale
getirebiliyordu. Tam metin `evidence["query"]` içinde korunuyor ve
arayüzde "Tam sorgu metni" katlanabilir alanında; bilgi kaybı yok.

**Kanıt satırı kompaktlaştı.** Ölçülen değer artık `facts` içinde vurgulu
gösterildiği için kanıt satırında TEKRARLANMIYOR; satır yalnızca kaynak
bilgisini (hangi metrik, hangi eşik, ne zaman ölçüldü) taşıyor ve daha
küçük/soluk bir stille bulgunun önüne geçmiyor. Kanıt zorunluluğu (Faz 17
İŞ 6) korunuyor — yalnızca sunumu değişti.

**Aynı bulgunun iki kez listelenmesi.** Kök neden İŞ 1'de bulunup
düzeltildi: `queryid` bazı satırlarda NULL geldiği için tek sorgu iki
kimliğe bölünüyordu. Bu işte tekillik hem veri tablosu hem bulgu listesi
için testle korunuyor; ayrıca tekilleştirmenin fazla agresif olmadığı
(gerçekten farklı iki sorgunun iki bulgu ürettiği) da doğrulanıyor.

**Testler:** `tests/test_report_readability.py` (10 test). Toplam: 436
test yeşil.

## Faz 18 — İŞ 5: Bölüm bölüm doğruluk denetimi

12 bölümün ürettiği her bulgu tipi için dört soru soruldu: **(a)** veri
kaynağı doğru mu, **(b)** eşik mantıklı mı, **(c)** işaret ettiği hedef
sayfada var mı, **(d)** önerisi uygulanabilir mi.

### Bulunan ve düzeltilen hatalar

**A. "Hiç metrik toplanmamış" bulgusu hiç üretilmiyordu (a).** Kod
`if first_sample is None: … continue` yazıyor, ardından AYNI koşulu
tekrar kontrol edip bulguyu üretmeye çalışıyordu — yani bulgu ulaşılamaz
koddaydı. Sonuç: etkin ama hiç veri gelmeyen bir instance raporda
sessizce görünmüyordu, ki bu raporun söylemesi gereken en temel şey.
Bulgu `continue`'dan öncesine taşındı ve kritik olarak işaretlendi;
kapalı (disabled) instance'lar için üretilmiyor (oradan veri gelmemesi
beklenen durum).

**B. `temp_bytes` kümülatif sayaç, gauge gibi kullanılıyordu (a + b).**
Bölüm `max(temp_bytes)` alıyordu; kümülatif bir sayaçta bu yalnızca "son
değer" demek. Yani geçmişte bir kez geçici dosya kullanmış her veritabanı
**sonsuza kadar** bu bulguyu üretiyordu — kalıcı yanlış pozitif. Artık
dönem farkı alınıyor (checkpoint'lerde zaten yapıldığı gibi), sayaç
sıfırlaması ele alınıyor ve 1 MB'lık bir eşik kondu; birkaç kilobayt her
veritabanında olur.

**C. Tek ölçümlük "servis down" bulgu üretiyordu (b).** 15 saniyelik bir
probe hıçkırığı gerçek bir kesinti değil. Artık en az iki ardışık "down"
ölçümü isteniyor.

**D. Grup cluster bulguları dönemsel değil, anlık (a).**
`GroupHealthSnapshot` grup başına TEK satır tutuyor (son kontrolün
sonucu). Split-brain, etcd quorum ve DR düğümü bulguları bu yüzden
"dönem boyunca izlenmiş" bir ölçüm değil. Bunu gizlemek yerine bulgunun
notuna yazdık; anlık görüntü rapor döneminin DIŞINDAysa tarihiyle
birlikte ayrıca belirtiliyor.

(İŞ 1'de bulunan iki yanlış hedef — parametre bulgularının instance
tuning sekmesine gitmesi, kullanılmayan index bulgusunun hiçbir nesneye
bağlı olmaması — ve şema bölümündeki veri yeterliliği hatası orada
düzeltilmişti.)

### Denetimden temiz geçen bulgu tipleri

| Bölüm | Bulgu | Not |
|---|---|---|
| Erişilebilirlik | toplama boşluğu | Eşik: toplama aralığının 3 katı, min 60 sn |
| Cluster | lider değişimi, lidersizlik | Anlık görüntü geçmişinden, dönemsel ✓ |
| Performans | yavaş sorgu | İŞ 1-4'te elden geçti |
| Kaynak | bağlantı zirvesi, cache hit | Gauge metrikler, doğru kullanım |
| Kaynak | checkpoint baskısı | Zaten dönem farkı alıyordu ✓ |
| Şema | büyüme, kullanılmayan index | İŞ 1'de eşik ayrıştırıldı |
| Alarmlar | gürültülü kural, uzun süre açık | Eşik 10 tetikleme, hedef /alerts ✓ |
| Kapasite | tahmin riskleri | PredictionInsight, güven aralığıyla ✓ |
| Parametreler | sapma, değişiklik | İŞ 1'de hedef düzeltildi |
| Ön koşullar | eksik kontrol | Yoksayılanlar ayrı, `unknown` eksiklik sayılmıyor ✓ |

### Düzeltilmeyenler

Kalan iki sınırlılık SORULAR.md'ye yazıldı: şema bölümünün günlük
fotoğrafa dayanması (canlı Şema sekmesiyle gün içinde ayrışabilir) ve
sistem sorgusu tespitinin desen tabanlı olması.

**Testler:** `tests/test_report_audit.py` (9 test) — dört hatanın her biri
için hem hatalı davranışın döndüğünü hem de düzeltmenin fazla agresif
olmadığını (kapalı instance bulgu üretmiyor, gerçek temp kullanımı hâlâ
yakalanıyor, tekrarlayan down hâlâ bulgu) doğruluyor. Toplam: 445 test
yeşil.

## Faz 19 — İŞ 1: 404'ler ve sayfa patlamaları

Bildirilen belirti tekti — "ayrıntılara girerken sayfalar patlıyor, geri
dönerken sık sık 404 çıkıyor" — ama altından birbirinden bağımsız yedi
kırık akış çıktı. Her biri kod üzerinden kanıtlandı, tahmine dayanan
düzeltme yapılmadı.

### Kırık akışlar — "şu yoldan şuraya giderken patlıyordu"

**1. Tanımsız herhangi bir adrese giderken bomboş ekran çıkıyordu.**
`App.tsx` içindeki `<Routes>` bloğunda catch-all (`path="*"`) yoktu. Bir
rota eşleşmediğinde React Router HİÇBİR ŞEY render etmiyor; kullanıcı
kenar çubuğunun yanında bomboş bir içerik alanı görüyordu. Eski bir yer
imi, silinmiş bir kaydın paylaşılmış bağlantısı ya da elle yazılan bir
adres bu hâle düşüyordu — "404 çıkıyor" şikâyetinin görünen yüzü buydu.
Artık `NotFoundPage` çıkıyor: hangi adresin bulunamadığı, neden ve iki
geri dönüş bağlantısı.

**2. Rapordaki bir bulgudan instance detayına giderken, instance
silinmişse sayfa sonsuza kadar "Yükleniyor…" kalıyordu.**
`InstanceDetailPage` yükleme hatasını yalnızca `<div className="error">`
ile gösteriyordu ve `/instances/abc` gibi sayısal olmayan bir adreste
`Number(id)` NaN verdiği için efekt `if (!instanceId) return` ile sessizce
çıkıyor, hiçbir zaman yüklenmiyordu. Artık geçersiz numara da silinmiş
kayıt da (HTTP 404) "Instance bulunamadı" ekranına düşüyor ve "Instance
listesine dön" bağlantısı veriyor.

**3. Instance detayı, ilgisiz bir uç patladığında hiç açılmıyordu.**
Instance, metrikler ve özetler tek bir `Promise.all` içindeydi: metrik ya
da özet ucu 500 dönerse instance başarıyla gelmiş olmasına rağmen sayfa
hiç render edilmiyordu. Artık instance ZORUNLU, diğer ikisi
`Promise.allSettled` ile isteğe bağlı — biri düşse de sayfa açılıyor.

**4. Grup detayından bir düğüme ya da geri giderken, grup silinmişse
"Database Group" başlıklı boş bir iskelet çıkıyordu.**
`GroupDetailPage`'de hiçbir yükleme/bulunamadı koruması yoktu: `group`
null iken tüm sayfa `group?.` ile render ediliyor, tepede ham hata metni
duruyordu. Geri bağlantısı da `application` yüklenemediği için hiç
görünmüyordu — sayfadan çıkış yolu kalmıyordu. Artık üç ayrı durum var:
bulunamadı, hata (tekrar dene), yükleniyor.

**5. Uygulama grupları sayfasında uygulama silinmişse geri dönüş
bağlantısı kayboluyordu.**
`DatabaseGroupsPage`, `api.getApplication(id)` hatasını `.catch(() =>
undefined)` ile SESSİZCE yutuyordu. Sonuç: sayfa açılıyor ama başlıkta
uygulama adı ve "← Uygulamalar" bağlantısı yok; kullanıcı çıkamadığı boş
bir sayfada kalıyordu. Aynı sessiz yutma `DatabaseWizardPage`'in
create-group modunda daha kötüydü: kullanıcı sihirbazın TAMAMINI
dolduruyor, ancak kaydederken patlıyordu.

**6. Kenar çubuğu ağacında bir dalı açarken API düşerse dal sonsuza kadar
"Yükleniyor…" kalıyordu.** `NavTreeBranch.toggle` ve `MainNavTree.loadRoots`
içinde `catch` yoktu — konsola yakalanmamış bir promise reddi düşüyor,
kullanıcıya hiçbir şey söylenmiyordu. Artık "Yüklenemedi — tekrar dene"
çıkıyor ve tıklanınca gerçekten yeniden deniyor.

**7. Dashboard'dan bir rapora giderken, rapor silinmişse sayfanın tepesinde
ham JSON hata metni beliriyordu.** `/reports?report=<id>` ile gelinen
silinmiş/saklama süresi dolmuş bir rapor 404 dönüyordu ve ekranda
`{"detail":"Rapor bulunamadı"}` görünüyordu. Ayrıca deep-link parametresi
okunduktan hemen sonra URL'den SİLİNİYORDU: seçili rapor adreste
kalmadığı için sayfa yenilendiğinde kayboluyor, bağlantı paylaşılamıyor,
geri düğmesi raporlar arasında gezinmiyordu.

### Hata sınırı (error boundary)

Kod tabanında hiç hata sınırı yoktu (`componentDidCatch` /
`getDerivedStateFromError` araması boş dönüyordu). Render sırasında
fırlayan bir hata React'in TÜM ağacı sökmesine yol açıyordu — beyaz ekran,
kenar çubuğu dahil. İki sınır eklendi:

- `main.tsx` — en dışta: oturum sağlayıcı, kenar çubuğu, gezinme ağacı.
- `App.tsx` — `<Routes>` çevresinde, `resetKey={location.pathname}` ile:
  bir sayfa patladıktan sonra kenar çubuğundan başka bir sayfaya
  geçildiğinde eski hata ekranda kalmıyor.

Her ikisi de hata mesajını, "Tekrar dene" ve "Sayfayı yenile"
düğmelerini gösteriyor; bileşen yığını konsola bırakılıyor.

Not: Hata sınırı yalnızca RENDER sırasındaki hataları yakalar. `await`
sonrası fırlayan hatalar için sayfalar `catch` + `PageError` kullanıyor —
o da aynı "Tekrar dene" düğmesini veriyor.

### `ApiError`: sayfalar artık 404'ü 500'den ayırabiliyor

Asıl yapısal eksik buydu. `api.ts`'teki `request()` düz bir `Error`
fırlatıyor ve mesaj olarak yanıt gövdesini OLDUĞU GİBİ veriyordu. İki
sonucu vardı: (a) hiçbir sayfa "silinmiş kayıt" ile "sunucu hatası"
arasında ayrım yapamıyordu, dolayısıyla "bulunamadı" ekranı yazılamazdı;
(b) kullanıcı ekranda ham JSON görüyordu. Artık:

- `ApiError` sınıfı `status` ve `path` taşıyor; `isNotFound` /
  `isForbidden` / `isBadRequest` yardımcıları var.
- FastAPI'nin `{"detail": …}` gövdesi (doğrulama hatalarındaki liste hâli
  dahil) okunabilir bir mesaja çevriliyor.
- Ağ seviyesinde düşen istek `status: 0` ile işaretleniyor — çağıran
  taraf bunu 404 sanıp "bulunamadı" göstermesin; "Sunucuya ulaşılamadı"
  diyor.

### Sekme ve seçim durumu URL'de

Geri/ileri düğmesi üç sayfada sekmeyi geri almıyordu: `AdminPage` ve
`AlertsPage` sekmeyi hiç URL'e yazmıyordu; `GroupDetailPage` ve
`InstanceDetailPage` yazıyordu ama `replace: true` ile — yani geçmişe bir
adım eklenmediği için geri basınca kullanıcı sekmeye değil bir önceki
SAYFAYA fırlıyordu. Ortak `useUrlTab` hook'u eklendi (sekme değişimi
`push`, filtre değişimi `replace` — her tuş vuruşunu geçmişe yazmak geri
düğmesini kullanılamaz hâle getirir). `ReportsPage`'de seçili rapor
(`?report=`) ve görünüm (`?view=`) de aynı şekilde adreste tutuluyor.

### Backend: 404 dönen uçların denetimi

71 `HTTPException(404)` çağrısının hepsi gözden geçirildi. Üç soru:
yetkisizlik 404 ile maskeleniyor mu, parametre hatası 404'e mi düşüyor,
404 gerçekten "kayıt yok" mu?

- **Yetki temiz.** `get_current_user` 401, `require_admin` ve
  `require_write_access` 403 dönüyor; hiçbir uç yetkisizliği 404 ile
  gizlemiyor.
- **Parametre hatası temiz.** `/api/instances/abc` FastAPI'nin yol
  doğrulamasıyla 422 dönüyor, 404 değil.
- **İki uç yanlıştı** — aşağıda.

### Bilerek kırılan API davranışı (proje kuralı gereği yazılıyor)

İki uç, kaydın KENDİSİ dururken yalnızca alt koleksiyon boş olduğu için
404 dönüyordu. İstemci bunu silinmiş bir kayıttan ayıramadığı için yeni
eklenmiş, henüz veri toplanmamış bir instance "bulunamadı" gibi
görünüyordu:

- `GET /api/metrics/{id}/latest` — eskiden 404 `"No metrics collected
  yet"`. Artık **200 + `null`**. (`api.ts`'te dönüş tipi
  `MetricSample | null` oldu; bu uç frontend'de henüz kullanılmıyor.)
- `GET /api/queries/{id}/history/{queryid}` — eskiden 404 `"No history
  for this queryid"`. Artık **200 + boş seri** (`points: []`). Sorgunun o
  pencerede örneği olmaması bir hata değil.

Her ikisinde de instance gerçekten yoksa 404 dönmeye devam ediyor;
mesajlar Türkçeleştirildi (`"Instance bulunamadı"`).

### Ortak durum bileşenleri

`components/PageState.tsx`: `PageLoading`, `PageError` (hata tipine göre
başlık + "Tekrar dene"), `NotFoundState` (ne aranıyordu, neden bulunamamış
olabilir, nereye dönülür), `EmptyState`. Öncesinde bu üç durum her sayfada
farklı görünüyordu; bazılarında hiç yoktu.

**Testler:** `tests/test_navigation_integrity.py` (76 test) — rota
tablosunu `App.tsx`'ten okuyup her frontend bağlantı hedefini ve her
backend `link_hint`'ini ona karşı doğruluyor; ayrıca catch-all'un
varlığını, `<Routes>`'un hata sınırıyla sarıldığını, sekmeli her sayfanın
sekmeyi URL'de tuttuğunu ve kayıt yükleyen her sayfanın "bulunamadı"
durumunu ele aldığını kontrol ediyor. `tests/test_endpoint_status_codes.py`
(7 test) — yukarıdaki üç denetim sorusunu kalıcı olarak kapatıyor. Toplam:
528 test yeşil, `npm run build` yeşil.

## Faz 19 — İŞ 2: Uçtan uca gezinme denetimi

Tarayıcı otomasyonu yoktu; denetim kaynak kod üzerinden ve **kanıtla**
yapıldı: rota tablosu `App.tsx`'ten okunup her bağlantı hedefiyle
eşleştirildi, her sayfanın yükleme/hata/boş yolları tek tek izlendi.
Sonuçlar kalıcı testlere çevrildi, böylece denetim bir kereye mahsus
kalmıyor.

### Bağlantı hedefleri — temiz

17 sayfa, 16 rota. Frontend'deki 49 benzersiz bağlantı hedefi (`to=`,
`navigate()` ve `deepLink` gibi yol kuran literaller) ile backend'in
ürettiği tüm derin bağlantılar (`report_sections.py`, `dashboard.py`,
`dashboard_snapshot.py`) tanımlı rotalara uyuyor. **Var olmayan bir
rotaya giden bağlantı bulunamadı** — yani İŞ 1'de görülen 404'ler kırık
linklerden değil, catch-all/bulunamadı/koruma eksikliğindendi.

Bu artık `tests/test_navigation_integrity.py` (82 test) ile kilitli.

### Bulunan ve düzeltilen hatalar

**A. "Veri yok" ile "yüklenemedi" aynı görünüyordu — 9 sayfa.**
Liste sayfalarının hepsi boş tabloya `<td className="empty">Kayıt yok</td>`
basıyordu ve bunu YÜKLEME BAŞARISIZ OLDUĞUNDA DA basıyordu. Yani API
düştüğünde kullanıcı "Kayıtlı instance yok" / "Açık tahmin yok" / "Aktif
alarm yok" okuyup gerçekten kayıt olmadığına inanıyordu. Bir izleme
aracında bu sessiz yanlış bilgilendirmedir: DBA "alarm yok" görüp rahatlar.
Ortak `TableState` bileşeni üç durumu ayırıyor (yükleniyor / hata + tekrar
dene / gerçekten boş). Düzeltilen sayfalar: Customers, Applications,
DatabaseGroups, Servers, Instances, Alerts (üç tablo), Admin, Predictions.

**B. Yazma işlemleri sessizce başarısız oluyordu — 6 akış.**
Silme ve kabul çağrılarında `catch` yoktu: işlem reddedilirse (başka
sekmede zaten silinmiş, yetki, sunucu hatası, ağ kopması) kullanıcıya
HİÇBİR ŞEY söylenmiyor, satır yerinde kalıyordu; hata yalnızca tarayıcı
konsoluna yakalanmamış bir promise reddi olarak düşüyordu. Kullanıcı
açısından düğme "çalışmıyor" gibi görünüyordu. Düzeltilenler:

- Müşteri silme (`CustomersPage`)
- Uygulama silme (`ApplicationsPage`)
- Grup silme (`DatabaseGroupsPage`)
- Düğüm silme (`GroupDetailPage`)
- Tahmin kabul etme (`PredictionsPage`)
- **Bulgu durumu değiştirme (`ReportsPage.applyStatus`)** — bunu denetim
  testi buldu, gözle taramada kaçmıştı. Durum değişikliği reddedildiğinde
  panel açık kalıyor ama hiçbir mesaj çıkmıyordu. Artık mesaj gösterilip
  hata yeniden fırlatılıyor, böylece panel açık kalıyor ve kullanıcı
  düzeltip tekrar deneyebiliyor.

**C. Geri dönüş bağlantısı koşullu olduğu için kaybolabiliyordu — 2 sayfa.**
`GroupDetailPage` ve `DatabaseWizardPage`'de "← Uygulama" bağlantısı
yalnızca üst kayıt yüklenebildiğinde render ediliyordu. O çağrı sessizce
başarısız olursa (İŞ 1'de düzeltilen sessiz `catch`'ler) sayfadan çıkış
yolu kalmıyordu. Artık koşulsuz: üst kayıt bilinmiyorsa bir üst seviyeye
(`/customers`) dönülüyor.

**D. Dashboard durum filtresi adreste tutulmuyordu.**
Bir durum kartına tıklayıp ("kritik gruplar") sonra geri basmak filtreyi
kaldırmak yerine kullanıcıyı sayfadan atıyordu; filtrelenmiş görünüm
paylaşılamıyordu da. Artık `?status=` ile adreste.

**E. Dashboard hata kutularında tekrar deneme yoktu.** Sağlık özeti
yüklenemediğinde ham hata metni beliriyor, kullanıcının tek çaresi sayfayı
yenilemekti. Artık "Tekrar dene" düğmesi var.

### Sayfa sayfa denetim sonucu

| Sayfa | Açılıyor | Veri yok | Hata | Geri dönüş |
|---|---|---|---|---|
| Dashboard | ✓ | bilgilendirici boş kart | PageError + tekrar dene (E) | kenar çubuğu |
| Instances | ✓ | TableState (A) | TableState + tekrar dene | kenar çubuğu |
| Instance detay | ✓ | sekme bazlı | bulunamadı/hata ekranı (İŞ 1) | ✓ gruba/dashboard'a |
| Reports | ✓ | "rapor yok" ayrımı (İŞ 1) | PageError + tekrar dene | kenar çubuğu |
| Predictions | ✓ | TableState (A) | TableState + tekrar dene | kenar çubuğu |
| Alerts | ✓ | 3 tablo, TableState (A) | TableState + tekrar dene | kenar çubuğu |
| Alarm kuralı formu | ✓ | — | hata kutusu | ✓ |
| Admin | ✓ | TableState (A) | hata kutusu | kenar çubuğu |
| Customers | ✓ | TableState (A) | TableState + tekrar dene | kenar çubuğu |
| Applications | ✓ | TableState (A) | bulunamadı/hata (İŞ 1) | ✓ |
| Servers | ✓ | TableState (A) | bulunamadı/hata (İŞ 1) | ✓ |
| Database groups | ✓ | TableState (A) | bulunamadı/hata (İŞ 1) | ✓ (C) |
| Group detay | ✓ | düğüm listesi | bulunamadı/hata (İŞ 1) | ✓ (C) |
| Sihirbaz | ✓ | — | bulunamadı/hata (İŞ 1) | ✓ (C) |
| Login / şifre değiştirme | ✓ | — | hata kutusu | oturum akışı |
| Bulunamadı | ✓ | — | — | ✓ (İŞ 1) |

**Testler:** `tests/test_frontend_state_handling.py` (55 test) — her sayfa
için "elle yazılmış boş tablo satırı yok" ve her sayfa/bileşen için "yazma
çağrısı varsa hatası yakalanıyor" denetimi. Bu ikinci test yukarıdaki
`applyStatus` hatasını gözle taramanın kaçırdığı yerde yakaladı.
`tests/test_navigation_integrity.py` dashboard bağlantı kaynaklarını da
kapsayacak şekilde genişletildi (82 test). Toplam: 589 test yeşil,
`npm run build` yeşil.

## Faz 19 — İŞ 3: Arayüz tutarlılığı

### Aynı işlev, aynı görünüm

İŞ 1 ve İŞ 2'de kurulan ortak bileşenler (`PageLoading`, `PageError`,
`NotFoundState`, `EmptyState`, `TableState`) bu turda kalan yerlere de
uygulandı. Öncesinde aynı üç durum sayfadan sayfaya farklı görünüyordu:
kimi yerde `<div className="error">`, kimi yerde `<td className="empty">`,
kimi yerde hiçbir şey.

**Yetki reddi ekranları.** `AdminPage` ve `CustomAlertRuleFormPage`, viewer
rolündeki bir kullanıcıya çıplak bir hata kutusu gösteriyordu — geri dönüş
yolu yoktu, kullanıcı çıkamadığı bir sayfada kalıyordu. Artık üçü de
(sihirbaz dahil) aynı ekranı kullanıyor: ne olduğu, neden ve nereye
dönüleceği.

### Bilgilendirici boş durumlar

Boş liste ekranları "neden boş" ve "ne yapılmalı" söylüyor artık. Örnekler:

- "Açık tahmin yok" → *"Tahminler geçmiş metriklerin trendinden üretilir;
  yeterli örnek biriktikçe burada görünür. Yukarıdaki hazırlık paneli hangi
  metriğin ne kadar veriye ihtiyacı olduğunu gösterir."*
- "Kayıtlı uygulama yok" → uygulamanın ne işe yaradığı + ekleme yolu (ve
  viewer rolündeyse bunun admin gerektirdiği).
- "Bu grupta düğüm yok" → standalone/cluster durumuna göre farklı metin,
  cluster ise doğrudan sihirbaz düğmesi.
- "Bu instance için alarm kuralı yok" → varsayılan kuralların ne zaman
  oluştuğu + kural ekleme bağlantısı.

### Uzun listeler

Hiçbir listede sayfalama yoktu; her şey tek seferde render ediliyordu.
`/api/instances` sunucuda sınırsız ve bir bankada birkaç yüz kayıt olması
normal; her satır kendi düğmeleri ve rozetleriyle geldiği için bu binlerce
DOM düğümü demek.

- `usePagination` + `Pagination` (25/50/100, "X–Y / Z") → Instances,
  Alerts (aktif / kurallar / geçmiş, üç tablo da).
- `useShowMore` + `ShowMoreButton` → rapor bölümlerindeki bulgular. Burada
  sayfalama yanlış olurdu: bulgular baştan sona okunur, sayfalara bölmek
  okumayı bozar. Önce 10 bulgu, gerisi "daha göster" ile.
- Tek sayfaya sığan listelerde sayfalama çubuğu hiç görünmüyor (gereksiz
  gürültü olmasın).

**Dürüstlük notu — bu istemci tarafı sayfalama.** Sunucu hâlâ tüm satırları
tek yanıtta gönderiyor. Asıl darboğaz olan render maliyetini çözüyor,
ağ/bellek maliyetini çözmüyor. Sunucu tarafı sayfalama birçok ucun
sözleşmesini değiştirir; SORULAR.md'ye yazıldı.

**Sessiz kesme düzeltildi.** `GET /api/alerts/events` yanıtı sunucuda 100
kayıtta kesiliyor. Frontend bunu "geçmişin tamamı" gibi gösteriyordu; artık
liste kesilmişse bunu açıkça yazıyor.

### Dar ekran / tablet

Tek kırılma noktası 800px'ti ve altında kenar çubuğu `min-height: 100vh`
ile TÜM ilk ekranı kaplıyordu: kullanıcı uygulamayı tablette açtığında
içerik ekranın tamamen altında kalıyordu. Ayrıca `.main { overflow-x:
hidden }` taşan içeriği kaydırılabilir yapmak yerine KESİYORDU — dar
ekranda geniş bir araç çubuğunun ya da tablonun sağ tarafına hiç
ulaşılamıyordu.

- `.main` artık `overflow-x: auto` — taşan içerik kesilmiyor, erişilebilir.
- Sekme şeritleri (`.detail-tabs`) alt alta kırılmak yerine yatay kayıyor.
- **1024px (yatay tablet):** kenar çubuğu 200px'e iniyor, iç boşluklar
  daralıyor, rapor düzeni sıkışıyor.
- **820px (dikey tablet):** tek sütun; kenar çubuğu `min-height: auto` ve
  gezinme bağlantıları yatay sarmalı bir şeride dönüşüyor; rapor geçmişi
  yapışkan olmaktan çıkıyor; sayfa başlıkları sarıyor.
- **560px (telefon):** iki sütunlu kutucuk ızgarası, küçültülmüş tablo
  yazısı, ortalanmış sayfalama.

**Testler:** `tests/test_ui_consistency.py` (10 test) — uzun listelerin
sınırlandığını VE dilimin gerçekten render edildiğini (sayfalama kurup
listenin tamamını basmak hiçbir işe yaramaz), yetki ekranlarının çıkış yolu
verdiğini, tablet kırılma noktalarının ve `.main` taşma davranışının
yerinde olduğunu doğruluyor. Toplam: 600 test yeşil (1 atlandı),
`npm run build` yeşil.

## Faz 20 — İŞ 1: Tahminlerde aksiyon komutları

### Kök neden: alan şemada vardı, modelde yoktu

Şikâyet "öneriler var ama çalıştırılacak komutlar yok" idi. Sebep tek bir
sessiz kopukluktu: `PredictionOut.advice` Faz 17 Ek İŞ B'de şemaya
eklenmiş, ama `PredictionInsight` MODELİNDE karşılığı hiç yazılmamıştı.
Pydantic eksik alanı hata vermeden `None` bırakıyor, dolayısıyla API her
tahmin için `advice: null` dönüyordu. Arayüz de bunu görüp standart öneri
kartı yerine tek cümlelik `recommendation` metnine düşüyordu:

```tsx
{p.advice ? <AdviceCard … />        // hiç çalışmıyordu
 : p.recommendation ? <RecommendationHeader … />   // hep buraya düşüyordu
```

Yani `AdviceCard` bileşeni, `advice.py`'deki standart yapı ve
`prediction_playbooks.py`'deki adım adım planlar zaten vardı — sadece
birbirine bağlı değildi. Tahminler, ürünün geri kalanının (rapor,
dashboard, DPA) kullandığı beş parçalı standardın dışında kalmıştı.

### Yapılan

`services/prediction_advice.py` — her tahmin türünü tam standarda çeviren
yeni modül. `advice` artık `prediction_insights` tablosunda bir kolon ve
tahmin ÜRETİLDİĞİ anda dolduruluyor (playbook'ta olduğu gibi), böylece
tahmin geçmişi kendi önerisini taşıyor.

Beş türün her biri için beş parça:

| Tür | Başlık (eylem) | Komutlu adım |
|---|---|---|
| database_size | Büyümeyi yavaşlatın, disk kapasitesini planlayın | 6 |
| connection_trend | Havuzu sınırlayın, limiti büyütmeden sızıntıyı kesin | 3-5 (engine'e göre) |
| table_growth | Tablonun büyümesini kontrol altına alın | 5 |
| wraparound | Transaction ID yaşını düşürün (VACUUM FREEZE) | 5 |
| index_bloat | Index'i yeniden oluşturun veya kaldırın | 5 |

Her birinde: **neden** iş etkisiyle ("PostgreSQL disk dolduğunda yazma
işlemlerini tamamen durdurur", "bir failover'da kaybedilecek veri artar"),
**numaralı adımlar**, **adım başına kopyalanabilir komut**, **dikkat
notları** (kilitleme, ek disk ihtiyacı, yeniden başlatma, geri
alınamazlık), **tahmini süre**, **geri alma** ve **doğrulama sorgusu**.

### Kapsam dışında kalan üç metrik de standarda alındı

Beş türün dışında kısa vadeli üç tahmin daha üretiliyordu ve bunların
HİÇ adımı/komutu yoktu — yalnızca tek cümlelik bir öneri:

- **cache_hit_ratio**: artık dört adım (diskten okunan tablolar, yeni
  seq scan var mı, çalışma kümesi belleğe sığıyor mu, pg_prewarm).
- **replication_lag_bytes**: üç adım (gecikme gönderimde mi uygulamada
  mı, replikada blokla­yan sorgu, slot birikimi ve WAL disk baskısı).
- **transactions_per_sec / ops_per_sec**: üç ölçüm adımı — burada
  bilinçli olarak "şunu değiştir" demiyoruz, çünkü artan yükün doğru
  cevabı ortama özgü; uydurma bir komut yerine hangi ölçümlere
  bakılacağı yazılıyor.

PostgreSQL dışı engine'lerde bu üçü için `unavailable_reason` dolduruluyor
("bu instance sqlserver ve karşılığı olan DMV/komut seti doğrulanmadı") —
boş bırakmak yerine NEDEN üretilemediği yazılıyor.

### Migration

`supabase/migrations/20260908090000_prediction_advice.sql` —
`prediction_insights.advice` (JSONB). DEPLOY.md tablosuna 30. sıra olarak
işlendi. SQLite tarafında `database.py` içindeki kolon eklemesiyle
otomatik oluşuyor.

**Testler:** `tests/test_prediction_advice.py` (30 test) — beş türün
hepsinde beş parçanın da dolu olduğunu, en az iki adımda çalıştırılabilir
komut geldiğini (yalnız yer tutucu değil), "neden" bölümünün ölçümü
tekrarlamak yerine SONUCU anlattığını, yıkıcı/kilitleyen her komutun
dikkat notunda karşılığı olduğunu, ve öneri üretilemeyen durumların
nedeniyle birlikte döndüğünü doğruluyor. Ayrıca uçtan uca: üretilen
tahminin öneriyi kaydettiğini ve API'nin gerçekten döndürdüğünü — bu
sonuncusu, hatanın kendisini (şemada alan var, modelde yok) kalıcı olarak
kapatıyor.

## Faz 20 — İŞ 2: Tahmin doğruluğu takibi

### Neden gerekliydi

dbace her tahmine bir `confidence` yazıyor ve arayüzde "güven: %85" diye
gösteriyordu. Bu değer regresyonun **R²**'siydi: "model GEÇMİŞ veriye ne
kadar iyi oturdu" demek. Tahminin tutup tutmadığıyla ilgisi yok —
gürültüsüz ama tamamen yanlış eğimli bir seri de R²=0.99 verir. Yani ürün
doğruluk **iddia ediyordu, ölçmüyordu**.

### Kurulan döngü

**1. Kayıt.** Her tahmin üretildiğinde yeni `prediction_outcomes`
tablosuna bir satır yazılıyor: ne tahmin edildiği, hangi tarih için,
güven aralığı, kullanılan yöntem (`linear_regression+weekday` gibi), kaç
örneğe ve kaç günlük pencereye dayandığı, R²'si. Tahminin kendisiyle aynı
transaction'da yazılıyor.

**2. Değerlendirme.** Saatlik bir scheduler işi (`prediction_accuracy`)
hedef tarihi gelmiş satırları alıp gerçekleşen değeri okuyor — kaynağına
göre `MetricSample` (hedefe en yakın örnek, ±15 dk), `MetricRollupDaily`
(o günün son değeri) ya da `SchemaObjectDailySample` (o nesnenin o günkü
boyutu). Mutlak hata, yüzde hata ve güven aralığının tutup tutmadığı
hesaplanıyor.

Saatlik çalışıyor çünkü kısa vadeli tahminlerin ufku 1 saat; günlük bir iş
onları değerlendirilemez hale getirirdi (ham örnekler saklama süresi
dolunca siliniyor).

**3. Metrik.** Tür bazında ortalama mutlak hata, ortalama yüzde hata ve
güven aralığının tutma oranı. `GET /api/predictions/accuracy` ile
sunuluyor, Tahminler sayfasının üstünde tablo olarak gösteriliyor.

### Üç tasarım kararı — hepsi dürüstlük gerekçeli

**Kontrol noktası (checkpoint) ufku.** Uzun vadeli tahminlerin manşet ufku
aylar sürüyor (disk için 180 gün, wraparound için 365). O tarihi beklemek
**altı ay boyunca hiçbir geri besleme almamak** demekti. Bunun yerine aynı
modelden 7 gün sonrası için ikinci bir tahmin alınıp o ölçülüyor. Ölçülen
şey modelin kendisi olduğu için bu geçerli bir vekil; `checkpoint_days`
kolonu hangi ufukta ölçüldüğünü taşıyor ve `predicted_value`'nun manşet
sayıdan farklı olabileceği model docstring'inde açıkça yazılı.

**"Ölçülemedi" ile "yanlış" ayrı.** Hedef tarih geçtiği hâlde gerçekleşen
değer okunamıyorsa (instance kapatılmış, toplama durmuş, ölçülen nesne
silinmiş) satır `expired` işaretleniyor ve nedeni yazılıyor — doğruluk
hesabına HİÇ girmiyor. Bunları "isabetsiz tahmin" saymak modeli haksız
yere cezalandırır ve doğruluk oranını toplama kesintilerinin bir
fonksiyonu haline getirirdi. Panelde ayrıca gösteriliyor ("12 ölçülemedi")
çünkü çok yüksek bir ölçülemedi sayısı da kendi başına bir sinyal.

Ayrıca bir tolerans penceresi var: hedefi yeni geçmiş bir satır hemen
"ölçülemedi" sayılmıyor (günlük kaynaklar için 2 gün, ham örnekler için
30 dakika) — rollup bir tur gecikmiş olabilir.

**Az ölçümle güven iddia edilmiyor.** En az 5 tamamlanmış ölçüm yoksa
güvenilirlik `unknown`. Üç ölçümle "%100 isabet" demek, hiç ölçmemekten
daha yanıltıcı olurdu.

### Güvenilirlik işareti

Her tahmin, ait olduğu **türün** ölçülmüş güvenilirliğini taşıyor
(`PredictionOut.reliability`): aralık tutma oranı ≥%75 → güvenilir,
≥%50 → orta, altı → düşük. Arayüzde `high` dışındaki her seviye tahminin
yanında rozet olarak görünüyor, üzerine gelince açıklaması çıkıyor.

Güvenilirlik **tür bazında ve instance'lar arası** hesaplanıyor. Sorulan
soru "bu model ne kadar tutuyor", "bu instance'ta ne kadar tutuyor"
değil; ayrıca tek bir instance'ta beş tamamlanmış ölçüme ulaşmak haftalar
sürerdi ve rozet pratikte hep "bilinmiyor" kalırdı.

**Düşük güvenilirlikte gizlemek yerine işaretlemeyi seçtim.** Görev ikisini
de kabul ediyordu ("işaretle veya hiç gösterme"). Gizlemek şu yüzden
yanlış olurdu: modelin zayıf olması riskin gerçek OLMADIĞI anlamına
gelmez. Disk gerçekten doluyor olabilir; bizim eğim tahminimizin tutmaması
DBA'in bunu bilmemesi gerektiği anlamına gelmez. Bir izleme aracının
sessizce bilgi saklaması, yanlış bilgi vermesinden farklı bir kötülük
değil. Rozet, kullanıcının sayıya ne kadar güveneceğini bilmesini sağlıyor
— karar onda kalıyor.

### Migration

`supabase/migrations/20260908100000_prediction_outcomes.sql` — yeni
`prediction_outcomes` tablosu + dört index (biri değerlendirme işinin
taradığı `(status, target_at)`). DEPLOY.md tablosuna 31. sıra.

**Testler:** `tests/test_prediction_accuracy.py` (15 test) — döngünün üç
adımı da: üretilen tahminin kaydedildiği ve tahminle ilişkilendiği,
kontrol noktasının ölçülebilir bir ufukta olduğu, isabetli/isabetsiz
tahminlerin doğru puanlandığı, ölçülemeyenin isabetsiz sayılmadığı,
tolerans penceresinin çalıştığı, ham örnekte hedefe EN YAKIN örneğin
seçildiği, şema nesnesinde yanlış nesnenin okunmadığı, metriklerin doğru
hesaplandığı, az ölçümde "bilinmiyor" dendiği, `expired` satırların oranı
düşürmediği ve API'nin rozeti doğruluk ucuyla TUTARLI döndürdüğü.

## Faz 20 — İŞ 3: Tahmin kalitesi

Altı maddenin ikisi (güven aralığı, veri yeterliliği ilanı) Faz 16'da
kısmen vardı; denetim sırasında ikisinin de **uygulanmadığı** ortaya çıktı.
Aşağıda önce bulunan iki gerçek hata, sonra eklenen dört yetenek.

### Bulunan hata A — saat bazlı mevsimsellik trendi yutuyordu

Bir günden kısa bir seride her saat kovası bir kez görülür. Yani "o saatin
ortalamadan sapması" ile "o ana kadarki artış" **aynı şeydir**: mevsimsel
bileşen çıkarıldığında geriye düz bir seri kalır, eğim sıfıra iner ve
tahmin noktası şu anki değerin ALTINA düşer.

Somut sonuç: %55'ten %82'ye tırmanan bir bağlantı serisi için bir saat
sonrası **%67.8** tahmin ediliyordu — yükselen bir metrik için düşüş
öngörülüyordu. Eşik ihlali de doğal olarak hiç tetiklenmiyordu.

Eski koruma (`len(points) < 8` ve "en az 2 farklı kova") bunu yakalamıyor:
16 saatte 17 farklı kova ve 50 nokta var, ikisi de sağlanıyor.

Doğru ölçüt desenin **tekrar etmesi**: her mevsim kovası en az iki AYRI
döngüde görülmeli (saat deseni için iki farklı gün, haftaiçi/haftasonu
deseni için iki farklı hafta). Sağlanmazsa düz regresyona düşülüyor —
uydurma bir desen uygulamaktansa desensiz kalmak doğru.

### Bulunan hata B — ilan edilen veri gereksinimi uygulanmıyordu

`PREDICTION_REQUIREMENTS["connection_trend"]` 40 örnek / 0.5 gün diyor ve
"Tahmin veri yeterliliği" paneli bunu kullanıcıya gösteriyordu. Ama
`_short_horizon_predictions` içinde çıplak bir `len(points) < 5` vardı:
panel "0.2/0.5 gün — bekleniyor" derken tahmin çoktan üretilmiş oluyordu.
Ayrıca 5 örnek (75 saniye) üzerinden 1 saat ilerisini kestirmek 48 katlık
bir ekstrapolasyon.

Bu, İŞ 1'deki hatayla aynı sınıftan: bir kural ilan edilmiş, gösterilmiş,
ama uygulanmamış. Artık üretim ve panel aynı yerden besleniyor.

**Bilerek değişen davranış:** yeni eklenen bir instance artık ilk 12 saat
kısa vadeli tahmin üretmiyor. Öncesinde 75 saniyelik veriyle üretiyordu —
o tahminler zaten güvenilmezdi. Mevcut iki test (`test_prediction.py`)
gerçekçi pencereyle güncellendi.

### Aykırı değerler

Tek seferlik sıçramalar (gece yedeği, toplu içe aktarma) eğimi olduğundan
dik gösterip yanlış aciliyet üretiyordu. `detect_outliers` artık MAD
(medyan mutlak sapma) tabanlı bir eşikle bunları ayıklıyor.

İki tasarım detayı:

- **Aykırılık ortalamaya değil TRENDE göre ölçülüyor.** Büyüyen bir seride
  değerler zaten geniş bir aralığa yayılır; ortalamadan uzaklık orada
  anlamsızdır. Önce bir doğru geçiriliyor, aykırılık kalıntılar üzerinden
  ölçülüyor. Standart sapma yerine MAD kullanılıyor çünkü standart sapmayı
  aykırı değerin kendisi şişirir ve kendini gizler.
- **Noktaların en fazla %20'si atılabilir.** Üstü atılıyorsa sorun tek bir
  sıçrama değil, modelin yanlış olmasıdır; orada veriyi kırpmak "veriyi
  tahmine uydurmak" olur.

### Doğrusal olmayan büyüme

`assess_fit` verinin şeklini adlandırıyor: `linear` | `exponential` |
`curved` | `noisy` | `flat`.

- **Üstel**: log dönüşümlü regresyon belirgin şekilde daha iyi uyuyorsa.
  Bulgu metnine "doğrusal tahmin bu durumda gerçekleşenden DAHA İYİMSER
  çıkar — tarih büyük ihtimalle olduğundan geç" notu ekleniyor.
- **Eğri**: kalıntılar ortada bir yöne, uçlarda diğer yöne sapıyorsa
  (hızlanan ya da doyuma ulaşan büyüme). Eşik ölçülerek seçildi: belirgin
  eğri bir seri 1.02, düzgün doğrusal bir seri 0.1'in altında veriyor —
  0.8 ikisini rahatça ayırıyor.
- **Gürültülü / sabit**: bu ikisinde **tahmin hiç üretilmiyor**. Gürültüden
  trend uydurmak, olmayan bir sinyali varmış gibi sunmaktır.

Üstel ve eğri seriler atılmıyor: orada gerçek bir büyüme VAR, yalnızca
doğrusal tahmin iyimser kalıyor. Gizlemek yerine uyarısıyla veriliyor.

**Aykırı temizliği ile doğrusallık testi iki geçişli**, çünkü birbirlerine
bağlılar: tek bir uç değer log uyumunu yapay olarak iyileştirip düz bir
seriyi "üstel" gösterebiliyordu (ilk denemede tam da bu oldu). Sıra: önce
MAD tabanlı temizlik (sıçramadan etkilenmez), sonra kalan noktaların HAM
değerleriyle uyum ölçümü, ve uyum "eğri/üstel" çıktıysa temizlik geri
alınıyor — doğrusal bir modele göre bir eğrinin UÇLARI en büyük kalıntıya
sahiptir, orada "aykırı" görünen şey gerçek veridir.

### Tek nokta yerine aralık

`eta_days_range` eşiğe ulaşma süresini **aralık** olarak veriyor
("45-60 gün arası"). Kaynağı eğimin kendi %90 güven aralığı
(`se(slope) = sqrt(kalıntı varyansı / Sxx)`) — nokta tahmininin
aralığından farklı bir şey: "büyüme hızı ne kadar belirsiz" sorusunu
cevaplıyor.

Eğimin alt sınırı sıfırın altındaysa üst uç `None` dönüyor ve arayüz
"en erken N gün (üst sınır belirsiz — büyüme durabilir)" yazıyor. Uydurma
bir üst sınır yazmaktansa belirsizliği söylemek doğru.

Tablo/index tahminleri de artık nokta yerine aralık gösteriyor
("30 gün sonra 12.4 GB–18.1 GB arasında").

### Yöntem şeffaflığı

Öncesinde tahminin yanında yalnızca "güven: %85" vardı ve bu regresyonun
R²'siydi — modelin geçmişe oturma iyiliğini söyler, verinin doğrusal
modele UYUP uymadığını değil. Üstel büyüyen ya da tek sıçramayla bozulmuş
bir seri de yüksek "güven" gösterebiliyordu.

Her tahmin artık taşıyor: kullanılan model (`doğrusal regresyon +
haftaiçi/haftasonu düzeltmesi + 1 aykırı ölçüm çıkarıldı`), örnek sayısı,
dönem uzunluğu, atılan aykırı sayısı, verinin şekli ve açıklaması.
Arayüzde katlanabilir bir "Yöntem" bölümünde; veri doğrusal değilse başlık
satırında uyarı görünüyor. R² de orada, ama artık ne OLMADIĞI da yazılı:
"tahminin tuttuğunu göstermez; onun ölçüsü üstteki doğruluk tablosudur".

### Migration

`supabase/migrations/20260908110000_prediction_method_transparency.sql` —
`prediction_insights`: method, sample_count, span_days, outliers_removed,
fit_kind, fit_note, eta_days_min, eta_days_max. DEPLOY.md sıra 32.

**Testler:** `tests/test_prediction_quality.py` (21 test) — altı kuralın
her biri, ve bulunan iki hata için ayrı regresyon testleri (yükselen seri
için düşüş öngörülmemesi; ilan edilen gereksinimin gerçekten uygulanması).
Toplam 670 test yeşil, `npm run build` yeşil.

## GERİLEME DÜZELTMESİ — Rapor bulgu detayı açılmıyordu

**Belirti (canlı):** `TypeError: Cannot read properties of undefined (reading 'length')` —
Raporlar sayfasında bir bulgunun detayına tıklayınca. Faz 19'da eklenen hata sınırı yakalayıp
"Sayfa render hatası" gösteriyordu.

### Kök neden — tahmin edilenden farklı

Teşhis yönü "Faz 18'de eklenen alanlar eski kayıtlarda yok" idi. Gerçek daha kötüsü: **alanlar
HİÇBİR kayıtta dönmüyordu.**

`ReportFinding` MODELİNDE `facts` ve `note` kolonları vardı, rapor motoru ikisini de yazıyordu,
migration'lar uygulanmıştı — ama `ReportFindingOut` ŞEMASINDA bu iki alan **hiç tanımlı
değildi**. Pydantic, `response_model` içinde tanımsız olan alanı sessizce kırpar. Sonuç:

- API her bulguda `facts` ve `note` alanlarını **hiç göndermedi** (null bile değil, yok).
- Arayüz `finding.facts.length` okudu → `undefined` → çöktü.
- Faz 18'in iki özelliği (İŞ 4 sayısal özet, İŞ 3 sınırlılık notu) üretildi, veritabanına
  yazıldı, ama **hiç görünmedi**. Kimse fark etmedi çünkü çökme yalnızca detay açılınca oluyor.

Bu, Faz 20 İŞ 1'deki `advice` hatasının **ayna görüntüsü**: orada şemada alan vardı modelde
yoktu, burada modelde var şemada yok. İkisi de aynı sınıftan: iki katmandaki alan listesinin
sessizce ayrışması.

### Neden derleyici yakalamadı

TypeScript tipi yalan söylüyordu:

```ts
facts: { label: string; value: string; tone: "neutral" | "good" | "bad" }[];  // "her zaman var"
```

Tip "zorunlu dizi" dediği için `tsc`, `.length` erişimini sorunsuz kabul etti. Hata ancak
canlıda, kullanıcı tıklayınca ortaya çıktı.

### Düzeltme — dört katman

**1. Backend şeması (kök neden).** `ReportFindingOut`'a `note` ve `facts` eklendi. `facts` için
ayrı bir `FindingFactOut` modeli yazıldı; null'ı boşa çeviren validator kondu. Model bozuk
veriye de dayanıklı: bilinmeyen bir `tone` "neutral"a indirgeniyor, eksik `label`/`value`
boş dizeye — geçersiz tek bir satır yüzünden raporun TAMAMI 500 dönmesin.

**2. `AdviceOut` da korundu.** `steps`/`cautions` varsayılanı vardı ama null koruması yoktu.
`advice` serbest biçimli bir JSON kolonu; eski bir kayıtta bu anahtarlar `null` olsa Pydantic
doğrulama hatası verirdi ve **raporun tamamı 500** dönerdi — bir bulgudan değil, RAPORDAN
olunurdu.

**3. TypeScript tipleri gerçeğe hizalandı.** Nullable JSON kolonlarından gelen alanlar artık
tipte de nullable: `ReportFinding.facts/commands/evidence`, `Prediction.playbook`,
`Advice.steps/cautions`. Bu değişiklik **anında iki yeni çökme noktası daha yakaladı**:
`PredictionsPage` ve `InstanceDetailPage`, olası-null `p.playbook`'u `steps.length` okuyan bir
bileşene geçiriyordu. Aynı hata, farklı sayfa — tip düzeltilmese sıradaki gerileme oydu.

**4. Bileşen korumaları.** `ReportFindingCard` (`facts ?? []`, `commands ?? []`),
`PredictionPlaybook` (`steps ?? []`), `AdviceCard` (`steps ?? []`, `cautions ?? []`).

### Frontend geneli tarama

Tüm `.length` / `.map` / `.filter` çağrıları, kaynak alanın backend'deki nullable'lığıyla
karşılaştırıldı. Bulunan tek gerçek eksik alan `facts` idi; `null` gelebilecek ama korumasız
kullanılan alanlar yukarıdaki dört yerdi. Denetimde çıkan diğer beş aday
(`ClusterHealthOut.services`, `NodeHealthOut.services`, `IndexAdviceReportOut.advice`,
`RefreshIntervalOut.options`, `RetentionStatusOut.options`) isim çakışmasıydı: hepsi kodda
üretilen yanıtlar, ORM kolonu değil — düzeltme gerektirmiyor.

### Yan bulgu: sıraya bağlı test kırılganlığı

`test_dashboard_issue_enrichment` bu turda kırmızıya döndü ama **kod değişikliğinden bağımsız**
(stash'lenmiş hâlde de düşüyor). Sebep: `collect_dashboard_summary` en fazla 10 sorun
döndürüyor, test veritabanı ise pytest çalıştırmaları arasında kalıcı; biriken
`GroupHealthSnapshot` satırları testin kendi grubunu ilk 10'un dışına itiyordu. Test artık
kendi grubunun dışındaki anlık görüntüleri temizliyor (o tablo grup başına tek satır tutan bir
önbellek, geçmiş değil — yeniden üretilir).

**Testler:**

- `tests/test_report_finding_payload.py` (8 test) — şema sürüklenmesi (modeldeki her kolon
  şemada karşılığını bulmalı), yeni kayıtta değerlerin taşınması, eski kayıtta null kolonların
  boş koleksiyona çevrilmesi, bozuk `tone`/eksik anahtar dayanıklılığı, `advice` içindeki null
  listelerin raporu 500'lememesi. **Düzeltme geri alındığında 8 testin 7'si düşüyor** —
  hatanın kendisini yakaladıkları doğrulandı.
- `tests/test_api_contract_alignment.py` (69 test) — TS arayüzü ↔ Pydantic şeması hizası:
  TS'te zorunlu olan her dizi/nesne alanının şemada karşılığı olmalı, ve nullable bir ORM
  kolonundan besleniyorsa null koruması bulunmalı. 59 çift eşleşiyor, 8'i ORM tabanlı.

Toplam 747 test yeşil, `npm run build` yeşil.

## CANLI 502 — `/api/instances/{id}/insights` (Railway)

**Belirti:** uç 502 Bad Gateway dönüyor; tarayıcıda ayrıca CORS hatası görünüyor. CORS hatası
yan etki: 502 uygulamadan değil Railway'in kenarından geliyor, dolayısıyla yanıtta CORS başlığı
yok.

### Teşhis

Uç **canlı veritabanına hiç bağlanmıyor** — erişilemeyen bir hedef yüzünden asılı kalma
ihtimali baştan elendi. Yaptığı her şey dbace'in kendi veritabanına giden dört sorgu, ve
hepsinin kalıbı aynı:

```sql
SELECT ... FROM <tablo> WHERE instance_id = ? ORDER BY collected_at DESC LIMIT 1
```

`metric_samples` ve `slow_query_samples` tablolarında yalnızca **ayrı ayrı** `instance_id` ve
`collected_at` indeksleri vardı. Bu erişim kalıbı için ikisi de kötü:

- `instance_id` indeksiyle: o instance'ın TÜM satırları çekilip sıralanır.
- `collected_at` indeksiyle: indeks sondan taranır, `instance_id` ile elenir. **Veri göndermeyi
  durdurmuş bir instance için bu, tablonun tamamını taramaya dönüşür** — planlayıcı "en yeniden
  başla, ilk eşleşmede dur" diye ucuz sanır, oysa eşleşme milyonlarca satır geride.

Hacim bunu ölümcül yapıyor. Toplama aralığı 15 saniye ve `collect_slow_queries` döngü başına
**20 satır** yazıyor:

| Tablo | Instance başına satır/ay (30 gün saklama) |
|---|---|
| `metric_samples` | ~172.800 |
| `slow_query_samples` | ~3.456.000 |

Sorgu dakikalarca asılı kalıyor → Railway'in kenarı bekleyip 502 döndürüyor.

**Neden diğer uçlar da etkilendi:** bu uç instance detay sayfasında **15 saniyede bir**
yenileniyor. Yavaş istekler üst üste binince SQLAlchemy havuzu (varsayılan 5 + 10) tükeniyor ve
sıradaki her istek bekliyor — tek bir yavaş sorgu tüm API'yi cevapsız bırakıyor.

Aynı kalıp beş dosyada daha var (`metrics.py`, `queries.py`, `dashboard_snapshot.py`,
`prediction.py`), hepsi aynı indeksten yararlanıyor.

### Düzeltme

**1. Bileşik indeksler (asıl düzeltme).**
`(instance_id, collected_at)` — eşitlik filtresi önce, sıralama kolonu sonra. Sorgu tek bir
indeks aramasına iniyor. Geliştirme veritabanında plan doğrulandı:

```
ÖNCE : SEARCH ... USING INDEX ix_slow_query_samples_instance_id
       USE TEMP B-TREE FOR ORDER BY          ← sıralama adımı
SONRA: SEARCH ... USING COVERING INDEX ix_slow_query_samples_instance_collected
```

**2. Migration kilitlemeden uygulanıyor.**
`supabase/migrations/20260909090000_hot_table_composite_indexes.sql`. Tablolar milyonlarca
satır olduğu için normal `CREATE INDEX` tamamlanana kadar tabloya YAZMAYI kilitler ve toplama
döngüsü durur — migration `CREATE INDEX CONCURRENTLY` kullanıyor. CONCURRENTLY bir transaction
bloğunun içinde çalışamaz, bu yüzden dosyada ve DEPLOY.md'de "iki satırı SQL Editor'de tek tek
çalıştırın" notu var. Kilitleyen alternatif yorum olarak duruyor.

**3. Motor sağlamlaştırma (savunma katmanı).**
- `pool_pre_ping=True` — Supabase pooler boştaki bağlantıları düşürüyor; ping olmadan havuzdan
  alınan ilk bağlantı "connection was closed" ile patlıyor.
- `statement_timeout` (varsayılan 120 sn, `DB_STATEMENT_TIMEOUT_SECONDS` ile ayarlanabilir,
  0 = sınırsız). Kaçak bir sorgu artık **asılı kalmak yerine hata veriyor**: istemci 502 yerine
  anlaşılır bir hata alıyor ve CORS başlığı da ekleniyor. Sınır bilerek geniş: rapor üretimi
  API süreciyle aynı event loop'ta çalışıyor (`health_report.py` `asyncio.create_task`), amaç
  yavaş sorguyu kesmek değil ASILI KALMAYI önlemek. Yalnızca dbace'in kendi veritabanını
  etkiler; izlenen hedeflerin sınırı `collectors/postgresql.py` içinde.

### Bilerek değişen test

`test_sqlalchemy_engine_disables_statement_cache_for_asyncpg_urls` `connect_args` sözlüğünün
TAM eşitliğini kontrol ediyordu; artık iki ayar daha taşıyor. Test niyetine (prepared statement
önbelleği kapalı olmalı) sadık kalacak şekilde anahtar kontrolüne çevrildi, yeni iki ayar için
ayrı testler eklendi.

**Testler:** `tests/test_hot_query_indexes.py` (9 test) — indeksin modelde tanımlı olduğunu VE
kolon sırasının doğru olduğunu (eşitlik önce, sıralama sonra), canlı şemada gerçekten
bulunduğunu (`create_all` var olan tabloya sonradan indeks EKLEMEZ), sorgu planının onu
kullandığını ve geçici sıralama yapmadığını, migration'ın iki indeksi de kilitlemeden
oluşturduğunu ve DEPLOY.md'ye işlendiğini doğruluyor. Toplam 758 test yeşil.

## Faz 21 — İŞ 1: GitHub Actions CI

Üç kez "yerelde yeşil, canlıda patlak" yaşandı ve üçü de aynı sınıftandı —
uygulama ayağa kalkıyor ama sözleşme bozuk:

1. `AdviceOut` ileri referansı — yerel 3.14 (PEP 649) sessizce geçti,
   canlı 3.12 import anında `NameError` verdi, API tamamen çöktü.
2. `ReportFindingOut.facts`/`.note` — model yazıyordu, şema kırpıyordu.
3. `PredictionOut.advice` — şemada alan vardı, modelde karşılığı yoktu.

`.github/workflows/ci.yml` her push ve pull request'te çalışıyor, iki iş
paralel:

**Backend — canlıyla AYNI sürümde.** `python-version: "3.12"`; kaynağı
`deploy/onprem/Dockerfile.backend`'deki `python:3.12-slim` (Railway bu
Dockerfile ile derliyor, bkz. `railway.toml`). Sürüm eşleşmesi bu CI'ın
varlık sebebi olduğu için workflow dosyasında yorumla işaretli.
Adımlar: bağımlılık kurulumu → model bütünlüğü → testler.

**Frontend.** `npm ci` → `npx tsc -b` → `npm run build`. Tip kontrolü ayrı
adım: kırılmanın tip hatası mı derleme hatası mı olduğu çıktıdan doğrudan
görülsün.

### Model bütünlüğü kontrolü ayrı bir adım

`backend/scripts/check_model_integrity.py` üç şeyi saniyeler içinde
doğruluyor:

1. `from app.main import app` — canlıdaki çöküş tam olarak buradaydı.
2. Her Pydantic modeli tam kurulmuş mu (`__pydantic_complete__`). 3.14'te
   çözülemeyen bir ileri referans sessizce `False` bırakır ve model ilk
   kullanımda yeniden kurulmaya çalışılır; 3.12'de aynı durum import anında
   patlar. Kontrol ikisini de sürümden bağımsız yakalıyor.
3. `app.openapi()` üretilebiliyor mu — bir `response_model` çözülemiyorsa
   burada patlar. Bu aynı zamanda İŞ 2'deki TypeScript tip üretiminin
   dayandığı çıktı.

Testlerden ÖNCE ve ayrı adım olarak koşuyor: canlıyı çökerten hata sınıfı
buysa 758 testin çıktısında kaybolmasın, adım adıyla kırmızı olsun.
Yerelde de tek başına çalıştırılabilir (`python scripts/check_model_integrity.py`).

### Ayrıntılar

- **Önbellek:** pip için `requirements.txt` + `requirements-dev.txt`, npm
  için `package-lock.json` anahtarlı.
- **`concurrency`:** aynı dalda üst üste gelen çalıştırmalarda önceki iptal
  ediliyor.
- **`permissions: contents: read`** — workflow'un yazma yetkisi yok.
- README'ye CI rozeti eklendi.

Yerel `backend/.venv` hâlâ 3.14; risk artık *canlıya* değil *CI'a* düşen
bir sürpriz. SORULAR.md'deki not bu ayrımla güncellendi.

## Faz 21 — İŞ 2: Backend'den TypeScript tipi üretimi

Elle yazılmış frontend tipleri API'den sessizce ayrışıyordu ve bugünkü
çökmelerin kökü buydu — en somutu `ReportFinding.facts`: tip "her zaman
var" diyordu, şemada alan hiç tanımlı olmadığı için API onu HİÇ
döndürmüyordu, arayüz `.length` okuyunca patlıyordu. Derleyici
yakalayamadı çünkü tip yalan söylüyordu.

### Üretim zinciri

`backend/scripts/dump_openapi.py` → `frontend/openapi.json` →
`openapi-typescript` → `frontend/src/api-types.ts`.

Şema **sunucu çalıştırılmadan** üretiliyor (`app.openapi()`). İki sebep:
CI'da sunucu/port/sağlık beklemesi gerekmiyor, ve çıktı deterministik
(ağ ve zamanlama devrede değil). Anahtarlar sıralı yazılıyor — aksi
hâlde sürüklenme kontrolü kod değişmeden de kırmızı olurdu.

```bash
cd frontend
npm run gen:types        # backend'i çalıştırmadan (python gerekir)
npm run gen:types:live   # çalışan backend'in /openapi.json ucundan
```

Üretilen dosyanın başına projeye özgü bir açıklama ekleniyor: neden var,
elle düzenlenmemeli, değişiklik backend'deki Pydantic şemasından yapılmalı.

### Elle yazılmış tiplerin bağlanması

En çok çökme yaşanan tipler artık üretilenden **türetiliyor**:
`ReportFinding`, `FindingFact`, `Advice`, `AdviceStep`, `Prediction`,
`PredictionStep`, `PredictionReliability`, `PredictionAccuracy`,
`DashboardSummary`.

`Omit<Gen[...], ...> & {...}` kalıbı yalnızca iki gerekçeyle kullanıldı ve
her biri yerinde açıklandı:

1. **OpenAPI'nin ifade edemediği daraltmalar** — `severity`, `status`,
   `change_state`, `tone`, `level` şemada serbest `string`; arayüz bu
   değerlere göre dallanıyor, literal birleşim olarak daraltıldı.
2. **Dağıtım kayması** — `facts`, `commands`, `evidence`, `playbook`,
   `steps`, `cautions` nullable JSON kolonlarından geliyor. Şema artık
   null'ı boşa çeviriyor ama ESKİ bir backend sürümü hâlâ null
   döndürebilir; tipin bunu kabul etmesi çağrı yerlerinde korumayı
   zorunlu kılıyor.

Kalan 82 arayüz elle yazılmış olarak duruyor — hepsini bir turda
çevirmek gerekmiyordu, en çok çökme yaşanan yerlerden başlandı.

### Bağlama anında çıkan gerçek uyumsuzluklar

Üretilen tipler daha katı: Pydantic varsayılanı olan bir alan OpenAPI'de
hem opsiyonel hem nullable (`?: T | null`) oluyor, oysa elle yazılan tip
`T | null` diyordu. Derleyici sekiz çağrı yerini işaretledi — hepsinde
`!== null` kontrolü yalnız yarısını kapsıyordu (`undefined` açıkta
kalıyordu). `!= null` / `?? null` ile düzeltildi:
`PredictionAccuracyPanel` (3), `DashboardPage` (4), `PredictionsPage` (1),
`InstanceDetailPage` (1).

Bunlar bugünkü backend'de pratikte tetiklenmezdi (FastAPI varsayılanı olan
alanları da serileştiriyor), ama sözleşme onlara izin veriyordu ve tip
artık sözleşmeyi dürüstçe anlatıyor.

### CI sürüklenme kontrolü

Üçüncü bir iş (`types`) hem python hem node kurup tipleri yeniden üretiyor
ve `git diff --exit-code` ile commit'lenmiş hâlle karşılaştırıyor. Farklıysa
iş kırmızı ve hata mesajı ne yapılacağını söylüyor: "backend şeması değişmiş
ama tipler güncellenmemiş".

### `test_api_contract_alignment.py` korundu, görevi değişti

Görev gereği kaldırılmadı — iki katman farklı şeyleri koruyor. Derleyici
"alan var mı, tipi ne" sorusunu; test ise "şema null gönderebiliyor mu"
sorusunu cevaplıyor ve bu OpenAPI'den okunamaz (Pydantic validator'ına
bakmak gerekir).

Test yeni gerçekliğe uyarlandı: türetilmiş tipler için alan alan denetim
yerine "türetilmiş KALDIĞINI" doğruluyor (birisi elle yazılmış hâline
döndürürse yakalanır), hâlâ elle yazılmış arayüzler için eski denetim
sürüyor.

### Yan bulgu: kendi betiklerimde Windows kodlama hatası

`dump_openapi.py` ve `check_model_integrity.py` Türkçe çıktı yazarken
Windows'un cp1252 konsolunda `UnicodeEncodeError` verip düşüyordu (dosya
yazılmış olsa bile). CI Linux/UTF-8 olduğu için orada görünmezdi — yani
"yerelde patlar, CI'da geçer" durumu. İkisinde de stdout/stderr UTF-8'e
sabitlendi.

## Faz 21 — İŞ 3: Veri saklama denetimi ve deploy notu

### Bulunan kusur A — günlük işler yeniden başlatmada HİÇ çalışmıyordu

Saklama temizliğinin mantığı doğruydu (`retention.py`), ama zamanlaması
değildi. `interval(days=1)` ilk çalışmasını scheduler BAŞLADIKTAN 24 saat
sonra planlar — APScheduler'ın `IntervalTrigger`'ı `start_date`
verilmezse `now + interval` alıyor. Davranış empirik olarak doğrulandı
(kurulu sürümle: fark tam 24.0 saat).

Worker günde bir kereden sık yeniden başlıyorsa — Railway'de yeniden
dağıtım, çökme, platform bakımı — sayaç her seferinde sıfırlanır ve iş
**hiç çalışmaz**. İki sessiz sonucu vardı:

1. Saklama temizliği yapılmadığı için `slow_query_samples` sınırsız
   büyüyordu.
2. **Günlük rollup da aynı tuzaktaydı** — `MetricRollupDaily` boş kalıyor,
   dolayısıyla uzun vadeli kapasite tahminleri (disk dolma, wraparound,
   tablo büyümesi) hiç üretilmiyordu. Bu, denetim sırasında ortaya çıkan,
   bildirilmemiş ikinci bir arıza.

`refresh_dashboard_snapshots` işinde `next_run_time=datetime.now()` vardı,
diğerlerinde yoktu — yani sorun biliniyordu ama tek bir işte çözülmüştü.

**Düzeltme:** günlük işler artık `cron` (saklama 03:00, rollup 03:30).
Cron sabit saate bağlıdır, sürecin ne zaman başladığından bağımsızdır.
`misfire_grace_time=3600` kısa kesintide işi kurtarıyor, `coalesce=True`
uzun kesinti sonrası birikmiş tetiklemeleri tek çalışmaya indiriyor.
Saatlik doğruluk işi de `next_run_time` ile hemen başlıyor.

### Bulunan kusur B — toplama oranı hacimle orantısız

Canlıda bildirilen **337 bin satır** (tek instance) hesaplandı:

| | Döngü/gün | Satır/gün | 30 günde |
|---|---|---|---|
| Eski (her döngü, 15 sn) | 5.760 | 115.200 | **3.456.000** |
| Yeni (5 dk) | 288 | 5.760 | **172.800** |

337.000 ÷ 115.200 ≈ **2,9 gün**. Yani bildirilen sayı ~3 günlük toplamaya
denk; 30 günlük saklamayla **tutarlı** (henüz silinecek bir şey yoktu) ve
tek başına saklamanın bozuk olduğunu KANITLAMIYOR. Kanıt kusur A'dan
geliyor: temizlik zaten hiç çalışmayacaktı. Asıl mesele oran — bu hızda
30 günde 3,5 milyon satıra çıkardı.

**Düzeltme:** yavaş sorgular artık metriklerden ayrı bir aralıkta
toplanıyor (`slow_query_interval_seconds`, varsayılan 300 sn). Gerekçe:
`pg_stat_statements` kümülatif, rapor ve DPA pencere FARKI alıyor —
15 saniyelik çözünürlük analize hiçbir şey katmıyor, 5 dakikalık örnekler
aynı sonucu veriyor. Metrik toplama 15 saniyede kalıyor; bağlantı zirvesi
gibi ani olaylar için gerekli.

Sonuç: instance başına aylık satır sayısı **20 kat** azalıyor ve
`metric_samples` ile aynı mertebeye iniyor. Saklama süresi kısaltılmadı
(1 ay), yani veri kaybı yok. Ayar `0` yapılırsa eski davranışa dönülüyor.

Toplama atlandığında hedef veritabanına da yük binmiyor: `pg_stat_statements`
taraması döngünün en pahalı kısmıydı.

### Admin ekranı — doğrulandı, değişiklik gerekmedi

Saklama görevinin son çalışma zamanı ve silinen kayıt sayısı zaten
gösteriliyor (`AdminPage`, `retention.last_run_at` / `last_deleted_count`;
`AppSetting`'te tutuluyor, ayrı bir denetim tablosu yok). Hiç çalışmamışsa
"henüz çalışmadı" yazıyor — kusur A canlıda muhtemelen bu şekilde
görünüyordu.

### Deploy notu — önceki talimatım YANLIŞTI

`hot_table_composite_indexes` migration'ı için "Supabase SQL Editor'de
satırları tek tek çalıştırın" yazmıştım. Bu çalışmıyor: SQL Editor her
gönderimi bir transaction'a sarıyor ve `CREATE INDEX CONCURRENTLY`
transaction bloğunda çalışmıyor — `cannot run inside a transaction block`
hatası veriyor. Satır sayısı sorunu çözmüyor, çünkü sorun satır sayısı
değil sarmalama. `supabase db push` de aynı sebeple çalışmıyor.

Doğru yol psql ile, **her komut ayrı bir `-c` çağrısı**. DEPLOY.md'ye
bağlantı dizesinin nereden alınacağı, pooler port uyarısı (transaction-mode
6543 yerine session-mode 5432), doğrulama (`\di+`) ve `INVALID` indeks
kurtarması komut örnekleriyle yazıldı. Aynı uyarı on-prem kurulum
dokümanına da (docker compose exec karşılığıyla) eklendi.

### Kural — gelecekteki CONCURRENTLY migration'ları

`CONCURRENTLY` kullanan yeni migration dosyalarının adı `_concurrently`
ile bitecek; ayrıca dosyanın baş yorumunda ve DEPLOY.md tablosunda
**"psql gerekir"** ibaresi bulunacak. 33 numaralı dosya bu kuraldan önce
yazıldığı için adı değişmedi (uygulanmış bir migration'ı yeniden
adlandırmak, onu çalıştırmış ortamlarda karışıklık yaratırdı) — içinde ve
tabloda işaretli.

**Testler:** `tests/test_retention_and_volume.py` (11) — APScheduler'ın
24 saatlik ilk çalışma davranışını belgeliyor, günlük işlerin cron
kullandığını ve kesintiye dayanıklı olduğunu, hacim hesabını, aralık
mantığının instance başına ayrı saydığını ve ayarın kapatılabildiğini
doğruluyor. `tests/test_hot_query_indexes.py`'a dört test eklendi:
CONCURRENTLY kullanan HER migration'ın DEPLOY.md'de işaretli olduğu,
çözümün komut örneğiyle yazıldığı ve aynı uyarının on-prem dokümanında da
bulunduğu. Toplam 766 test yeşil.

## Faz 22 — İŞ 1: Engine ve topolojiye göre alan gösterimi

Kural daha önce istenmişti ama her formda AYRI AYRI yazıldığı için
ayrışmıştı. Denetimde bulunanlar:

- **InstancesPage**: "Sunucu servisleri" listesi (etcd/patroni/postgresql/
  keepalived/haproxy) HER engine'de görünüyordu — SQL Server ve MongoDB
  kayıtlarında da. Patroni REST portu, etcd portu, HAProxy stats portu ve
  keepalived VIP ise her PostgreSQL kaydında görünüyordu, standalone
  olanlarda dahi. `Cluster` ve `Rol` alanları her zaman açıktı.
- **DatabaseWizardPage**: cluster adımı zaten topolojiye bağlıydı, ama
  içindeki alanlar tek tek değil blok hâlinde koşulluydu.
- **GroupDetailPage** ve **App.tsx**: aynı "standalone mı" kararı üç ayrı
  yerde `topology !== "standalone"` diye elle yazılmıştı.

### Tek kaynak: `frontend/src/formFields.ts`

18 alan için `{engines?, topologies?, why}` tablosu. `showField(key, ctx)`
tek karar noktası; `ctx` formun engine'i ve topolojisi.

İlke: kural sağlanmıyorsa alan **DOM'da hiç bulunmuyor** — gizlenmiyor,
devre dışı bırakılmıyor. Gizli bir alan hâlâ form durumunda yer tutar,
sekme sırasında görünür ve kaydedilirken ilgisiz değer gönderir.

Her kuralda zorunlu bir `why` var (test bunu doğruluyor): kuralı sonradan
değiştiren kişi gerekçesini görsün, örneğin "etcd, Patroni'nin dağıtık
yapılandırma deposu; standalone'da ve diğer engine'lerde yok".

### InstancesPage'e açık topoloji seçimi eklendi

`Instance` modelinde topoloji kolonu yok; form bunu `cluster_name` dolu mu
diye ÖRTÜK çıkarıyordu, dolayısıyla kural uygulanamıyordu. Artık açık bir
"Topoloji" seçimi var (Standalone / Cluster üyesi). Düzenlemeye açılan bir
kayıt için `cluster_name`'den türetiliyor.

`standalone`'a geçildiğinde cluster alanlarının **değerleri de
temizleniyor** (`cluster_name`, `role`, `services`, `keepalived_vip`) —
görünmeyen bir cluster adının kaydedilip instance'ı yanlış gruplaması
mümkün olmasın. MongoDB seçilince topoloji standalone'a düşüyor (dbace
MongoDB için cluster topolojisi modellemiyor).

### Her alan kendi kuralıyla

Patroni/etcd/HAProxy/keepalived bugün aynı kuralı paylaşıyor, ama tek bir
koşula bağlamak biri değiştiğinde sessizce yanlış olurdu. Dördü de ayrı
`showField` çağrısıyla kontrol ediliyor. Aynı şekilde `ssl_mode` ile
`uses_pooler` ayrıldı.

### Varsayılan portlar

`ENGINE_DEFAULTS` (5432/1433/27017) zaten `api.ts`'te tekti ve hem
sihirbaz hem InstancesPage engine değişiminde portu ve veritabanı adını
oradan dolduruyor — doğrulandı, değişiklik gerekmedi.

**Testler:** `tests/test_form_field_visibility.py` (61 test). İki katman:

1. **Tablonun içeriği** — her engine × topoloji kombinasyonu için hangi
   alanların göründüğü. Standalone'da 12 cluster alanının üçü engine için
   de yokluğu, PostgreSQL cluster servislerinin başka engine'de hiç
   çıkmaması, SQL Server / MongoDB alanlarının izolasyonu, MongoDB'nin
   her iki topolojide de cluster alanı görmemesi.
2. **Formların tabloyu kullandığı** — bir form ilgili alanı render
   ediyorsa `showField` ile sarmalamış olmalı; kendi `engine === "..."`
   koşulunu yazarsa test kırılır. Ayrıca standalone'a geçişte değerlerin
   temizlendiği.

Sınır (testin başında yazılı): bu statik bir denetim. Tablonun içeriğini
ve formların ona bağlı olduğunu doğruluyor, tarayıcıda gerçekten render
edilmediğini doğrulamıyor — frontend'in test koşucusu yok.

## Faz 22 — İŞ 2: Görsel tutarlılık ve hizalama

### Boşluk ölçeği — 26 değerden 8'e

gap/padding/margin için **26 farklı rem değeri** vardı: 0.05, 0.1, 0.15,
0.2, 0.3, 0.35, 0.4, 0.45, 0.55, 0.6, 0.65, 0.7, 0.8, 0.85, 0.9, 1.1,
1.2, 1.3, 1.9, 2.9... Aynı işlevdeki iki öğe 1-2px farkla hizasız
duruyordu ve yeni kod hangi değeri seçeceğini bilmiyordu.

`:root`'a 4px'lik bir ölçek kondu (`--space-1` … `--space-12`) ve mevcut
değerler en yakın adıma oturtuldu: **CSS'te 253, satır içi stillerde 44
değer**. En büyük kayma 3.2px (0.05rem → 0.25rem), çoğu 1px'in altında.
Sonuç: 8 farklı değer, hepsi ölçek üzerinde.

Dokunulmayanlar: `font-size`, `border-radius`, `width`, `top` gibi
özellikler — orada 1-2px kayma anlam değiştirebilir.

### Sayfa geçişlerinde kayma

**Yatay kayma.** Kısa bir sayfadan uzun bir sayfaya geçerken dikey
kaydırma çubuğu belirip içeriği ~15px sola itiyor, geri dönerken geri
itiyordu. `html { scrollbar-gutter: stable }` yeri her zaman ayırıyor.

**Dikey sıçrama.** Başlığın altında açıklama satırı olan sayfalarla
olmayanlar arasında geçerken içerik yukarı/aşağı zıplıyordu.
`.page-header`'a sabit bir alt sınır (`min-height: 3.5rem`) kondu.

### Yükleniyor durumları içeriğin yerini koruyor

`PageLoading` ortalanmış küçük bir kutuydu: veri gelince sayfa boyu birden
değişiyor ve içerik sıçrıyordu. `PageSkeleton` ve `TableSkeleton` eklendi —
gelecek içeriğin kabaca yüksekliğini şimdiden ayırıyorlar.

Değiştirilen yerler:

| Sayfa/bileşen | Öncesi | Sonrası |
|---|---|---|
| ApplicationsPage, DatabaseGroupsPage, ServersPage | ortalanmış kutu | 4 satırlık iskelet |
| GroupDetailPage, InstanceDetailPage, DatabaseWizardPage | ortalanmış kutu | 6 satırlık iskelet |
| `TableState` (tüm liste sayfaları) | tek satır spinner | iskelet satırlar |
| DashboardPage "Sorunlar ve öneriler" | tek satır "Yükleniyor…" | 3 satırlık iskelet |
| QueryDiagnosticsPanel | tek satır "Yükleniyor…" | 3 satırlık iskelet |

İskelet `prefers-reduced-motion` altında parıldamıyor.

### Birincil eylem konumu

Üç farklı kalıp vardı: çoğu sayfada `.header-actions`, `InstancesPage`'de
doğrudan `<header>` çocuğu (dikey hizası farklı düşüyordu),
`ReportsPage`'de ayrı tanımlı `.report-actions`. İkisi tek kurala bağlandı
(`align-items: center`, ölçekten boşluk, `flex-shrink: 0`) ve
`InstancesPage`'in düğmesi ortak kaba alındı.

### Kart yükseklikleri

Yan yana duran kartlar içeriği kısa olanda yukarıda bitiyor, satır kırık
görünüyordu. `.grid` için `align-items: stretch` + `.grid > .card { height: 100% }`;
aynısı `.stats-grid` kutucukları için.

**Testler:** `tests/test_visual_consistency.py` (61 test) — ölçeğin
tanımlı olduğu, CSS'teki ve HER sayfa/bileşendeki satır içi boşlukların
ölçek üzerinde olduğu, `scrollbar-gutter` ve başlık alt sınırının
yerinde durduğu, iskelet bileşenlerinin var olduğu ve kayıt yükleyen
sayfaların onları kullandığı (`PageLoading` geri dönerse test kırılır),
birincil eylemlerin ortak kapta olduğu, ızgara kartlarının eşit
yükseklikte olduğu.

Sınır (testin başında yazılı): denetim statik. "Kural yerinde mi"
sorusunu cevaplıyor, "piksel doğru mu" sorusunu değil — frontend'in test
koşucusu ve tarayıcı otomasyonu yok. Gerçek görsel doğrulama için
`npm run dev` ile bakılmalı.

## CANLI 500 DÜZELTMESİ — instance silme (Faz 23)

**Belirti:** `DELETE /api/instances/1` → 500. Arayüz "Sunucuya ulaşılamıyor"
diyordu. Silme öncesi kontrol ise "bağlı hiçbir kayıt yok — güvenle
silinebilir" diyordu; ikisi çelişiyordu.

### Kök neden — dört katmanlı

**1. Bağımlılık listesi elle yazılmıştı ve ayrışmıştı.** `instances.id`'ye
foreign key ile bağlı **10 tablo** var; sayım ve cascade listesi yalnızca
**8'ini** biliyordu. Eksik olanlar: Faz 20'de eklenen `prediction_outcomes`
ve Faz 17'de eklenen `daily_state_snapshots`. `prediction_outcomes`
üretilen HER tahmin için satır yazıyor, yani instance 1'de kesinlikle
kayıt vardı.

Bu, projede **üçüncü kez** görülen aynı hata sınıfı: elle tutulan bir liste
model listesiyle sessizce ayrıştı (önceki ikisi `PredictionOut.advice` ve
`ReportFindingOut.facts`).

**2. Yerelde yakalanamıyordu.** SQLite foreign key zorlamasını varsayılan
olarak KAPALI tutuyor (`PRAGMA foreign_keys = 0`). Yerelde ve testlerde
silme sessizce başarılı oluyor, geride öksüz satır bırakıyordu; Postgres
her zaman zorluyor. "Yerelde yeşil, canlıda patlak"ın bu vakadaki
mekanizması buydu.

**3. Hata 500 olarak dönüyordu.** `IntegrityError` yakalanmıyordu.

**4. 500'de CORS başlığı yoktu.** Starlette'in sunucu-hatası katmanı CORS
middleware'inin DIŞINDA; tarayıcı yanıtı okuyamıyor ve `fetch` ağ hatası
gibi başarısız oluyor. Kullanıcı "Sunucuya ulaşılamıyor" görüyordu — oysa
sunucuya ulaşılmış ve 500 dönmüştü. Teşhisi saptıran şey buydu.

### Düzeltme

**Liste artık türetiliyor.** `services/deletion.py` bağımlı tabloları
SQLAlchemy metadata'sından çıkarıyor. Yeni bir tablo `instances.id`'ye
foreign key koyduğu anda sayıma ve cascade'e kendiliğinden dahil oluyor —
unutulması mümkün değil. Metadata boş gelirse fonksiyon sessizce boş liste
DÖNMÜYOR, hata veriyor (aynı hatanın tekrarı olurdu).

Cascade kuralı FK'nın kendisinden geliyor: **nullable ise bağ koparılır**
(`nodes.instance_id` — düğüm cluster topolojisinin parçası, veritabanı
kaydının değil; `instances.group_id` — instance grup silinince yaşamalı),
**zorunlu ise kayıt silinir**. Temizlik **özyinelemeli**, çünkü bağımlılar
kendileri de üst kayıt olabiliyor (müşteri → uygulama → grup → düğüm);
düz bir silme zincirin ortasında ihlale düşerdi.

**SQLite'ta `PRAGMA foreign_keys=ON`.** Bu olmadan aşağıdaki testlerin
hiçbiri hatayı yakalayamazdı.

**409 + hangi tablo.** `commit_or_conflict` `IntegrityError`'ı yakalayıp
"Bu instance'a bağlı kayıtlar var (3 tahmin doğruluk kaydı, 1 günlük durum
fotoğrafı)" biçiminde 409 döndürüyor.

**500'lerde CORS.** `app.main`'e bir `Exception` işleyicisi eklendi;
middleware zincirinin İÇİNDE çalıştığı için yanıt CORS'tan geçiyor ve
istemci gerçek durum kodunu görebiliyor.

### Aynı hata diğer silme akışlarında da vardı

Denetim sonucu:

| Akış | Durum |
|---|---|
| Müşteri | ORM `applications` zincirini kapsıyordu ama **`servers` kapsanmıyordu** → Postgres'te 500 |
| Grup | **`group_health_snapshots` hiçbir ilişkiyle kapsanmıyordu** → 500; ayrıca bağlı instance'ların silinmesi değil bağının kopması gerekiyor |
| Uygulama | Zinciri ORM kapsıyordu, ama grup seviyesindeki eksikler oraya da yansıyordu |
| Sunucu | Zaten düğüm sayıp 409 dönüyordu — doğruydu, ortak commit yoluna bağlandı |
| Düğüm | `nodes.id`'ye bağlı tablo yok — güvenli, yine de ortak yola bağlandı |

Beşi de artık aynı türetilmiş listeyi ve aynı hata yolunu kullanıyor.

### Frontend

`ApiError` zaten kullanılıyordu ve davranışı DOĞRUYDU: CORS başlığı
olmayan bir yanıtı tarayıcı gerçekten okuyamaz, bu ağ hatasından
ayırt edilemez. Asıl düzeltme sunucu tarafındaydı. Yine de status-0 mesajı
her iki olasılığı da söyleyecek şekilde güncellendi ("bağlantı kopmuş ya
da sunucu CORS başlığı olmayan bir hata döndürmüş olabilir"), ve silme
işleyicisi `errorMessage()` kullanarak sunucudan gelen `detail` metnini
gösteriyor.

**Testler:** `tests/test_delete_dependencies.py` (17 test) — SQLite'ın
kısıtları gerçekten uyguladığı (ön koşul), listenin türetildiği ve iki
eksik tablonun adıyla kilitlendiği, sayımın onları gördüğü, gizli
bağımlılığın 500 değil 409 verdiği, cascade'in öksüz satır bırakmadığı,
düğüm/instance bağlarının koparıldığı (silinmediği) ve müşteri/grup
zincirlerinin eksiksiz temizlendiği. **Düzeltme geri alındığında 5 test
düşüyor** — hatayı yakaladıkları doğrulandı. Toplam 906 test yeşil.

## Faz 24 — Playwright tarayıcı testi altyapısı

**Gerekçe:** 900+ backend testi hepsi API katmanında; bu turda tekrar
görüldüğü gibi "kod doğru ama arayüz kırık" durumlarını (rapor bulgu
detayının çökmesi, 404'ler, kırpılan DROP INDEX komutu, boş instance
formu) hiçbiri yakalayamıyor. Bu tur yeni özellik yok — eksik olan test
katmanı kuruldu.

### İŞ 1 — Kurulum

- `frontend/playwright.config.ts`: iki `webServer` (backend 8001,
  frontend 5174) otomatik başlıyor. Portlar geliştirme portlarından
  (8000/5173) BİLEREK ayrı — testler açık bir dev sunucusuna bağlanıp
  geliştirme verisine dokunmasın diye.
- `frontend/e2e/start-backend.mjs`: backend'i izole ortamla açıyor.
  `DATABASE_URL` ayrı bir SQLite dosyası (`data/dbace_e2e.db`) ve dosya
  her koşudan ÖNCE siliniyor → her koşu temiz şemayla başlıyor.
  `RUN_MODE=api` ile zamanlayıcı kapalı: testler hiçbir hedef
  veritabanına bağlanmaya çalışmıyor.
- Her test kendi verisini API üzerinden kuruyor (`ApiHelper`) ve
  siliyor; benzersiz ad üretimi (`ApiHelper.unique`) testlerin
  birbirini ezmesini engelliyor.
- `npm run test:e2e`, `test:e2e:ui`, `test:e2e:critical` script'leri.
  Çalıştırma talimatı README'de ("Tarayıcı testleri (Playwright)").
- `frontend/e2e/global.setup.ts`: zorunlu ilk şifre değişimini API'den
  tamamlıyor, sonra GERÇEK arayüzden giriş yapıp `storageState`
  kaydediyor — token'lar uygulamanın kendi kodu tarafından yazılsın diye
  (elle token enjekte etmek gerçek oturum akışını atlar).

### İŞ 2 — Kritik akış testleri

32 test, 6 dosya; 15'i `@critical` etiketli:

| Dosya | Kapsam |
|---|---|
| `auth.spec.ts` | giriş, çıkış, oturumsuz yönlendirme, hatalı şifre |
| `routing.spec.ts` | deep-link'ler, derin adreste yenileme (SPA rewrite), silinmiş kayda deep-link, geri düğmesi |
| `wizard.spec.ts` | dört topolojide grup ekleme; her adımda doğru alanların göründüğü ve yanlış alanların DOM'da HİÇ olmadığı |
| `instances.spec.ts` | düzenleme, bağlı kaydı olan/olmayan silme, engine/topoloji alan görünürlüğü |
| `reports.spec.ts` | rapor üretme, bulgu detayı açma, durum değiştirme, dışa aktarma, yönetici görünümü |
| `dpa-dashboard.spec.ts` | DPA sekmeleri, grafik aralık seçimi, dashboard sayaç kartıyla filtreleme |

### İŞ 3 — Konsol hatası denetimi

`e2e/support/fixtures.ts` içindeki `consoleWatcher` fixture'ı her testte
`console.error` ve yakalanmamış sayfa hatalarını topluyor; test bitiminde
liste boş değilse test KIRILIYOR. Vite HMR ve React DevTools tavsiyesi
gibi gürültü desenleri filtreleniyor. Bu sayede "ekran doğru görünüyor
ama arkada hata var" durumu sessizce geçemiyor.

### İŞ 4 — CI entegrasyonu

`.github/workflows/ci.yml`'ye dördüncü iş (`e2e`) eklendi:

- Her push/PR'da yalnızca `@critical` akışlar (`test:e2e:critical`) →
  CI süresi makul kalıyor.
- Tamamı gecelik (`schedule: 0 2 * * *`) ve elle tetiklemede
  (`workflow_dispatch`).
- Yalnızca chromium kuruluyor; amaç tarayıcı uyumluluğu değil, akışların
  çalışması.
- Başarısız testte ekran görüntüsü, video ve iz (trace)
  `actions/upload-artifact@v4` ile yükleniyor (7 gün). İz
  `npx playwright show-trace <dosya>` ile adım adım incelenebiliyor.

### Tarayıcı testlerinin BULDUĞU gerçek hatalar

Altyapı kurulur kurulmaz iki gerçek hata çıktı — ikisi de backend
testlerinin göremeyeceği yerdeydi:

1. **Sekme değişimi geri düğmesini bozuyordu.**
   `InstanceDetailPage` sekmeyi URL'e `replace: true` ile yazıyordu.
   Sonuç: kullanıcı bir sekmeye geçip GERİ bastığında beklediği sekmeye
   değil, uygulamadan TAMAMEN DIŞARI çıkıyordu (`about:blank`). Faz
   19'da diğer sayfalar `useUrlTab` ile düzeltilmişti ama burası elle
   yazılmış olduğu için atlanmıştı. Düzeltme: `setSearchParams(params)`.
2. **Dashboard sayaç kartıyla filtreleme geri alınamıyordu.**
   `useUrlFilter` her zaman `replace` kullanıyordu. Arama kutusu için
   doğru (her tuş vuruşu geçmişe yazılmamalı), ama TIKLAMAYLA seçilen
   filtre bilinçli bir gezinme adımı. `useUrlFilter` artık
   `options.history` alıyor; varsayılan `replace`, dashboard durum
   filtresi `push`.

Ayrıca `InstancesPage`'deki bağımlılık dökümü elle yazılmış alan
listesinden, sunucudan gelen `breakdown` haritasına çevrildi (Faz 23'te
eklenen iki tabloyu göstermiyordu) ve `InstanceDependencies` tipi elle
yazılmak yerine üretilen şemadan türetildi.

### Statik testlerin güçlendirilmesi

Tarayıcı testinin yakaladığı hatanın bir daha girmemesi için statik
denetim de sıkılaştırıldı:

- `test_api_contract_alignment.py`: `MUST_BE_DERIVED` listesine
  `InstanceDependencies` eklendi.
- `test_navigation_integrity.py`: yeni
  `test_tab_changes_are_pushed_to_history_not_replaced`. Mevcut kontrol
  "sekme URL'de mi" diye bakıyordu ve `replace` ile yazmayı yeterli
  sayıyordu; yeni test hem `useUrlTab` hook'unun push yaptığını hem de
  sekmeyi elle yazan sayfaların `replace` kullanmadığını doğruluyor.
  **Testin ilk hâli hatayı yakalayamıyordu** (bir yorum satırında geçen
  "useUrlTab" kelimesi sayfayı denetim dışı bırakıyordu); düzeltme geri
  alınıp koşularak yakaladığı doğrulandı. Setter bulunamazsa test
  sessizce atlamak yerine kırılıyor — hiçbir şeyi korumadığı hâlde yeşil
  görünmesin diye.

### Kapsam dışı (bilerek)

- **Görsel piksel karşılaştırması (visual regression)** bu turda yok;
  önce işlevsel akışlar.
- Yalnızca chromium; Firefox/WebKit çalıştırılmıyor.

**Durum:** 32 e2e testi ~29 saniyede yeşil, backend 907 test yeşil
(1 skip), `npm run build` yeşil.

## Faz 25 — İŞ 1: Aktif oturum örnekleyicisi (bekleme analizi altyapısı)

**Eksik olan neydi:** pg_stat_statements'ın kümülatif toplamları "bu sorgu
206 ms sürdü" diyor ama bu sürenin NEREDE geçtiğini söylemiyor — disk
okuyarak mı, kilit bekleyerek mi, CPU'da mı. Bu ayrım olmadan öneri de
üretilemiyor: "index ekleyin" ile "uzun transaction'ı kısaltın" bambaşka
teşhisler.

### Neden ayrı bir döngü

Mevcut toplama döngüsü 15 saniyede bir çalışıyor ve KÜMÜLATİF sayaç okuyor.
Bekleme analizi bunun tersini ister: anlık durumun SIK tekrarlanan fotoğrafı.
15 saniyede bir bakmak, 200 ms süren bir kilit fırtınasını hiç görmemek
demek. Bu yüzden örnekleyici kendi işinde (`wait_event_sampling`), kendi
aralığında (varsayılan 1 sn) ve kendi KALICI bağlantısında çalışıyor.

### İzlenen sunucuya yük bindirmemek için yapılanlar

- **Kalıcı tek bağlantı**, turlar arasında yeniden kullanılıyor. Saniyede bir
  bağlantı açıp kapatmak (TCP + TLS el sıkışması + backend fork), ölçmeye
  çalıştığımız yükün kendisini üretirdi.
- **Tek sorgu, tek round trip**, filtre sunucu tarafında (`state='active'`) —
  binlerce boşta oturum ağdan geçmiyor.
- **`statement_timeout = 1000ms`** (toplama döngüsünün 5000 ms'inden sıkı):
  saniyede bir çalışan bir sorgunun asılı kalması, sıradaki turları da
  geciktirir ve ölçümde delik açar.
- **`pg_blocking_pids()` yalnızca kilit bekleyen satırlarda** çağrılıyor. Bu
  fonksiyon lock manager'ı dolaşır; her satır için çağrılsaydı örnekleme
  sorgusunun en pahalı parçası olurdu, oysa cevabı sadece `Lock`
  beklemesinde anlamlı.
- **`max_instances=1`**: bir tur 1 saniyeyi aşarsa APScheduler ikinci turu
  paralel başlatırdı — aynı bağlantı üzerinde iki eşzamanlı sorgu ve bozuk
  sayaçlar demekti.
- Instance'lar **paralel** örnekleniyor: erişilemeyen tek bir sunucu
  diğerlerinin örneklemesini geciktirmiyor.
- `WAIT_SAMPLING_ENABLED=false` ile tamamen kapatılabiliyor — o zaman hedefe
  hiç bağlantı açılmıyor.

README'nin "İzleme yükü" bölümüne yeni bir alt bölüm eklendi. Depolama
maliyeti ÖLÇÜLDÜ (satır başına 192 bayt, indeksler dahil); izlenen sunucudaki
sorgu maliyeti bu ortamda ölçülemedi (PostgreSQL/psql/Docker yok) — bunun
yerine kullanıcının kendi sunucusunda çalıştırabileceği ölçüm sorgusu
yazıldı ve sınır SORULAR.md'ye işlendi.

### Ham örnek saklanmıyor — dakikalık toplama

1 saniyelik örnekleme, aktif oturum başına saniyede bir satır demek: 10
eşzamanlı aktif oturumlu bir sunucuda günde ~864 bin satır. Örnekler süreç
belleğinde dakikalık kovalarda toplanıp dakika kapandığında yazılıyor:
`(dakika, queryid, kategori, olay)` başına TEK satır. Ölçülen küçülme ~40 kat.

Üç tablo:

| Tablo | Ne tutuyor |
|---|---|
| `active_session_minutes` | Dakikalık PAYDA: alınan örnek sayısı, aktif oturum toplamı, bloklanan oturum toplamı |
| `wait_sample_minutes` | Dakikalık KIRILIM: (queryid, kategori, olay) başına örnek sayısı |
| `wait_query_signatures` | queryid → sorgu metni sözlüğü (distinct queryid başına tek satır) |

**Payda neden ölçülüyor, varsayılmıyor:** AAS = aktif oturum toplamı / alınan
örnek sayısı. Örnek sayısını `60/aralık` diye sabit varsaymak, örnekleyicinin
geciktiği, worker'ın yeniden başladığı ya da sunucunun erişilemediği
dakikaları beşte bir yüke sahipmiş gibi gösterirdi — yani tam da incelenmesi
gereken dakikaları sakinleştirirdi.

**Aynı dakikaya ikinci kova EKLENİYOR, üzerine yazılmıyor:** worker dakika
ortasında yeniden başlarsa iki yarım kova aynı dakikayı temsil eder ve
toplamları o dakikanın gerçeğidir.

### Bekleme taksonomisi tek yerde

`app/domain/waits.py`, PostgreSQL `wait_event_type` ve SQL Server `wait_type`
sözlüklerini ORTAK bir kategori kümesine indirger (cpu, io, lock, lwlock,
client, ipc, timeout, buffer_pin, activity, extension, memory, other).
Grafik, öneri üretimi ve rapor aynı kategorileri konuşuyor — iki modülün aynı
beklemeyi farklı sınıflandırması, projede daha önce yaşanan "aynı veriyi iki
yerde ayrı hesaplama" tuzağının aynısı olurdu.

İki eşleme kararı özellikle önemli:

- **Beklemenin YOKLUĞU da veridir.** `wait_event_type IS NULL` + `state=active`
  = oturum CPU'da çalışıyor. Bu satırları atmak, veritabanı yükünün genellikle
  en büyük bileşenini görünmez yapar ve grafiği "sistem hep bekliyor" diye
  yalancı hâle getirirdi.
- **`SOS_SCHEDULER_YIELD` bir bekleme değil CPU baskısıdır.** Adı bekleme gibi
  görünse de anlamı "CPU kotasını doldurdu, sıraya girdi". IO ya da kilit
  saymak, CPU sorununu tamamen yanlış yerde arattırırdı.

Ayrıca paralel işçiler (`backend_type='parallel worker'`) DAHİL sayılıyor:
yalnızca lideri saymak, 8 işçiyle çalışan paralel bir sorgunun yükünü 1
gösterir ve ölçüyü anlamsız kılardı.

### Saklama ve silme

Üç tablo da saklama politikasına dahil (`services/retention.py`) — dakikalık
toplansa da sınırsız değil. `wait_query_signatures` `last_seen_at` üzerinden
temizleniyor: artık hiç görülmeyen sorgunun imzası anlamsız.

Instance silme akışı bu tabloları KENDİLİĞİNDEN gördü: Faz 23'te bağımlılık
listesi elle yazılan bir listeden SQLAlchemy metadata'sından türetmeye
çevrilmişti. Üç yeni tablo hiçbir şey yapmadan bağımlılık sayımına ve
cascade'e girdi — o turda yapılan işin karşılığı. Arayüzdeki etiket haritasına
Türkçe adları eklendi.

**Testler:** `tests/test_wait_sampling.py` (14 test) — 60 örneğin tek satıra
indiği, paydanın ölçüldüğü, dakika dönüşü, yeniden başlatmada kovaların
toplandığı, sorgu metninin sözlükte tek kez durduğu, bağlantı koptuğunda
önceki örneklerin kaybolmadığı, MongoDB/kapalı instance'ların
örneklenmediği, ve taksonomi kararları. **Kova toplama yerine üzerine yazma
konulduğunda ilgili test düşüyor** — hatayı yakaladığı doğrulandı. Toplam
923 test yeşil.

## Faz 25 — İŞ 2: Veritabanı yükü (Average Active Sessions)

AAS, bekleme analizinin merkez metriği: bir aralıkta ortalama kaç oturumun
aynı anda iş yaptığı. Tek başına bir sayı olarak bile "sunucu ne kadar
meşgul" sorusunu CPU yüzdesinden daha doğru cevaplıyor — CPU yüzdesi, kilit
bekleyen 40 oturumu %2 diye gösterir. Asıl gücü kırılımında: AAS bekleme
kategorisine bölününce "sistem neyi bekliyor" sorusu ölçümle cevaplanıyor.

```
AAS = (aralıkta görülen aktif oturum toplamı) / (aralıkta ALINAN örnek sayısı)
```

### Yeni uç

`GET /api/instances/{id}/database-load?hours=` veya `?start=&end=`

Metrik grafiğiyle AYNI kalıp: grafikte sürükleyerek seçilen aralık aynı uca
`start`/`end` olarak gidiyor. Ayrı bir "seçili aralık" ucu yazmak, iki farklı
hesap ve iki farklı cevap riski demekti.

Yanıt: zaman serisi (kategoriye göre kırılmış), aralık toplamları, en çok yük
üreten sorgular ve her birinin bekleme profili, baskın kaynak ve onun
CÜMLEYLE yazılmış hükmü.

### Toplama veritabanında yapılıyor

7 günlük bir aralıkta ham satır sayısı yüz binleri bulabiliyor; hepsini
Python'a çekmek, Faz 21'de `slow_query_samples` yüzünden yaşanan 502'nin
aynısını davet ederdi. Kova (bucket) hesabı SQL'de yapılıyor; epoch ifadesi
lehçeye göre değişiyor (SQLite `strftime`, PostgreSQL `extract`), tek fark bu.

Kova genişliği aralığa göre seçiliyor ki nokta sayısı ~180'i geçmesin: 1 saat
→ 1 dakika, 7 gün → 60 dakika. Daha fazlası hem ağdan boşuna geçer hem de
ekranda piksel başına birden çok noktaya düşer.

### Veri yetersizse SAYI ÜRETİLMİYOR

Üç ayrı durum, üçünde de boş grafik değil AÇIKLAMA dönüyor:

| Durum | Ne diyor |
|---|---|
| Hiç örnek yok | Örnekleyicinin yalnızca worker sürecinde çalıştığı, ilk verinin birikmesinin birkaç dakika sürdüğü |
| 60'tan az örnek | Kaç örnek olduğu ve anlamlı ortalama için kaç gerektiği — "bu kadar az örnekten yük ortalaması üretmek, ölçüm gibi görünen bir tahmin olurdu" |
| MongoDB | Bekleme sözlüğünün karşılığı olmadığı |
| Örnekleyici kapalı | `WAIT_SAMPLING_ENABLED=false` olduğu |

### Baskın kaynak kullanıcıya CÜMLEYLE söyleniyor

Grafikten çıkarım yapmasını beklemek yerine: "Yükün %78'i disk g/ç kaynaklı.
Veri diskten okunuyor ya da diske yazılıyor; cache'te bulunamadı."

Bir kategori baskın sayılmak için %40 eşiğini geçmek zorunda. Altındaysa
"tek bir baskın kaynak yok — en yüksek pay %34 ile ..." deniyor. %34'lük bir
kategoriye bakıp "IO darboğazı" demek yanıltıcı olurdu ve tek bir
değişiklikle toparlanma beklentisi yaratırdı.

### "En yavaş sorgu" ile "en çok yük üreten sorgu" farklı sorular

5 saniye süren ama günde iki kez çalışan bir sorgu, 20 ms süren ama saniyede
300 kez çalışan bir sorgunun yanında hiçbir şey. Yavaş sorgu listesi
birincisini, AAS ikincisini öne çıkarıyor — DPA sınıfı araçların asıl katkısı
bu. Her sorgu kendi bekleme profiliyle geliyor: süresinin yüzde kaçını hangi
beklemede geçirdiği.

### Dürüstlük kararları

- **Arka plan boşta beklemesi (`activity`) yük sayılmıyor** — saymak grafiğe
  hiç inmeyen yalancı bir taban ekler.
- **queryid'siz satırlar sahte bir "sorgu" olmuyor.** PostgreSQL 14 öncesinde
  `pg_stat_activity.query_id` yok; kırılım yine üretiliyor ama sorgu listesi
  boş kalıyor ve `query_attribution_available=false` ile bu AÇIKÇA
  bildiriliyor.
- **Sorgu metni sözlükte yoksa uydurulmuyor**: "(sorgu metni kaydedilmemiş —
  queryid X)" deniyor ki kullanıcı en azından kimliği pg_stat_statements'ta
  arayabilsin.
- **Kategori etiketi ve anlamı SUNUCUDAN geliyor.** Arayüzde ayrı bir çeviri
  tablosu tutmak, aynı beklemenin iki farklı adla görünmesi demekti.

**Testler:** `tests/test_database_load.py` (18 test). **Paydayı sabit 60
varsayan hatalı sürüm konulduğunda ilgili test düşüyor** — hatayı yakaladığı
doğrulandı. TypeScript tipleri üretilen şemadan türetildi. Toplam 942 test
yeşil.

## Faz 25 — İŞ 3: Veritabanı yükü grafiği ve darboğaz sınıflandırmasının gerçek veriyle beslenmesi

### Yeni sekme: Veritabanı Yükü

DPA sekmelerine `load` eklendi — Metrikler ile Yavaş Sorgular ARASINDA.
Sıra bilinçli: "ne kadar meşgul" → "neyi bekliyor" → "hangi sorgu", teşhisin
doğal akışı. Mevcut sekmeler değişmedi.

`components/DatabaseLoadPanel.tsx`:

- **Yığılmış alan grafiği**: X ekseni zaman, Y ekseni AAS, renkler bekleme
  kategorisi. Yığının kalınlığı yükü, rengi sebebini söylüyor.
- **Sürükleyerek aralık seçme**: mevcut `useChartRangeSelection` altyapısı
  kullanıldı; seçilen aralık `start`/`end` ile AYNI uca gidiyor ve altta o
  aralığın sorguları listeleniyor. Seçilen son kova da aralığa dahil
  ediliyor — aksi halde kullanıcının gördüğü son sütun cevaba girmiyordu.
- **Her sorgu için bekleme profili**: tek satırlık yığılmış çubuk + yüzde
  etiketleri. "Bu sorgu süresinin yüzde kaçını hangi beklemede geçirdi."
- **Baskın kaynak** üstte, cümleyle ve kategori renginde bir şeritle.
- **İskelet yükleme** (Faz 22 kuralı): içeriğin yeri korunuyor, kayma yok.
- **Hata durumu ham `unknown` olarak saklanıyor**, metne çevrilmiş hâli
  değil: `PageError` `ApiError.status`'a bakıp 500 ile ağ kopmasını ayırıyor
  (Faz 23'te "Sunucuya ulaşılamıyor" yanlış teşhisi tam bu ayrımın
  kaybolmasından çıkmıştı). "Tekrar dene" düğmesi var.

Renkler arayüzde, **ad ve anlam sunucuda**. İkinci bir çeviri tablosu tutmak,
aynı beklemenin grafikte ve raporda iki farklı adla görünmesi demekti.
Kategori sırası da sunucudan geliyor (AAS'e göre sıralı) — burada ayrı bir
sıra tanımlamak iki farklı öncelik olurdu.

CSS boşlukları tek ölçekten (`--space-*`), göz kararı değer yok.

### Darboğaz sınıflandırması artık ÖLÇÜMLE besleniyor

`query_diagnostics.py` bugüne kadar `exec_user_time`/`exec_sys_time`
sütunlarından türetiyordu. O sütunlar pg_stat_statements'ın sürüm/ayarına
bağlı ve pratikte çoğu kurulumda BOŞ geliyor — yani sınıflandırma çalışıyor
görünüyordu ama neredeyse her zaman "bilinmiyor" dönüyordu.

Artık bir sorgunun bekleme profili varsa teşhis ONDAN yapılıyor ve
`confidence="observed"` oluyor. Bunun üç somut sonucu var:

1. **"Kilit" sınıfı artık çıkarım değil ölçüm.** Öncesinde dbace sorgu başına
   kilit bekleme süresi tutmadığı için kilit teşhisi her zaman
   `inferred`'dı ve "kesin teşhis için Activity sekmesine bakın" deniyordu.
2. **Ölçüm, blok sayaçlarını yeniyor.** Sayaçlar sorgunun neye DOKUNDUĞUNU
   söyler, beklemenin nerede olduğunu değil: diskten çok okuyan bir sorgu,
   süresinin çoğunu bir kilidi beklerken geçiriyor olabilir. Bu çelişkide
   ölçüm kazanıyor.
3. **Yeni bir sınıf: `client`.** Bekleme ölçümü, sorgunun süresini uygulamanın
   veriyi çekmesini bekleyerek geçirdiğini gösterebiliyor. Bunu "bilinmiyor"
   saymak, DBA'yı veritabanında olmayan bir sorunu aramaya yollardı —
   teşhisin en değerli hâli bazen "sorun burada değil"dir.

Bekleme verisi yoksa eski türetme aynen devrede; yalnızca "bilinmiyor" mesajı
artık ne yapılacağını söylüyor (Veritabanı Yükü sekmesini işaret ediyor).

Baskınlık eşiği (%40) iki modülde de aynı sabit; farklı olsaydı aynı sorgu
için grafikte "IO baskın" derken tanıda "belirsiz" denebilirdi.

**Testler:** `tests/test_wait_diagnostics_ui.py` (14 test). Yarısı teşhis
mantığı, yarısı arayüz garantisi — frontend'in kendi test koşucusu olmadığı
için (CLAUDE.md) kaynak sınıfı listesinin, kategori renklerinin ve sekme
kaydının statik olarak korunması gerekiyor: backend yeni bir kaynak sınıfı
üretip arayüz haritasına eklenmezse kullanıcı BOŞ bir hücre görür, hata da
alınmaz.

**Tarayıcı testi:** `e2e/dpa-dashboard.spec.ts` sekme turuna eklendi, ayrıca
yeni bir `@critical` test: e2e'de örnekleyici kapalı olduğu için sekme "veri
yok" durumunu gösteriyor — boş grafik gösterip susmak yerine SEBEBİNİ yazdığı
tarayıcıdan doğrulanıyor. 33 e2e testi yeşil, konsol temiz.

Toplam 958 backend testi yeşil.

## Faz 25 — İŞ 4: Bekleme tipine göre öneri

Bekleme analizinin değeri ölçümde değil, ölçümün EYLEME dönüşmesinde. "Yükün
%78'i disk g/ç" tek başına bir bilgi; "shared_buffers'ı RAM'in %25'ine
çıkarın, komutu şu, riski şu, doğrulaması şu" bir eylem.

`services/wait_advice.py`, baskın bekleme kategorisine göre CLAUDE.md'deki
beş parçalı standarda uyan öneri üretiyor: neden (iş etkisiyle) → numaralı
adımlar → adım başına komut → dikkat notları (kilit/süre/bakım
penceresi/geri alma) → doğrulama sorgusu. Öneri, `DatabaseLoadOut.advice`
olarak dönüyor ve arayüzde rapor/dashboard/tahminlerle AYNI `AdviceCard`
bileşeniyle gösteriliyor — grafiğin hemen altında, "ne yapmalıyım" için sayfa
değiştirmek gerekmesin diye.

### Kategori bazında ne öneriliyor

| Baskın kategori | Önerinin özü |
|---|---|
| **IO** | Önce cache oranını ölç, sonra en çok disk okuyan tabloyu bul, planına bak: Seq Scan varsa index (CONCURRENTLY), Index Scan'de hâlâ okuyorsa bellek |
| **Lock** | Kimin kimi beklettiğini gör, `idle in transaction` süresini ölç, `idle_in_transaction_session_timeout` ile kalıcı çözüm, acil durumda önce cancel sonra terminate |
| **LWLock** | Ayrı ele alındı: kullanıcı kilidi değil iç çekişme. Çözüm bağlantı havuzu — **`max_connections` artırmak sorunu BÜYÜTÜR** |
| **CPU** | Plan incele (Rows Removed by Filter), sıralamayı index'le kaldır, istatistik tazeliğini kontrol et |
| **Client** | **Veritabanında yapılacak bir şey yok** — uygulama tarafına yönlendir |
| **IPC** | Paralellik ayarları; Workers Planned/Launched farkına bak |
| **Memory** | work_mem, ama çarpım uyarısıyla |

SQL Server için ayrı komut seti (`sys.dm_*`, `UPDATE STATISTICS`,
`READ_COMMITTED_SNAPSHOT`). PostgreSQL komutunu SQL Server'a önermek,
kullanıcının kopyalayıp yapıştırdığında hata alması demekti — çalışmayan
öneri, öneri değildir.

### En değerli cevap: "sorun burada değil"

`client` baskınsa öneri açıkça "veritabanı sorunu DEĞİLDİR; index eklemek,
parametre değiştirmek ya da donanım büyütmek bu süreyi kısaltmaz" diyor ve
dikkat notunda "bu tabloyu görüp veritabanı parametreleriyle oynamak zaman
kaybıdır" uyarısı var. Bunu gizleyip yerine genel bir "sorgularınızı gözden
geçirin" önerisi üretmek, ölçümü çöpe atmak ve ekibi haftalarca yanlış yerde
arattırmak olurdu.

### Öneri üretilemeyen durumlar

- **Baskın kategori yoksa** (%40 eşiğinin altı): "en yüksek pay %35, bu orana
  göre eylem önermek yükün yarısından azını hedefleyen bir işe yönlendirmek
  olurdu" deniyor. Eşik `database_load.py` ve `query_diagnostics.py` ile
  aynı sabit.
- **Kategori tanınmıyorsa / motor desteklenmiyorsa / o kategori için hazır
  plan yoksa**: nereye bakılacağı yazılıyor (pg_stat_activity'nin wait_event
  değeri, `sys.dm_os_wait_stats`), genel geçer bir cümle uydurulmuyor.

En çok yük üreten sorgunun metni `EXPLAIN` adımına GÖMÜLÜYOR: adım somut bir
komut oluyor, kullanıcının doldurması gereken bir şablon değil.

**Testler:** `tests/test_wait_advice.py` (80 test, çoğu parametrik). Her
kategori × her motor için beş parçalı standardın tamamı kontrol ediliyor;
ayrıca komut sızıntısı testi var (PostgreSQL önerisinde `sys.dm_` geçemez,
SQL Server önerisinde `pg_stat_activity` geçemez).

Bir tuzak testin kendisinde çıktı ve not edilmeye değer: Python'ın
`str.lower()`'ı Türkçe "İ"yi "i" + birleşen nokta olarak veriyor, yani
`"DEĞİLDİR".lower() != "değildir"`. Öneri metinleri vurgu için büyük harf
kullandığından, karşılaştırma normalize edilmeden yapılsaydı test yeşil
görünürken hiçbir şeyi kontrol etmiyor olurdu. `_says()` yardımcısı bunu
Türkçeye uygun şekilde normalize ediyor.

Toplam 1039 backend testi yeşil.

## Faz 25 — İŞ 5: Bekleme analizi ön koşulları

Bekleme örnekleyicisinin YETKİSİZ çalışması, hiç çalışmamasından DAHA
KÖTÜDÜR — ve bunu görmek zor. Yetkisiz bir PostgreSQL rolü
`pg_stat_activity`'de diğer kullanıcıların satırlarını görür ama `state`,
`query` ve `wait_event` alanları NULL gelir. Sonuç: örnekleyici çalışır, veri
birikir, grafik çizilir, hiçbir hata çıkmaz — ama yalnızca kendi oturumları
sayılır. Ekran "sunucu sakin" der, sunucu yanarken. Yani boş veri değil,
**YANLIŞ veri** üretir.

### PostgreSQL — üç yeni kontrol

**`wait_visibility`** — yalnızca rol üyeliğine bakmıyor, GÖRÜNÜRLÜĞÜ ÖLÇÜYOR:

```sql
SELECT count(*) FILTER (WHERE backend_type = 'client backend')            AS toplam,
       count(*) FILTER (WHERE backend_type = 'client backend'
                          AND state IS NULL)                             AS maskeli
FROM pg_stat_activity WHERE pid <> pg_backend_pid()
```

Kullanıcının isteği "yetki yoksa ne kadarını görebileceğini söyle" idi;
cevap bir tahmin değil ölçüm: "20 istemci oturumundan yalnızca 3 tanesini
görebiliyor; 17 oturumun durumu ve beklemesi maskeli geliyor." `detail`
alanında da `3/20 oturum görülebiliyor` yazıyor. 20 oturumdan 3'ünü görmek
ile 19'unu görmek bambaşka kararlar gerektirir; "yetkiniz eksik" demek bunu
söylemez.

Rol yok ama o anda maskeli oturum da yoksa (tek kullanıcılı sunucu) durum
yine `partial`: yeşil göstermek, yük geldiğinde körleşecek bir kurulumu
onaylamak olurdu.

**`compute_query_id`** — beklemeyi SORGUYA bağlayan tek alan. Kapalıysa
kırılım yine üretiliyor ("sistem neyi bekliyor" cevaplanıyor), kaybolan
yalnızca "hangi sorgu". Bu yüzden `medium`, `high` değil — çalışan bir
özelliği bozukmuş gibi göstermemek için. `auto` değeri tek başına bir cevap
değil: pg_stat_statements yüklüyse açık demektir ve öyle raporlanıyor;
"auto" görüp "kapalı" demek kullanıcıyı gereksiz bir ayar değişikliğine
yollardı.

**PostgreSQL 14 öncesi** ayrı ele alındı: `query_id` o sürümlerde YOK. "Şu
ayarı açın" demek çalışmayan bir düzeltme önermek olurdu; tek yolun sürüm
yükseltmesi olduğu açıkça yazılıyor ve önem derecesi `low`.

### SQL Server — fonksiyonel DMV testi

`VIEW SERVER STATE` zaten genel olarak kontrol ediliyordu; bekleme analizi
için AYRI ve fonksiyonel bir kontrol eklendi (`dm_exec_requests` +
`dm_os_waiting_tasks` gerçekten sorgulanıyor). Yetki bayrağı ile gerçek
erişim ayrışabiliyor — sunucu düzeyinde DENY, Azure SQL kısıtları, sınırlı
sürümler. Bayrağa güvenip örneklemeyi açmak, boş bir grafiğin sebebini
gizlemek olurdu. Reddedilirse mesaj yine "YANLIŞ veri üretir" diyor; sebebi
belirsiz bir hata ise `unauthorized` değil `unknown` — bilinmeyen hatayı
"yetki eksik" diye raporlamak kullanıcıyı yanlış düzeltmeye yollar.

**Testler:** `tests/test_wait_prerequisites.py` (10 test). **Ölçüm yerine
genel bir "yetkiniz eksik" mesajı konulduğunda iki test düşüyor** — sayının
gerçekten korunduğu doğrulandı.

### Yan bulgu: paralel koşuda kırılgan bir tarayıcı testi

E2E paketini koştururken `routing.spec.ts`'teki "silinmiş instance'a derin
bağlantı" testi paralel koşuda düşüyor, tek başına geçiyordu. Sebep test
altyapısında değil SQLite'ın davranışında: yeni satıra `max(rowid) + 1`
veriliyor, yani EN YÜKSEK id'li satır silindiğinde o id bir sonraki eklemede
YENİDEN KULLANILIYOR. Paralel koşan başka bir spec tam o anda instance
oluşturup silinen id'yi kapıyor ve test "silinmiş kayıt" yerine bambaşka bir
instance'ı açıyordu.

Düzeltme: test, silinecek kayıttan SONRA bir koruyucu kayıt daha oluşturuyor;
böylece silinen id artık en yüksek değil ve yeniden kullanılamıyor. Testi
seri koşmaya zorlamak ya da bekleme eklemek belirtiyi gizlerdi. 33 e2e testi
yeşil, kritik paket arka arkaya iki kez temiz.

Toplam 1049 backend testi yeşil.

## Faz 26 — İŞ 1: auto_explain entegrasyonu (gerçek çalıştırmanın planı)

**Sorun:** dbace EXPLAIN'i SONRADAN çalıştırıyordu. Bu planın, sorgunun yavaş
çalıştığı andaki planla aynı olduğu garanti değil — hatta çoğu zaman değil:

- **Parametre bilinmiyor.** pg_stat_statements sorguyu normalleştirir
  (`WHERE id = $1`); sonradan EXPLAIN alırken `$1` yerine `NULL` konur ve
  planlayıcı bambaşka bir plan seçebilir. Üstelik asıl sorun genelde tam da
  budur: *bazı* parametre değerlerinde plan çöker.
- Veri, istatistikler ve index'ler o günden beri değişmiş olabilir.
- Sorgu yavaşken sunucu yük altındaydı; şimdi değil.

Yani sonradan alınan plan bir TAHMİNDİR. auto_explain eşiği aşan sorguların
gerçekten kullanılan planını yazar.

### Log ayrıştırma

`services/auto_explain.py` auto_explain'in JSON çıktısını host-agent'ın verdiği
log satırlarından çıkarıyor. Üç tasarım kararı:

- **Süslü parantez dengesi sayılıyor**, satır sayısı ya da girinti değil:
  `log_line_prefix` her kurulumda farklı ve ona bağlanan bir ayrıştırıcı
  müşteriden müşteriye kırılırdı.
- **Yarım plan ATILIYOR.** Log penceresi planın ortasında bitmişse blok
  kaydedilmiyor — tamamlanmamış bir planı "yakalandı" diye göstermek yanıltıcı
  olurdu.
- **Ayrıştırılamayan bloklar SAYILIYOR** ve sebebi yazılıyor. En yaygın sebep
  `log_format` değerinin varsayılan `text` olması; sessizce atmak "auto_explain
  açık ama dbace'te plan yok" bilmecesini üretirdi.

Tur başına 50 plan sınırı var: sınırsız okumak, yoğun bir sunucuda tek turda
binlerce satır yazmak demekti (bekleme örnekleyicisinde kaçındığımız hatanın
aynısı).

### Eşleştirme kesin değil ve bu saklanmıyor

auto_explain log'u **queryid yazmıyor** (PostgreSQL 16'da `log_line_prefix`'e
`%Q` eklenebiliyor ama her kurulumda yok, 16 öncesinde hiç yok). Eşleştirme
metin normalleştirmesiyle yapılıyor: literaller ve yer tutucular aynı anahtara
indirgeniyor.

Normalleştirmede bir sıra tuzağı çıktı ve test yakaladı: yer tutucular, sayı
kuralından ÖNCE değiştirilmeli. Tersi olursa yer tutucudaki rakam önce soru
işaretine dönüşüyor, desen artık eşleşmiyor ve pg_stat_statements'ın
normalleştirilmiş metni ile auto_explain'in gerçek değerli metni ASLA
eşleşmiyor — yani yakalanan hiçbir plan sorgusuna bağlanamıyordu.

**Eşleşme bulunamazsa plan yine kaydediliyor** (queryid boş). Yanlış bir sorguya
bağlamaktansa bağlamamak yeğdir ve planın kendisi eşleşmeden bağımsız olarak
değerli.

### Planın kaynağı kullanıcıya gösteriliyor

`ExplainOut` artık `source`, `source_label`, `source_caveat` ve `captured_at`
taşıyor. Üç kaynak var ve üçü farklı güvenilirlikte:

| Kaynak | Etiket | Uyarı |
|---|---|---|
| `auto_explain` | Gerçek çalıştırmadan yakalandı | Yok — bu ölçüm |
| `manual_analyze` | Sonradan EXPLAIN ANALYZE ile alındı | Gerçek satır sayıları doğru ama plan yavaşlık anındaki plan olmayabilir |
| `manual_estimate` | Sonradan EXPLAIN ile alındı | Sorgu çalıştırılmadı; gerçek satır sayısı yok, ayrıca parametre tuzağı |

Arayüzde plan ağacının üstünde renkli bir şeritle gösteriliyor — metin okunmasa
bile renk farkı "bu ölçüm mü tahmin mi" sorusunu cevaplıyor.

### Toplama ve saklama

Ayrı bir zamanlayıcı işi (varsayılan 5 dakika). **Hedef veritabanına HİÇ
bağlanmıyor** — yalnızca host-agent'a HTTP isteği. Sık çekmenin kazancı yok: her
çekim log'un son satırlarını yeniden okuyor.

Tekrar yazma koruması UNIQUE kısıtla: (instance, captured_at, duration_ms,
fingerprint). "En son ne zaman çektik" saymacı kullanmak worker yeniden
başladığında çöker ve aynı planlar ikinci kez yazılırdı.

`captured_plans` saklama politikasına dahil — plan JSON'u satır başına
kilobaytlar tuttuğu için dışarıda bırakılsa en hızlı büyüyen tablo olurdu.

### Ön koşullar: DÖRT ayrı kontrol

"auto_explain kurulu" tek başına hiçbir şey söylemiyor:

| Kontrol | Neyi yakalıyor |
|---|---|
| `auto_explain` | Kütüphane `shared_preload_libraries` içinde mi |
| `auto_explain_threshold` | `-1` = sessizce hiçbir şey yapmıyor; `0` = her sorgu, log diskini doldurur |
| `auto_explain_format` | `text` ise dbace ayrıştıramıyor, planlar log'da birikip görünmüyor |
| `auto_explain_analyze` | Kapalıysa plan gerçek ama GERÇEK SATIR SAYISI yok — sapma analizi (İŞ 2) yapılamaz |

Üçü farklı sonuç, üçü farklı düzeltme. Tek kontrole sıkıştırmak "auto_explain var
ama plan gelmiyor" bilmecesini üretirdi.

Bir yardımcı da eklendi: `_parse_duration_ms`. `SHOW` süre ayarlarını BİRİMLE
döndürüyor (`1s`, `500ms`) ve ham metni `int()` ile çevirmeye çalışmak
patlıyordu — kontrol sessizce "bilinmiyor"a düşer, yani ayar doğruyken bile
kırmızı görünürdü.

### Yönetilen servisler

`docs/AUTO_EXPLAIN.md` yazıldı: kurulum adımları, eşik seçiminin maliyeti
(`log_analyze` ölçüm maliyeti ekler, `log_timing = off` ile azaltılabilir), ve
**log erişimi olmayan ortamlar** (Supabase, RDS, Azure, Cloud SQL) için iki
seçenek. İkincisi salt-okunur izleme kullanıcısının EXPLAIN alabilmesi için
SECURITY DEFINER fonksiyon; kurulum SQL'i, `search_path` sabitlemesinin NEDEN
zorunlu olduğu (klasik ayrıcalık yükseltme yolu), doğrulama ve geri alma
adımlarıyla birlikte yazıldı. dbace bu fonksiyonu bugün otomatik kullanmıyor;
belge, izleme kullanıcısını daha da kısıtlamak isteyen kurulumlar için.

### Yan düzeltme: elle yazılmış tip yine ayrıştı

`ExplainResult` ve `ExplainPlanNode` frontend'de elle yazılmıştı ve yeni alanları
bilmiyordu. Üretilen şemadan türetildi, `MUST_BE_DERIVED` listesine eklendi.
Faz 24'te `InstanceDependencies` ile yaşanan senaryonun aynısı — tek farkı bu
sefer sessizce değil, derlemede patlaması.

**Testler:** `tests/test_auto_explain.py` (16 test) — çok satırlı plan
ayrıştırma, bilinmeyen `log_line_prefix`, yarım planın atılması, metin biçiminin
sayılıp açıklanması, sınır, `log_analyze` durumunun ÇIKTIDAN okunması,
normalleştirme, tekrar yazmama, eşleşmeyen planın yine saklanması. Toplam 1068
test yeşil.

## Faz 26 — İŞ 2: Tahmini vs gerçek satır analizi

Planlayıcı "bu koşuldan 10 satır döner" derse nested loop seçer; gerçekte 2
milyon satır dönüyorsa aynı plan 200 bin kez iç döngü çalıştırır. Plan maliyeti
UCUZ görünür, sorgu pratikte pahalıdır — ve `EXPLAIN` (ANALYZE olmadan) bunu
ASLA göstermez, çünkü orada yalnızca tahmin vardır. Sorgu gecikmesinin en yaygın
sebeplerinden biri budur.

`services/plan_analysis.py` her düğüm için tahmini/gerçek oranını, kendi
süresini ve süre payını hesaplıyor.

### Üç tuzak — üçü de sessizce yanlış sonuç üretir

**1. `Plan Rows`, `Actual Rows` ve `Actual Total Time` DÖNGÜ BAŞINADIR.**
Nested loop'un iç tarafında bir düğüm 200 bin kez çalışıyorsa `Actual Rows: 1`
yazar; toplam 200 bin satırdır. Aynı şekilde `0.02 ms` görünen bir düğüm
gerçekte 4 saniye harcamıştır. Çarpmayı atlamak, sorgunun tamamını tüketen
düğümü "ihmal edilebilir" gösterirdi. (Oran hesabı ham değerlerden yapılıyor —
ikisi de döngü başına olduğu için doğru; toplam satır/süre ise `× loops`.)

**2. `Actual Total Time` ÇOCUKLARI İÇERİR.** Düğümün kendi maliyeti =
kendi toplamı − çocukların toplamı. Bu çıkarma yapılmazsa "en pahalı düğüm"
her zaman kök düğüm çıkar (çünkü kök altındaki her şeyi taşır) ve liste
kullanıcıya hiçbir şey söylemez. Ölçüm gürültüsünde negatife düşmemesi için
sıfırda kırpılıyor.

**3. SAPMA YUKARI DOĞRU YAYILIR.** Alttaki tarama 10 yerine 200 bin satır
döndürdüyse üstündeki her join de yanlış tahmin eder. Hepsini "sapmış" diye
işaretlemek kullanıcıya 12 suçlu gösterir; asıl suçlu EN DERİNDEKİDİR. Kök
neden ayrıca işaretleniyor ve öneri ona göre üretiliyor. Aynı derinlikte iki
bağımsız sapma varsa ikisi de kök nedendir — birini seçmek keyfi olurdu.

### Eşikler

10x oran eşiği (PostgreSQL topluluğunda yaygın kullanılan değer: altındaki
sapmalar planı nadiren değiştirir) VE 100 satır mutlak eşik birlikte aranıyor.
Yalnızca orana bakmak, "1 yerine 15 satır" gibi hiçbir şeyi değiştirmeyen
sapmaları uyarıya çevirir ve gerçek sapmaları gürültüde boğardı.

Fazla tahmin de işaretleniyor: gereksiz hash join ve gereksiz sıralamaya yol
açar. Yalnızca az tahmine bakmak sorunun yarısını görmezden gelmek olurdu.

### Öneri — beş parçalı, en ucuz çözümden başlayarak

Sıra bilinçli: **ANALYZE saniyeler sürer ve sapmaların çoğunu çözer.**
Kullanıcıyı önce `CREATE STATISTICS`'e yollamak çoğu durumda gereksiz
karmaşıklık olurdu.

1. `pg_stat_user_tables` ile istatistik tazeliğini ölç (`n_mod_since_analyze`)
2. `ANALYZE <tablo>`
3. **Koşulda birden çok kolon varsa** → `CREATE STATISTICS ... (dependencies,
   ndistinct)`. Planlayıcı kolonları BAĞIMSIZ varsayar ve seçicilikleri çarpar;
   şehir/posta kodu gibi ilişkili kolonlarda bu korkunç bir az-tahmin üretir.
4. **Koşulda ifade varsa** (`lower(email)`) → ifade istatistiği (PG 14+) ya da
   ifade indeksi. Planlayıcı ifadeler için istatistik tutmaz, varsayılan
   seçiciliğe düşer ve neredeyse her zaman yanılır.
5. `ALTER COLUMN ... SET STATISTICS 500` — çarpık dağılımlar için

Dikkat notlarında iki tuzak açıkça yazılı: `CREATE STATISTICS` **yalnızca
ANALYZE sonrasında** etkili olur (bunu bilmeyen kullanıcı "işe yaramadı" diye
vazgeçer), ve istatistik hedefini 1000'in üstüne çıkarmak nadiren işe yarar ama
planlama süresini uzatır.

Sapma yoksa öneri de YOK — sapmamış bir plan için "istatistiklerinizi
güncelleyin" demek, olmayan bir sorunu varmış gibi göstermek olurdu.

### Arayüz

Plan ağacında her düğümde tahmini ve gerçek satır YAN YANA (`satır 10 →
200.000`), döngü sayısı, düğümün kendi süresi ve süre payı gösteriliyor.
Vurgular:

- **Kök neden**: kırmızı şerit + "kök neden" rozeti (en güçlü vurgu — sapmış 12
  düğüm arasında bakılacak tek yer)
- **Sapmış düğüm**: turuncu şerit + "20.000x AZ tahmin" etiketi
- **Sıcak düğüm**: süre payı %5'i geçenlerde arka plan vurgusu

Renk tek başına bırakılmıyor: her vurgunun yanında metin etiketi var, renk
körlüğünde de okunabilir.

Gerçek satır yoksa (ANALYZE'siz plan ya da `log_analyze` kapalı) boş liste
gösterilip susulmuyor — "sapma yok" demek, hiç ölçüm yapılmadığı hâlde her şeyin
yolunda olduğunu iddia etmek olurdu.

Analiz hem canlı EXPLAIN hem de auto_explain ile yakalanan planlar için
üretiliyor; ikisi de HAM plan JSON'undan besleniyor çünkü `Actual Loops` ve
koşul metinleri sadeleştirilmiş ağaçta yok.

**Testler:** `tests/test_plan_analysis.py` (23 test). **Döngü çarpanı
kaldırıldığında iki test düşüyor** — hatayı yakaladıkları doğrulandı. Toplam
1092 test yeşil.

## Faz 26 — İŞ 3a: Blocking hiyerarşisi (canlı ağaç ve sessiz bloklar)

**"Kaç oturum bloklandı" bir sayıdır; "kim kimi blokluyor" bir cevaptır.**
Bankada en sık sorulan soru ikincisiydi ve dbace onu cevaplayamıyordu.

İŞ 3 büyük olduğu için ikiye bölündü (sıra bozulmadan): bu commit canlı ağaç ve
sessiz blok tespiti; geçmiş kayıtları, deadlock yakalama ve rapor bölümü
ardından geliyor.

### Bloklanma bir ZİNCİRDİR

A, B'yi bekler; B, C'yi bekler; C hiçbir şeyi beklemez ama kilidi tutar.
Ekranda "3 oturum bloklandı" yazması hiçbir işe yaramaz — **müdahale edilecek
tek oturum C'dir.** Zincirin ortasındaki B'yi sonlandırmak sorunu çözmez,
yalnızca bekleyeni değiştirir.

`services/blocking.py` kenar listesini ormana çeviriyor ve kök engelleyiciyi
işaretliyor. Kararlar:

- **`blocked_total` dolaylı kurbanları da sayıyor.** Yalnızca doğrudan
  çocukları saymak, kök engelleyicinin etkisini olduğundan küçük gösterirdi;
  zincirin tamamı onun yüzünden duruyor.
- **Aynı oturum ağaçta bir kez görünüyor.** Çoklu kilitte bir oturumu birden
  çok blokçu bekletebilir; aynı pid'i üç ayrı yerde göstermek zinciri okunamaz
  kılardı.
- **Anlık görüntüde olmayan blokçu için yer tutucu düğüm konuyor.** Blokçu
  başka bir veritabanına bağlı olabilir; zinciri kesmek kullanıcıyı yanlış
  oturuma yönlendirirdi.
- **Bloklamaya karışmayan oturumlar ağaca girmiyor** — "kim kimi blokluyor"
  sorusunun cevabında boşta oturumların yeri yok.
- Kendi kendini bekleyen kayıt (anlık görüntü tutarsızlığı) yok sayılıyor,
  zincir derinliği sert bir sınıra bağlı: sonsuz döngüye girmemek için.

### Sessiz bloklama

`idle in transaction` bir oturum **hiçbir sorgu çalıştırmaz**: CPU harcamaz,
yavaş sorgu listesinde görünmez, aktivite ekranında dikkat çekmez — ama açık
transaction'ıyla kilit tutar ve onlarca oturumu durdurabilir. Bu yüzden:

- Kilit tutan ama bekletmeyen oturumlar da listeleniyor (henüz kimseyi
  bekletmiyor olabilir, ama zaman bombasıdır).
- Uzun süredir açık transaction'lar ayrı bir liste.
- SQL Server'daki karşılığı (`sleeping` + açık transaction) ortak ada
  çevriliyor ki bloklama mantığı iki motorda tek kod olsun.

### Öneri kök engelleyicinin NE YAPTIĞINA göre değişiyor

Bu ayrım kritik ve iki durum için aynı öneriyi vermek ikisinden birini her
zaman yanlış yönlendirmek demek:

| Kök engelleyici | Öneri |
|---|---|
| **Sorgu çalıştırmıyor** | "Sorun veritabanında DEĞİL, uygulamada." Sorguyu optimize etmek hiçbir şeyi değiştirmez. Doğrudan `pg_terminate_backend` (iptal anlamsız — beklemede sorgu yok), kalıcı çözüm `idle_in_transaction_session_timeout` |
| **Uzun sorgu çalıştırıyor** | Önce `pg_cancel_backend` (oturum yaşar), sonra terminate. Sorgunun neden uzun sürdüğünü ölç |

SQL Server için ayrı komut seti. Dikkat notunda bir tuzak özellikle yazılı:
**KILL anında dönmez** — büyük bir transaction'ın geri alınması dakikalar
sürebilir ve bekleme bu süre boyunca devam eder. Bunu bilmeyen DBA "işe
yaramadı" der.

Bloklanma yoksa öneri de YOK.

### Toplama: canlı sorgu, periyodik değil

`collect_blocking` `collect_activity`'den AYRI bir sorgu, çünkü sorulan soru
daha pahalı: her oturum için beklediği kilidin türü/nesnesi (`pg_locks`,
`granted = false`) ve tuttuğu kilit sayısı da gerekiyor. Bunları saniyede
birkaç kez açılan aktivite ekranına eklemek onu gereksiz yavaşlatırdı; bloklama
ekranı kullanıcı açtığında çalışıyor ve 10 saniyede bir tazeleniyor.

Sorgu başarısız olursa **boş ağaç değil sebep** dönüyor: "bloklama yok" demek,
ölçüm yapılmadığı hâlde her şeyin yolunda olduğunu iddia etmek olurdu.

### Arayüz

DPA'ya "Bloklama" sekmesi eklendi — Activity'nin hemen ardından, çünkü ikisi de
"şu anda ne oluyor" sorusunu cevaplıyor ve bloklama onun bir adım derinleşmiş
hâli. Sekme rozetinde bloklanan oturum sayısı görünüyor.

Hiyerarşi görsel olarak da kuruluyor: kök engelleyici kırmızı şerit + rozet,
sessiz bloklar turuncu, alt düğümler girintili ve sol çizgiyle bağlı. Dar
ekranda girinti kaldırılıyor, hiyerarşiyi sol çizgi taşıyor.

Her düğümde: oturum, kullanıcı, uygulama, sorgu, ne kadar süredir
bekliyor/blokluyor, transaction'ın açık kalma süresi, tuttuğu kilit sayısı,
beklenen kilidin türü ve nesnesi.

**Testler:** `tests/test_blocking.py` (25 test). **Kök hesabı bozulduğunda
(her düğüm kök yapıldığında) beş test düşüyor** — zincir mantığını gerçekten
koruduğu doğrulandı. Toplam 1121 test yeşil, 16 kritik tarayıcı testi yeşil.

## Faz 26 — İŞ 3b: Bloklama geçmişi, deadlock tespiti ve rapor bölümü

İŞ 3a canlı ağacı verdi ("şu anda kim kimi blokluyor"). Bu commit ikinci soruyu
cevaplıyor: **"dün gece 03:14'te ne oldu."** En kötü bloklama olayları kimsenin
ekrana bakmadığı saatlerde yaşanır ve sabah geriye kalan tek şey "gece sistem
yavaştı" cümlesidir.

### Olay kaydı — örnekleyicinin kalıcı bağlantısı üzerinde

Bloklama kontrolü **bekleme örnekleyicisinin var olan kalıcı bağlantısında**
çalışıyor, 10 saniyede bir. Ayrı bir iş yapıp 10 saniyede bir yeni bağlantı
açmak, ölçmeye çalıştığımız yükün kendisini üretirdi — Faz 25'te örnekleyici
için verilen kararın aynısı. Bunun için `collect_blocking` dışarıdan bağlantı
kabul edecek şekilde açıldı (canlı uç kendi bağlantısını açmaya devam ediyor).

Olay tanımı: **aynı kök engelleyicinin KESİNTİSİZ bekletme dönemi.** Aynı pid
tekrar bekletmeye başlarsa bu yeni bir olaydır; arada sistem düzelmiş demektir
ve iki dönemi tek olay saymak süreyi olduğundan uzun gösterirdi.

Kararlar:

- **Zirve değerler saklanıyor, anlık değil.** Olayın etkisini "o an kaç oturum
  bekliyordu" değil "en fazla kaç oturum bekledi" anlatır.
- **Süren olayın satırı her turda güncelleniyor.** Bunu ilk yazdığımda yalnızca
  açılışta yazıp kapanışta güncelliyordum; test yakaladı: 9 oturumu bekleten bir
  olay, açılışta 2 görülmüşse raporda 2 kalıyordu. Ayrıca worker olay sürerken
  çökerse son bilinen değerler artık diskte kalıyor.
- **Tek turluk kayboluş olayı kapatmıyor.** Ölçüm penceresine denk gelmemiş
  olabilir; hemen kapatmak tek bir olayı onlarca kısa parçaya bölerdi.
- **5 saniyeden kısa bloklamalar kaydedilmiyor.** Kilit beklemesi veritabanının
  çalışma biçiminin parçasıdır; her çakışmayı olay yazmak gerçek olayları
  gürültüde boğardı.
- **Başlangıç, transaction yaşından tahmin ediliyor**, "ilk gördüğümüz an"dan
  değil: 10 saniyelik örnekleme aralığı olayları sistematik olarak kısa
  gösterirdi. Bu bir yaklaşımdır ve kodda öyle yazılı.
- **"Sessizdi" bilgisi bir kez bile doğruysa korunuyor** — kök engelleyici arada
  sorgu çalıştırmaya başlayabilir ama teşhis açısından belirleyici olan, hiç
  çalıştırmadan bekletmiş olmasıdır.
- Kapanışta açık olaylar kapatılıyor; yoksa `ended_at` sonsuza kadar NULL kalır
  ve olay raporda "hâlâ sürüyor" görünür.

### Deadlock — yalnızca geriye dönük görülebilir

Deadlock bloklamadan **farklı** bir olaydır: veritabanı döngüyü kendisi kırar ve
bir tarafı iptal eder. Yani sürüp giden bir durum değil, ANLIK bir olay — canlı
ekranda hiçbir izi kalmaz, bir dakika sonra bakan hiçbir şey göremez.

- **PostgreSQL**: sunucu log'undan ayrıştırılıyor. Önemli olan, bunun
  auto_explain planlarıyla **AYNI LOG ÇEKİMİNDEN** yapılması — ikinci bir çekim
  aynı satırları ağdan iki kez geçirmek ve agent'a iki kat istek atmak olurdu.
- **SQL Server**: `system_health` XE halka tamponundaki deadlock XML'i
  (2012+ varsayılan açık, ek yapılandırma gerektirmiyor).

**Kurban ve kazanan ayrımı** yapılıyor: kurban veritabanının iptal ettiği
taraftır ve uygulamada hata alan odur. Yalnızca kurbanı göstermek yarım
teşhistir — kurbanın "suçu" genelde yoktur, döngüyü oluşturan kilit sırası
KAZANANINDIR ve düzeltme orada yapılır. SQL Server kurbanı XML'de açıkça
işaretlediği için orada tahmin gerekmiyor; PostgreSQL'de `STATEMENT:`
satırından çıkarılıyor.

**Tekrar anahtarı sorgu metinlerinden türetiliyor, pid'lerden değil.** pid'ler
her deadlock'ta farklıdır ama aynı deadlock tekrar ettiğinde sorgular aynıdır;
bu sayede hem tekrar yazma engelleniyor hem de "aynı deadlock 40 kez oldu"
sorusu cevaplanabiliyor.

### Rapor bölümü

Günlük rapora "Bloklama ve deadlock" bölümü eklendi (Alarmlar'dan önce — "şu
anda ne oldu" bölümü alarmlardan önce okunmalı).

En değerli ayrım bulgu metnine taşındı: kök engelleyici sorgu çalıştırmıyorduysa
bulgu açıkça **"Bu bir veritabanı sorunu değildir: uygulama transaction'ı açmış
ve kapatmamıştır"** diyor. DBA'nın orada yapabileceği kalıcı bir şey yok ve
bunu söylememek onu boşuna arattırır.

Deadlock bulgusunda da yaygın bir yanlış düzeltiliyor: **"deadlock'ın çözümü
yeniden deneme değil, kilit sırası tutarlılığıdır."**

Örnekleyici kapalıysa bölüm `ok` değil `unknown` dönüyor: "bloklama olmadı" ile
"bloklama ölçülmedi" farklı şeylerdir ve ikincisini sorunsuz diye raporlamak
yanıltıcı olurdu.

### Saklama

`blocking_episodes` ve `deadlock_events` saklama politikasına dahil. Hacimleri
küçük ama sınırsız değil — politikanın dışında kalan her tablo eninde sonunda
en büyük tablo oluyor (`slow_query_samples` dersi).

**Testler:** `tests/test_blocking_history.py` (18 test) — olay yaşam döngüsü,
zirve değerler, tek turluk kayboluş, sessizlik bayrağının korunması, başlangıç
tahmini, kapanış; PostgreSQL log ayrıştırma (kurban/kazanan/döngü kenarları,
pid'den bağımsız parmak izi, tekrar yazmama), SQL Server XML ayrıştırma ve bozuk
XML'in tüm turu düşürmemesi. Toplam 1141 test yeşil.

## Faz 27 — İŞ 1: EXPLAIN ve index önerisi çalışmıyordu

Canlıdan bildirilen iki hata:

```
EXPLAIN başarısız: missing FROM-clause entry for table "pn"
Index önerisi: 'public.recurse' tablosu bu veritabanında bulunamadı
```

İkisi de aynı kök nedenin iki yüzü: **emin değilken tahmin etmek.** Sorgular
elle yazılmış bir regex ile ayrıştırılıyordu (`\b(?:FROM|JOIN)\s+(ad)`) ve o
regex takma adları, CTE'leri, alt sorguları birbirinden ayıramıyordu.

### Hata 1 — `recurse` bir CTE, tablo değil

`WITH RECURSIVE recurse AS (...) SELECT ... FROM recurse` sorgusunda regex
`FROM recurse` görüp gerçek tablo sandı, katalogda aradı, bulamadı. Kullanıcının
bildirdiği **"index önerisi bugüne kadar hiç üretilemedi"** durumunun sebebi
buydu: gerçek tablolara (`person`, `orders`) hiç sıra gelmiyordu.

### Hata 2 — `pn` bir takma ad, sorun ise KESİLMİŞ metin

`pn` diye bir tablo aranması kullanıcıyı yanlış yere götürüyordu. Asıl sebep
başka: pg_stat_statements sorgu metnini `track_activity_query_size` sınırında
KESER. FROM yan tümcesi kırpılınca geriye `pn.kolon` referansları kalıyor ve
EXPLAIN haklı olarak "böyle bir tablo yok" diyor. Yani hata sorguda değil,
elimizdeki metnin eksikliğindeydi.

### Gerçek bir SQL ayrıştırıcı

`services/sql_analysis.py` eklendi; **sqlglot** kullanılıyor. pglast
(PostgreSQL'in kendi ayrıştırıcısı) daha doğru olurdu ama C eklentisi derlenmesi
gerekiyor ve Dockerfile'a dokunmak proje kuralıyla yasak — tam gerekçe
SORULAR.md'de. Doğrulanan davranışlar: CTE adları, alt sorgu ve VALUES takma
adları, küme döndüren fonksiyonlar (`generate_series`), şema nitelikli adlar,
`$1` yer tutucuları.

**Ayrıştırma başarısız olursa regex'e DÜŞÜLMÜYOR.** "Sorgu çözümlenemedi" denip
sebebi yazılıyor: yanlış bir tabloya index önermek, kullanıcının canlıda
gereksiz bir index oluşturması demek.

### Kesilmiş metin — EXPLAIN denenmeden reddediliyor

Üç sinyal, güçlüden zayıfa:

1. Metin `...` ile bitiyor (pg_stat_statements'ın kırpma işareti)
2. Kapanmamış parantez — dizgi sabitleri ve tırnaklı tanımlayıcılar sayımdan
   çıkarılıyor, yoksa `'yarim(parantez'` gibi meşru bir sabit yanlış alarm
   üretirdi
3. Uzunluk sınıra dayanmış VE metin ayrıştırılamıyor (tek başına uzunluk
   yeterli değil: tam sınıra denk gelen geçerli bir sorgu da olabilir)

Kesikse EXPLAIN hiç denenmiyor; kullanıcıya sebebi ve `ALTER SYSTEM SET
track_activity_query_size = 4096;` komutu veriliyor.

### Yer tutuculu metinde plan

Önceki davranış `$1` yerine `NULL` koyup denemekti. **Sessizce yanlıştı:**
planlayıcıya BAŞKA bir sorgu sunuluyor ve dönen plan, gerçek çalıştırmanın planı
olmadığı hâlde öyleymiş gibi gösteriliyordu. Kaldırıldı.

| Durum | Davranış |
|---|---|
| PostgreSQL 16+ | `EXPLAIN (GENERIC_PLAN)` — değerden bağımsız plan, uyarısıyla birlikte |
| 16 öncesi | Plan alınamıyor; sebebi ve auto_explain alternatifi yazılıyor |
| ANALYZE + yer tutucu | **Hiçbir sürümde** çalıştırılmıyor — ANALYZE sorguyu gerçekten çalıştırır, uydurma değerlerle çalıştırmak izlenen veritabanında öngörülemez maliyet çıkarır |

GENERIC_PLAN uyarısı plan kaynağı uyarısıyla birleştirilip tek yerde
gösteriliyor (Faz 26'da eklenen `source_caveat` alanı).

### Hata mesajları Türkçe ve eyleme dönük

Yedi yaygın PostgreSQL hatası çevriliyor: ne oldu, neden oldu, ne yapılmalı.
Örnek — `missing FROM-clause entry for table "pn"` artık şunu diyor: *"Sorguda
'pn' takma adına başvuruluyor ama onu tanımlayan FROM/JOIN yan tümcesi metinde
yok. Bu neredeyse her zaman sorgu metninin KESİLMİŞ olmasından kaynaklanır..."*

Eşleşmeyen hatalarda **ham metin korunuyor** — uydurma bir açıklama yazmaktansa
ham hatayı göstermek yeğdir, en azından aranabilir.

### Index önerisinde yeni "üretilemedi" sebepleri

`truncated_query` ve `unparsable_query` eklendi; ikisi de düzeltme komutuyla
birlikte. Mevcut sebep metinlerindeki "regex tabanlı ayrıştırıcı" ifadesi de
gerçeğe göre güncellendi.

**Testler:** `tests/test_sql_analysis.py` (37 test) ve
`tests/test_index_advisor_parsing.py` (9 test). **Eski regex geri konduğunda
CTE ve takma ad testleri düşüyor** — hatayı yakaladıkları doğrulandı. Uçtan uca
test, canlıda hiç öneri üretemeyen sorgunun artık `person` için index önerdiğini
gösteriyor. Toplam 1188 test yeşil.

Bir not: uçtan uca testin eski regex'le de geçtiğini fark ettim — çünkü
`advise()` ayrıştırmayı doğrudan yapıyor, `_extract_tables` üzerinden değil.
Bunu test dosyasındaki yoruma yanlış yazmıştım, düzelttim.

## Faz 27 — İŞ 2: Log gürültüsü

Bekleme örnekleyicisi saniyede bir çalışıyor ve APScheduler her tetiklemeyi INFO
seviyesinde logluyordu. Railway'de **günde 86.400 tur × 2 satır ≈ 172 bin satır**
gürültü — gerçek hatalar bu yığının içinde kayboluyor. Log'un varlık sebebi ise
tam olarak onları görebilmek.

### Gürültülü kütüphane logger'ları susturuldu

`app/logging_setup.py` eklendi. `apscheduler.executors.default` ("Running job",
"Job executed successfully"), `apscheduler.scheduler`, `httpx` (her host-agent
yoklamasını yazıyor) ve `asyncio` INFO ve üstünde WARNING'e çekiliyor.

**Susturma hataları GİZLEMİYOR** ve bu test edildi: APScheduler bir iş exception
fırlattığında zaten ERROR yazıyor, ERROR > WARNING olduğu için görünür kalıyor.

`DEBUG`'da susturma **uygulanmıyor** — DEBUG'a çeken kişi teşhis yapıyordur ve
gürültüyü de istiyordur; orada susturmak onu aradığı satırdan mahrum bırakırdı.

### Örnekleyici: tur başına log yok, 5 dakikada bir özet

Örnekleyici artık iki şey yazıyor:

| Ne zaman | Ne yazıyor |
|---|---|
| 5 dakikada bir | Özet: kaç tur, kaç instance, ortalama süre, en yavaş tur, kaç gecikmiş tur, kaç instance hata veriyor |
| Tur aralığın 3 katını aşarsa | Tek başına WARNING — gecikmiş tur ölçümde delik açıyor ve AAS'in paydasını düşürüyor; özeti beklemek sorunun 5 dakika görünmez kalması demekti |

Aynı bilgi 86.400 satır yerine 288 satırda veriliyor.

Bağlantı kopması ve yetki hataları zaten seyreltilmiş şekilde loglanıyordu
(Faz 25: ilk hata + her 60 turda bir), o davranış korundu.

### LOG_LEVEL ortam değişkeni

`LOG_LEVEL` ile ayarlanabiliyor ve `Settings` üzerinden okunuyor (ham
`os.getenv` değil) — .env dosyası ve Railway değişkenleri tek yerden yönetilsin
diye. Tanınmayan bir değer (`LOG_LEVEL=verbose`) INFO'ya düşüyor **ve uyarı
yazıyor**: yanlış yazım yüzünden log'un tamamen susması, teşhis edilmesi en zor
durumlardan biri olurdu.

API ve worker artık **aynı** yapılandırmayı kullanıyor. Öncesinde yalnızca
worker'da `basicConfig` vardı; ikisinin farklı davranması "worker'da görünen hata
API'de görünmüyor" gibi bir teşhis kaybı demekti.

### Diğer sık işler denetlendi

`evaluate_custom_rules_tick` (10 sn), `collect_all_instances` (15 sn) ve
`wait_sampling_tick` yalnızca HATA durumunda yazıyor — statik bir test bunu
kilitliyor ki gürültü sorunu başka bir kapıdan geri gelmesin.

**Testler:** `tests/test_logging_noise.py` (15 test). En önemlisi
`test_a_normal_round_writes_nothing`: 120 tur koşuluyor ve tek satır bile
beklenmiyor — bu testin düşmesi, günde 86.400 satırın geri geldiği anlamına
gelir. Toplam 1204 test yeşil.

## Faz 27 — İŞ 3: Terminoloji tutarlılığı

**Bildirilen hata:** ana ekranda sağ üstte "+ Yeni instance" yazıyordu; aynı
hedefe (`/customers`) giden Veritabanları sayfasındaki buton "+ Veritabanı Ekle"
diyordu. Aynı yere götüren iki buton, iki farklı şey yapıyormuş gibi
görünüyordu.

Tarama daha fazlasını çıkardı: büyük harf kullanımı bile tutarsızdı —
"+ Uygulama ekle" ile "+ Uygulama Ekle", "+ Düğüm ekle" ile "+ Düğüm Ekle" aynı
üründe yan yana duruyordu.

### Karar: kullanıcı **veritabanı** ekliyor

"Instance" teknik bir terim ve hedef kitlenin yarısı (müşteri yöneticisi) için
hiçbir şey ifade etmiyor. En açık kanıt ürünün kendi metnindeydi: boş durum
mesajı terimi TANIMLAMAK zorunda kalıyordu — *"Bir instance, bağlantı
bilgileriyle izlenen tek bir veritabanıdır."* **Tanım gerektiren bir arayüz
terimi yanlış terimdir.** Zaten CLAUDE.md kuralı da net: kullanıcıya görünen
metinler Türkçe.

| Ekranda | Kodda / adreste |
|---|---|
| **Veritabanı** | `Instance`, `/instances`, `/api/instances` |
| **Düğüm**, **Veritabanı grubu**, **Sunucu** | `Node`, `DatabaseGroup`, `Server` |

**Adresler değiştirilmedi.** `/instances` rotasını değiştirmek kullanıcıların
kaydettiği bağlantıları kırardı ve terminoloji kazancı bunu karşılamaz.

**Tek istisna: SQL Server'ın kendi "named instance" kavramı** ("Instance adı",
"Instance portu"). Onu "veritabanı adı" diye çevirmek bambaşka bir şeyi
kastederdi ve kullanıcıyı yanlış değeri girmeye yönlendirirdi.

### Tek gerçeklik kaynağı

`frontend/src/terminology.ts` eklendi: `TERMS` (tekil/çoğul biçimler),
`ADD_ACTIONS` (buton metinleri) ve `ADD_ACTION_BY_TARGET` (hangi rotaya hangi
metin). Metinler artık elle yazılmıyor.

Büyük harf kuralı da orada sabit: **Türkçede Title Case yok**, cümle düzeni
kullanılıyor (yalnızca ilk harf büyük).

### Taranan ve düzeltilen yerler

Buton metinleri, sayfa başlıkları (`Instances` → `Veritabanları`), kenar çubuğu,
boş durum mesajları, "bulunamadı" ekranları, sihirbaz adım adları, açılır liste
seçenekleri, tablo başlıkları, silme onayı metinleri. 12 dosya.

E2E testleri de yeni terminolojiyi bekleyecek şekilde güncellendi.

### Tutarlılığı kalıcı kılan test

`tests/test_ui_terminology.py` (50 test):

- **Aynı hedefe giden birincil butonlar ortak sabiti kullanıyor mu** — bu, tam
  olarak kullanıcının bildirdiği hatayı yakalayan test.
- Elle yazılmış hiçbir "+ ... Ekle" metni kalmamış mı.
- Türkçe cümle düzeni korunuyor mu.
- Her `.tsx` dosyası için ayrı ayrı: kullanıcıya görünen metinde "instance"
  geçiyor mu (kod tanımlayıcıları, rota adresleri ve CSS sınıfları hariç
  tutuluyor; SQL Server istisnası tanımlı).
- Sözlük CLAUDE.md'ye yazılmış mı.

### CLAUDE.md

Terminoloji sözlüğü eklendi. Ayrıca güncelliğini yitirmiş iki yer düzeltildi:
test sayısı (600 → 1200+, Playwright eklendi) ve servis tablosu (Faz 25-27'de
eklenen 8 servis eksikti).

**Not:** CLAUDE.md 212 satır, kuraldaki 200 sınırının 12 satır üstünde.
Sıkıştırabildiğim kadar sıkıştırdım (özellik listesi, servis tablosu, dizin
ağacı); daha fazlası içerik kaybı olurdu. Sınırı 220'ye çekmek ya da Mimari
bölümünü `docs/MIMARI.md`'ye taşımak iki seçenek — karar sizin.

Toplam 1254 backend testi yeşil, 33 tarayıcı testi yeşil.

## Faz 27 — İŞ 4: Desteklenmeyen metrikler için alternatif kaynak

**Bildirilen eksik:** DPA ekranında şu yazıyordu —

> `buffers_backend_per_sec` — PostgreSQL 17+ sürümünde `pg_stat_bgwriter`'dan
> kaldırıldı; doğrudan bir karşılığı yok.

**Bu eksikti: karşılığı VAR.** PostgreSQL 17 sütunu kaldırdı ama aynı bilgi
`pg_stat_io` içinde `backend_type` ve `context` bazında duruyor. Metrik
toplanamıyor değildi; sadece başka yerden alınması gerekiyordu.

"Desteklenmiyor" demek, kullanıcının ekranında bir eksiklik bırakmak ve onu
başka bir araca yönlendirmektir. Alternatifi varken bunu söylemek yanlıştır.

### Sürüm yetenek matrisi — tek kaynak

`app/domain/pg_capabilities.py` eklendi. Her metrik bir **kaynak öncelik
listesi** taşıyor; sürüme uyan ilk kaynak kullanılıyor, hiçbiri uymuyorsa
NEDENİ yazılıyor.

Öncesinde sürüm eşikleri `collectors/postgresql.py` içinde sabit olarak
yazılıydı, her dal kendi "desteklenmiyor" metnini üretiyordu ve biri yanlıştı.
Yeni bir sürüm çıktığında nereye bakılacağı belli değildi.

| Metrik | PG 12-16 | PG 17-18 |
|---|---|---|
| checkpoint sayaç ve süreleri | `pg_stat_bgwriter` | `pg_stat_checkpointer` |
| `buffers_clean`, `buffers_alloc` | `pg_stat_bgwriter` | `pg_stat_bgwriter` (taşınmadı) |
| **`buffers_backend`, `buffers_backend_fsync`** | `pg_stat_bgwriter` | **`pg_stat_io`** |
| `io_*` | 16+ `pg_stat_io`; öncesinde gerçekten yok | `pg_stat_io` |

PG 17+ sorgusu:

```sql
SELECT SUM(writes), SUM(fsyncs) FROM pg_stat_io
WHERE object = 'relation' AND context = 'normal'
  AND backend_type NOT IN ('checkpointer', 'background writer');
```

Arka plan süreçlerini dışlamak eski `buffers_backend`'in tam karşılığı —
o sütun da zaten "backend'lerin doğrudan yazdığı buffer sayısı"ydı.

### Ayrım korundu: her "yok" yanlış değil

`io_*` metriklerinin PG 16 öncesinde **gerçekten** karşılığı yok. Orada
"desteklenmiyor" demek doğru — ama mesaj artık en yakın alternatifi de
söylüyor (cache hit oranı, `shared_blks` sayaçları). Sürüm numarası da
okunabilir biçimde geçiyor: `150004` değil `15.4`.

### Kaynak kullanıcıya gösteriliyor

Aynı metrik sürüme göre farklı yerden gelebildiği için kullanıcı sayının
nereden geldiğini bilmeli. `Instance.metric_sources` (metrik → view) ve
`server_version_num` saklanıyor; DPA'da "Metrik kaynakları" başlığı altında
katlanabilir bir liste olarak görünüyor.

`server_version_num` ayrıca İŞ 5'in temeli: sürüme bağlı her karar (yetenek
matrisi, ön koşullar, EXPLAIN stratejisi) bu sayıya bakıyor ve her seferinde
sunucuya yeniden sormak gereksiz bir round trip.

### Yan düzeltme: `Instance` tipi de elle yazılmıştı

Frontend'deki `Instance` arayüzü yeni alanları bilmiyordu ve derleme hata
verdi. Üretilen şemadan türetildi (`engine` ve `options` daraltmaları
korunarak), `MUST_BE_DERIVED` listesine eklendi. Bu, aynı hatanın üçüncü
tekrarı — Faz 24 `InstanceDependencies`, Faz 26 `ExplainResult`, şimdi
`Instance`.

**Testler:** `tests/test_pg_capabilities.py` (36 test) — sürüm sahteleyerek her
metriğin her sürümdeki kaynağı, kaynak aralıklarının çakışmadığı, her metriğin
ya kaynağı ya sebebi olduğu (ikisi birden değil), sürüm biçimlendirme ve destek
aralığı uyarıları. `test_postgresql_version_adapt.py`'deki PG 17 testi
**eski yanlış davranışı doğruluyordu** — güncellendi: artık `buffers_backend`'in
toplandığını ve "desteklenmiyor" denmediğini doğruluyor.

README'ye sürüm yetenek matrisi tablo hâlinde yazıldı. Toplam 1290 test yeşil.

## Faz 27 — İŞ 5: PostgreSQL 15-18 tam destek

İŞ 4'te kurulan sürüm yetenek matrisi (`app/domain/pg_capabilities.py`) bu işin
temeliydi; burada kapsamı 15-18 aralığına genişletildi ve **gerçek bir kırılma**
bulundu.

### Bulunan kırılma: PG 18'de `pg_stat_io.op_bytes` yok

PostgreSQL 18 `op_bytes` sütununu kaldırdı ve yerine gerçek bayt sayaçlarını
koydu (`read_bytes`, `write_bytes`, `extend_bytes`).

dbace'in sorgusu `COALESCE(MAX(op_bytes), 0)` içeriyordu. PG 18'de bu sorgu
"column does not exist" verip **tüm `pg_stat_io` sorgusunu düşürürdü** — yani
tek bir sütun yüzünden `io_reads`, `io_writes` ve `io_extends` de kaybolurdu.
Sorgu artık sürüme göre farklı sütun listesi kuruyor.

`op_bytes` bir işlem BAŞINA bayt veriyordu (kullanmak için çarpmak
gerekiyordu); PG 18'in sayaçları doğrudan toplam bayt — daha kullanışlı ve
`io_read_bytes_per_sec` / `io_write_bytes_per_sec` olarak toplanıyor.

### Sürüm bazlı yetenek matrisi tamamlandı

| Metrik | 12-15 | 16 | 17 | 18 |
|---|---|---|---|---|
| checkpoint sayaç/süre | bgwriter | bgwriter | **checkpointer** | checkpointer |
| `buffers_clean/alloc` | bgwriter | bgwriter | bgwriter | bgwriter |
| `buffers_backend(_fsync)` | bgwriter | bgwriter | **pg_stat_io** | pg_stat_io |
| `io_reads/writes/extends` | — | pg_stat_io | pg_stat_io | pg_stat_io |
| `io_op_bytes` | — | pg_stat_io | pg_stat_io | **kaldırıldı** |
| `io_read/write_bytes` | — | — | — | **pg_stat_io** |

Sorgu tarafındaki dallanmalar da README'ye tablo hâlinde yazıldı:
`pg_stat_activity.query_id` (14+), `EXPLAIN (GENERIC_PLAN)` (16+),
`pg_stat_statements.total_exec_time` (13+).

### Ön koşullar sürüme göre doğru kontrol yapıyor

İki yeni kontrol:

- **`server_version`**: sunucu desteklenen aralıkta mı. Aralık dışındaysa
  `partial` (uyarı) — **reddedilmiyor**, çünkü çalışabilecek bir kurulumu boşuna
  engellemek yanlış olurdu. Denetimin geri kalanı yine üretiliyor.
- **`generic_plan`**: yer tutuculu sorguların planı alınabiliyor mu (16+).
  Alınamıyorsa önem derecesi **düşük** — bu bir bozukluk değil, sürümün
  getirmediği bir yetenek. Mesaj auto_explain alternatifini gösteriyor.

Sabit sürüm sayıları (`140_000`) yetenek modülündeki adlandırılmış eşiklerle
değiştirildi.

Bir test ham sürüm numarası sızıntısı yakaladı: `compute_query_id` kontrolünün
`detail` alanı `130009` yazıyordu. Kullanıcıya hiçbir şey söylemeyen bu sayı
`13.9` olarak biçimlendirildi.

### Testler

`test_pg_capabilities.py` 40 teste çıktı; `test_postgresql_version_adapt.py`'ye
PG 18 ve PG 15 dalları eklendi. PG 18 testi gönderilen SORGUYU denetliyor:
`MAX(op_bytes)` istenmemeli, `read_bytes` istenmeli. **Sürüm dallanması
kaldırıldığında bu test düşüyor** — kırılmayı gerçekten yakaladığı doğrulandı.
`test_wait_prerequisites.py`'ye 5 sürüm testi eklendi.

Toplam 1299 test yeşil.

### Doğrulanmayan varsayımlar (SORULAR.md'ye işlendi)

Matris PostgreSQL sürüm notlarına ve katalog belgelerine dayanıyor; **hiçbir
sürüme karşı gerçek bir bağlantıyla test edilmedi** — bu ortamda PostgreSQL yok.
Testler sürüm numarasını sahteleyerek hangi sorgunun gönderildiğini kanıtlıyor;
kanıtlanmayan şey, o sorgunun ilgili sürümde gerçekten çalıştığı.

En riskli üç varsayım (PG 18 sütun adları, PG 17 backend I/O eşleştirmesi,
`pg_stat_checkpointer` sütun adları) SORULAR.md'de tek tek yazılı, kapanma
koşuluyla birlikte: her sürüm için bir kap ayağa kaldırıp `collect_metrics`
çalıştırmak ve `_unsupported_metrics`'in boş geldiğini görmek. Bir sürümde
varsayım yanlışsa ilgili metrik grubu boş gelir, **çökme olmaz** — her sorgu
kendi try/except'inde.

PG 18'in yeni vacuum süre sayaçları (`total_vacuum_time` vb.) bilerek
eklenmedi: yeni metrik eklemek katalog + saklama + arayüz değişikliği demek ve
İŞ 5 sürüm UYUMU işiydi. Gerekçe ve nasıl ekleneceği SORULAR.md'de.

## Tahmin düzeltmesi — ETA aralığı tek noktaya çöküyordu

Faz 26 İŞ 3c üzerinde çalışırken `test_every_prediction_records_how_it_was_produced`
düştü. Değişikliklerim olmadan da düştüğünü doğruladım (stash ile) — yani
**mevcut bir kusurdu**, benim eklediğim bir gerileme değil.

**Belirti:** `assert 39.0 < 39.0` — tahminin "en erken" ve "en geç" günü aynı.

**Kök neden:** eğim belirsizliği regresyonun ARTIKLARINDAN hesaplanıyor
(`se_slope = sqrt(kalıntı varyansı / Sxx)`). Veri kusursuz doğrusalsa artıklar
sıfır, belirsizlik sıfır, `slope_lower == slope_upper` ve aralık tek noktaya
çöküyor.

**Neden dün geçiyordu:** test verisi `date.today()`'e göre kuruluyor. x
değerleri tarihten türetildiği için sayısal koşullanma günden güne değişiyor;
bazı günlerde artık varyansı kayan noktada tam sıfıra inmiyor ve minik bir
belirsizlik kalıyordu. Yani test **tesadüfen** geçiyordu, doğrulukla değil.

**Neden bu bir ürün kusuru:** Faz 20 İŞ 3'ün açık kuralı "tek nokta yerine
aralık sunulsun, tarih uydurmayalım"dı. "39-39 gün" tam olarak yasaklanan şey —
sahip olmadığımız bir kesinliği iddia etmek. **Geçmişin kusursuz uyması geleceği
garanti etmiyor:** yük deseni değişebilir, yeni bir iş eklenebilir, temizlik
çalışabilir.

**Düzeltme:** `MIN_ETA_RELATIVE_SPREAD = 0.10` — aralık en dar hâlinde merkezin
±%5'i olacak şekilde genişletiliyor. Taban YALNIZCA çökmüş aralıklara dokunuyor:
gerçek belirsizlik zaten tabandan genişse olduğu gibi bırakılıyor, çünkü
hesaplanmış bir aralığı yapay olarak büyütmek ölçümü bozmak olurdu.

İki uç durum korundu:

- Eşik zaten aşılmışsa `(0, 0)` — oraya yapay aralık koymak saçma olurdu.
- Eğimin alt sınırı sıfır/negatifse "en geç" **bilinmiyor** kalıyor (`None`),
  çünkü o senaryoda eşiğe hiç ulaşılmayabilir. Genişletme merkez gerektirdiği
  için bu duruma hiç dokunmuyor.

**Testler:** 4 yeni test. **Taban kaldırıldığında kusursuz uyum testi düşüyor** —
çöküşü gerçekten yakaladığı doğrulandı. Ayrıca gürültülü serinin kendi geniş
aralığının daraltılmadığı da test ediliyor.

## Faz 26 — İŞ 3c: Bloklama işinin yarım kalan uçları

Faz 26 İŞ 3 iki commit'te yapılmıştı (canlı ağaç, sonra geçmiş + deadlock).
Gözden geçirince **üç uç açık kalmıştı** — üçü de "yazıldı ama bağlanmadı"
cinsinden, yani test yeşil görünürken özellik çalışmıyordu.

### 1. SQL Server deadlock toplama ÖLÜ KODDU

`SQLSERVER_DEADLOCK_SQL` ve `parse_sqlserver_deadlock_xml` yazılmış ve test
edilmişti ama **üretim kodunda hiçbir yerden çağrılmıyordu**. PostgreSQL
deadlock'ları log çekiminden geliyordu; SQL Server tarafı sessizce hiç
çalışmıyordu. Testler geçiyordu çünkü ayrıştırıcıyı doğrudan çağırıyorlardı.

Bağlandı: `collect_deadlocks()` collector'a eklendi ve olay yakalama işine
(5 dakikada bir) SQL Server dalı kondu.

**İki motor, iki ayrı yol** ve bu koda yazıldı: PostgreSQL'de deadlock sunucu
log'una yazılıyor ve host-agent üzerinden okunuyor; SQL Server'da log yok,
olaylar `system_health` XE oturumunun halka tamponunda duruyor ve oraya
SORGUYLA erişiliyor — agent gerekmiyor. Aynı işte toplanmalarının sebebi
ikisinin de "geriye dönük olay yakalama" olması ve aynı seyrek aralığın ikisine
de uyması.

**Bir hata da yakalandı:** sorgu `DATEADD` ile zaman damgasını yerel saate
çeviriyordu, ama XE zaman damgası **zaten UTC**. Sonuç UTC gibi saklanacaktı —
saat farkı kadar kaymış deadlock kayıtları demek. Dönüşüm kaldırıldı.

### 2. Kurban ve kazanan SORGULARI hiçbir yerde görünmüyordu

Kullanıcının isteği açıktı: *"kurban ve kazanan sorguları göster."* Rapor
yalnızca **pid** taşıyordu — ve pid olaydan sonra hiçbir şey ifade etmiyor,
süreç çoktan kapanmış oluyor.

Artık hem rapor bulgusunun `facts` alanında (kısaltılmış) hem de geçmiş
listesinde (tam metin) görünüyor. Arayüzde kurban kırmızı, kazanan turuncu
şeritle ayrılıyor — **kazanan yeşil DEĞİL**, çünkü döngüyü oluşturan kilit
sırası genelde onundur ve yeşil onu masum gösterirdi.

### 3. Toplanan geçmiş ekrandan görünmüyordu

Veri birikiyordu ama tek çıkışı günlük rapordu. `GET
/api/instances/{id}/blocking-history` eklendi ve Bloklama sekmesinin altına
iki tablo kondu: geçmiş bloklama olayları (başlangıç, süre, etkilenen oturum,
zincir derinliği, kök engelleyici — sessiz blok rozetiyle) ve deadlock'lar.

Geçmiş **ayrı** yükleniyor: canlı ağaç 10 saniyede bir tazeleniyor, geçmiş ise
nadiren değişiyor; ikisini birlikte çekmek 10 saniyede bir gereksiz sorgu
demekti. Geçmiş alınamazsa canlı ağaç yine gösteriliyor — ek bilgi, ana
işlevi düşürmemeli.

Kayıt yoksa sebep yazılıyor ve **kısa beklemelerin bilerek kaydedilmediği**
söyleniyor (5 saniyenin altı), yoksa kullanıcı "hiç mi olmadı" diye şüphelenir.

### Yakalanan bir hata daha

Yeni uç `timedelta` kullanıyordu ama import edilmemişti. **Tam test paketi
yeşil geçti** — çünkü hiçbir test o ucu çağırmıyordu. Canlıda ilk istekte 500
verirdi. Uç testleri yazılınca 8 test birden düştü ve hata görünür oldu.

Bu, bu turun kendi dersi: *"yazıldı ve test edildi" ile "bağlandı ve çağrıldı"
aynı şey değil.* Üç boşluğun üçü de bu ayrımın içine düşmüştü.

**Testler:** `tests/test_blocking_history_api.py` (17 test) — geçmiş ucu,
kurban/kazanan sorguları, zaman penceresi, instance kapsamı, boş durumun sebebi,
SQL Server toplamasının GERÇEKTEN bağlı olduğu, XE zaman damgasının kaymadığı,
bozuk tek bir XML'in turu düşürmediği. **`timedelta` importu kaldırıldığında 8
test düşüyor.** Toplam 1320 test yeşil, 16 kritik tarayıcı testi yeşil.

## Doküman yapısı — CLAUDE.md 212 satırdan 126'ya

CLAUDE.md kuraldaki 200 satır sınırını aşmıştı. Sınırı yükseltmek yapısal
sorunu çözmez, erteler: dosya **her oturumda** okunuyor ve token maliyeti var.
Mimari detayı ise ihtiyaç anında okunacak bir referans.

**Ayrım ölçüsü:** "her oturumda uygulanması gerekiyor mu?" Kurallar ve
terminoloji evet; servis tablosu, süreç diyagramı ve kurulum komutları hayır.

### Taşınanlar

| İçerik | Nereye | Neden |
|---|---|---|
| Mimari bölümü (dizin ağaçları + 20 satırlık servis tablosu) | `docs/MIMARI.md` | Kodda yön bulmak için, ihtiyaç anında |
| Yerel çalıştırma komutları | README (zaten vardı) | **Üçlü mükerrerdi**: CLAUDE.md, README ve MIMARI.md'de ayrı ayrı |
| Canlı ortam tablosu | `docs/MIMARI.md` bileşen tablosu | Aynı bilginin iki hâliydi |

Yerine tek bir **"Nerede ne var"** yönlendirme tablosu kondu (12 satır): hangi
soru için hangi dosyaya bakılacağı.

### docs/MIMARI.md birleştirilirken düzeltildi

Mevcut dosya epey eskimişti ve olduğu gibi üzerine eklemek mükerrer + yanlış
bilgi bırakırdı:

- "SQL Server (stub)" deniyordu — çoktan tam collector.
- Yol haritasında **bitmiş işler** duruyordu: SQL Server collector, metrik
  retention policy.
- Tahmin anlatımı Faz 20 öncesiydi ("son ~40 örnekte linear trend") — artık
  güven aralığı, aralık olarak tarih ve doğruluk geri beslemesi var.
- "Yerel geliştirme" bloğu README ile mükerrerdi.

Eklenen: worker'daki zamanlanmış işlerin tablosu (hangi iş hangi aralıkta ve
neden), günlük işlerin neden `cron` ile kurulduğu (canlıda yaşanan sorun),
tip üretiminin neden zorunlu olduğu, alarm/tahmin/bulgu ayrımı.

### Kalanlar ve gerekçesi

| Bölüm | Satır | Neden kaldı |
|---|---|---|
| Vizyon | 12 | İki ayrı hedef kitle, rapor ve arayüz kararlarının çoğunu belirliyor |
| Nerede ne var | 12 | Yönlendirme tablosu |
| Mevcut durum | 15 | Var olanı yeniden yazmayı engelliyor |
| Testler | 15 | Her iş sonunda çalıştırılıyor |
| Kurallar | 30 | Her oturumda uygulanıyor |
| Terminoloji | 17 | Kullanıcı isteği: kural, referans değil |
| Bilinen sınırlar | 21 | "Bunu bilerek yapmadık" — düzeltmeye kalkışmayı engelliyor |

**126 satır**, sınırın 74 satır altında.

### Sınır artık test edilerek korunuyor

`tests/test_docs_structure.py` (7 test): satır bütçesi (180 — tam sınıra
dayanmış bir dosya bir sonraki eklemede yine aynı soruna düşerdi), referans
içeriğin CLAUDE.md'ye geri sızmaması, kuralların ve terminolojinin yerinde
kalması, ve **yönlendirilen dosyaların gerçekten var olması** (var olmayan bir
dosyaya yönlendirmek, bilgiyi taşımaktan beter: okuyucu arar ve bulamaz).

Mimari bölümü geri konduğunda iki test düşüyor — korumanın çalıştığı
doğrulandı.

## Faz 28 — İŞ 1a: Yedek izleme toplama altyapısı

dbace'te yedek izleme **hiç yoktu**. Bir DBA aracında bu temel direklerden
biri; bankada bir olay sonrası ilk sorulan soru "son yedek ne zaman alındı" ve
dbace bu soruya cevap veremiyordu.

İŞ 1 büyük olduğu için ikiye bölündü: **1a toplama**, 1b değerlendirme ve
raporlama.

### Yön veren tek kural

> **YEDEK ALINDIĞI VARSAYILMAZ.**

Kayıt bulunamadığında "yedek yok" denmiyor. `backup_probes` tablosu **hangi
yöntemlere bakıldığını, nelerin bulunduğunu ve neyin engellediğini** tutuyor,
çünkü "yedek yok" ile "dbace bulamadı" çok farklı iki şey: birincisini
ikincisi gibi sunmak gerçekten yedeği olan kurumu paniğe, ikincisini birincisi
gibi sunmak yedeği olmayanı sahte güvene sürükler. İkinci hata daha pahalı.

### Kaynaklar motora göre bambaşka

| Motor | Kaynak | Nereden |
|---|---|---|
| SQL Server | `msdb.dbo.backupset` (90 gün, D/I/L) | Veritabanının kendisi |
| PostgreSQL | `pg_stat_archiver`, replikasyon slotları, devam eden taban yedek | Veritabanının kendisi |
| PostgreSQL | pgBackRest / Barman / WAL-G | Host-agent üzerinden araç çıktısı |

PostgreSQL'in `msdb` gibi merkezi bir yedek geçmişi yok; gerçek yedekler
neredeyse her zaman harici bir araçla alınıyor ve durumları yalnızca o aracın
komut çıktısında. Bu yüzden host-agent'a `/v1/backup` ucu eklendi.

### Agent'ta komut çalıştırma DEĞİL, sabit izin listesi

`BACKUP_TOOLS` üç komutu ad→argüman listesi olarak sabitliyor. Genel bir
"komut çalıştır" ucu çok daha esnek olurdu ve **agent'ı ele geçiren herkese
müşteri sunucusunu teslim ederdi**. Agent müşteri sunucusunda root'a yakın
yetkiyle çalışıyor; esneklik burada kabul edilebilir bir bedel değil.

Aracın kurulu olmaması (çıkış kodu 127) **hata değil**: çoğu kurulumda üç
araçtan yalnızca biri var, diğer ikisinin "bulunamadı" demesi normal. Hata
diye raporlamak kullanıcıyı olmayan bir sorunu aramaya yollardı.

**Ayrıştırma agent'ta değil dbace tarafında** (`services/backup_tools.py`):
agent müşteri sunucusunda ve güncellenmesi zor. Araç çıktı biçimini
değiştirdiğinde yalnızca dbace'i güncellemek yetsin diye ham metin taşınıyor.

Ayrıştırılamayan çıktı için **boş liste** dönüyor, tahmin yürütülmüyor —
yanlış ayrıştırılmış bir tarih "yedek 40 gün eski" gibi sahte bir kritik bulgu
üretirdi ve bu, hiç göstermemekten kötü.

### Süre anomalisinde statik taban ŞART

`DURATION_ANOMALY_MULTIPLIER = 2.0` ile birlikte
`DURATION_ANOMALY_MIN_SECONDS = 900` var. Yalnızca son 5 yedeğin ortalamasına
bakmak, süreler küçükken sürekli yanlış alarm üretir: 4 saniyelik yedeklerin
8 saniye sürmesi oran olarak %100 sapma ama pratikte hiçbir şey değil.

### Devam eden yedek sonsuza kadar "devam ediyor" kalmıyor

`store_backup_records` var olan kaydı **güncelliyor**. Yalnızca "yoksa ekle"
yapsaydık, sonda sırasında yakalanan çalışan bir yedek bittikten sonra da
`running` görünürdü ve süre anomalisi mantığı çöpe giderdi.

### Saklama politikası izlemenin kendisini bozuyordu

Genel saklama penceresi 7 güne kadar inebiliyor. **Haftalık tam yedek alan bir
kurumda bu, en son tam yedeği silmek demek** — ve dbace o zaman "hiç yedek
bulunamadı" derdi. Yani saklama politikası, izlemeyi bozardı.

`BACKUP_MIN_RETENTION_DAYS = 60` ile yedek kayıtlarına ayrı ve daha uzun
pencere verildi; `test_backup_retention_is_longer_than_the_general_window` bunu
koruyor.

### Replikaya özel tuzak

Slot sorgusundaki `pg_current_wal_lsn()`, **replikada hata veriyor**. Sorgu
`CASE WHEN pg_is_in_recovery() THEN NULL` ile korundu — korunmasaydı Patroni
replikalarında yedek sondasının tamamı düşerdi.

### Ne yapılmadı ve neden (SORULAR.md'de)

- **msdb saat dilimi taşımıyor**: naive değerler UTC varsayılıyor. Tam yedek
  eşiği gün mertebesinde olduğu için önemsiz, ama **log eşiği 1 saat** — orada
  yanlış kritik üretebilir. Varsayım tek fonksiyonda (`_parse_dt`) duruyor.
- **msdb başarısız yedeği kaydetmiyor**: satır yalnızca yedek başarıyla
  bittiğinde yazılıyor. Uydurma bir `failed` kaydı üretmek yanlış olurdu; SQL
  Server'da başarısızlık ancak Agent iş geçmişinden görülebilir (ayrı kapsam).
- **Barman JSON vermiyor**: metin deseniyle okunuyor ve kırılgan.

### Testler

`tests/test_backup_collection.py` — 28 test: eşikler ve instance başına
ayarlar, ters çevrilmiş eşiğin düzeltilmesi, üç araç ayrıştırıcısı, tekrar
yazmama, devam eden → bitmiş güncellemesi, sonda kaydı, MongoDB'nin
desteklenmediğinin **sessizce atlanmayıp kaydedilmesi**, collector hatasının
yutulmayıp yazılması, saklama penceresi koruması.

Tüm arka uç: **1358 geçti, 1 atlandı**.

## Faz 28 — İŞ 1b: Yedek durumu değerlendirmesi ve raporlanması

İŞ 1a "ne toplandı"yı kurdu; 1b "toplanandan ne sonuç çıkarıldı"yı kuruyor:
yaş eşikleri, süre ve boyut anomalisi, başarısız yedek, arşivleme sağlığı,
slot birikimi, recovery model uyumu — ve hepsinin raporlara bağlanması.

### Üç durum vardır, iki değil

Değerlendirmenin tamamı tek bir ayrımın üzerine kurulu: **yedek var**, **yedek
yok**, **dbace göremedi**. `sla_ok` bu yüzden üç değerli ve `None` hiçbir yerde
`False` gibi işlenmiyor.

`_detection_is_conclusive` kararı veriyor: dbace yalnızca **yetkili kaynağı
gerçekten okuyabildiyse** "yedek yok" diyor.

- **SQL Server**: tek yetkili kaynak `msdb`. Okunabildiyse ve boşsa gerçekten
  yedek yok demektir → kritik.
- **PostgreSQL**: tek yetkili kaynak **yok**. Gerçek yedekler harici araçlarla
  alınıyor ve yalnızca host-agent üzerinden görülebiliyor. Agent yoksa dbace
  kördür ve "yedek yok" **demez** — bu, PostgreSQL tarafında yapılabilecek en
  kolay yanlış olurdu.

Erişilemeyen kaynak "uyarı + belirlenemedi" üretiyor, "kritik + yedek yok"
değil. Yedeği olan bir kuruma "yedeğiniz yok" demek gereksiz panik; olmayana
sessiz kalmak felaket. İkisi de üretilmiyor.

### Eşiklerin ve anomalilerin tasarımı

**Yedek yaşı bitiş zamanından ölçülüyor.** Başlangıcı almak, 6 saat süren bir
yedeği 6 saat daha taze gösterirdi.

**Log eşiği yalnızca log zinciri VARSA uygulanıyor.** SIMPLE recovery ya da
arşivlemesiz bir kurulumda saat başı kritik üretmek gürültüden başka bir şey
olmazdı; "hiç log yedeği yok" durumu recovery model ve arşiv bulgularının işi.

**Süre anomalisinde oran ve statik taban BİRLİKTE aranıyor.** Yalnızca oran:
4 saniyelik yedeğin 9 saniyeye çıkması "2 kat yavaşladı" ama kimse bunun için
uyandırılmamalı. Yalnızca statik taban: büyük veritabanlarında normal olan uzun
süreler sürekli alarma dönerdi. Karşılaştıracak en az 3 geçmiş yedek yoksa
bulgu üretilmiyor — tek bir öncekine göre "iki kat yavaşladı" demek ölçüm değil
tahmin olurdu.

**Boyut küçülmesi büyümeden daha ciddi** (kritik vs uyarı): yarıya inen bir
yedek genelde eksik yedektir ve bu, geri dönüş anında — yapılabilecek hiçbir
şeyin kalmadığı anda — fark edilir.

**Başarısız yedek iki koşulla "güncel"**: yapışkanlık penceresi içinde olmalı
VE sonrasında aynı türde başarılı bir yedek alınmamış olmalı. Yalnızca zamana
bakmak, 15 dakika sonra düzelmiş bir sorunu 24 saat göstermek demekti; hiç
zaman sınırı koymamak ise altı ay önceki hatayı sonsuza kadar kırmızı yakıp
alarm körlüğü üretirdi. Pencere instance başına ayarlanabiliyor.

**Arşivlemede sayaç değil zaman damgası belirleyici.** `failed_count`
istatistik sıfırlanana kadar birikir; geçmişte bir kez hata almış olmak
arşivlemenin bozuk olduğu anlamına gelmez. Bulgu ancak **son hata son başarılı
arşivlemeden sonraysa** üretiliyor.

### Bulunan ve kapatılan eksik: recovery_models atılıyordu

İŞ 1a'da SQL Server collector'ı recovery model bilgisini zaten topluyordu ama
`BackupProbe`'da saklanacak yer yoktu ve veri sessizce atılıyordu. Kolon
eklendi (migration #39). Bilgi yedek kayıtlarından **türetilemez**: log yedeği
hiç alınmamış bir veritabanının geçmişinde hiç satır olmaz, yani "kayıt yok"
durumunun kendisi ancak sunucu yapılandırmasıyla birlikte okunabiliyor.

### Yönetici raporunda yedek güvencesi

İstenen cümle: "son yedek X gün önce, SLA'ya uygun/uygun değil".
`_backup_from_sections` bunu teknik bölümün **sayılarından** türetiyor,
metninden değil — teknik bölüm sunucu adı, kaynak adı ve komut içeriyor ve
hiçbiri yönetici raporuna giremez. Üretilen cümle ayrıca `assert_no_technical_leak`
taramasından geçiyor.

**En eski yedek belirleyici, ortalama değil**: on veritabanından dokuzunun
yedeği dünse ve birininki 40 günse ortalama "4 gün" der ve gerçek riski gizler.

Bölüm bulgu **olmasa da** gösteriliyor. Yöneticinin sorduğu soru "sorun var mı"
değil "yedeğim var mı"; bu sorunun cevabı yalnızca kötü haber olduğunda
görünürse rapor güvence vermiyor demektir.

### Ön koşullara eklendi

- **PostgreSQL**: `archive_mode` durumu ve **host-agent gereksinimi**. Agent
  yoksa kontrol "high" önem derecesiyle eksik işaretleniyor ve metni açıkça
  "GÖREMEZ / bilinmiyor" diyor — "yedek yok" değil.
- **SQL Server**: `msdb.dbo.backupset` okuma yetkisi, düzeltme komutuyla.

### Dördüncü tip kayması yakalandı

`ExecutiveReport` elle yazılmıştı ve yeni `backup` alanını bilmiyordu — alan
sessizce kaybolurdu. `Instance`, `ExplainResult` ve `InstanceDependencies` ile
aynı hata. Türetilmiş tipe çevrildi ve `MUST_BE_DERIVED` listesine eklendi;
artık elle yazılmış hâline dönmesi testte düşüyor.

### Testler

`tests/test_backup_health.py` — 45 test. Ayrıca `test_prerequisites.py`'ye üç
yeni test. Tüm arka uç: **1407 geçti, 1 atlandı**.

## Faz 28 — İŞ 2: Bağımlılık bastırma

Bir düğüm düştüğünde rapor **40 bulgu** üretiyordu. Biri gerçekti ("düğüme
erişilemiyor"), kalan 39'u onun sonucuydu: parametre denetimi dünkü fotoğrafı
okuyup sapma bildiriyor, yedek bölümü eskimiş yedek diyor, ön koşullar eksik
eklenti sayıyor, kapasite tahmini eski trendden konuşuyor.

Asıl zarar sayı değil: **gerçek bulgu 40 satırın içinde kayboluyor** ve kritik
sayacı 40 gösterince "kritik" kelimesi anlamını yitiriyor.

### Yol açan gerçek hata: sonda kalan boşluk hiç görülmüyordu

Kesinti tespiti yalnızca **iki ölçüm arasındaki** boşluklara bakıyordu. Son
ölçümden dönem sonuna kadar geçen süre hiç sayılmıyordu — yani bir düğüm üç
saat önce düşüp bir daha gelmediyse rapor **hiç kesinti göstermiyordu**. En kötü
durum tam da görünmez olan durumdu; erişilebilirlik yüzdesi de olduğundan iyi
çıkıyordu.

Sonda kalan boşluk artık ölçülüyor ve ayrı bir kök sebep bulgusuna çıkıyor:
"veri toplama durmuş, sürüyor". "Dönem içinde 3 kesinti oldu" ile "şu anda hâlâ
erişilemiyor" çok farklı iki şey ve ikincisi bugün müdahale gerektiriyor.

### Grafik tek yerde

`services/finding_dependencies.py` tek merkez. Bölümler kendi bastırma
mantığını yazmıyor; hiçbir bulgu tipi "beni şu durumda gösterme" demiyor.
Gerekçe: bastırma kararının bütünlüğü ancak tek yerden görülebilir — 11 bölüme
dağılmış bir mantıkta "neden bu bulguyu görmüyorum" sorusunun cevabı 11 dosyada
aranır ve iki bölüm birbirini bastırdığında kimse fark etmez.

Aynı grafik **rapor, dashboard ve alarmları** besliyor. "40 alarm e-postası
gitmesin" isteğinin karşılığı alarm tarafına ayrı bir liste yazmak değil, aynı
grafikten geçmek — iki liste zamanla mutlaka ayrışırdı.

| Kök sebep | Kapsam | Bastırdığı |
|---|---|---|
| Veritabanına erişilemiyor (`no_samples`, `unreachable_now`) | instance | yedek, performans, kaynak, şema, bloklama, kapasite, parametre, ön koşul bölümleri + eşik alarmları |
| Patroni servisi kapalı | instance | lider/lider değişimi/lag bulguları |
| etcd quorum kaybı | grup | lider/lider değişimi bulguları (gruptaki tüm düğümlerde) |

### Bastırılan bulgu SİLİNMİYOR

Raporda kalıyor, işaretleniyor ve "kök sebep nedeniyle N kontrol yapılamadı"
satırının altında açılabiliyor. Silmek, bastırma kuralı yanlışsa gerçek bir
sorunu görünmez yapardı; katlamak ise en kötü ihtimalle bir tıklama maliyeti.

Dashboard'da bastırılan satır listeden çıkıyor ama **sayısı kök sebebin
mesajına ekleniyor** ("— 1 bağlı kontrol bastırıldı"): sessizce yok etmek,
kural yanlış olduğunda kimsenin fark edememesi demekti.

### Kısmi sorun kök sebep değildir

En kolay hata, "kısmi" bir durumu kök sebep saymak olurdu. Bir düğüm günün 20
saati ayakta olup son 4 saatte düştüyse **o 20 saatin performans bulguları
gerçektir** ve bastırılmaları veri kaybı olurdu. Bu yüzden kapanmış bir toplama
boşluğu (`collection_gap`) kök sebep sayılmıyor; yalnızca "hiç ölçüm yok" ve
"dönem sonunda toplama hâlâ durmuş" sayılıyor. İkisi de bağlı analizlerin
dayanacağı canlı verinin olmadığını kanıtlıyor.

### Bastırılmayan üç bölüm ve gerekçesi

- **`availability`** — kök sebebin kendisi orada; bastırmak tek gerçek bulguyu
  silmek olurdu.
- **`cluster`** — cluster bilgisi host-agent/Patroni API üzerinden geliyor, yani
  veritabanı bağlantısından **bağımsız bir kanal**. Veritabanına
  bağlanılamazken cluster bilgisi hâlâ doğru olabilir ve o an en çok ihtiyaç
  duyulan bilgi odur.
- **`alerts`** — alarm gürültüsünün kendisini raporlayan bölüm; bastırmak,
  bastırmanın çalışıp çalışmadığını görmeyi engellerdi.

### İki kök sebep birbirini bastırmıyor

Hangisinin "daha kök" olduğuna karar vermek için elimizde kanıt yok; yanlış
tahmin gerçek bir arızayı gizlemek olurdu. İkisi de kök sebep olarak duruyor.

### Servis adına bakan desen

`service_down:patroni` deseni servis adını da eşleştiriyor. Bakmasaydı
**haproxy'nin kapalı olması lider bulgularını bastırırdı** — oysa haproxy'nin
lider seçimiyle ilgisi yok.

### Sayaçlar ve sıralama

Kritik/uyarı sayaçları (rapor listesi, yönetici özeti, genel durum) bastırılmış
bulguları saymıyor. Kök sebep 3× öncelik çarpanı alıyor ve listenin başına
çıkıyor; bastırılanlar dibe iniyor ama sıfırlanmıyor ki açıldıklarında kendi
içlerinde anlamlı sıralansınlar.

### Testler

`tests/test_finding_dependencies.py` — 22 test: grafiğin kendisi (her kural bir
şey bastırmalı, kök sebep kendi alarmını bastırmamalı), kapsam sızmaması,
grup→düğüm inişi, kısmi sorunun kök sebep sayılmaması, servis adı ayrımı,
alarm ve dashboard bastırması, uçtan uca rapor üretimi.

Ayrıca `test_outage_that_is_still_ongoing_at_period_end_is_detected` sonda kalan
boşluk hatasını kalıcı olarak kapatıyor. Tüm arka uç: **1431 geçti, 1 atlandı**.

## Faz 28 — İŞ 3a: Bakım pencereleri ve planlı/plansız kesinti ayrımı

Bakım penceresi olmadan erişilebilirlik sayıları **dürüst değildi**: planlı bir
bakım için alınan 40 dakikalık kesinti, plansız bir arızayla aynı kefeye girip
aylık %99.9 hedefini tek başına deliyordu. Müşteriye "bu ay SLA'yı
tutturamadınız" demek, o kesintiyi müşterinin kendisi onayladıysa yanlış bir
suçlama.

İŞ 3 büyük olduğu için ikiye bölündü: **3a pencereler ve ayrım**, 3b SLA tanımı
ve takibi.

### Ters yön de aynı ölçüde önemli

Her kesintiyi "planlıydı" diye etiketlemek sayıyı yalancı yapar. Buna karşı üç
koruma var:

- **Pencere önceden tanımlanmış olmalı.** Tekrar kuralı **geriye doğru
  genişletilmiyor**; pencere tanımlanmadan önceki kesintileri geçmişe dönük
  planlı saymak, sayıyı istediğin gibi düzeltebilmek demek olurdu.
- **`created_by` oturumdan yazılıyor**, istemciden alınmıyor. "Bu kesinti
  planlıydı" iddiasının denetlenebilir olması bu alanın doğruluğuna bağlı ve
  istemcinin doldurduğu bir alan denetlenebilir değildir.
- **Pencere süresi tekrar aralığından kısa olmalı.** 25 saatlik günlük bir
  pencere üst üste biner ve fiilen "hep bakımdayız" demektir — her kesintiyi
  planlı göstermenin en kolay yolu. Doğrulama bunu reddediyor.

### Tekrar kuralı saklanıyor, örnekler değil

Altı aylık haftalık bir bakımı 26 satır olarak açmak, biri değiştiğinde hepsini
düzeltmek demekti. Kural saklanıyor ve sorgu anında genişletiliyor.

**İleri sarma şart oldu:** kural iki yıl önce tanımlanmış olabilir ("her gün
02:00"). Örnekleri baştan tek tek üretmek `MAX_OCCURRENCES` sınırına bugüne
*varmadan* takılırdı — yani eski bir bakım penceresi sessizce hiç uygulanmazdı.
Sınır artık ilerleme aracı değil, yalnızca bozuk veriye karşı güvenlik.

**Aylık tekrarda kayma düzeltildi:** 31 Ocak'ta tanımlı bir bakım şubatta 29'a
çekiliyor ama martta yine 31 olmalı. Zincirleme eklemek 29 Mart üretirdi ve
bakım her ay bir gün öne kayardı; ay indeksi her zaman tanımdan sayılıyor.

### Kesinti bütün olarak damgalanmıyor, örtüşme ölçülüyor

Bakım 02:00-04:00 iken 03:30'da başlayıp 06:00'a kadar süren bir kesinti yarı
planlı yarı plansızdır. Hepsini planlı saymak arızayı gizler, hepsini plansız
saymak onaylanmış bakımı ceza olarak yazar.

5 dakikalık tolerans var: bakım 02:00'de başlıyorsa servis 01:59'da durmuş
olabilir. Tolerans bilinçli olarak dar — geniş bir tolerans, pencere dışındaki
gerçek bir arızayı planlı göstermeye başlar.

### Sonuçlar

- **Erişilebilirlik yüzdesi plansız süreye göre.** Planlı süre kaybolmuyor,
  ayrı alan olarak raporlanıyor.
- **Tamamen planlı kesintiler bulguya dönüşmüyor**: onaylanmış bir bakımı her
  raporda bulgu olarak göstermek, bulgu listesini takvim haline getirirdi.
- **Bakım sırasında alarm üretilmiyor.** Alarmı üretip "bakımdaydı" diye
  işaretlemek yetmezdi: e-posta yine gider ve bakım gecelerinde nöbetçiyi
  uyandırmaya devam ederdi.
- **Süregelen kesinti bakım penceresindeyse kritik değil, bilgi.** Susturulmuyor
  — bakımın sürdüğünü bilmek de bilgi.
- **Teknik rapora kesinti dökümü** eklendi: ne zaman, ne kadar, planlı mı,
  sürüyor mu.

### Arayüz

Bakım pencereleri Yönetim sayfasına yeni bir sekme olarak eklendi (kendi üst
seviye sayfası yerine: bu bir yapılandırma). "Ayın ilk pazarı" gibi kurallar
bilinçli olarak yok — arayüz karmaşıklığı kazanılan esnekliğe değmiyor ve yanlış
anlaşılan bir kural, olmayan bir bakım penceresi demek.

### Testler

`tests/test_maintenance_windows.py` — 20 test. Testler aynı SQLite dosyasını
paylaştığı için global kapsamlı pencereler sızıyordu; her test artık temiz
tabloyla başlıyor. Ayrıca yoğun örnekleme yardımcısı eklendi: seyrek örnekler
kesinti tespitini eşiğin sınırına oturtup testin ne ölçtüğünü belirsizleştiriyordu.

Tüm arka uç: **1457 geçti, 1 atlandı**.

## Faz 28 — İŞ 3b: SLA tanımı ve takibi

Hedef olmadan erişilebilirlik sayısı bir bilgi ama bir **karar** değil: %99.7 iyi
mi kötü mü, ancak taahhüde göre söylenebilir.

### Kesinti tespiti tek yere taşındı

Kesinti pencereleri yalnızca rapor bölümünün içinde hesaplanıyordu. SLA da aynı
sayıya ihtiyaç duyunca iki seçenek vardı: hesabı kopyalamak ya da tek yere
taşımak. Kopyalamak, raporun "%99.95" derken SLA ekranının "%99.7" demesi
demekti. `services/availability.py` artık tek gerçeklik kaynağı.

### İki türev sayı çıplak yüzdeden değerli

Ayın 3'ünde "%99.2" görmek yöneticiye hiçbir şey söylemiyor: ay dolmadı, sayı
daha değişecek.

- **En iyi durum** — kalan dönem kesintisiz geçerse ulaşılabilecek oran. Bu sayı
  hedefin altındaysa **ay matematiksel olarak kaybedilmiştir** ve bunu ayın
  3'ünde bilmek, 30'unda öğrenmekten bambaşka bir yönetim kararı üretir.
- **Kalan kesinti bütçesi** — SLA'yı ihlal etmeden karşılanabilecek azami
  kesinti. "47 dakikanız kaldı" cümlesi bakım planlamak için doğrudan
  kullanılabilir; "%99.2" değildir.

Bütçe negatife düştüğünde **negatif gösteriliyor**, sıfıra kırpılmıyor: aşımı
gizlemek, ihlali gizlemek olurdu.

### Ölçüm yokluğu %100 değildir

Hiç ölçümü olmayan bir instance ortalamaya `None` olarak giriyor ve dışarıda
bırakılıyor. Aksi halde **izlenmeyen bir sunucu SLA'yı kurtarır** hale gelirdi.
Hiçbir instance ölçülemiyorsa durum "belirlenemedi" — "sistem ayaktaydı" değil.

### Çok veritabanlı kapsamda toplama kararı ve bilinen sınırı

"Uygulamanın erişilebilirliği" tek doğru cevabı olan bir soru değil: replikalı
bir kümede bir düğümün düşmesi uygulama için kesinti değildir ama dbace bunu
bilmiyor.

Seçilen tanım **kapsamdaki veritabanlarının ortalaması**, çünkü erişilebilirlik
bölümü zaten bunu kullanıyor. "Kalan bütçe" de aynı ortalamadan türetiliyor ki
iki sayı çelişmesin — biri ortalamaya diğeri en kötü düğüme dayansaydı hangisine
güvenileceği belirsiz kalırdı. Ortalamanın gizlediği düğüm için **en kötü
veritabanı** ayrıca raporlanıyor.

Bu tanım replikalı kümede SLA'yı **olduğundan kötü** gösterir. Ters yönde hata
yapmamak bilinçli: SLA'yı olduğundan iyi göstermek çok daha pahalı bir yanlış.
Sınır SORULAR.md'de kapanma koşuluyla yazılı.

### Yıllık dönem yok

`monthly` ve `quarterly` var; yıllık bilinçli olarak yok. Bir yılın ortasında
"kalan kesinti bütçesi" o kadar büyük çıkıyor ki uyarı değeri kalmıyor.

### Bulgular ve raporlar

- **Kritik**: dönem matematiksel olarak kaybedildi; **kritik**: bütçe tükendi;
  **uyarı**: bütçenin dörtte birinden azı kaldı.
- Öneriler teknik değil **yönetsel**: SLA'yı kurtaran şey bir komut değil, kalan
  dönemde risk almamak ve müşteriyle doğru zamanda konuşmak.
- Yönetici raporuna "Hizmet seviyesi (SLA)" bölümü eklendi. Sunucu adı ("en kötü
  veritabanı") oraya **girmiyor** — teknik raporda duruyor.
- Aynı kapsam için ikinci bir hedef reddediliyor (409): iki hedef aynı kapsama
  uygulanırsa "SLA tutuyor mu" sorusunun iki cevabı olurdu.

### Testler

`tests/test_sla.py` — 18 test: dönem sınırları (ay/çeyrek, yıl dönümü), planlı
bakımın bütçeyi tüketmemesi, ölçüm yokluğunun %100 sayılmaması, en iyi durumun
gerçekleşenden düşük olamaması, kaybedilmiş dönem tespiti, en kötü düğümün
raporlanması, yönetici metninde teknik sızıntı olmaması.

Bir test `global` kapsam kullanınca diğer test dosyalarının bıraktığı
instance'ları içine aldı ve ne ölçtüğünü kaybetti; grup kapsamına çevrildi.

Tüm arka uç: **1481 geçti, 1 atlandı**.

## API uyumluluğu

Faz 15 İŞ 1 hariç mevcut hiçbir endpoint kırılmadı; `Instance` ile
ilgili tüm uçlar ve davranışları (cluster-health dahil) aynı kaldı.

**Faz 15 İŞ 1 kırıyor (bilerek, gerekçesi görevin kendisi):** `/api/health`
ve `/api/auth/login`/`/refresh` DIŞINDA her `/api/*` ucu artık
`Authorization: Bearer <token>` şart koşuyor — token'sız her istek artık
`401` dönüyor (öncesinde tamamen anonim erişilebiliyordu).
`POST`/`PUT`/`PATCH`/`DELETE` istekleri ayrıca `role=admin` şart koşuyor
(`403` viewer için). Bu, kimlik doğrulaması olmayan hiçbir eski
istemcinin (script, entegrasyon vb.) artık çalışmayacağı anlamına
geliyor — beklenen ve istenen davranış, ama var olan otomasyon varsa
önce bir token alıp `Authorization` header'ı eklemesi gerekecek.
