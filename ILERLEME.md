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

**Kapsam notu:** Bu component `DashboardPage.tsx` dışında hiçbir
yerde kullanılmıyor (grep ile doğrulandı) — başka bir sayfada ayrı bir
komut-kutusu deseni yok, bu yüzden değişiklik tek dosyaya sınırlı
kaldı.

**Doğrulama:** `tsc -b && vite build` yeşil; backend değişmedi (saf
frontend/CSS değişikliği), 47 test yeşil kaldı. Görsel taşma/kayma
davranışı tarayıcıda tıklanarak denenmedi (otomasyon yok).

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
