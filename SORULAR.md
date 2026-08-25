# Sorular / Varsayımlar

Karar veremediğim veya kapsam belirsizliği olan noktalar burada; her biri için
makul bir varsayımla devam ettim.

## Faz 7 — İŞ 2: deployment mode / environment ayrımı

- `GET /api/config`, `deployment_mode`/`default_customer_name` alanlarını
  `GET /api/health` ile birebir aynı kaynaktan (`Settings`) döndürüyor —
  bilerek: istek açıkça ayrı bir endpoint istedi (muhtemelen health-check
  amaçlı `/api/health`'i uygulama önyükleme/config amaçlı çağırılardan
  ayırmak için), iki endpoint aynı veriyi taşısa da anlamsal olarak ayrı
  tutuyorum.
- Varsayılan müşteri oluşturma (`ensure_default_customer`) `run_mode`'dan
  bağımsız, `init_db()` gibi her zaman çalışıyor (worker sürecinde de) —
  `init_db()` de aynı şekilde `run_mode` kontrolünden önce çalıştığı için
  bu tutarlı bir tercih; worker'ın da müşteri tablosunu görmesi gerekebilir
  (ör. ileride group-based collection worker'da çalışırsa).
- "müşteri oluşturma/silme UI'da kapalı olsun" isteğini iki katmanlı
  uyguladım: private modda `/customers` sayfası otomatik olarak tek
  müşterinin `/customers/{id}/applications`'ına yönlendiriliyor (normal
  kullanıcı hiç customers listesini görmüyor), ayrıca create formu ve Sil
  butonu da `isPrivate` iken render edilmiyor (yönlendirme başarısız olursa
  veya URL'ye elle gidilirse diye ek güvenlik).
- Sidebar'daki link private modda "Müşteri Grupları" yerine "Uygulamalar"
  etiketiyle doğrudan `/customers/{id}/applications`'a gidiyor — tek
  müşteri olduğundan "müşteri seç" adımı anlamsız.
- `seed_demo.py`'deki yeni test grupları (`boa-sqlserver-test`,
  `aapara-postgres-test`) `topology=standalone`, tek düğüm, `role_hint=
  unknown` — prod'daki tam HA topolojisini tekrarlamak yerine gerçekçi bir
  "tek düğümlü test ortamı" örneği verdim.

## Faz 7 — İŞ 3: Dashboard özet endpoint'i

- **Canlı prob, cache yok:** `GET /api/dashboard/summary` her çağrıda tüm
  gruplar için `collect_group_health` + (postgresql gruplarda)
  `collect_parameter_audit`'i paralel (asyncio.gather) çalıştırıyor —
  Faz 2-4'te kurulan "on-demand, cache'siz" mimariyle tutarlı ama demo'daki
  4 grup (tamamı erişilemez host) için ölçtüğümde ~6 saniye sürdü. Gerçek
  altyapıda erişilebilir host'lar çok daha hızlı yanıt verir/hızlı
  reddeder; DNS çözümlemesi başarısız olan demo host'ları timeout'a kadar
  bekliyor. Çok sayıda grup olan kurulumlarda bu endpoint yavaş
  hissedilebilir — periyodik arka plan toplama + cache katmanı ayrı bir iş.
- **`index_advisor` ve `performance_insights` kaynakları `Instance.
  group_id` bağlantısına muhtaç:** Bu iki servis `Instance`/`MetricSample`/
  `SlowQuerySample` üzerinden çalışıyor (Node üzerinden değil). Bir gruba
  `group_id` ile bağlı `Instance` yoksa (ki seed_demo.py hiç Instance
  oluşturmuyor, sadece Node) bu iki kaynak o grup için sessizce boş kalır.
  `parameter_audit` (Node tabanlı) her zaman denenir. Bu üç kaynağın hepsi
  best-effort: herhangi biri hata verirse (ör. `db_username` tanımsız,
  bağlantı reddi) o grup için sessizce atlanır, endpoint hiçbir zaman
  bundan dolayı patlamaz.
- **`top_issues` granülerliği:** Spec "en kritik 10 sorun" dedi, "grup
  başına 1 sorun" demedi — ben grup health raporundaki her somut problemi
  (split-brain, etcd quorum kaybı, düğüm down, no-leader) ayrı bir "sorun"
  olarak ürettim; aynı gruptan birden fazla sorun çıkabiliyor. `prod`
  ortamdakiler her zaman `preprod/test/dev`'den önce sıralanıyor
  (`environment` sonra `severity`), sonra ilk 10'a kesiliyor.
- **Recommendations'da link yok:** Spec şeması `{severity, source, group,
  message}` — `link_hint` içermiyor (issues'daki gibi). Grup adı var ama
  id yok, UI'da bu yüzden tıklanabilir link değil, düz metin olarak
  gösteriliyor.
- **Private modda backend filtrelemiyor, UI gizliyor:** "customer alanı
  gizlenebilir" ifadesini UI katmanında yorumladım — private modda zaten
  tek müşteri var (backend'ce garanti), bu yüzden sunucu tarafında ayrıca
  bir müşteriye göre filtreleme eklemedim; `top_issues`'daki `customer`
  sütununu sadece `isPrivateGroups` iken DashboardPage'de render etmiyorum.

## Faz 8 — İŞ 4: Dashboard düzeni

- **Eski Instance-tabanlı blok tamamen kaldırıldı (birleştirilmedi):**
  Karar: stat kartları (Toplam instance/Aktif bağlantı/Healthy/Alerting/
  Warning/Açık tahmin) üstteki yeni grup-bazlı sağlık sayaçlarıyla
  kavramsal olarak çakışıyordu ("neyin dikkat istediği" sinyali iki kez
  veriliyordu). Filtrelenebilir per-instance tablosunun satırları
  (bağlantı sayısı, cache hit, TPS, I/O, temp, DB boyutu gibi ham metrik
  değerleri) gerçekten üst özette YOK ve benzersizdi — ama Faz 8 İŞ 1'den
  beri her instance zaten sol menü ağacından (Grup → Düğüm → Instance) ve
  Group Detail'deki düğüm kartından erişilebilir; hiçbir instance sayfası
  artık ulaşılamaz durumda değil. Bu yüzden "üst özete birleştir"
  yerine, Faz 7'nin "status-first" tasarım yönünü tamamlayarak tamamen
  kaldırmayı seçtim — ham metrik tablosunu üst özetin içine sıkıştırmak
  o özeti yeniden eski karmaşık haline döndürürdü. Predictions/Alerts
  sayaçları zaten kendi özel sayfalarında (`/predictions`, `/alerts`)
  mevcut, ayrıca yinelenmedi.
- **Sıralama önceliği değişti (bu bir önceki Faz 7 kararının üzerine
  yazıyor):** Faz 7'de `top_issues` `(environment, severity)` sırasına
  göre dizilmişti (prod birincil, severity ikincil) — bu istek açıkça
  "kritik önce, sonra uyarı, PROD ortam öncelikli" dedi, yani severity
  şimdi birincil, environment sadece aynı severity içinde eşit
  durumları ayırt eden ikincil anahtar. `_issue_sort_key` bu yüzden
  `(severity_rank, env_rank)` oldu.
- **Sorun→öneri eşleşmesi sebep-sonuç değil, "aynı grubun en önemli
  önerisi":** dbace'de bir "sorun" (cluster health'ten: split-brain, node
  down, vb.) ile bir "öneri" (parameter_audit/index_advisor/
  performance_insights'tan) arasında gerçek bir nedensel bağlantı yok —
  farklı, birbirinden habersiz tanı alt sistemlerinden geliyorlar. İstek
  "ilgili çözüm önerisi" dediği için elimdeki en yakın yaklaşıklık:
  sorunun ait olduğu grubun en yüksek öncelikli (aynı severity/env
  sıralamasıyla) önerisini o satıra iliştirmek. Bu, bazen konusu
  alakasız görünebilir (ör. "split-brain" sorununun yanında
  "shared_buffers çok düşük" önerisi çıkabilir) — ama spesifikasyonun
  istediği "öneri yoksa boş kutu koyma" davranışı tam olarak uygulanıyor
  (grubun hiç önerisi yoksa `recommendation: null`, UI hiçbir şey
  render etmiyor).

## Faz 8 — İŞ 3: Otomatik yenileme aralığı

- **Ayar global/paylaşımlı, kullanıcı-bazlı değil:** dbace'de bir
  auth/kullanıcı modeli yok (herkes aynı backend'e bağlanıyor), bu yüzden
  yenileme aralığını yeni `app_settings` (basit key/value) tablosuna
  DB'de tek satır olarak sakladım — tüm tarayıcılar/DBA'lar için ortak.
  localStorage (tarayıcı-bazlı) alternatifti ama "Scheduler bu aralığa
  göre çalışsın" isteği zaten sunucu tarafında paylaşımlı bir değer
  gerektiriyordu; ikisini ayrı tutmak (localStorage sadece UI polling'i,
  DB sadece scheduler'ı etkiler) kafa karıştırıcı olurdu, tek kaynak
  seçtim.
- **Aynı ayar hem scheduler'ın canlı-prob aralığını hem dashboard'ın
  önbellek okuma (poll) aralığını kontrol ediyor:** Dashboard'ın kendi
  otomatik yenilemesi `POST /refresh` (pahalı, canlı prob) değil,
  `GET /summary` (ucuz, önbellek okuma) çağırıyor — aynı aralıkta iki
  ayrı kaynağı (biri canlı prob, biri UI poll) senkron tutmanın en basit
  yolu tek bir paylaşılan değer kullanmaktı; kullanıcı isterse ileride
  ikisini ayrı ayar yapabilir ama şu anki istekte "aynı aralık" ima
  ediliyordu.
- **Ayar değişikliği çalışan scheduler'ı `APScheduler.reschedule_job`
  ile canlı günceller** (yeniden başlatma gerekmez) — ama sadece
  `run_mode=worker`/`all` olan, o an ayağa kalkmış process'te. `api`-only
  bir deployment'ta scheduler hiç çalışmadığından bu no-op; DB'ye yazılan
  değer bir sonraki worker başlangıcında (`start_scheduler()` artık DB'den
  okuyor) devreye giriyor.
- **Geçersiz/desteklenmeyen bir `seconds` değeri 400 ile reddediliyor**
  (sadece 10/30/60/300/900/3600) — açık uçlu bir sayısal input yerine
  spesifikasyondaki 6 seçenekle sınırlı tutuldu, istenirse gevşetilebilir.

## Faz 8 — İŞ 2: Cluster'a dönüşüm akışı

- **Engine/topology uyumu denetlenmiyor:** `convert-to-cluster` teorik
  olarak bir `postgresql` grubunu `alwayson`'a veya `sqlserver` grubunu
  `patroni`'ye "dönüştürmeye" izin veriyor (400 dönmüyor) — UI'da
  öntanımlı seçim engine'e göre doğru geliyor ama kullanıcı elle
  değiştirebilir. Mevcut `DatabaseGroupCreate`/`Update` şemaları da bu
  ikisini hiç çapraz doğrulamıyordu, tutarlı bir tercih olarak aynı
  gevşekliği koruyorum; istenirse ayrı bir doğrulama eklenebilir.
- **`access_name` vs `cluster_name` ayrımı:** `access_name` dışa dönük
  bağlantı adresi (DBA'nın bağlanacağı listener/VIP hostname — sol
  menüde ve grup listesinde görünen budur), `cluster_name` ise dahili/
  betimsel cluster kimliği (ör. Always On AG adı, Patroni cluster_name
  parametresi). İkisi genelde aynı değeri taşıyabilir ama zorunlu tutmadım
  — kullanıcı isterse farklı iki değer girebilir.
- **Dönüşüm, var olan düğümün `options`'ını güncellemiyor:** Standalone'dan
  Patroni'ye dönüşünce mevcut düğüm hâlâ `services` override'ı olmayan
  (veya standalone'dayken ayarlanmış) `options` ile kalıyor —
  `_node_services()` zaten `group.topology == "patroni"` kontrolüyle
  düğümün servis setini otomatik genişletiyor (bkz. `cluster_health.py`),
  bu yüzden ekstra bir migrasyon gerekmedi; ama `keepalived_vip`,
  `patroni_port` gibi düğüm-seviyesi prob ayarları hâlâ kullanıcı
  tarafından elle girilmeli (yeni "VIP / IP" alanı sadece `DatabaseGroup.
  vip_address`'e yazıyor, `node.options.keepalived_vip`'e otomatik
  yansımıyor — istenirse ayrı bir iyileştirme).

## Faz 8 — İŞ 1: Node/Instance birleştirme

- **Node silme, bağlı Instance'ı silmiyor:** Bir düğüm silindiğinde
  `node.instance_id` sadece FK'si kırılır (Instance kalır) — hem çünkü
  düğüm kullanıcının seçtiği MEVCUT bir Instance'a bağlanmış olabilir
  (silinmesi başka bir yerde kullanılan gerçek veriyi yok eder), hem de
  metrik/yavaş sorgu geçmişini korumak için. Otomatik oluşturulan
  Instance'lar da bu yüzden "öksüz" kalabilir — kullanıcı isterse
  `/instances`'tan elle silebilir.
- **Instance.environment ≠ DatabaseGroup.environment:** İkisi de
  "environment" adını taşıyor ama anlamları farklı — `Instance.
  environment` eski, "public"/"private" (müşteri görünürlüğü) değeri
  alıyor (auto-create'te `customer.type`'tan geliyor); `DatabaseGroup.
  environment` yeni, "prod"/"preprod"/"test"/"dev" alıyor. Karışıklığı
  önlemek adına isim değiştirmedim (mevcut API'yi kırardı), ama burada
  not düşüyorum.
- **Auto-create edilen Instance, `cluster_name` + `services=
  ["postgresql"]` alıyor ama tam Patroni/etcd/haproxy stack'i değil:**
  Amaç, mevcut per-instance collector döngüsünün (`collect_all_
  instances`) bu instance'ı metrik toplamaya alması (bu da dashboard
  önerilerinin dolmasını sağlıyor — asıl istenen buydu). `cluster_name`
  boş bırakılsaydı legacy Dashboard tablosunda "Cluster/Rol" sütunu boş
  kalırdı; ama `cluster_name` set edip `services`'i boş bırakmak eski
  `collect_cluster_health()`'in varsayılan tam Patroni stack'ini (yanlış
  portlarla, çünkü `instance.options` boş) tekrar tekrar prob'lamasına
  yol açardı — bu da yeni grup-seviyeli `collect_group_health()` ile
  tamamen örtüşen, gereksiz ve potansiyel olarak yanıltıcı bir ikinci
  probe olurdu. `services=["postgresql"]` ile bu, tek bir hafif TCP
  kontrolüne indirgendi.
- **Var olan bir Instance'a bağlanınca `group_id` üzerine yazılıyor:**
  Bir düğümü mevcut bir Instance'a bağlarken o Instance'ın `group_id`'si
  bu düğümün grubuna set ediliyor — Instance başka bir grupta zaten
  kullanılıyorsa sessizce oradan "taşınmış" olur. Kullanıcı bilinçli bir
  seçim yaptığı için (dropdown'dan elle seçiyor) bunu bir hata değil,
  beklenen davranış olarak ele aldım.
- **Sol menü bir seviye daha büyüdü:** Grup → Düğüm; düğüm bir Instance'a
  bağlıysa `/instances/{id}`'ye linkli, değilse düz metin (tıklanamaz,
  "Bağlı instance yok" title'ı ile). "Instance Gezgini" (eski Instance-
  tabanlı ağaç) tamamen kaldırıldı — istenen buydu, artık tüm instance
  erişimi yeni ağacın içinden.
- **`node.options`'ta `db_username`/`db_password`/`db_database` artık
  kullanılmıyor** (bu üç anahtar artık Node oluşturma/güncellemede üst
  seviye alanlar, Instance'a taşınıyor) — ama `services/credentials.py`
  içindeki `encrypt_node_options`/`decrypt_node_options`/
  `redact_node_options` fonksiyonlarını kaldırmadım; herhangi bir eski
  kayıtta `options.db_password` kalmışsa hâlâ doğru şifrelenip/redakte
  ediliyor (geriye dönük uyumluluk, ölü kod değil).

## Faz 7 — Sol menü: tek gezinme ağacı (test turu revizyonu)

İlk İŞ 2 denemesi (bkz. aşağıdaki "test turu düzeltmeleri" bölümü) sadece
etiketleri değiştirmişti; asıl istenen tek bir gerçek Customer →
Application → DatabaseGroup ağacıydı. Bu revizyonda:

- **Müşteri/uygulama satırları link değil, sadece toggle:** "müşteriye
  tıklayınca altında Uygulamalar açılsın" ifadesini birebir uyguladım —
  ara düğümler (`NavTreeBranch`, `href` yoksa) tıklanınca sadece
  genişliyor/daralıyor, herhangi bir sayfaya gitmiyor. Sadece yaprak
  (grup) `Link` ile `/groups/{id}`'e gidiyor.
- **Bu yüzden CRUD sayfalarına (yeni müşteri/uygulama/grup ekleme
  formları) ağaçtan doğrudan erişim kayboluyordu** — eskiden düz link
  doğrudan o sayfaya götürüyordu. Regresyonu önlemek için: bir dal boş
  children döndürürse ("Kayıt yok" yerine) ilgili create sayfasına giden
  küçük bir "+ X ekle" linki gösteriyorum (`emptyHref`/`emptyLabel`).
  Dolu bir müşteri/uygulamanın CRUD sayfasına ulaşmak hâlâ sadece
  breadcrumb üzerinden oluyor (Group Detail → "← Uygulama" →
  "← Müşteriler"); bunu da eklemek istenirse ayrı bir istekte
  netleştirilmeli.
- **Ağaç lazy-load:** Her seviye ilk açıldığında ilgili API'yi
  (`getCustomers`/`getApplications`/`getGroups`) çağırıp cache'liyor
  (tekrar kapat/aç API'yi tekrar çağırmıyor). Şu anki demo verisi küçük
  olduğu için performans sorunu yaratmıyor ama büyük kurulumlarda bu
  önbelleğin route değişince (ör. bir grup silindiğinde) bayat kalma
  riski var — sayfa yenilenene kadar. Kapsam dışı bıraktım.
- **Deep-link otomatik genişletme yok:** Kullanıcı doğrudan
  `/groups/42`'ye giderse ağaç bunu otomatik açıp o düğümü
  vurgulamıyor (sadece zaten açıksa `active` sınıfı uyguluyor). Eski
  `CustomerTree`'nin de query-param'a bağlı kısmi bir versiyonu vardı;
  yeni ağaç için tam ata-zincirini yükleyip açmak ek karmaşıklık
  getireceğinden bilinçli olarak atladım.
- **Eski Instance-tabanlı ağaç artık her iki modda da görünüyor** (önceki
  turda sadece public modda gösteriyordum) — adı "Instance Gezgini",
  konumu nav'ın en altında, ayrı bir bağlantı. Artık yeni ağaçla aynı
  "Müşteriler" ismini paylaşmadığı için private modda gizlemeye gerek
  kalmadı.

## Faz 7 — Test turu düzeltmeleri (dashboard performansı, sol menü, cluster tekilleştirme)

- **`last_checked` = en eski snapshot:** Özet birden çok grubun
  `GroupHealthSnapshot`'ını birleştiriyor; `last_checked` olarak grupların
  en eski `checked_at`'ini (min) döndürdüm — "en güncel" değil "en bayat"
  veri ne kadar eski, onu gösteriyor. Batch içindeki gruplar aynı
  `asyncio.gather` turunda prob'landığı için pratikte aralarındaki fark
  milisaniyeler, bu seçim çoğunlukla görünmez ama tutarlılık için
  belirtiyorum.
- **Dashboard refresh scheduler'ı worker/all run_mode'a bağlı:** Instance
  metrik toplama ile aynı desen — `run_mode=api` olan bir deployment'ta
  (ör. ayrı web dyno) hiçbir arka plan job çalışmaz, veri sadece
  `POST /api/dashboard/refresh` ile manuel tetiklenene kadar bayat kalır.
  Mevcut mimariyle tutarlı bir tercih, ayrı bir "worker her zaman
  gerekli" kısıtı eklemedim.
- **`GroupStatusSummaryOut.primary_node` iki kaynaklı:** Önce Patroni
  `cluster.leader` (canlı prob sonucu), yoksa gruptaki `role_hint=
  "primary"` işaretli `Node.name` (statik, kullanıcı girişi) kullanılıyor.
  Always On grupları için gerçek AG DMV tabanlı primary tespiti sadece
  `GET /api/groups/{id}/alwayson`'da var (ayrı, pahalı bir DMV sorgusu) —
  grup listesindeki özet bunu cache'lemiyor, bu yüzden Always On
  gruplarında "primary" alanı genelde statik `role_hint` fallback'inden
  geliyor, canlı değil.
- **`replication_lag_bytes` özeti sadece Patroni'de dolu:** Genel
  `collect_group_health` prob'u sadece Patroni `/cluster` member'larından
  `lag` alıyor; Always On'un log/redo queue metrikleri ayrı DMV
  endpoint'inde. Grup listesindeki lag özeti bu yüzden Always On
  gruplarında her zaman boş — detay için Group Detail'in Always On
  sekmesine gitmek gerekiyor.
- **`access_name` sadece görüntüleme/organizasyon amaçlı:** Hiçbir
  probe/collector `access_name`'e bağlanmıyor — düğümler hâlâ kendi
  `host`/`port` alanlarından prob'lanıyor. Alan sadece UI'da cluster
  grubunun "gerçek" erişim adını (listener/VIP) göstermek için.
- **Down-node issue konsolidasyonu `_issues_from_group_health`'te:** Bu
  fonksiyon İŞ 1'in önbellek geçişiyle aynı commit'te değişti (aynı
  fonksiyonu iki kez düzenlemek yapay olurdu) — bir gruptaki tüm down
  node'lar artık "N düğüm erişilemez: ad1 (site1), ad2 (site2)..." tek
  satırında birleşiyor, önceden düğüm başına ayrı satır vardı.

## ~~Faz 2 — Grup seviyeli alert kalıcılığı~~ (KAPANDI — Faz 6)

~~`AlertRule`/`AlertEvent` modelleri `instance_id`'ye bağlı...~~

**Çözüm (Faz 6):** `AlertRule`/`AlertEvent`'e nullable `group_id` eklendi
(`instance_id` de nullable'a çevrildi — bir event ya bir instance'a ya bir
gruba ait, ikisi birden değil). `alert_engine.ensure_group_alert_rules()` /
`evaluate_group_alerts()` eklendi; `GET /api/groups/{id}/health` artık
health raporunu döndürmeden önce `group_health_metric_flags()`'i
`evaluate_group_alerts`'e besleyip sonucu commit ediyor (alert yazımı
başarısız olursa health yanıtını bozmuyor, sadece loglayıp rollback
ediyor). `/api/alerts/rules` ve `/api/alerts/events` artık `group_id`
alanını da döndürüyor; AlertsPage bunu "Group #id" olarak gösterip
`/groups/{id}`'e linkliyor. Doğrulandı: TestClient ile grup health'i iki
kez çağırdım — 4 kural otomatik oluştu, 2 event tetiklendi, ikinci çağrıda
tekrar event açılmadı (idempotent), resolve edince aktif listeden düştü.

## Faz 2 — Node servis seçimi

`Node` modelinde `services` alanı yok (kullanıcı şemasında belirtilmedi).
`_node_services()` şu mantığı kullanıyor: `node.options.services` override
edilmişse onu kullan; yoksa `group.topology == "patroni"` ise tam Patroni
yığınını (`etcd, patroni, postgresql, keepalived, haproxy`), değilse
(standalone/alwayson) sadece `postgresql` (genel TCP port testi) prob'la.

**Varsayım:** SQL Server Always On düğümleri için Faz 2'nin ürettiği
`postgresql` servis adı yalnızca "veritabanı portu açık mı" testidir,
motor adı değil — Faz 4'teki `alwayson_health.py` asıl AG sağlığını
DMV'lerle ayrı bir endpoint'te verecek.

## Faz 3 — Node'da veritabanı kimlik bilgisi yok (plaintext sorunu Faz 6'da KAPANDI)

Kullanıcının verdiği `Node` şeması (id, group_id, name, host, port, site,
role_hint, agent_url, agent_token, options) veritabanına bağlanmak için
username/password içermiyor — `Instance` modelinin aksine. Ama parametre
denetimi (`pg_settings` okumak) gerçek bir SQL bağlantısı gerektiriyor.

**Varsayım:** `node.options` JSON'ına üç opsiyonel anahtar ekledim:
`db_username`, `db_password`, `db_database` (yoksa `db_database` varsayılan
`"postgres"`). `db_username` tanımlı değilse endpoint 400 ile açık bir hata
mesajı döner (`Node '{name}' için node.options.db_username tanımlı değil`).

**Çözüm (Faz 6):** `services/credentials.py`'e `encrypt_node_options`/
`decrypt_node_options`/`redact_node_options` eklendi — `db_password`
artık `Instance.password` ile birebir aynı Fernet mekanizmasıyla
şifreleniyor (`CREDENTIALS_MASTER_KEY` yoksa aynı `plain:` dev-fallback'i
kullanıyor). Node router'ları yazarken şifreliyor, `parameter_audit.py`/
`alwayson_health.py` bağlanırken çözüyor, API yanıtları (`create_node`,
`get_node`, `update_node`, `list_group_nodes`) her zaman `"***"` döndürüyor
— artık ne düz metin ne de şifreli blob API'den sızmıyor.

**Kapsam dışı kalan (bilerek):** `Node.agent_token` hâlâ düz metin — bu ayrı
bir mekanizma (host-agent paylaşımlı sırrı, DB kimlik bilgisi değil) ve bu
istekte adı geçmedi; istenirse aynı desenle kolayca eklenebilir.

## Faz 4 — Always On denetimi de aynı Node credential varsayımını kullanıyor (Faz 6'da şifrelendi)

`services/alwayson_health.py`, `node.options.db_username/db_password/
db_database` alanlarını Faz 3'teki parametre denetimiyle aynı şekilde
okuyor (aynı varsayım, tekrar yazmadım). AG DMV'lerini gruptaki
`role_hint == "primary"` düğümünden (yoksa ilk düğümden) sorguluyor;
`sys.dm_hadr_availability_replica_states.replica_server_name` alanını
`Node.name` veya `Node.host` ile eşleştirerek `site`/`node_id` bilgisini
ekliyor. Gerçek bir Always On cluster'a erişimim olmadığından bu eşleştirme
mantığı canlı DMV çıktısıyla doğrulanamadı; SQL sözdizimi standart
Microsoft dokümantasyon örneklerine dayanıyor.

## Faz 5 — Sol menüde iki ayrı "Müşteriler" kavramı

Mevcut sidebar'da `Instance.customer_name`/`application` string alanlarından
türetilen bir "Müşteriler" ağacı zaten vardı (App.tsx'teki `CustomerTree`).
Yeni Customer/Application/DatabaseGroup/Node modeli bundan tamamen ayrı bir
tablo hiyerarşisi. İkisini aynı "Müşteriler" etiketiyle yan yana koymak
kafa karıştırıcı olacağından yeni giriş noktasını **"Müşteri Grupları"**
olarak adlandırdım (`/customers` rotası). Eski ağaç ve `/instances` akışı
olduğu gibi çalışmaya devam ediyor; iki sistem şu an bilerek bağlanmadı
(bir Instance'ı bir Node'a otomatik eşlemiyorum) — bu, mevcut API'yi
kırmadan geriye dönük uyumluluğu korumak için Faz 1'den beri süregelen
tercih.

**Doğrulama notu:** Bu ortamda tarayıcı otomasyon aracı yoktu (claude-in-chrome
skill'i bu oturumda kullanılamadı). Bunun yerine: `tsc -b && vite build`
hatasız geçti, backend+frontend dev sunucularını gerçekten ayağa kaldırıp
`curl` ile customers→applications→groups→nodes→health uçlarını uçtan uca
çağırarak JSON şekillerini TypeScript tipleriyle birebir karşılaştırdım
(hepsi eşleşti), ve customer silme gibi bir yazma işlemini de canlı olarak
test ettim. Sayfaları gerçek bir tarayıcıda tıklayarak görsel/etkileşim
doğrulaması yapılmadı — kullanıcı fırsat bulduğunda `npm run dev` ile
kontrol etmeli.
