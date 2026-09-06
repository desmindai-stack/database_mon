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
