# Arayüz sözlüğü — Türkçe temizliği ADIM 1 (Faz 32 Commit 12a)

Bu doküman **tarama çıktısı**, çeviri DEĞİL. CLAUDE.md'nin "kullanıcıya görünen metinler Türkçe"
kuralına rağmen koda zaman içinde sızmış İngilizce kelime/ifadeleri listeler. **Bu adımda hiçbir
metin değiştirilmedi.** ADIM 2'de bu tablo onaylandıktan sonra, yalnızca `frontend/src/
terminology.ts` (tek kaynak) üzerinden uygulanacak; aynı commit'te "izin listesi dışında İngilizce
arayüz metni varsa kırmızı" koruma testi eklenecek.

İngilizce dil paketi (i18n) bu işin KAPSAMINDA DEĞİL — ayrı, ertelenmiş bir faz.

## Yöntem

Tarama hem elle hem de iki ayrı (frontend / backend) tam-dosya okuma geçişiyle yapıldı:
frontend `src/pages/`, `src/components/`, `src/hooks/`, `App.tsx`, `auth.tsx`, `main.tsx`,
`formFields.ts` (53 dosyanın TAMAMI okundu, yalnızca grep değil); backend `app/routers/*.py`
`HTTPException(detail=...)`, `app/services/performance_insights.py`, `app/services/health_report.py`,
`app/services/index_advisor.py`, `app/schemas.py` validator mesajları, `app/services/report_*.py`
PDF/CSV rapor metinleri. `frontend/src/api-types.ts` (OpenAPI'den ÜRETİLİYOR, tip/alan adları — kod
tanımlayıcısı) ve kod yorumları kapsam DIŞI; CLAUDE.md zaten bunların İngilizce kalacağını söylüyor.

## Gruplar

- **ÇEVRİLECEK** — genel arayüz etiketi/menü/kelime; DBA olmayan bir müşteri yöneticisi de görür.
- **TEKNİK TERİM, KALACAK** — DBA'ların zaten İngilizce kullandığı yerleşik terim (SQL anahtar
  kelimeleri, PostgreSQL/SQL Server'ın kendi resmi özellik adları, protokol/format adları).
- **KARAR GEREKLİ** — emin olunamayan, iki yönde de savunulabilir — kullanıcının onayı bekliyor.

---

## A) Genel arayüz — marka, gezinme, sayfa başlıkları

En YÜKSEK görünürlüklü bulgular: her sayfada (marka alanı, sol menü) ya da bir sayfanın KENDİ
başlığında (`<h2>`) görünüyorlar.

| İngilizce ifade | Nerede (dosya, adet) | Önerilen Türkçe | Grup |
|---|---|---|---|
| `DBA monitoring platform` | `App.tsx:340` (marka alanı, HER sayfada), `LoginPage.tsx:31` (giriş ekranı) — birebir aynı metin iki yerde | "DBA izleme platformu" | ÇEVRİLECEK |
| `Dashboard` (nav linki) | `App.tsx:345` (sol üst menü, HER sayfada) | "Genel Bakış" (ya da TERMS'e yeni bir anahtar) | ÇEVRİLECEK |
| `DBA Overview` (sayfa `<h2>`) | `DashboardPage.tsx:252` — ana iniş sayfasının KENDİ başlığı, tamamen İngilizce | "DBA Genel Bakış" | ÇEVRİLECEK |
| `Dashboard` (bağlantı metni) | `InstanceDetailPage.tsx:825` (`← Dashboard` linki) | "← Genel Bakış" | ÇEVRİLECEK |
| `Dashboard'a dön` (karma) | `AdminPage.tsx:224`, `NotFoundPage.tsx:23` | "Genel Bakış'a dön" | ÇEVRİLECEK (tutarsızlık da ayrıca aşağıda) |
| `Dashboard` (sıfat olarak) | `AdminPage.tsx:426,431` ("Dashboard otomatik yenileme aralığı", "Dashboard sayfasının...") | "Genel Bakış otomatik yenileme aralığı" | ÇEVRİLECEK |
| `Predictions` (nav linki) | `App.tsx:374` — kardeş linkler "Veritabanları"/"Raporlar"/"Yönetim" Türkçe | "Tahminler" | ÇEVRİLECEK |
| `Alerts` (nav linki + sayfa `<h2>`) | `App.tsx:377`, `AlertsPage.tsx:141` | "Alarmlar" (TERMS'te zaten "alarm" kökü başka yerlerde kullanılıyor — bkz. tutarsızlık F) | ÇEVRİLECEK |
| `← Alerts` (bağlantı) | `CustomAlertRuleFormPage.tsx:129` | "← Alarmlar" | ÇEVRİLECEK |
| `Resolve` (buton) | `AlertsPage.tsx:196` | "Çöz" / "Kapat" | ÇEVRİLECEK |
| `Cluster health` (panel başlığı + buton) | `ClusterHealthPanel.tsx:101`, `InstanceDetailPage.tsx:1051` (buton "Cluster health") | "Cluster sağlığı" (Cluster teknik terim kalır, "health" çevrilir) | ÇEVRİLECEK |
| `Schema health` (panel başlığı + buton) | `SchemaHealthPanel.tsx:83`, `InstanceDetailPage.tsx:1052` (buton "Schema health") | "Şema sağlığı" | ÇEVRİLECEK |
| `Unused index · dead tuple / bloat / vacuum lag` (alt başlık) | `SchemaHealthPanel.tsx:84` | "Kullanılmayan index · dead tuple / bloat · vacuum gecikmesi" (vacuum/index teknik terim) | KARAR GEREKLİ |
| `Database Groups` (sayfa `<h2>`) | `DatabaseGroupsPage.tsx:124` | "Veritabanı grupları" (TERMS.group zaten var — aynı anlam İngilizce tekrarlanmış) | ÇEVRİLECEK |
| `Database group` fallback başlık | `GroupDetailPage.tsx:373` — `{group?.name ?? "Database Group"}` | "Veritabanı grubu" | ÇEVRİLECEK |
| `Database groups` (StatCard etiketi) | `DashboardPage.tsx:352` | "Veritabanı grupları" | ÇEVRİLECEK |
| "...bir **database group** yok." (cümle içi) | `DashboardPage.tsx:317` | "...bir veritabanı grubu yok." | ÇEVRİLECEK |
| `Customer → Application → Database Group → Node` (cümle içi, 4 terim birden) | `CustomersPage.tsx:98` | "Müşteri → Uygulama → Veritabanı grubu → Düğüm" | ÇEVRİLECEK |
| `Database Group` (`<option>` metni) | `CustomAlertRuleFormPage.tsx:158` | "Veritabanı grubu" | ÇEVRİLECEK |

---

## B) DPA panelleri — stat kutucukları, tablo sütun başlıkları, grafik başlıkları

Bu bölüm en ÇOK bulgu içeren grup. Her dosya için TEKİL ifadeler listelendi (aynı kelime aynı
dosyada birden çok yerde geçiyorsa tek satırda, adet belirtilerek).

### SYSTEMIC — ham İngilizce enum değeri doğrudan ekrana basılıyor

`terminology.ts`'nin kendi başlık yorumu bu TAM hatayı önlemek için `STATUS_LABELS`/`statusLabel()`
yazıldığını söylüyor ("ham İngilizce durum değeri ekranda GÖRÜNMEZ"), ama aynı regresyon başka
birçok yerde hâlâ var — hiçbiri `statusLabel()`/bir etiket sözlüğü çağırmıyor, backend'den gelen
`"critical"`, `"warning"`, `"healthy"`, `"public"`, `"standalone"`, `"primary"` gibi ham string'i
doğrudan JSX'e basıyor:

| Ham değer | Nerede | Grup |
|---|---|---|
| `{g.environment}` (çevre rozeti) | `DashboardPage.tsx:402,450` | ÇEVRİLECEK → `statusLabel()`/ortak sözlük kullan |
| `{card.severity}` | `DashboardPage.tsx:441` | ÇEVRİLECEK |
| `{data.overall}` | `ClusterHealthPanel.tsx:103` | ÇEVRİLECEK |
| `{group.topology}` / `{group.environment}` | `GroupDetailPage.tsx:375,376` | ÇEVRİLECEK |
| `{health.overall}` / `{alwaysOn.overall}` | `GroupDetailPage.tsx:491,873` | ÇEVRİLECEK |
| `{node.role_hint}` | `GroupDetailPage.tsx:644` | ÇEVRİLECEK |
| `{f.severity}` (parametre sapması tablosu) | `GroupDetailPage.tsx:822` | ÇEVRİLECEK |
| `{node.site} / {node.role_hint}` (sihirbaz özeti) | `DatabaseWizardPage.tsx:1183,1189` — AYNI dosyanın 832. satırındaki node-kart özeti site'ı doğru çeviriyor ("DR"/"Ana DC") | ÇEVRİLECEK + tutarsızlık |
| `Ortam: {instance.environment}` | `InstanceDetailPage.tsx:991` | ÇEVRİLECEK |
| `{p.severity}` (tahminler tablosu) | `InstanceDetailPage.tsx:1939`, `PredictionsPage.tsx:118` | ÇEVRİLECEK |
| `{g.topology}` / `{status.overall}` | `DatabaseGroupsPage.tsx:184,214,218` | ÇEVRİLECEK |
| `{engine}` / `{topology}` / `{environment}` (sihirbaz özet adımı) | `DatabaseWizardPage.tsx:1153-1163` | ÇEVRİLECEK |

### SYSTEMIC — `STATUS_TR` 3 yerde bağımsız, FARKLI değerlerle yeniden tanımlanmış

| Dosya | İçerik | Not |
|---|---|---|
| `ClusterHealthPanel.tsx:13-18` | `up: "UP", down: "DOWN", unknown: "UNKNOWN", skipped: "SKIP"` | gerçek çeviri değil, büyük harfe çevrilmiş İngilizce |
| `GroupDetailPage.tsx:35` | AYNI sözlüğün birebir kopyası | bakım riski — biri değişirse diğeri sessizce ayrışır |
| `ReportsPage.tsx:37-43` | `ok: "Sorun yok", info: "Bilgi", warning: "Dikkat", critical: "Kritik", unknown: "Değerlendirilemedi"` | **terminology.ts'nin `STATUS_LABELS`'ıyla ÇAKIŞIYOR**: `warning`→"Uyarı" (orada) vs "Dikkat" (burada); `unknown`→"Bilinmiyor" (orada) vs "Değerlendirilemedi" (burada) |

Üçü de KARAR GEREKLİ: ReportsPage'in farklılığı kasıtlı mı (rapor-bölümü ifadesi vs. genel durum
ifadesi), yoksa birleştirilmeli mi?

### ActivityPanel.tsx (DPA → Veritabanı yükü sekmesi, canlı oturumlar)

| İngilizce ifade | Nerede (adet) | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Activity` (yükleniyor/veri yok metni içinde) | satır 28 (2 kullanım) | "Oturum verisi yükleniyor…" / "Oturum verisi yok" | ÇEVRİLECEK |
| `Blocking zinciri` (karma) | satır 56 | "Bloklama zinciri" (BlockingTreePanel'de zaten böyle — tutarsızlık) | ÇEVRİLECEK |
| `Blocked PID` / `Blocking PID` / `Wait` (tablo başlığı) | satır 61-64, 145 | "Bloklanan PID" / "Bloklayan PID" / "Bekleme" | ÇEVRİLECEK |
| `Wait event dağılımı` / `Aktif wait event yok` (karma) | satır 86, 88 | "Bekleme olayı dağılımı" / "Aktif bekleme olayı yok" | ÇEVRİLECEK |
| `Type` / `Event` (sütun başlığı) | satır 93 | "Tür" / "Olay" | ÇEVRİLECEK |
| `Active` / `Idle` / `Idle in tx` / `Waiting` / `Blocked` (stat kutucuğu) | satır 47-51 | "Aktif" / "Boşta" / "İşlemde boşta" / "Bekliyor" / "Bloklanmış" | ÇEVRİLECEK |
| `State` (sütun/panel başlığı) | satır 93, 109, 111, 116, 144 | "Durum" | ÇEVRİLECEK |
| `User` / `App` (sütun başlıkları) | satır 141-146 | "Kullanıcı" / "Uygulama" | ÇEVRİLECEK |
| `Client backend oturumu yok` (karma) | satır 135 | olduğu gibi kalabilir ("Client" `pg_stat_activity.backend_type` değeri) | KARAR GEREKLİ |
| `<span class="state-pill blocked">blocked</span>` — sabit kodlu, veriden gelmeyen literal | satır 158 | "bloklanmış" | ÇEVRİLECEK |

### ClusterHealthPanel.tsx (DPA → Cluster sağlığı sekmesi)

| İngilizce ifade | Nerede (adet) | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Cluster health yükleniyor…` | satır 93 | "Cluster sağlığı yükleniyor…" | ÇEVRİLECEK |
| `online` / `unreachable` (agent durumu, sabit kodlu literal) | satır 109-110 | "çevrim içi" / "erişilemiyor" | ÇEVRİLECEK |
| `UP` / `DOWN` / `Unknown` / `Skipped` (stat kutucuğu) | satır 121-124 — AYNI etiketler `GroupDetailPage.tsx:505,509` içinde tekrarlanıyor | "AÇIK" / "KAPALI" / "Bilinmiyor" / "Atlandı" | ÇEVRİLECEK |
| `Leader: … · Members: …` (satır içi etiket) | satır 131 | "Lider: … · Üyeler: …" | ÇEVRİLECEK |
| `Node` / `Role` / `Host` (tablo başlığı, Durum ile karma) | satır 137 | "Düğüm" / "Rol" / "Sunucu" (Host KARAR GEREKLİ) | KARAR GEREKLİ (Host) / ÇEVRİLECEK (diğer) |

### SchemaHealthPanel.tsx (DPA → Şema sağlığı sekmesi)

| İngilizce ifade | Nerede (adet) | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Unused index` (başlık + stat kutucuğu + boş durum) | satır 83(alt başlık), 105, 122 | "Kullanılmayan index" | ÇEVRİLECEK (index teknik terim kalır) |
| `Vacuum lag` / `Vacuum / analyze lag` (stat kutucuğu + bölüm başlığı) | satır 114, 195 | "Vacuum gecikmesi" / "Vacuum / analyze gecikmesi" | ÇEVRİLECEK (vacuum teknik terim kalır) |
| `Scan` / `Index` (sütun başlığı) | satır 128-133 | "Tarama" (`idx_scan`); Index teknik terim kalır | KARAR GEREKLİ |
| `Live` / `Dead` / `Dead %` (sütun başlığı) | satır 163-171 | "Canlı" / "Ölü" / "Ölü %" | ÇEVRİLECEK |
| `Last autovacuum` (sütun başlığı) | satır 169 | "Son autovacuum" (autovacuum teknik terim kalır) | ÇEVRİLECEK |
| `Lag` / `Freeze age` (sütun başlığı) | satır 206-209 | "Gecikme" / "Freeze yaşı" | KARAR GEREKLİ (freeze teknik mi?) |

### InstanceDetailPage.tsx (en çok bulgu içeren TEK dosya)

**Sekme çubuğu karma** (satır 951-983): 11 sekmeden 7'si Türkçe ("Özet", "Metrikler", "Veritabanı
Yükü", "Yavaş Sorgular", "Bloklama", "Uyarılar", "Tahminler"), 4'ü çıplak İngilizce:
`Activity` (960), `Cluster` (972), `Schema` (977), `Tuning` (980) — aynı satırda, hiçbir stilistik
gerekçe yok ("Cluster'a dönüştür" gibi eklentili alıntı kelimelerden farklı olarak bunlar hiç
Türkçe ek almıyor.

**Metrikler sekmesi — TÜM grafik başlıkları İngilizce; Özet sekmesinin neredeyse birebir aynı
grafikleri Türkçe** (doğrudan aynı-uygulama-içi tutarsızlık — bkz. F.8): Özet sekmesindeki bağlantı
grafiği `"Bağlantılar (son 60 dk)"` (satır 1098) derken Metrikler sekmesindeki neredeyse aynı grafik
`"Connections over time"` (satır 1183) diyor.

| İngilizce ifade | Nerede | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Connections over time` | 1183 | "Zaman içinde bağlantılar" | ÇEVRİLECEK |
| `Cache hit ratio & TPS` | 1117, 1203 (Özet VE Metrikler, kendi içinde tutarlı ama ikisi de İngilizce) | "Cache isabet oranı & TPS" | KARAR GEREKLİ (TPS teknik kalır) |
| `Database size` | 1219 | "Veritabanı boyutu" | ÇEVRİLECEK |
| `Replication lag` | 1238 | "Replikasyon gecikmesi" | ÇEVRİLECEK |
| `Deadlocks & temp bytes` | 1251 | "Deadlock & temp bayt" (deadlock/temp teknik kalır) | ÇEVRİLECEK |
| `I/O blocks per second` | 1267 | "Saniyede I/O blok" | ÇEVRİLECEK |
| `Tuple throughput` | 1292 | "Tuple verimi" (tuple teknik terim kalır) | ÇEVRİLECEK |
| `Temp files & bytes` | 1310 | "Geçici dosyalar & bayt" | ÇEVRİLECEK |
| `Checkpoints & buffers` | 1326 | "Checkpoint & buffer" (ikisi de teknik terim) | ÇEVRİLECEK (bağlaç) |
| Grafik serisi `name=` etiketleri (tooltip/legend'da görünür) — "Cache hit %", "TPS", "Disk read", "Buffer hit", "Returned", "Fetched", "Inserted", "Updated", "Deleted", "Timed checkpoints", "Requested checkpoints", "Buffers checkpoint/s", "Buffers backend/s", "Buffers clean/s", "Deadlocks", "Temp bytes", "Temp files/s", "Temp bytes/s" | 1126-1338 civarı | tek tek çevrilmeli | ÇEVRİLECEK |
| `<ReferenceLine label="max" />` | 1111 | "maks" | ÇEVRİLECEK |
| `Cache hit ratio` (StatTile) | 1042 | "Cache isabet oranı" | ÇEVRİLECEK |
| `Transaction/sn` (StatTile, yarı çevrili) | 1043 | "İşlem/sn" (ya da Transaction teknik kalır → "Transaction/sn" tutarlı) | KARAR GEREKLİ |
| `Activity / Blocking` (buton, kendi sekmesiyle karma) | 1050 | "Oturum / Bloklama" | ÇEVRİLECEK |
| `Query` / `Calls` / `Mean (ms)` / `Total (ms)` / `Rows` (yavaş sorgu tablosu başlıkları, en çok kullanılan tablo) | 1564-1568 | "Sorgu" / "Çağrı" / "Ortalama (ms)" / "Toplam (ms)" / "Satır" | ÇEVRİLECEK |
| `History yükleniyor…` (karma, yanındaki `<h4>Zaman serisi</h4>` doğru Türkçe) | 1609 | "Geçmiş yükleniyor…" | ÇEVRİLECEK + tutarsızlık |
| `#{instanceId} numaralı instance yok` / `geçerli bir instance numarası değil` | 800-801 | "Veritabanı" | ÇEVRİLECEK (terminology.ts'nin kendi kuralını ihlal ediyor) |

### GroupDetailPage.tsx (veritabanı grubu detayı — Always On / Patroni)

| İngilizce ifade | Nerede (adet) | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Leader yok` (karma) | satır 539 | "Lider yok" | ÇEVRİLECEK |
| `Node` / `Host` / `Lag` (tablo başlığı, Rol/Durum ile karma) | satır 548 | "Düğüm" / "Sunucu" / "Gecikme" | KARAR GEREKLİ (Host) |
| `Primary` / `Replica` (rol `<select>` seçenekleri) | satır 624-625 — `ClusterHealthPanel.tsx:41`'de AYNI kavram "birincil"/"replika" diye çevrilmiş | "Birincil" / "Replika" | ÇEVRİLECEK + tutarsızlık |
| `primary site` (satır içi, ham) | satır 642-643, 886-887 — `ServersPage.tsx`'in kendi `SITE_LABELS`'ı aynı kavramı "Ana DC" diye çeviriyor | "Ana DC" | ÇEVRİLECEK + tutarsızlık |
| `{sev}` (ham önem anahtarı, parametre özeti) | satır 806 | `SEVERITY_LABELS` kullan | ÇEVRİLECEK |
| `Log queue` / `Redo queue` (Always On tablo başlığı) | satır 901 | "Log kuyruğu" / "Redo kuyruğu" | KARAR GEREKLİ (SQL Server Always On terimi) |
| `Primary: … · Sync health: …` (satır içi) | satır 871 | "Birincil: … · Eşitleme sağlığı: …" | ÇEVRİLECEK |
| `Always On Availability Group` | satır 857 | değişmez (SQL Server'ın KENDİ resmi özellik adı) | TEKNİK TERİM, KALACAK |

### ExplainPlanTree.tsx

| İngilizce ifade | Nerede | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Plan cost: …` / `Planning: … ms` / `Execution: … ms` | satır 97-99 | "Plan maliyeti:" / "Planlama:" / "Çalıştırma:" | ÇEVRİLECEK |
| `cost {...}` (satır içi) | satır 39 | "maliyet" | ÇEVRİLECEK |
| `ANALYZE` / `EXPLAIN` | satır 100 | değişmez (SQL anahtar kelimesi) | TEKNİK TERİM, KALACAK |

### ServersPage.tsx / InstancesPage.tsx / CustomersPage.tsx / DatabaseWizardPage.tsx — kısa alan etiketleri

| İngilizce ifade | Nerede | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Private` / `Public` (`<option>` metni, 2 form) | `CustomersPage.tsx:143-144,190-191`, `InstancesPage.tsx:393-394` | "Özel" / "Genel" | KARAR GEREKLİ (DEPLOYMENT_MODE=public/private koddaki değerle karışır mı?) |
| `{c.type}` (ham rozet) | `CustomersPage.tsx:160` | çevrilmiş etiket kullan | ÇEVRİLECEK |
| `{inst.environment}` (ham tablo hücresi) | `InstancesPage.tsx:702` | çevrilmiş etiket kullan | ÇEVRİLECEK |
| `"Private"` (fallback ortam adı) | `DashboardPage.tsx:255` | "Özel" | ÇEVRİLECEK |
| `Prod` / `Preprod` (ortam etiketi, `ENV_LABELS` + 2 sihirbaz formu) | `DatabaseGroupsPage.tsx:190-191`, `DatabaseWizardPage.tsx:731-734,817-820` | "Üretim" / "Ön üretim" — ya da DBA jargonunda zaten yaygın | KARAR GEREKLİ |
| `Agent` (sütun başlığı) | `ServersPage.tsx:165`, `DatabaseWizardPage.tsx:1089` | "Agent" teknik terim (host-agent) olarak KALABİLİR | KARAR GEREKLİ |
| `Host` (tekrarlanan, çok sayıda dosya) | `ServersPage.tsx:161`, `DatabaseWizardPage.tsx:1182,1188`, `ClusterHealthPanel.tsx:137`, `GroupDetailPage.tsx:548` | "Sunucu adresi" / olduğu gibi "Host" | KARAR GEREKLİ |
| `Linux` / `Windows` | `ServersPage.tsx:188-189` | değişmez (işletim sistemi adı) | TEKNİK TERİM, KALACAK |
| `Listener port` (form alanı) | `DatabaseWizardPage.tsx:1169` | "Dinleyici portu" | ÇEVRİLECEK |
| `Instance adı` / `instance: {...}` / `Bağlı instance yok` | `GroupDetailPage.tsx:612,649,746` | "Veritabanı adı" / "Bağlı veritabanı yok" | ÇEVRİLECEK (terminology.ts kuralı) |
| `Instance #{id}` / `Instance: {name}` | `AlertsPage.tsx:104,130` | "Veritabanı #{id}" / "Veritabanı: {name}" | ÇEVRİLECEK |
| `...bir instance'ı eklerken...` | `ServersPage.tsx:144` | "...bir veritabanı eklerken..." | ÇEVRİLECEK |
| `label="instance"` (sayfalama, "X–Y / Z instance") | `InstancesPage.tsx:721` | "veritabanı" | ÇEVRİLECEK |
| SQL Server "instance adı (named instance)" / "Instance portu" | `DatabaseWizardPage.tsx:971,980,1169` | muhtemelen KALIR — SQL Server'ın KENDİ "named instance" kavramı (CLAUDE.md'nin tek istisnası) | TEKNİK TERİM, KALACAK (ama görünür kelime hâlâ "Instance" — gözden geçirilmeli) |
| `Standalone` (sihirbaz topoloji kart başlığı) | `DatabaseWizardPage.tsx:30` — bkz. tutarsızlık F.2 (3 farklı gösterim) | "Tek sunucu" (ClusterHealthPanel'in TOPOLOGY_LABEL'ı zaten böyle çeviriyor) | ÇEVRİLECEK + tutarsızlık |
| `Standalone (tek düğüm)` (yarı çevrili) | `InstancesPage.tsx:408` | "Tek sunucu (tek düğüm)" | ÇEVRİLECEK + tutarsızlık |
| `Top` (sonuç sayısı seçici etiketi) | `QueryDiagnosticsPanel.tsx:55` | "İlk" / "En çok" | ÇEVRİLECEK |
| `Top sorgulara index öner` (karma) | `TuningPanel.tsx:91` | "En çok sorguya index öner" | ÇEVRİLECEK |
| Ham önem seviyesi `<option>` listesi (critical/high/warning/medium/low/info) | `AlertsPage.tsx:221-222`, `CustomAlertRuleFormPage.tsx:20,21,241` | zaten var olan `SEVERITY_TR`/`SEVERITY_LABELS` kullan | ÇEVRİLECEK |
| `Tüm engine'ler` / `Engine` (form etiketi) | `AlertsPage.tsx:225`, `CustomAlertRuleFormPage.tsx:187` — `SchemaHealthPanel.tsx` aynı kavramı tutarlı biçimde "motor" diyor | "Motor" | ÇEVRİLECEK + tutarsızlık |
| `placeholder="shared secret"` | `InstancesPage.tsx:518` | "paylaşılan anahtar" | ÇEVRİLECEK |
| `placeholder="agent token"` | `ServersPage.tsx:209` | "agent anahtarı" (agent KARAR GEREKLİ) | ÇEVRİLECEK (kısmen) |
| `placeholder="...ticket no..."` | `FindingStatusControl.tsx:113` | "...bilet no..." (borderline, destek bileti jargonu) | KARAR GEREKLİ |
| rol `<select>` seçenekleri tutarsız biçimlendirme (`admin`/`viewer` çıplak vs. `viewer (salt-okunur)`) | `AdminPage.tsx:347-349` vs `:406-407` | tutarlı hâle getir | KARAR GEREKLİ (rol adları teknik kimlik kalabilir) |

---

## C) Backend — API hata mesajları (`HTTPException(detail=...)`)

**En büyük, en sistemik bulgu.** `backend/app/routers/*.py` genelinde `"X not found"` ve
`"X already exists"` biçiminde **163 `detail=` ifadesinden 52'si İngilizce**. Bunlar frontend'in
`PageError`/`TableState` bileşenleri tarafından **ham metin olarak kullanıcıya gösteriliyor** — yani
en çok kullanıcı tarafından GÖRÜLEN backend metni kategorisi.

| İngilizce ifade | Kaç dosyada / kaç kez | Önerilen Türkçe | Grup |
|---|---|---|---|
| `Instance not found` | `instances.py` (satır 260,270,332,359,396,426,444,468,487,509,601,634,651,972 — 14), `nodes.py:84` (1), `wizard.py:84`'te karşılığı Türkçe zaten var ("Seçilen sunucu bulunamadı") — toplam 15 | "Veritabanı bulunamadı" (TERMS: Instance → Veritabanı) | ÇEVRİLECEK |
| `Instance bulunamadı` (zaten Türkçe, ama `instances.py` İÇİNDE aynı kavramın İngilizcesiyle KARIŞIK) | `instances.py:673,804,905,963` (4) | zaten doğru — İngilizce kardeşleriyle (üstteki satır) BİRLEŞTİRİLMELİ | tutarsızlık (aynı dosya, aynı kavram, 2 dil) |
| `Instance bulunamadi` (Türkçe ama yazım farklı — noktasız/çizgisiz "ı") | `metrics.py` (9 kez) | "Instance bulunamadı" → aslında "Veritabanı bulunamadı" | ÇEVRİLECEK + tutarsızlık |
| `Database group not found` | `database_groups.py` (7), `nodes.py:99,242` (2), `wizard.py:173,242` (2) — toplam 11 | "Veritabanı grubu bulunamadı" | ÇEVRİLECEK |
| `Server not found` | `servers.py` (6), `nodes.py:102` (1) — toplam 7 | "Sunucu bulunamadı" | ÇEVRİLECEK |
| `Customer not found` | `customers.py` (2), `applications.py` (1) — toplam 3 | "Müşteri bulunamadı" | ÇEVRİLECEK |
| `Application not found` | `wizard.py:173` (1), `database_groups.py` (1) — toplam 2 — AYNI dosyada (`applications.py:72`) aynı kavram zaten "Uygulama bulunamadı" diye TÜRKÇE | "Uygulama bulunamadı" | ÇEVRİLECEK + tutarsızlık |
| `Application/customer not found for this group` | `wizard.py:251` | "Bu grup için uygulama/müşteri bulunamadı" | ÇEVRİLECEK |
| `Node not found` | `nodes.py:118,126,153` (3) | "Düğüm bulunamadı" | ÇEVRİLECEK |
| `Rule not found` | `alerts.py` (2) | "Kural bulunamadı" | ÇEVRİLECEK |
| `Event not found` | `alerts.py` (1) | "Olay bulunamadı" | ÇEVRİLECEK |
| `Group name already exists for this application` | `wizard.py:184` | "Bu uygulama için grup adı zaten var" | ÇEVRİLECEK |
| `Server name already exists for this customer` | `servers.py` (1) | "Bu müşteri için sunucu adı zaten var" | ÇEVRİLECEK |
| `Instance name already exists` | `instances.py` (1) | "Veritabanı adı zaten var" | ÇEVRİLECEK |
| `Customer name already exists` | `customers.py` (1) | "Müşteri adı zaten var" | ÇEVRİLECEK |
| `Application name already exists for this customer` | `applications.py` (1) | "Bu müşteri için uygulama adı zaten var" | ÇEVRİLECEK |
| `Group has no nodes to probe` | `database_groups.py` (1) | "Grubun yoklanacak düğümü yok" | ÇEVRİLECEK |
| `Index advice is only available for PostgreSQL` | 1 yer | "Index önerisi yalnızca PostgreSQL için kullanılabilir" | ÇEVRİLECEK |
| `EXPLAIN is only available for PostgreSQL` | 1 yer | "EXPLAIN yalnızca PostgreSQL için kullanılabilir" (EXPLAIN teknik terim) | ÇEVRİLECEK |
| `Group health failed: {exc}` | `database_groups.py:186` | "Grup sağlığı alınamadı: {exc}" | ÇEVRİLECEK |
| `Always On health failed: {exc}` | `database_groups.py:265` | "Always On sağlığı alınamadı: {exc}" | ÇEVRİLECEK |
| `Cluster health failed: {exc}` | `instances.py:472` | "Cluster sağlığı alınamadı: {exc}" | ÇEVRİLECEK |
| `Cluster logs failed: {exc}` | `instances.py:494` | "Cluster logları alınamadı: {exc}" | ÇEVRİLECEK |

**"`{X} failed: {exc}`" deseni** — 4 noktada (yukarıdaki son 4 satır) 502 hatasının gövdesi İngilizce
sabit önek + serbest `{exc}` (alttaki istisna mesajı — HANGİ dilde olacağı garanti değil, genelde
İngilizce bir kütüphane/sürücü mesajı) birleşimi. Çeviri yalnızca sabit öneki kapsayabilir; `{exc}`
kısmı kaynağına bağlı olarak İngilizce kalabilir — bu KARAR GEREKLİ değil ama ADIM 2'de not
düşülmeli (teknik mesaj her hâlükârda yönetici raporuna ASLA sızmıyor, yalnızca DBA görünümünde).

### Ham İstisna Sızıntısı (iki ayrı, önemli yol)

Bu ikisi "ÇEVRİLECEK" değil — çünkü sabit bir metin değil, **kütüphanenin/Python'un kendi hata
mesajı ham hâliyle** kullanıcıya ulaşıyor. ADIM 2'de ayrı ele alınmalı (muhtemelen sarmalayan sabit
bir Türkçe önek + teknik detayı yalnızca DBA görünümünde göster):

1. **Rapor üretimi:** `health_report.py:695` — `report.error = str(exc)[:2000]` (except bloğu, ham
   Python istisna metni) → `ReportsPage.tsx:582` — `<div className="error">{report.error || "Rapor
   üretimi başarısız oldu."}</div>` ile DOĞRUDAN ekrana basılıyor. Rapor DBA VE yönetici tarafından
   görülebiliyor olabilir — CLAUDE.md'nin "yönetici raporunda teknik detay ASLA görünmez" kuralıyla
   çakışma riski var, kontrol edilmeli.
2. **Index önerisi:** `sql_predicates.py:108` — `parse_error=str(exc)[:200]` (sqlglot'un ham
   ayrıştırma hatası) → `index_advisor.py:204` — `what_to_do=parse_error` olarak öneri kaydına
   konuyor → `IndexAdvicePanel.tsx:106` — `<p>{r.what_to_do}</p>` ile DOĞRUDAN ekrana basılıyor.

**Not:** `app/schemas.py`'deki Pydantic validator mesajları (`raise ValueError(...)`) VE
`app/services/report_documents.py` / `report_sections.py`'deki PDF/CSV rapor metinleri (hem DBA hem
yönetici raporu) tarandı — **tamamı zaten Türkçe**, bu ikisinde bulgu YOK.

### performance_insights.py — Tuning paneli, karma dil (YENİ bölüm)

`InstanceDetailPage.tsx`'in "Tuning" sekmesini besleyen `app/services/performance_insights.py`,
Türkçe cümleler İÇİNE gömülü İngilizce terim/başlıklar üretiyor — `report_sections.py`'deki PDF/CSV
rapor eşdeğerleri AYNI kavramlar için TAMAMEN Türkçe (ör. "Cache isabet oranı") olduğu için bu
doğrudan bir **tutarsızlık**: aynı veri, iki yerde, iki farklı dilde sunuluyor.

| İngilizce ifade | Nerede (satır) | Bağlam | Grup |
|---|---|---|---|
| `Buffer cache hit ratio düşük` | 154 | insight başlığı | ÇEVRİLECEK |
| `Cache hit ratio iyileştirilebilir` / `Cache hit ratio iyi` / `Cache hit ratio %...` (description, 3+ yer) | 168,183-184,155,169 | insight başlığı + açıklama | ÇEVRİLECEK |
| `Connection limit kritik` | 200 | insight başlığı | ÇEVRİLECEK |
| `Temp: {x} file/s, {y} MB/s` | 246 | açıklama metni | ÇEVRİLECEK |
| `...(requested)` | 279 | açıklama metni sonu | ÇEVRİLECEK |
| `Replication lag yüksek` | 328 | insight başlığı | ÇEVRİLECEK |

**Tutarsızlık notu:** bu başlıklar `InstanceDetailPage.tsx`'in Metrikler sekmesi grafik başlıklarıyla
(§B, "Cache hit ratio & TPS", "Replication lag") aynı terimi kullanıyor — en azından KENDİ İÇİNDE
tutarlı, ama ikisi de `report_sections.py`'nin Türkçe karşılığından (muhtemelen "Cache isabet oranı",
"Replikasyon gecikmesi") AYRIŞIYOR. Bu, CLAUDE.md'nin "aynı veriyi gösteren yerler tek gerçeklik
kaynağından beslensin" kuralına metinsel düzeyde bir örnek — veri aynı kaynaktan geliyor ama ekran
etiketi DPA'da İngilizce, raporda Türkçe.

---

## D) Teknik terim — KALACAK (DBA'ların İngilizce kullandığı yerleşik terimler)

Bu liste DOĞRULAMA amaçlı — kodda zaten İngilizce ve BURADA kalmaları öneriliyor, çeviri listesine
GİRMEMELİ:

`index`, `deadlock`, `WAL`, `vacuum` / `autovacuum`, `replica` / `replication`, `Query Store`,
`EXPLAIN` / `ANALYZE`, `CONCURRENTLY`, `WHERE` / `JOIN ON` / `HAVING` / `ORDER BY` / `GROUP BY` (SQL
anahtar kelimeleri — `IndexAdvicePanel.tsx`), `cluster`, `checkpoint`, `TPS`, `tuple`, `Always On
Availability Group` (SQL Server'ın KENDİ resmi özellik adı — `GroupDetailPage.tsx:857`), `Patroni`
(özel ad), `PostgreSQL` / `SQL Server` / `Linux` / `Windows` (ürün/işletim sistemi adları), `DBA`
(evrensel kısaltma), `pg_stat_activity` / `pg_stat_statements` vb. katalog görünümü adları (zaten kod
içi SQL metni, ekran metni değil), `AAS` (Average Active Sessions, DatabaseLoadPanel genelinde),
`transaction` (BlockingTreePanel/ClusterHealthPanel'de "işlem" yerine tutarlı biçimde kullanılıyor),
`shared hit` / `local hit` (Postgres buffer-stat sütun adları), `etcd quorum`, `split-brain`,
`PDF` / `HTML` / `Markdown` (export format adları).

## E) Karar gerekli — borderline, kullanıcı onayı bekliyor

Yukarıdaki tablolarda "KARAR GEREKLİ" işaretli satırlara ek, genel karar gerektiren sorular:

1. **"Host"** çok sayıda yerde tekrarlanıyor (sütun başlığı + form alanı, hem DPA hem Server/Group
   sayfalarında). "Sunucu" ile çakışıyor mu (zaten `TERMS.server.singular = "Sunucu"` var) — aynı
   kavram için iki Türkçe kelime mi olacak (Host = bağlantı adresi, Sunucu = envanterdeki Server
   kaydı), yoksa "Host" teknik terim olarak mı kalsın?
2. **"Agent"** (host-agent) — "Ajan" a çevrilsin mi, yoksa host-agent zaten kod/dokümanda İngilizce
   bir özel ad olarak yerleşik olduğu için ekranda da "Agent" mi kalsın? (`Agent testi`, `Host agent
   URL`, `agent token` placeholder'ı da bu kararı bekliyor.)
3. **"Private" / "Public"** (müşteri tipi) — bunlar `DEPLOYMENT_MODE=public/private` ortam
   değişkeniyle AYNI kavram mı (CLAUDE.md: "Çok müşterili: public ve private müşteriler")? Çevrilirse
   kod tarafındaki `type` alanının DEĞERİ değil yalnızca EKRANDAKİ etiketi değişecek — onaylanmalı.
4. **"Prod" / "Preprod"** — DBA ortamlarında bu kısaltmalar Türkçe konuşmada da YAYGIN kullanılıyor
   (bir DBA "prod'da" der, "üretimde" demez her zaman) — tam çeviri mi, yoksa olduğu gibi mi? Aynı
   soru "Test"/"Dev" ortam etiketleri için de geçerli (`ENV_LABELS`, 3 ayrı dosyada kullanılıyor) —
   bunlar gerçek çeviri değil, yalnızca büyük harfle yazılmış İngilizce kelimeler.
5. **"Freeze age"** (SchemaHealthPanel) — "freeze" PostgreSQL'in transaction ID wraparound önleme
   mekanizmasının adı, "age" genel bir kelime. "Freeze yaşı" gibi karma mı, yoksa tamamı mı çevrilsin?
6. **"Log queue" / "Redo queue"** (SQL Server Always On metrikleri, `GroupDetailPage.tsx:901`) —
   SQL Server DMV'lerinin kendi terminolojisi mi (KALACAK), yoksa genel kuyruk kavramı olarak
   çevrilsin mi ("Log kuyruğu"/"Redo kuyruğu")?
7. **"Transaction/sn"** (`InstanceDetailPage.tsx:1043`) — yarı çevrili ("Transaction" + Türkçe "/sn"
   eki) — "İşlem/sn" mi olsun yoksa Transaction teknik terim olarak mı kalsın?
8. **Rol adları (`admin`/`viewer`)** — `AdminPage.tsx`'in iki ayrı `<select>`'i bunları tutarsız
   biçimlendiriyor (biri çıplak, biri "(salt-okunur)" notu eklenmiş). Rol KİMLİĞİ İngilizce teknik
   değer olarak kalmalı (kod tarafı), ama görünen metin tutarlı hâle getirilmeli — ikisi de aynı
   notu mu taşısın?

---

## F) Tutarsızlıklar (aynı kavram, farklı Türkçe/İngilizce karşılık)

1. **"Bulunamadı" mesajları — İKİ DİL BİR ARADA, hatta AYNI DOSYADA:**
   `applications.py`'de bazı yerlerde İngilizce `"Application not found"`, AMA `applications.py:72`
   Türkçe `"Uygulama bulunamadı"`. Aynı dosya, aynı kavram, iki farklı dil. Aynı desen
   `instances.py` içinde DAHA BÜYÜK ölçekte: 14 kez İngilizce `"Instance not found"` VE 4 kez
   (satır 673/804/905/963) Türkçe `"Instance bulunamadı"` — AYNI dosya, AYNI kavram, iki dil bir
   arada.
2. **"Instance bulunamadı" — Türkçe ama YAZIM FARKLI:** `metrics.py`'de 9 kez `"Instance bulunamadi"`
   (noktasız/çizgisiz "i") yazılmış — `ı` harfi eksik; "bulunamadı" biçimiyle TUTARSIZ.
3. **"Dashboard" — bazen çevrilmeden, bazen Türkçe ekle karma:** `App.tsx`/`InstanceDetailPage.tsx`'te
   çıplak "Dashboard", `AdminPage.tsx`/`NotFoundPage.tsx`'te "Dashboard'a dön" (İngilizce kök +
   Türkçe iyelik eki) — ikisi de nihai Türkçe karşılığı belirlenince AYNI olmalı.
4. **"Blocking zinciri" / "Bloklama zinciri":** `ActivityPanel.tsx:56` "Blocking zinciri" derken
   `BlockingTreePanel.tsx:198` "Bloklama zinciri" diyor — aynı kavram, yarı-çevrilmiş iki farklı metin.
5. **"UP"/"DOWN" iki yerde tekrarlanan, aynı ama bağımsız tanımlanmış etiket seti:**
   `ClusterHealthPanel.tsx:13-18` ve `GroupDetailPage.tsx:35` — AYNI anlamda, iki AYRI yerde
   hardcode edilmiş (terminology.ts'te YOK) — çeviri sırasında tek bir ortak sabite taşınmalı, yoksa
   ikisi ayrışmaya devam eder (tam olarak bu dosyanın var olma nedeni olan hata sınıfı).
6. **"Alerts" / "alarm":** sayfanın KENDİ başlığı İngilizce "Alerts" derken, aynı sayfadaki buton
   (`ADD_ACTIONS.alertRule` → "+ Özel kural ekle") ve URL (`/alerts`) "alarm" kavramını Türkçe
   kullanıyor (kural, olay) — başlık dışarıda kalmış.
7. **"standalone" topolojisi — ÜÇ farklı gösterim:**
   `ClusterHealthPanel.tsx:22` (`TOPOLOGY_LABEL`) → `"Tek sunucu"` (tam Türkçe, doğru) vs.
   `InstancesPage.tsx:408` → `"Standalone (tek düğüm)"` (yarı çevrili) vs.
   `DatabaseWizardPage.tsx:30` (topoloji kart başlığı) → `"Standalone"` (ham İngilizce).
8. **"primary"/"disaster" site — İKİ farklı gösterim:**
   `ServersPage.tsx:8` (`SITE_LABELS`) → `"Ana DC"` / `"Disaster (DR)"` (ServersPage VE
   DatabaseWizardPage'in node-özet satırında tutarlı kullanılıyor) vs.
   `GroupDetailPage.tsx:642-643,886-887` → ham `"primary site"` / `"DR"` (çevrilmemiş).
9. **"primary"/"replica" rolü:** `ClusterHealthPanel.tsx:41` → `"birincil"` / `"replika"` çeviriyor;
   `GroupDetailPage.tsx:624-625` ve `DatabaseWizardPage.tsx:867-868` AYNI `role_hint` alanı için ham
   İngilizce `"Primary"` / `"Replica"` seçeneklerini gösteriyor.
10. **`warning`/`unknown` durum ifadesi:** `terminology.ts`'nin `STATUS_LABELS`'ı `warning→"Uyarı"`,
    `unknown→"Bilinmiyor"` derken; `ReportsPage.tsx`'in yerel `STATUS_TR`'si (37-43) AYNI tür durum
    rozeti için `warning→"Dikkat"`, `unknown→"Değerlendirilemedi"` diyor.
11. **"engine" / "motor":** `SchemaHealthPanel.tsx` tutarlı biçimde "motor" diyor ("SQL Server
    motoru"); `AlertsPage.tsx:225` ve `CustomAlertRuleFormPage.tsx:187` AYNI kavram için ham
    İngilizce "engine"/"Engine" kullanıyor.
12. **Özet-sekmesi vs. Metrikler-sekmesi grafik başlıkları:** `InstanceDetailPage.tsx:1098`
    "Bağlantılar (son 60 dk)" vs. `:1183` "Connections over time" — TAM OLARAK AYNI grafik (zaman
    içinde bağlantı sayısı), bir sekmede Türkçe, diğerinde İngilizce.
13. **Tuning paneli (DPA, `performance_insights.py`) vs. sağlık raporu (`report_sections.py`):**
    aynı metrikler (cache isabet oranı, replikasyon gecikmesi) DPA'da İngilizce terimle, PDF/CSV
    raporda muhtemelen tam Türkçe sunuluyor — tek gerçeklik kaynağı veri düzeyinde korunuyor ama
    ETİKET düzeyinde ayrışıyor (bkz. §C sonu).

---

## Olası kasıtlı teknik terimler (bırakılmadı, bayraklandı)

Frontend taramasının ayrıca işaretlediği, muhtemelen bilinçli DBA jargonu olan ama resmî bir karara
bağlanmamış terimler: **AAS**, **transaction**, **index/Index** (Türkçe ek alarak kullanılıyor:
"index'i", "Index önerisi"), **Cluster** (aynı ek deseni: "Cluster'a dönüştür", "Cluster adı"),
**Agent/agent**, **Query Store**, **EXPLAIN/ANALYZE**, **etcd quorum**, **split-brain**, **WAL**,
**Always On Availability Group**, **shared hit/local hit**, **Log queue/Redo queue**,
**PDF/HTML/Markdown**.

---

## Sıradaki adım

Bu tablo onaylandıktan (ve §E'deki kararlar netleştikten) sonra ADIM 2: `frontend/src/terminology.ts`'e
yeni anahtarlar eklenir (ör. `TERMS.dashboard`, ortak `STATUS_LABELS` genişlemesi — UP/DOWN/Unknown/
Skipped, Active/Idle/Waiting/Blocked gibi tekrarlanan setler TEK yerden; 3 kez tekrarlanan
`STATUS_TR` sözlükleri birleştirilir), backend'teki ~30 distinct `detail=` mesajı Türkçeye çevrilir
(`instances.py`/`metrics.py`'deki Türkçe-ama-tutarsız kardeşleriyle birlikte), `performance_insights.py`
Tuning içgörüleri Türkçeleştirilir, iki ham-istisna-sızıntı yolu (health_report.py→ReportsPage.tsx,
sql_predicates.py→IndexAdvicePanel.tsx) ayrı ele alınır, ve "izin listesi dışında İngilizce arayüz
metni varsa kırmızı" koruma testi eklenir (muhtemelen `tests/test_ui_terminology.py`'ye, mevcut
statik denetim testleri kalıbında — ham enum-değeri-ekrana-basma (§B SYSTEMIC #1) ayrı bir denetimle
yakalanmalı, çünkü bu yalnızca kelime listesi taramasıyla görünmez).
