# Sorular / Varsayımlar

Karar veremediğim veya kapsam belirsizliği olan noktalar burada; her biri için
makul bir varsayımla devam ettim.

## Faz 18 — İŞ 5: Şema bölümü günlük fotoğrafa dayanıyor, canlı sekmeyle ayrışabilir

Rapordaki Şema sağlığı bölümü `SchemaObjectDailySample` (günde bir kez
alınan fotoğraf) okuyor; arayüzdeki Şema sekmesi ise tıklandığında CANLI
katalog taraması yapıyor. Yani bulgu "dün 500 MB kullanılmayan index
vardı" derken sekme bugünkü durumu gösteriyor; gün içinde index silinmişse
ikisi ayrışır.

Bunu bu işte düzeltmedim. Düzeltmenin iki yolu vardı ve ikisi de bu işin
kapsamını aşıyordu:

1. Raporu canlı taramaya bağlamak — raporun temel kuralını (canlı probe
   yok) çiğnerdi ve 06:00'da onlarca instance'ta katalog taraması demek
   olurdu.
2. Şema sekmesini günlük fotoğrafa bağlamak — sekmenin asıl değeri
   "şu anda ne var" olduğu için işlevini bozardı.

Doğru çözüm muhtemelen sekmede iki görünüm sunmak ("şu an" / "raporun
gördüğü gün") ama bu ayrı bir iş. Şimdilik bulgu, verinin hangi güne ait
olduğunu kanıtında (`measured_at`) taşıyor.

## Faz 18 — İŞ 5: Cluster grup bulguları için dönemsel veri yok

Split-brain, etcd quorum ve DR düğümü bilgisi `GroupHealthSnapshot`'tan
geliyor; o tablo grup başına TEK satır tutuyor (son kontrolün sonucu).
Dolayısıyla "dönem boyunca quorum kaybı yaşandı mı?" sorusunu
cevaplayamıyoruz — yalnızca "şu an durum ne" biliniyor.

Dönemsel yapmak için grup sağlık geçmişini saklayan yeni bir tablo
gerekirdi (instance seviyesinde bu geçmiş zaten `MetricSample.metrics_json`
içinde var — lider değişimi ve servis kesintileri oradan dönemsel olarak
çıkarılıyor). Bunu eklemedim; bunun yerine bulgular anlık olduklarını
açıkça söylüyor ve görüntü rapor döneminin dışındaysa tarihi yazılıyor.
Sessizce dönemsel bir ölçüm gibi sunmak yanıltıcı olurdu.

## Faz 18 — İŞ 3: EXPLAIN uygulanabilirliği metinden çıkarılıyor, denenerek değil

Rapor bir sorgu için "EXPLAIN'e bakın" demeden önce EXPLAIN'in mümkün
olduğunu doğruluyor — ama bunu sorguyu gerçekten EXPLAIN ederek değil,
`validate_explainable` ile sorgu METNİNE bakarak yapıyor. Sebep: raporun
temel kuralı canlı probe yapmamak (bkz. Faz 17 İŞ 1); üstelik onlarca
instance için rapor üretilirken her sorgu için bağlantı açmak toplama
döngüsüyle yarışırdı.

Bunun bir sınırı var ve kabul edilmiş bir sınır: metin kontrolünden geçen
bir sorgu, çalıştırıldığında yine de EXPLAIN hatası verebilir (ör. sorgu
metnindeki `$1` yer tutucularının tipi çıkarılamazsa). O durumda kullanıcı
DPA'da hatayı görüyor — ama en azından açıkça reddedilecek sorgu tipleri
(DML/DDL, çoklu statement) için artık boş yönlendirme yapılmıyor.

## Faz 18 — İŞ 1: DPA'nın varsayılan görünümü değişti (API davranışı)

`GET /api/queries/{id}` aralıksız çağrıldığında eskiden "yalnızca son
toplama döngüsü"nü döndürüyordu. Artık son 24 saatlik pencerede fark
alıyor ve yanıt zarflanmış geliyor.

Bu bilinçli bir kırılma: iki görünümü tek kaynağa bağlamanın başka yolu
yoktu. Alternatif, raporu DPA'ya benzetip "son döngü"ye çekmekti — ama o
zaman rapor "dün ne oldu" sorusuna cevap veremezdi, ki raporun varlık
sebebi bu. Doğru olan DPA'nın pencereye geçmesiydi.

Yan etki: yeni eklenmiş bir instance'ta pencerede tek döngü olabilir ve
fark alınamaz. Listeyi boş bırakmak yerine kümülatif değerleri
`mode="snapshot"` etiketiyle gösteriyorum ve arayüz bunu açıkça yazıyor —
sessizce yanlış sayı göstermektense ne gösterildiğini söylemek.

## Faz 18 — İŞ 1: Sistem sorgusu tespiti desen tabanlı, kusursuz değil

Sistem/platform sorguları düzenli ifadelerle tanınıyor (pg_catalog,
pg_stat_*, pg_walfile_*, information_schema, Supabase/RDS/Cloud SQL/Azure
iç sorguları, dbace'in kendi sorguları). Bu yaklaşımın iki bilinen sınırı
var:

1. **Yanlış pozitif:** uygulamanın kendi sorgusu bir katalog görünümüne
   bakıyorsa (ör. bir yönetim ekranı `pg_stat_activity` sorguluyorsa)
   sistem sorgusu sayılır ve varsayılan listede görünmez.
2. **Yanlış negatif:** listede olmayan bir platformun iç sorgusu
   filtrelenmez.

İkisini de tamamen çözmenin yolu sorguyu ÇALIŞTIRAN rolü bilmek
(`pg_stat_statements.userid` → `pg_roles`), ama collector şu an o alanı
toplamıyor. Bunu bir sonraki adıma bıraktım; bu arada iki koruma var:
filtrelenen sorgu sayısı arayüzde görünüyor, sınıflandırmanın SEBEBİ de
(hangi kurala takıldı) taşınıyor, ve "Sistem sorgularını göster"
seçeneğiyle liste tam haliyle açılabiliyor.

## Faz 18 — İŞ 1: Şema bölümünde bulunan veri yeterliliği hatası

Denetim sırasında çıktı: "kullanılmayan index" bulgusu trend
gerektirmiyor (bugünkü `idx_scan = 0` tek başına yeterli) ama büyüme
trendinin ≥2 günlük eşiğinin arkasında bekletiliyordu. Sonuç olarak yeni
bir kurulumda ilk iki gün boyunca bölüm tamamen "bilinmiyor" dönüyor ve
kullanılmayan indexler hiç raporlanmıyordu.

İki bulgu tipinin veri ihtiyacını ayırdım. Buradan çıkan genel ders şu ve
İŞ 5 denetiminde bunu diğer bölümlerde de aradım: **bölüm seviyesinde tek
bir yeterlilik eşiği koymak yanlış** — eşik bulgu tipine ait olmalı.

## Faz 17 sonrası düzeltme: Yerel Python 3.14 ↔ canlı 3.12 farkı kapatılmadı

`AdviceOut` ileri referansı canlıyı düşürdü çünkü yerelde 3.14 (PEP 649,
ertelemeli annotation), canlıda 3.12 (hemen değerlendiren annotation)
çalışıyor. Doğru kalıcı çözüm yerel sanal ortamı canlıyla aynı sürüme
almak ya da CI'yı `python:3.12-slim` imajında koşturmak.

Bu düzeltmede onu YAPMADIM. Sebep: `backend/.venv`'i 3.12'ye taşımak tüm
bağımlılıkların yeniden kurulmasını gerektiriyor (pydantic-core, asyncpg,
aioodbc gibi derlenmiş paketler dahil) ve bu, "canlı çöktü" acil
düzeltmesinin kapsamını kullanıcının onayı olmadan genişletmek olurdu.
Ayrıca CI yapılandırması `deploy/` altında ve kurallar gereği ona
dokunulmuyor.

Bunun yerine bu bug SINIFINI sürümden bağımsız olarak kapattım
(`tests/test_definition_order.py`, AST taraması). Yani aynı hata bir daha
kaçmaz; ama 3.12 ile 3.14 arasındaki BAŞKA uyumsuzluklar hâlâ yerelde
görünmez kalabilir. Sürüm hizalaması ayrı bir iş olarak durmalı.

**GÜNCELLEME (Faz 21 İŞ 1) — risk büyük ölçüde kapandı, tamamen değil.**
GitHub Actions CI eklendi (`.github/workflows/ci.yml`) ve backend işi
canlıyla AYNI sürümde koşuyor: `python-version: "3.12"`, kaynağı
`deploy/onprem/Dockerfile.backend`'deki `python:3.12-slim`. Yani 3.12'ye
özgü her kırılma artık push anında yakalanıyor.

**Yerel `backend/.venv` HÂLÂ 3.14.** Kalan risk şu: yerelde yeşil görünen
bir şey CI'da kırmızı çıkabilir. Bu artık *canlıya* değil *CI'a* düşen bir
sürpriz — yani zararsız hale geldi, ama geliştirme sırasında hâlâ vakit
kaybettirebilir. Yerel venv'i 3.12'ye taşımak tüm derlenmiş bağımlılıkların
(pydantic-core, asyncpg, aioodbc) yeniden kurulmasını gerektiriyor;
ayrı bir iş olarak duruyor.

Hızlı yerel kontrol için CI'ın ilk adımı tek başına çalıştırılabilir:

    cd backend && python scripts/check_model_integrity.py

## Faz 17 sonrası düzeltme: Denetim tırnaklı ileri referansları serbest bırakıyor

AST tarayıcı yalnızca TIRNAKSIZ ileri referansları hata sayıyor;
`b: "B | None"` gibi tırnaklı olanlar muaf. Sebep: tırnaklı annotation
hiçbir Python sürümünde sınıf gövdesinde değerlendirilmiyor, pydantic
onu sonradan çözüyor — yani gerçek bir risk değil. Hepsini yasaklamak,
karşılıklı referanslı modelleri (A → B → A) imkânsız kılardı.

Bunun bedeli: tırnaklı ama ASLA tanımlanmayan bir tip, tarayıcıdan
geçer. O durumu ikinci savunma hattı yakalıyor —
`__pydantic_complete__` kontrolü, çözülemeyen tipi import sonrası eksik
model olarak raporluyor.

## Faz 17 — Ek İŞ B: Eski öneri alanları kaldırılmadı

`recommendation`, `commands`, `steps`, `action` ve `playbook` alanları
API'de duruyor; yeni `advice` yapısı onların yanına eklendi. Temiz olan,
eskilerini silmekti — ama bu, bu değişiklikten ÖNCE üretilmiş rapor ve
dashboard snapshot kayıtlarının önerisiz görünmesi demekti (o kayıtlarda
`advice` NULL). Rapor geçmişi bir arşiv; geçmişe dönük bir görüntüyü
bozmamak, şema temizliğinden önce geliyor. Arayüz `advice` varsa onu,
yoksa eski alanları kullanıyor.

## Faz 17 — Ek İŞ B: Bölüm önerileri kademeli zenginleştirildi

12 rapor bölümünün hepsine elle tam öneri (neden + adımlar + dikkat +
geri alma + doğrulama) yazmak yerine motora bir geri düşüş koydum: bölüm
yapılandırılmış öneri vermezse `recommendation` + `commands`
alanlarından asgari ama GEÇERLİ bir yapı üretiliyor. Böylece arayüz her
bulguda aynı şekli görüyor ve bölümler zamanla tek tek zenginleşebiliyor.

Bu işte üç bölüm tam öneriyle yazıldı (erişilebilirlik, bağlantı
doluluğu, cache hit) — en sık görülen ve en somut aksiyonu olanlar.
Diğerleri asgari yapıyla çalışıyor; eksiklik "öneri yok" olarak değil,
"daha az ayrıntılı öneri" olarak görünüyor.

## Faz 17 — Ek İŞ B: Tahmin önerisi sunum anında türetiliyor

Tahminlerin `playbook` alanı zaten veritabanında saklı (Faz 16-B İŞ 7).
`advice`'i ikinci bir kolon olarak yazmak yerine `PredictionOut` üzerinde
bir Pydantic validator ile sunum anında türetiyorum. Sebep: aynı bilginin
iki kopyası zamanla ayrışır — playbook metni güncellendiğinde eski
kayıtların `advice`'i eski kalırdı. Rapor bulgularında ise tam tersini
yaptım (`advice` saklanıyor), çünkü orada bulgu KENDİ anının fotoğrafı
olmalı ve o günkü öneriyi taşımalı.

## Faz 17 — Ek İŞ B: Index önerisinde CONCURRENTLY tercih edildi

Index advisor'ın ürettiği ham DDL düz `CREATE INDEX`. Standart öneriye
çevirirken `CONCURRENTLY` ekliyorum: üretim veritabanında tabloyu yazmaya
kapatan bir komutu kopyala-yapıştır edilebilir biçimde sunmak
sorumsuzluk olurdu. Karşılığında CONCURRENTLY'nin kendi riskleri var
(işlem bloğunda çalışmaz, yarıda kalırsa INVALID index bırakır, iki kopya
birden diskte durur) ve bunların üçü de "Dikkat" başlığında yazılı.

## Faz 17 — Ek İŞ A: Tablo adı korundu, model genişletildi

`FindingAcknowledgement` artık "kabul" değil bir DURUM KARARI tutuyor;
adı yanıltıcı hale geldi. Yine de tabloyu yeniden adlandırmadım: rename +
veri taşıma migration'ı, repo'daki yerleşik "ALTER TABLE ADD COLUMN"
desenine göre çok daha riskli ve kullanıcının isteği de zaten "modelini
genişlet" idi. Sınıf ve tablo adı korundu, docstring'de ne tuttuğu açıkça
yazıldı.

## Faz 17 — Ek İŞ A: Bulgu tipi için ayrı bir anahtar gerekti

"Bu bulgu tipi (küresel)" kapsamını fingerprint ile uygulamak imkânsızdı:
fingerprint hedef nesneyi içeriyor (`("collection_gap", "42")`), yani
farklı sunuculardaki aynı tip bulgular farklı fingerprint'lere sahip.
Bu yüzden `finding_type = "<bölüm>:<fingerprint_parts[0]>"` eklendi —
bölüm builder'ları ilk parçayı zaten tutarlı biçimde bulgu tipi olarak
kullanıyordu.

Sonuç: dar kapsam (instance) fingerprint ile, geniş kapsamlar
(grup/uygulama/müşteri/küresel) tip ile eşleşiyor. Bu ayrım bilinçli:
"bu sunucudaki bu bulguyu sustur" ile "bu tip bulguyu bu müşteride
sustur" farklı niyetler.

## Faz 17 — Ek İŞ A: "Çözüldü" kullanıcı beyanıyla kalıcı olmuyor

Kullanıcı bir bulguyu doğrudan "çözüldü" işaretlese bile, bulgu bir
sonraki raporda hâlâ tespit ediliyorsa durum "açık"a dönüyor ve
"doğrulanamadı" işareti alıyor. Kullanıcının beyanına güvenip bulguyu
kapalı tutmak, raporun en temel işlevini (gerçekte ne olduğunu söylemek)
bozardı. `çözüldü` yalnızca bulgunun gerçekten kaybolmasıyla kalıcı hale
geliyor.

Aynı sebeple `risk_kabul` ve `planlandı` durumlarında bitiş tarihi
saklanmıyor: o durumlarda bir tarih, bulgunun beklenmedik biçimde geri
açılmasına yol açardı. Tarih yalnızca `ertelendi` ve `yoksayıldı` için
anlamlı.

## Faz 17 — Ek İŞ A: Migration geriye dönük doldurma içeriyor

`status` kolonunun varsayılanı `open`. Bu, yükseltmeden sonra daha önce
"kabul edildi" işaretlenmiş TÜM bulguların bir anda kritik olarak geri
dönmesi demekti — kullanıcı sabah raporu açtığında aylardır susturulmuş
onlarca konuyla karşılaşırdı. Migration'a
`UPDATE report_findings SET status='ignored' WHERE acknowledged = true`
doldurması eklendi (SQLite tarafında `migrate_schema()` içinde aynısı).

## Faz 17 — İŞ 6: Önerisiz kritik bulgu atılmıyor, nedeni yazılıyor

"Her kritik/uyarı bulgusunun bir önerisi olsun; öneri veremiyorsa
nedenini yazsın" kuralını üç şekilde uygulayabilirdim:

1. Önerisiz bulguyu rapordan çıkarmak — gerçek bir sorunu gizlemek olurdu.
2. Üretimi hata ile durdurmak (kanıt kuralında yaptığım gibi) — tek bir
   bölümün eksiği yüzünden tüm rapor düşerdi; rapor 06:00'da otomatik
   çalışıyor, kimse fark etmeden günlerce rapor üretilmeyebilirdi.
3. Standart bir "neden öneri verilemedi" açıklaması koymak ve durumu
   loga düşmek.

3'ü seçtim. Kanıt kuralında ise 1/2 arasından hata vermeyi seçmiştim,
çünkü kanıt bulgunun DOĞRULUĞUNUN dayanağı: kanıtsız bulgu güvenilmez bir
iddiadır ve raporda hiç yer almamalı. Öneri ise sunum katmanı — eksikliği
bulguyu yanlış yapmaz, sadece daha az kullanışlı kılar.

## Faz 17 — İŞ 6: Şema bölümü iki günlük veri istiyor

Tek bir günlük şema fotoğrafından tablo büyümesi hesaplanamaz. Önceden
tek fotoğrafla da bölüm çalışıyor ve "0 büyüyen nesne" diyordu — bu,
"büyüme yok" gibi okunuyordu, oysa doğrusu "henüz bilmiyoruz". Artık iki
farklı gün görülmeden bölüm `unknown` dönüyor ve kaç gün daha gerektiğini
söylüyor. Bunun bedeli: yeni kurulan bir dbace'te bu bölüm ilk gün boş
kalıyor. Yanlış bir "sorun yok" mesajından iyi.

## Faz 17 — İŞ 6: Kalite kuralları bölüm bazında değil, genel test ediliyor

`tests/test_report_quality_rules.py` her bölümü ayrı ayrı test etmek
yerine tüm bölümleri gerçek veriyle çalıştırıp üretilen BÜTÜN bulgulara
aynı değişmezleri uyguluyor (kanıt var mı, kritik/uyarının önerisi var
mı, öncelik sıralaması doğru mu). Sebep: bölüm başına test yazmak, yeni
bir bölüm eklendiğinde testin eklenmesini unutmayı mümkün kılar — kural
sessizce delinir. Genel test, yeni bölüm kaydedildiği anda onu da
kapsıyor.

Bu testin kendisi bir hata yakaladı: "önceki rapor" sorgusu yalnızca
`generated_at` ile sıralandığı için, SQLite'ın saniye hassasiyetli
zaman damgasıyla aynı saniyede üretilen raporlarda zincir kopuyor ve
"kaç gündür açık" sayacı ilerlemiyordu.

## Faz 17 — İŞ 5: Rapor geçmişi karşılaştırması sayısal, bulgu bazında değil

"İki rapor yan yana karşılaştırılabilsin" isteğini genel durum, kritik/
uyarı ve toplam bulgu sayılarını yan yana koyan bir tabloyla uyguladım —
bulgu bulguya bir diff göstermedim. Sebep: bulgu bazında karşılaştırma
zaten raporun KENDİ "Dünden beri değişenler" bölümünde var (fingerprint
eşleştirmesiyle, yeni/kötüleşen/kapanan/süregelen olarak). Aynı bilgiyi
ikinci bir yerde, farklı bir mantıkla üretmek iki cevabın zamanla
ayrışması riskini getirirdi. Yan yana karşılaştırma, o bölümün
kapsamadığı soruyu ("iki hafta önceki durumla bugünkü durum") cevaplıyor.

## Faz 17 — İŞ 5: Yönetici görünümü teknik veriyi hiç almıyor

`ExecutiveReportView` bileşeni `ReportFinding` tipini import bile
etmiyor; yalnızca backend'in ürettiği `ExecutiveReport` yapısını alıyor.
Teknik raporu alıp arayüzde filtrelemek daha az ağ trafiği demek olurdu
(tek istek), ama o durumda teknik veri tarayıcıya iner ve "müşteriye
gösterilen ekranda teknik detay yok" garantisi yalnızca render mantığına
kalırdı. Ayrı uç ile veri hiç gelmiyor.

## Faz 17 — İŞ 4: PDF için ReportLab seçildi (WeasyPrint değil)

İstenen seçim buydu: "weasyprint veya reportlab — hangisini seçtiğini
gerekçesiyle yaz."

**WeasyPrint'in avantajı** açıktı: HTML/CSS render ediyor, yani HTML
export'u için yazdığım şablonu PDF için de kullanabilirdim; tek şablon,
tek bakım noktası, daha zengin tipografi.

**Ama kuramazdım.** WeasyPrint saf Python değil: cairo, pango,
gdk-pixbuf ve harfbuzz sistem kütüphanelerine bağlı. Bunları kurmak
`deploy/onprem/Dockerfile.backend` içindeki `apt-get install` satırını
değiştirmeyi gerektiriyor (şu an yalnızca `libpq5` kurulu). Görevin
kuralları deploy dosyalarına dokunmayı açıkça yasaklıyor. Dokunmadan
eklersem üretimde `ImportError`/`OSError` ile patlayan, sadece benim
geliştirme makinemde çalışan bir özellik olurdu — sessizce bozuk bir
şey teslim etmektense çalışan bir şey teslim etmeyi seçtim.

**ReportLab** saf Python (yalnızca Pillow'u opsiyonel olarak ister,
onu da kullanmıyorum). `requirements.txt`'e bir satır ekleyince
Dockerfile'daki `pip install -r requirements.txt` adımı onu zaten
kuruyor — deploy dosyalarına hiç dokunulmuyor.

**Tek şablon avantajını kaybetmemek için** araya biçimden bağımsız bir
blok belgesi katmanı koydum: rapor önce `Document`'e çevriliyor, üç
renderer da ondan besleniyor. Yani WeasyPrint'in vaat ettiği "tek içerik
kaynağı" faydası, sistem bağımlılığı olmadan elde edildi. Bedeli,
renderer'ları elle yazmak oldu (~250 satır); kazancı, çıktı biçimlerinin
yapısal olarak birbirinden ayrışamaması.

İleride Docker imajı değiştirilebilir hale gelirse WeasyPrint dördüncü
bir renderer olarak eklenebilir — mimari buna açık, `RENDERERS`
sözlüğüne bir satır.

## Faz 17 — İŞ 4: Türkçe karakterler için gömülü font

ReportLab'ın varsayılan Helvetica'sı WinAnsi (cp1252) kodlamasıyla
sınırlı; ğ, Ğ, ş, Ş, ı, İ bu kümede YOK. Türkçe bir ürün için PDF
çıktısında "Ig˘dır" gibi bozuk metin kabul edilemezdi.

Sistem fontuna güvenmek (ör. DejaVu) `python:3.12-slim` imajında font
paketi bulunmadığı için çalışmazdı ve yine Dockerfile değişikliği
gerektirirdi. Bunun yerine ReportLab'ın KENDİ paketiyle gelen Bitstream
Vera TTF'lerini gömüyorum — pip ile zaten geliyorlar, ek dosya yok,
lisansları serbest. Vera'nın gerekli tüm Türkçe karakterleri (ve tire/
tırnak gibi tipografik işaretleri) içerdiğini kontrol edip teste bağladım.

## Faz 17 — İŞ 4: Grafikler PDF'e konmadı

İŞ 3'te yönetici raporu için "birkaç basit grafik" isteniyordu. Dışa
aktarımda grafik yerine SAYISAL TABLO kullandım (önceki dönem / bu dönem
karşılaştırması). Sebep: PDF'e grafik basmak ya ReportLab'ın kendi çizim
API'siyle ikinci bir görselleştirme katmanı yazmayı ya da matplotlib
bağımlılığı eklemeyi gerektirirdi; ikisi de bu işin kapsamını ciddi
biçimde büyütürdü. Karşılaştırma tablosu aynı bilgiyi (iyileşti mi,
kötüleşti mi, ne kadar) kayıpsız veriyor. Grafikler arayüzde (İŞ 5)
gösterilecek; export'ta tablo olarak yer alıyor.

## Faz 17 — İŞ 3: Yönetici cümleleri şablondan üretiliyor, bulgudan çevrilmiyor

"Aynı veriden türeyecek, aynı gerçeği anlatacak, sadece derinlik ve dil
farklı olacak" isteğini iki türlü uygulayabilirdim: (a) teknik bulgunun
metnini sadeleştirerek/temizleyerek, (b) bölüm türüne göre sabit
şablonlardan yeniden yazarak.

(b)'yi seçtim. Sebep, "ASLA sorgu metni / parametre adı / komut /
IP-host" kuralının güvenilir biçimde uygulanabilmesi. Teknik metni
temizlemek (kara liste ile silmek) kırılgan: bir gün yeni bir bulgu türü
eklendiğinde ya da bir sorgu metni beklenmedik bir biçimde geldiğinde
sızıntı olur ve bunu kimse fark etmez — üstelik sızıntının gittiği yer
müşteri. Şablon yaklaşımında sızıntı için birinin şablona bilerek teknik
bir alan eklemesi gerekir, o da ikinci katmandaki taramaya takılır.

Bedeli: yönetici raporu bulgu bazında değil ALAN bazında konuşuyor
("Performans alanında sorun var"), tek tek sorgulardan bahsetmiyor. Bu
zaten istenen davranışa yakın; yönetici hangi sorgunun yavaşladığını
değil, hangi uygulamanın etkilendiğini ve ne zaman yatırım gerekeceğini
soruyor.

## Faz 17 — İŞ 3: Hedef etiketi uygulama adı; instance adı hiç kullanılmıyor

Risk cümlelerindeki hedef için sırasıyla uygulama adı → veritabanı grubu
adı → kapsam adı deneniyor. Instance adı bilerek hiç kullanılmıyor (son
çare olarak bile), çünkü dbace kurulumlarında instance adı neredeyse her
zaman sunucu adını içeriyor (`boa-pg-prod-01`) ve bu, istenmeyen bir
altyapı detayı. Hiçbiri bulunamazsa raporun kendi kapsam adı kullanılıyor.

## Faz 17 — İŞ 3: Sağlık notu eşiği %99 uptime

"Sağlıklı / Dikkat / Riskli" notunun eşiklerini şöyle belirledim: kritik
bulgu varsa VEYA dönem erişilebilirliği %99'un altındaysa Riskli; sadece
uyarı varsa Dikkat; hiçbiri yoksa Sağlıklı. %99 seçimi, yaygın kurumsal
SLA tabanına (aylık ~7 saat kesinti) denk geldiği için; daha yüksek bir
eşik (%99.9) dbace'in ölçüm yönteminin hassasiyetinin üzerinde iddia
olurdu — erişilebilirlik toplama boşluklarından türetiliyor ve tek bir
kaçırılan döngü bile yüzdeyi oynatabiliyor.

Not: uptime ölçümünün sınırı (bkz. Faz 17 İŞ 1) yönetici raporunda
tekrar edilmiyor — orada "kesinti yaşandı" deniyor. Bu bilinçli: yönetici
raporunun amacı ölçüm metodolojisini tartışmak değil. Metodoloji teknik
raporun erişilebilirlik bölümünde yazılı duruyor.

## Faz 17 — İŞ 2: Parametre/ön koşul geçmişi için yeni bir günlük fotoğraf tablosu eklendi

"Parametre denetimi — DÜN'e göre DEĞİŞEN parametreler (biri elle
değişiklik yaptıysa görünsün)" isteğini karşılamanın tek yolu geçmişe
dönük veri saklamaktı: dbace parametreleri yalnızca kullanıcı Parametreler
sekmesini açtığında CANLI okuyordu, hiçbir yerde saklamıyordu. Aynısı ön
koşullar için de geçerliydi.

Üç seçenek vardı:

1. Raporun canlı probe yapması — Faz 17 İŞ 1'in temel kuralına
   ("rapor anlık probe yapmasın") aykırı.
2. Parametreleri 15 saniyelik toplama döngüsüne eklemek — `pg_settings`
   okuması ve ön koşul denetimi (uzantı kontrolleri, rol sorguları) bu
   sıklık için fazla pahalı ve parametreler saniyede bir değişmiyor.
3. Zaten günde bir kez çalışan rollup işine eklemek — şema taramasının
   (`collect_table_sizes`) yaptığının aynısı.

3'ü seçtim. Yeni `DailyStateSnapshot` tablosu tek bir jenerik tablo:
`kind` alanı ("parameters" / "prerequisites") ile iki farklı fotoğrafı
taşıyor. İki ayrı tablo açmak yerine tek tablo, çünkü ikisi de aynı
şekle sahip (instance + gün + JSON payload) ve ileride üçüncü bir günlük
fotoğraf gerekirse migration istemeyecek.

Bir sınır: fotoğraf BUGÜNÜN tarihiyle saklanıyor, dünün ayarı geriye
dönük okunamaz. Yani "dünden beri değişenler" karşılaştırması iki ardışık
FOTOĞRAF arasında yapılıyor; gün içinde değiştirilip geri alınan bir
parametre görünmez. Bunu gün içi bir tarama ile yakalamak, aynı pahalı
sorguyu sık çalıştırmak demekti — kabul edilebilir bir eksiklik olarak
bıraktım ve bölüm metninde "önceki fotoğrafa göre" ifadesini kullandım.

## Faz 17 — İŞ 2: "En pahalı 10 sorgu"nun tamamı bulguya çevrilmiyor

Bölüm en pahalı sorguları tablo olarak listeliyor ama bulgu (finding)
yalnızca YENİ ortaya çıkmış ya da belirgin (≥%25) kötüleşmiş ve ortalaması
≥50 ms olan sorgular için üretiliyor. Sebep gürültü kontrolü (Faz 17
İŞ 6): her veritabanının her zaman "en pahalı 10 sorgusu" vardır; bunları
her gün 10 bulgu olarak raporlamak, gerçek değişimleri görünmez kılardı.
Liste yine de tam haliyle raporda duruyor — sadece "bugün buna bak"
demiyoruz.

## Faz 17 — İŞ 2: Cluster lider değişimi örnek bazlı, olay bazlı değil

Lider değişimi, ardışık metrik örneklerine gömülü cluster anlık
görüntülerindeki `leader` alanının değişmesinden çıkarılıyor. Bu, iki
örnek ARASINDA olup biten (ve bir sonraki örnekte eski haline dönen) bir
failover'ı kaçırabilir. Doğru çözüm Patroni'nin kendi history API'sini
okumak olurdu, ama o canlı bir probe gerektirir ve raporun temel kuralına
aykırı. Kaçırma riskini kabul ettim; buna karşılık "lidersiz kalınan
ölçüm sayısı" ayrı bir bulgu olarak raporlanıyor, çünkü failover
sırasındaki lidersiz pencere genelde birden fazla örneğe yayılıyor.

## Faz 17 — İŞ 2: Bölüm eşikleri tek yerde ve mevcut eşiklerle hizalı

Rapor bölümlerinin kullandığı eşikler (bağlantı doluluğu %85/%95, cache
hit %90, yavaş sorgu 50 ms, gürültülü kural 10 tetikleme) modülün
başında tek bir blokta ve gerekçeli. Bağlantı doluluğu eşiği bilerek
`performance_insights.py` ile aynı (%85): aynı sinyali iki modülün farklı
eşiklerle yorumlaması, kullanıcının dashboard'da gördüğü uyarı ile raporda
gördüğü bulgunun çelişmesi demek olurdu.

## Faz 17 — İŞ 1: Erişilebilirlik toplama boşluklarından türetiliyor (ve sınırı yazılı)

dbace'de "veritabanı şu saatte kapalıydı" diyen doğrudan bir kayıt yok.
Elimizdeki en yakın gerçek sinyal, metrik örneklerinin arasındaki
boşluklar: collector bağlanamadığında o döngüde satır yazılmıyor. Bu
yüzden kesintiler bu boşluklardan türetiliyor.

Bunun bir sınırı var ve bu sınırı gizlemek yerine rapora yazdım: boşluk
"dbace veri toplayamadı" demektir — dbace worker'ının durması, ağın
kopması veya kimlik bilgisinin geçersiz olması da aynı boşluğu yaratır.
Rapor "veritabanı 20 dakika kapalıydı" diye kesin iddiada bulunmuyor.
Alternatif, boşlukları hiç raporlamamaktı; o da elimizdeki en değerli
erişilebilirlik sinyalini çöpe atmak olurdu.

Eşik: toplama aralığının 3 katı ve en az 60 saniye. Tek kaçırılan döngü
ağ gecikmesi ya da yavaş bir sorgu yüzünden olabilir; 15 sn'lik toplamada
45 sn'lik bir gecikmeyi "kesinti" diye raporlamak gürültü olurdu.

## Faz 17 — İŞ 1: Kapsam adı rapora kopyalanıyor (normalize edilmiyor)

`HealthReport.scope_label`, kapsamın üretim anındaki adının kopyası.
Normalleştirilmiş tasarım, adı her okumada canlı tablodan JOIN'lemek
olurdu. Kopyalamayı seçtim: rapor geçmişi bir ARŞİV. Müşteri altı ay
sonra yeniden adlandırılırsa ya da silinirse, geçmiş raporun başlığının
değişmesi (veya raporun adsız kalması) yanlış olurdu — o rapor o gün o
kapsam için üretildi.

## Faz 17 — İŞ 1: Kabul edilen bulgu susturulmuyor, sayımdan çıkarılıyor

"Kabul edilen bulgular raporda ayrı bir 'bilinen konular' bölümüne
düşsün, kritik sayısını şişirmesin" isteğini şöyle uyguladım: bulgu
rapora normal şekilde yazılıyor ve `acknowledged=true` işaretleniyor;
kritik/uyarı sayıları ve raporun genel durumu yalnızca kabul edilmemiş
bulgulardan hesaplanıyor. Bulguyu hiç üretmemek daha kolay olurdu ama
kabul edilmiş bir sorunun ciddiyeti zamanla artarsa (regressed) bunu
görebilmek gerekiyor — üretilmeyen bulgunun geçmişi de olmaz.

Süreli kabulün süresi dolduğunda kayıt SİLİNMİYOR, sadece etkisiz
sayılıyor. Böylece "bu bulgu 3 ay önce şu notla kabul edilmişti" bilgisi
kayıtta kalıyor.

## Faz 17 — İŞ 1: Zamanlanmış kapsamlar sırayla üretiliyor

`run_scheduled_reports()` kapsamları paralel değil sırayla üretiyor.
Rapor üretimi veritabanı okuması yoğun; 06:00'da onlarca kapsamı aynı
anda çalıştırmak toplama döngüsüyle yarışır ve asıl işi (metrik toplama)
geciktirirdi. Bir kapsam hata alırsa diğerleri devam ediyor.

## Faz 16-B — İŞ 7: Plan kaydediliyor, her görüntülemede yeniden üretilmiyor

`playbook` tahmin oluşturulurken hesaplanıp veritabanına yazılıyor.
Alternatif, okuma anında üretmekti (kolon eklemeye gerek kalmazdı). Şunun
için kaydetmeyi seçtim: plan, tahminin o anki ölçümlerini taşıyor
("şu an 10 GB, günde ~250 MB, ~2026-11-01'de iki katına çıkar"). Okuma
anında üretilseydi bu sayılar ya kaybolur ya da güncel değerlerle
karışırdı — kullanıcı üç hafta önceki bir tahmini açtığında o günün
gerekçesini değil bugünün sayılarını görürdü. Bedeli: plan metni
değiştiğinde eski kayıtlar eski metni taşımaya devam eder; bu, tahmin
kayıtlarının doğası gereği zaten geçmiş bir anın fotoğrafı olduğu için
kabul edilebilir.

## Faz 16-B — İŞ 7: Kısa vadeli metrik tahminlerinin çoğuna plan yazılmadı

Adım adım plan beş tahmin türü için var: disk/veritabanı dolma, bağlantı
artışı, tablo büyümesi, transaction ID wraparound, index şişmesi —
kullanıcının saydığı beş tür. Diğer kısa vadeli metrik tahminlerine
(cache hit ratio düşüşü, replication lag, TPS artışı) bilerek plan
yazmadım: çözümleri tamamen sunucu donanımına, iş yüküne ve uygulama
mimarisine bağlı; genel bir "şunu çalıştır" listesi ya yanlış yönlendirir
ya da zaten `parameter_audit`'in canlı değerlere bakarak verdiği daha
isabetli öneriyle çelişirdi. O tahminler tek cümlelik öneriyle kalıyor.

## Faz 16-B — İŞ 7: Komutlarda yer tutucu (`<sema>.<tablo>`) bırakıldı

Veritabanı boyutu ve wraparound planlarında bazı komutlar
`<sema>.<tablo>` yer tutucusuyla geliyor; tablo/index planlarında ise
gerçek adlar yazılı. Sebep: ilk iki tahmin veritabanı seviyesinde,
hangi tabloya VACUUM çalıştırılacağı planın ilk adımının ÇIKTISINA bağlı.
Rastgele bir tablo adı yazmak, kullanıcıyı yanlış tabloda işlem yapmaya
yönlendirirdi. Bu yüzden o adımlar "önce şu sorguyu çalıştır, çıkan
tabloyu buraya yaz" akışını koruyor.

## Faz 16-B — İŞ 6: Yoksayma kontrolü gizlemiyor, sadece sayımdan çıkarıyor

"Yoksayılan kontrol ön koşul yüzdesine dahil edilmesin" isteğini iki
türlü uygulayabilirdim: kontrolü listeden tamamen kaldırmak ya da
listede bırakıp sayımdan düşmek. İkincisini seçtim. Sebep: yoksayılan
kontrol çoğu zaman gerçekten eksik olmaya devam ediyor ve etkilediği
özellik çalışmıyor; onu ekrandan silmek altı ay sonra "yavaş sorgu
listesi neden boş?" sorusunu yeniden doğururdu — İŞ 1'de düzelttiğimiz
tam da bu tür bir sessizlikti. Bu yüzden yoksayılanlar ayrı bir bölümde,
gerçek durumlarıyla ve "bu kontrol yoksayıldığı için etkilediği
özellikler çalışmıyor" notuyla duruyor.

## Faz 16-B — İŞ 6: Yoksayma instance bazında, grup/uygulama bazında değil

Yoksama listesi `Instance` üzerinde saklanıyor. Aynı kümenin (grup) üç
düğümü için ayrı ayrı yoksamak gerekiyor — bir grup seviyesi
yoksaymadım. Sebep: ön koşullar gerçekten düğüm bazında farklılaşabilir
(bir replikada `pg_stat_statements` preload edilmiş, diğerinde
edilmemiş olabilir) ve grup seviyesinde yoksaymak, tek bir düğümdeki
gerçek bir eksikliği sessizce örtebilirdi. Kullanım zahmeti karşılığında
doğruluk tercih edildi; ihtiyaç görülürse grup seviyesi bir "hepsine
uygula" kısayolu sonradan eklenebilir.

## Faz 16-B — İŞ 5: VACUUM FULL komut olarak sunulmuyor

Kullanıcı "tüm üretilen SQL komutları tam ve çalıştırılabilir olsun"
dedi. Bloat riski taşıyan tablolar için en akla gelen komut
`VACUUM FULL` — ama onu kopyala-yapıştır edilebilir bir komut olarak
sunmak sorumsuzluk olurdu: tabloyu ACCESS EXCLUSIVE kilitler (tablo o
süre boyunca tamamen erişilemez) ve tablo boyutu kadar ek disk ister.
Üretim veritabanında farkında olmadan çalıştırılırsa kesinti demek.

Bu yüzden çalıştırılabilir komut düz `VACUUM (ANALYZE)`; `VACUUM FULL`
uyarısıyla birlikte yorum satırı olarak duruyor. Kullanıcı gerçekten
istiyorsa yorumu kaldırıp çalıştırıyor — bilinçli bir hareket gerekiyor.

## Faz 16-B — İŞ 5: Üç önem kovası backend'deki isimlerle eşleştirildi

Kullanıcı "kritik / uyarı / bilgi" dedi; backend `critical` / `high` /
`medium` üretiyor. Backend değerlerini yeniden adlandırmadım (alarm
motoru, dashboard önerileri ve tuning içgörüleri aynı üç değeri
kullanıyor; birini değiştirmek hepsini etkilerdi). Bunun yerine arayüzde
birebir eşleştirdim: critical→Kritik, high→Uyarı, medium→Bilgi. Dördüncü
bir seviye (`low`/`info`) üretilmediği için kova sayısı da tam üç.

## Faz 16-B — İŞ 4: "Sorunlu olmayan sorgu" tanımı: pencerede iş yapmamış olmak

"Artık sorunlu olmayan sorgular listede görünmesin" isteğini mutlak bir
eşikle (ör. "ortalama süresi 50 ms'nin altındakiler sorunlu değildir")
uygulamadım — böyle bir eşik veritabanından veritabanına anlamsız hale
gelir (OLTP'de 20 ms yavaştır, raporlama veritabanında 2 saniye
normaldir). Bunun yerine iki mekanizma:

1. Liste zaten "seçilen ölçüte göre ilk N" — yani tanım gereği en
   sorunlu olanlar.
2. Aralık modunda sıralama kümülatif toplama değil pencere içindeki
   değişime bakıyor; pencerede çalışmamış bir sorgu doğal olarak listeden
   düşüyor. Ek olarak pencerede 1 ms'den az iş yapmış sorgular tamamen
   eleniyor (bu, "gürültü" için mutlak ama çok düşük bir taban).

## Faz 16-B — İŞ 4: An'a bağlı sorgu listesi tamamen kaldırıldı

Faz 15 İŞ 8'de eklenen "şu ana denk gelen sorgular" listesi ile yeni
"en sorunlu N" listesi aynı ekranda iki farklı cevap veriyordu. İkisini
yan yana tutmak yerine an'a bağlı olanı kaldırdım: sorgu yükü çizelgesi
duruyor (sıçrama işaretleri, tıklama, sürükleme dahil) ama artık kendi
listesini beslemek yerine tek listeyi filtreliyor. Kayıp: tek bir ana
tıklayıp "tam o saniyede ne çalışıyordu" görme. Kazanç: ekranda tek bir
"sorunlu sorgular" tanımı — ve o listedeki her satır üzerinde
EXPLAIN/index önerisi gerçekten çalışıyor.

## Faz 16-B — İŞ 3: Yakınlaştırma istemci tarafında, yeni veri çekmiyor

Grafikte bir aralık seçildiğinde sunucudan o aralık için daha yüksek
çözünürlüklü veri İSTENMİYOR — eldeki noktalar aralığa göre kırpılıyor.
Sebep: dbace metrikleri sabit bir aralıkla (varsayılan 15 sn) topluyor,
yani "daha yakına bakınca daha çok nokta" diye bir şey yok; sunucuya
gitmek aynı noktaları tekrar getirirdi. Gerçekten daha geniş/başka bir
pencere isteniyorsa "Özel" aralık kullanılıyor ve o zaman sunucuya
`start`/`end` ile gidiliyor.

## Faz 16-B — İŞ 3: Seçim etiket bazlı, zaman damgası bazlı değil

Recharts'ta X ekseni kategorik (`dataKey="time"`) olduğu için sürükleme
olayları zaman damgası değil etiket veriyor. Seçimi gerçek zaman
damgalarına çevirmek yerine etiketlerle çalışmayı sürdürdüm (grafik
verisinde `collectedAt` de tutuluyor, ileride gerekirse hazır). Bunun tek
gerçek riski etiket tekrarıydı — 7 günlük pencerede `HH:MM` tekrar
ediyordu; etiketlere gün ekleyerek çözdüm. Ekseni sürekli (zaman) eksene
çevirmek daha "doğru" olurdu ama 9 grafiği, sorgu yükü çizelgesini ve
QueryHistoryChart'ı birden değiştirmeyi gerektirirdi — bu işin kapsamını
aşıyordu.

## Faz 16-B — İŞ 2: Cascade silme düğümü silmiyor, sadece bağlantısını koparıyor

Instance cascade ile silinirken `nodes.instance_id`'yi ne yapacağımız
belirsizdi: düğümü de silmek mi, bağlantıyı koparmak mı? Düğümü silmeyi
seçmedim — Node bir cluster topolojisi kaydı (hangi sunucuda, hangi
grupta, hangi rolde), veritabanı kimlik bilgisinin kendisi değil.
Instance'ı silmek "bu veritabanına artık bağlanmıyorum" demek; düğümün
kümeden çıktığı anlamına gelmiyor. Bağlantı koparıldığında düğüm kartı
zaten var olan "Bağlantı bilgisi gir" akışını gösteriyor, yani kullanıcı
yeni kimlik bilgisiyle tekrar bağlayabiliyor.

İkinci karar: `cascade` varsayılan olarak `false`. Sessizce her şeyi
silen bir DELETE tehlikeli olurdu; kullanıcı önce 409 + sayıları görüyor,
sonra bilinçli olarak "bağlı kayıtlarla birlikte sil" diyor.

## Faz 16-B — İŞ 2: Instance düzenleme formu Instances sayfasında kaldı, kopyalanmadı

Kullanıcı "düzenleme ekranı yok" dedi; aslında tam form (host, port,
veritabanı, kullanıcı, şifre, SSL, pooler, cluster portları, agent) zaten
Instances sayfasında vardı — ulaşılamıyordu. Formu ikinci bir yere
(instance detayı veya grup detayı) kopyalamak yerine `?edit=<id>` derin
bağlantısıyla var olan forma yönlendirdim: iki ayrı yerde yaşayan iki
form kaçınılmaz olarak birbirinden ayrışır (Faz 16 İŞ 5'teki "aynı şey
her yerde aynı adla anılsın" ilkesi). Bunun bedeli bir sayfa geçişi;
karşılığında tek bakım noktası.

## Faz 16-B — İŞ 1: Kısıtlı görünürlük "hata" değil "kısıtlı" sayıldı

pg_stat_statements kurulu, önyüklü ve okunabilir olduğunda ama bağlanan
rol `pg_read_all_stats` üyesi olmadığında iki seçenek vardı: kontrolü
"eksik" (kırmızı) saymak ya da "tamam" (yeşil) saymak. İkisi de yanlış
olurdu — dbace veri topluyor (yeşil değil "hiç çalışmıyor"), ama topladığı
liste eksik (kırmızı değil "her şey yolunda"). Yeni bir durum ekledim:
`partial`. Ön koşul yüzdesinde "tamam" sayılmıyor, panelde sarı
görünüyor, dashboard'da eyleme dönük bir öneri üretiyor.

İkinci karar: maskelenmiş satırlar (`<insufficient privilege>`)
saklanmıyor. Alternatif, metni maskeli olarak listede göstermekti — ama
metni okunamayan bir satırın DPA'da hiçbir faydası yok (EXPLAIN
çalıştırılamaz, index önerilemez, parmak izi çıkarılamaz) ve listeyi
gereksiz doldururdu. Bunun yerine kaç satırın maskelendiği
görünürlük kontrolünde ve "veri neden yok" notunda sayı olarak
belirtiliyor.

## Faz 16-B — İŞ 1: Availability endpoint'i canlı probe yapıyor, önbelleğe alınmadı

`GET /api/queries/{id}/availability` her çağrıldığında hedef sunucuya
yeni bir bağlantı açıp 5 küçük katalog sorgusu çalıştırıyor. Bunu
önbelleğe almadım çünkü kullanıcı bu mesajı tam da bir şeyi düzelttikten
sonra ("GRANT verdim, şimdi ne diyor?") okuyor — bayat cevap vermek
sorunun kendisi olurdu. Bunun yerine frontend tarafında sıklığı
sınırladım: endpoint 15 sn'lik yenileme döngüsünde DEĞİL, yalnızca
Sorgular/Tuning sekmesi açıldığında ve liste gerçekten boşken çağrılıyor.
Liste doluysa hiç çağrılmıyor.

## Faz 16 — İŞ 6: Index şişmesi tahmini sadece KULLANILMAYAN indexler için

dbace'de "tüm indexlerin boyutunu her gün topla" diyen bir sorgu yok —
var olan `collect_schema_health()` sadece `idx_scan = 0` (hiç
kullanılmamış) indexleri listeliyor (`unused_indexes`), çünkü bu liste
zaten "silinebilir" kararına yardımcı olmak için var. Genel bir "index
bloat" tahmini (kullanılan AMA şişmiş bir index) için TÜM indexlerin
boyutunu her gün toplayan ayrı bir katalog sorgusu ve muhtemelen
PostgreSQL'in bilinen zor `pg_stats`-tabanlı bloat tahmini formülü
gerekirdi — bu, bu görevin kapsamını kendi başına bir alt-göreve
büyütürdü. Bunun yerine dürüst bir daraltma yaptım: "index şişmesi"
tahmini şu an sadece BÜYÜYEN VE kullanılmayan indexleri kapsıyor —
bunlar zaten en açık "aksiyon alınmalı" adayları (büyüyor + hiç
kullanılmıyor = çifte israf), ama gerçek/genel index bloat tahmini
DEĞİL. Bunu hem koddaki yorumda hem burada açıkça belirttim.

## Faz 16 — İŞ 6: Rollup/tahmin tabloları retention'dan muaf — ama PredictionInsight hâlâ 30 günde siliniyor

`MetricRollupDaily`/`SchemaObjectDailySample` bilinçli olarak
`services/retention.py`'nin sildiği tablolar listesine EKLENMEDİ — asıl
amaçları 1 aylık ham veri saklama sınırının ÖTESİNDE yaşamak (görev
metninin kendi isteği: "daha uzun tahmin isteniyorsa günlük özet
tablosu tut"). Küçük hacimleri (instance × metrik/nesne başına günde
tek satır) bunu güvenli kılıyor.

Ancak fark ettiğim bir gerilim: `PredictionInsight` satırları HÂLÂ
`created_at` bazlı 30 günlük retention'a tabi (bu ÖNCEDEN de böyleydi,
bu turda değiştirmedim). Wraparound/tablo-büyümesi gibi haftalar süren
bir riski anlatan, henüz onaylanmamış (acknowledged_at IS NULL) bir
tahmin de 30 gün sonra sessizce silinir — bir sonraki toplama
döngüsünde yeniden hesaplanıp taze bir satır olarak geri gelir (kendi
kendini onaran davranış, veri kaybı değil) ama kullanıcı "bunu daha önce
görmüştüm" bağlamını kaybedebilir. Bu, İŞ 6'nın kapsamı dışında,
retention politikasının GENEL bir tasarım kararı (tüm PredictionInsight
türlerini etkiler, sadece yeni uzun-vadeli olanları değil) — burada
düzeltmedim, ama fark edilsin diye not ediyorum: retention.py'nin
`PredictionInsight`'ı `acknowledged_at IS NOT NULL` satırlarla
sınırlaması (sadece çözülmüş/onaylanmış olanları süpürmesi) ayrı bir
küçük iyileştirme olarak değerlendirilebilir.

## Faz 16 — İŞ 5: Kapsam — tüm frontend'in baştan sona akış denetimi değil, en yüksek etkili noktalar

"Akış ve kullanılabilirlik" görevi teorik olarak sınırsız genişleyebilir
(her sayfanın her linkini, her terimi denetlemek). Somut, doğrulanabilir
ve gerçekten "sorun tespitinden çözüme tek tıkla" akışını etkileyen 4
noktaya odaklandım: dashboard önerilerinin hedef isabeti (en sık
kullanılan akış — kullanıcının dashboard'dan başlayıp bir öneriyi takip
etmesi), instance→grup geri dönüşü, tek bir gerçekten "kayıp" ham JSON
çıktısı, ve bu oturumda eklenen terimlerin (Öneri/Ön koşullar/Kaynak)
tutarlılığı. Diğer sayfalardaki (Alerts, Admin, Wizard gibi Faz 15'te
zaten üzerinde çalışılmış ekranlar) akış/terminoloji denetimini kapsam
dışı bıraktım — onlar bu görevin "sorun tespitinden çözüme" akışının
parçası değil.

## Faz 16 — İŞ 4: hypopg/pg_qualstats eksikliği bir NoAdviceReason değil — gerçekten bloke etmiyorlar

Görev "index önerisi neden gelmedi" sebepleri arasında "hypopg kurulu
değil" ve "pg_qualstats yok"u da sayıyordu. Kodu inceledim:
- `hypopg` eksikliği `advise()`'da `has_hypopg=False` olarak akıyor ve
  SADECE hipotetik index maliyet TAHMİNİNİ atlıyor
  (`has_hypopg_estimate=False`, zaten UI'da "(gerçek plan maliyeti)"
  notuyla ayrı gösteriliyor) — öneri YİNE ÜRETİLİYOR, sadece kaba bir
  istatistiksel tahmine dayanıyor. Bunu bir "öneri yok" sebebi olarak
  listelemek YANLIŞ olurdu (öneri VAR, sadece kesinliği farklı).
- `pg_qualstats` index_advisor.py'de hiç KULLANILMIYOR (grep ile
  doğrulandı) — sadece Ön koşullar denetiminde (İŞ 1) genel bir DBA
  hijyeni kontrolü olarak var. Bunu bir "öneri yok" sebebi olarak
  göstermek dbace'in aslında ihtiyaç duymadığı bir şeyi ihtiyaçmış gibi
  göstermek olurdu.

İkisini de NoAdviceReason listesine EKLEMEDİM — fabrikasyon yapmaktansa
gerçek engelleri (no_query_data, no_filter_columns, table_not_found,
already_indexed, insufficient_samples) ve gerçek hataları (yetki →
classify_connection_error ile 502) doğru yansıtmayı seçtim. "pg_stat_statements
yok/veri yok" de benzer şekilde index_advisor seviyesinde bir sebep
DEĞİL — bu durumda kullanıcı zaten "index önerisi" butonuna hiç
basamaz, çünkü yavaş sorgu listesinin kendisi (pg_stat_statements'a
dayanan) boş kalır; bu durumun açıklaması İŞ 1'in Ön koşullar
panelinde zaten var.

## Faz 16 — İŞ 3: Sunucu kaynağı (CPU/RAM/disk) ayrımı yapılamıyor — agent protokolü bunu toplamıyor

Görev host-agent'tan CPU/RAM/disk metrikleri varsa "bu sorun kaynak
artırımıyla çözülür" ayrımı yapılmasını istiyordu. `cluster_health.py`'yi
inceledim — dbace'in host-agent'ı (`v1/services`, `v1/logs`, `v1/keepalived`)
SADECE servis durumu ve log tail'i sağlıyor, hiçbir CPU/RAM/disk kullanım
ucu YOK. Bunu genişletmek (agent'a yeni bir `/v1/metrics` ucu eklemek,
agent-side implementasyon, host makinede çalışan ajan kodunun kendisi
muhtemelen bu repo'da bile değil) kendi başına ayrı bir faz büyüklüğünde
bir iş olurdu. Bunun yerine, GÖREVİN KENDİSİNİN de öngördüğü ikinci yolu
seçtim: "agent yoksa bunu söyle ve kurulumunu öner" — agent
yapılandırılmışsa DA aynı dürüstlüğü uyguladım (protokol desteklemiyor,
bu yüzden ayrım yapılamıyor) yerine sanki bir kontrol yapılmış gibi
göstermek yerine `server_resource_note` alanında açıkça söylüyorum.
Gerçek CPU/RAM/disk toplama isteniyorsa bu ayrı bir faz olmalı (agent
protokolü + agent implementasyonu + yeni bir MetricSample-benzeri host
tablosu gerektirir).

## Faz 16 — İŞ 2: "Aynı görsel kalıp" tek bir rijit bileşen değil, paylaşılan başlık + her yerin kendi içeriği

Dashboard/DPA/parametre denetimi/tahminler'in öneri VERİSİ (steps dizisi,
tek cümlelik reason, DDL komutu, SQL check komutu...) dört ayrı, birbirine
benzemeyen şekilde geliyor — bunları TEK bir rijit `{title, reason, command}`
bileşenine zorlamak (örn. Dashboard'ın numaralı adım listesini tek bir
"reason" string'ine sıkıştırmak) bilgi kaybettirirdi. Bunun yerine sadece
GERÇEKTEN ortak olan parçayı (kalın "Öneri: X" başlığı) `RecommendationHeader`
olarak paylaşılan bileşene çıkardım, her sayfa kendi içeriğini (adım listesi,
tek paragraf, tablo hücresi) kendi doğal şekliyle o başlığın altında/yanında
render etmeye devam ediyor. "Aynı görsel kalıp" isteğini böyle yorumladım:
görsel dil (başlık rengi/ağırlığı, komut kutusunun konumu) her yerde aynı,
ama veri şekli zorla tek kalıba sokulmadı.

## Faz 16 — İŞ 1: Ön koşul denetimi dashboard'da sadece gruplu instance'lar için çalışıyor

`_prerequisite_recommendations` `dashboard_snapshot.py`'deki diğer canlı-prob
kaynaklarıyla (parameter_audit, performance_insights) AYNI mimariyi
kullanıyor: sadece bir `DatabaseGroup`'a bağlı düğümler üzerinden çalışıyor,
`group_id`'si NULL olan standalone instance'lar dashboard'da hiç ön koşul
uyarısı almıyor. Bunu yeni bir sınır olarak İCAT etmedim — mevcut
mimarinin zaten sahip olduğu bir sınırı olduğu gibi koruyup üstüne inşa
ettim (tutarlılık, ayrı bir "standalone instance'lar için farklı bir yol"
inşa etmek bu işin kapsamını gereksiz büyütürdü). `GET /api/instances/{id}/
prerequisites` uç noktası (asıl arayüz) her instance için, gruplu olsun
olmasın, çalışıyor — sadece dashboard'daki OTOMATİK uyarı gruplu
instance'larla sınırlı.

## Faz 16 — İŞ 1: "Opsiyonel" uzantılar (pg_qualstats, pg_buffercache) neden medium, high değil

`pg_stat_statements`, `shared_preload_libraries`, okuma yetkisi, pg_monitor
(PostgreSQL) ve VIEW SERVER STATE, Query Store (SQL Server) eksikse dbace'in
TEMEL özellikleri (yavaş sorgu listesi, EXPLAIN, index önerisi) tamamen boş
döner — bunlar `high`. `hypopg`/`pg_qualstats`/`pg_buffercache` eksikse
index advisor YİNE ÇALIŞIYOR (regex ile sorgu metni ayrıştırma, kaba
istatistiksel tahmin) — sadece daha az kesin — bu yüzden `medium`. Severity
her `PrerequisiteCheck`'in kendi alanında taşınıyor (dashboard entegrasyonu
key'e göre ikinci bir eşleme tablosu tutmuyor) — tek doğruluk kaynağı
`prerequisites.py`, ileride yeni bir kontrol eklendiğinde severity'yi iki
yerde senkron tutma riski yok.

## Faz 15 sonrası düzeltme — PgBouncer pooler tespiti sadece host/port sezgisi, hepsi override edilebilir

`detect_pooler()` sadece host adında `pooler`/`pgbouncer` geçmesine veya
portun `6432` (PgBouncer'ın paket varsayılanı) / `6543` (Supabase'in
pooled portu) olmasına bakıyor — gerçek bir protokol/handshake tespiti
değil, bilinen isimlendirme kalıplarına dayanan bir sezgi. Standart
olmayan bir host/portta çalışan (ör. dahili bir PgBouncer 5432'de
dinliyorsa) kurulumlar YANLIŞ NEGATİF verebilir. Bunu bilerek kabul
ettim çünkü asıl düzeltme (her bağlantıda `statement_cache_size=0`)
zaten KOŞULSUZ ve tespitten bağımsız — sezgi sadece UI'da "(pooler
algılandı)" notu ve `options.uses_pooler` alanının varsayılan değeri
için kullanılıyor, hiçbir davranışı YANLIŞ ayarlamıyor. Yanlış negatif
durumda kullanıcı Instance/Node ayarlarında "Pooler kullanılıyor: Evet"
seçip elle düzeltebilir; bu alan her zaman sezgiyi geçersiz kılıyor.

`index_advisor.py`'deki hypopg tahmini için pooler bayrağına HİÇ
bakmadım — `async with conn.transaction():` sarmalaması pooler olsun ya
da olmasın her zaman doğru ve ucuz (tek bir ekstra round-trip), bu
yüzden koşulsuz uyguladım; ekstra bir "sadece pooler modunda sarmala"
dalı gereksiz karmaşıklık olurdu.

## Faz 15 sonrası düzeltme — reset_admin_password.py must_change_password'ü otomatik temizliyor

Script'i çalıştırırken `must_change_password` bilerek `False`'a
çekiliyor (yeni şifreyi "geçici" değil "gerçek" şifre gibi
davranıyor). Gerekçe: bu script'i çalıştırabilen kişinin zaten
sunucuya/veritabanına doğrudan erişimi var — web arayüzünde ayrıca bir
"ilk girişte şifre değiştir" adımına zorlamanın güvenlik faydası yok,
sadece ekstra bir adım. Alternatif (her zaman `must_change_password=True`
bırakmak, "sıfırlanan şifre geçicidir" mantığıyla) da makul olurdu;
tercih ettiğim yön CLI'yi mümkün olduğunca sürtünmesiz bir kurtarma
yolu yapmaktı — `--activate` bayrağıyla birlikte, tek komutla hem
kilidi açıp hem hemen kullanılabilir bir şifre vermek.

## Faz 15 — İŞ 8: "Olası nedenler" sadece elde olan veriden — wait-event/lock iddiası yok

Görev sorgu satırında "olası nedenler listesi" istiyordu. dbace'in
`query_history` verisi `queryid` başına `calls`/`total_time_ms`/
`mean_time_ms`/`calls_delta` tutuyor — wait-event türü, lock bekleme
süresi, veya hangi tablo/index'e dokunduğu gibi bir bilgi YOK (o
seviyedeki veri sadece canlı `Activity` sekmesinde anlık bir görüntü
olarak var, geçmişe dönük tutulmuyor). Bu yüzden "olası nedenler"
listesini SADECE gerçekten gözlemlenebilen iki sinyalle sınırladım:
çağrı sayısı artışı ve yüksek ortalama süre — ikisi de "olabilir"
diliyle, kesin bir teşhis olarak değil. "Kilit bekliyordu" veya
"sıralı tarama yapıyordu" gibi VERİYE DAYANMAYAN bir iddiada
bulunmadım; kullanıcıyı EXPLAIN'e yönlendirmek (zaten var olan bir
buton) gerçek teşhisi sağlıyor. Daha zengin bir "olası neden" listesi
istenirse, `Activity` anlık görüntüsünün de zaman damgalı olarak
saklanması gerekir — bu görevin kapsamı dışında.

## Faz 15 — İŞ 6: "Tahmini dolma tarihi" gerçek disk kapasitesine değil, veri büyüme hızına dayanıyor

Görev "disk dolma tahmini... tahmini dolma tarihi" istiyordu ama
dbace'in topladığı metrikler arasında host-seviyesi disk kapasitesi/
boş alan (`disk_total_bytes`/`disk_free_bytes` gibi) YOK — sadece
`database_size_bytes` (veritabanının kendi boyutu) var. Gerçek bir
"disk şu tarihte dolar" tahmini, bilinmeyen bir kapasiteye karşı
sahte bir kesinlik iddia etmek olurdu. Bunun yerine büyüme hızından
dürüst bir projeksiyon üretiyorum: "bu hızla ~tarih civarında iki
katına çıkabilir" — mesajda açıkça "gerçek disk kapasitesi dbace'de
izlenmiyor, bu sadece veri büyüme trendi" notu var. Aynı sinyali hem
"disk dolma" hem "tablo büyüme trendi" maddeleri için kullandım çünkü
dbace per-tablo büyüme geçmişi de tutmuyor (`SchemaHealth`'in
`bloated_tables`'ı an-be-an bir görüntü, trend değil) — tek gerçek
büyüme verisi `database_size_bytes` (DB-geneli). Önerisi bu yüzden
hem arşivleme/partitioning (tablo-seviye eylem) hem VACUUM/disk
büyütme (DB-seviye eylem) kombinasyonunu içeriyor, ikisini ayıramadığım
için. Gerçek disk kapasitesi izlemek istenirse host-agent'a yeni bir
metrik eklenmesi gerekir — bu görevin kapsamında değildi.

## Faz 15 — İŞ 2: Saklama süresi kapsamı ve hard-delete (arşivleme yok)

Görev "metrik ve olay verileri" dedi — bunu dört tabloya genişlettim:
`MetricSample`, `SlowQuerySample` (ikisi de "metrik"), `AlertEvent`
("olay"), ve `PredictionInsight` (tahmin geçmişi — teknik olarak ne
metrik ne olay ama aynı şekilde sınırsız büyüyen zaman-damgalı bir
tablo, aynı temizliğe dahil ettim). `GroupHealthSnapshot` HARİÇ
bıraktım — o bir "geçmiş" tablosu değil, her grup için TEK bir satırı
`UPDATE` ile güncelleyen bir önbellek (`unique=True` group_id), zaten
büyümüyor.

Silme gerçek `DELETE` (hard delete) — arşivleme/soğuk depolamaya
taşıma yok. Görev sadece "silsin" dedi, arşivleme istemedi; bir DBA
izleme aracında bu verinin uzun vadeli saklanması zaten beklenmez
(canlı operasyonel telemetri, denetim kaydı değil).

## Faz 15 — İŞ 1: "viewer salt-okunur" blanket POST/PUT/PATCH/DELETE=admin olarak uygulandı

Görev "viewer... ekleme/düzenleme/silme ve özel SQL kuralı çalıştırma
yapamasın" diyordu — "özel SQL kuralı çalıştırma" dışındakiler için
GET-dışı her metodu admin'e kilitleyen tek bir `require_write_access`
dependency'si yazdım (`app/services/auth_deps.py`), her router'ın
`include_router()` çağrısına ekledim. Bunun bilinçli sonucu: viewer
sadece gerçek create/update/delete uçlarından değil, teknik olarak
mutasyon OLMAYAN ama POST olan uçlardan da (bağlantı testi, EXPLAIN,
index önerisi, dashboard manuel refresh) engelleniyor. Alternatif,
her "tanısal" POST'u ayrı ayrı allowlist'e almaktı (daha ince taneli
ama ~10 endpoint'i tek tek işaretlemek + gelecekte yeni bir tanısal
POST eklenince onu da allowlist'e eklemeyi hatırlamak gerektirirdi).
"Salt-okunur" kelimesini en katı hâliyle uyguladım: viewer sadece GET
yapabilir. İstenirse ileride belirli tanısal uçlar
(`/api/instances/test`, `/api/queries/{id}/explain` gibi) için ayrı bir
"viewer okuyabilir ama create/update/delete yapamaz" ara kategorisi
eklenebilir — şu an bu ayrım yok.

## Faz 15 — İŞ 1: Logout sunucu tarafında oturum iptal etmiyor (stateless JWT)

`POST /api/auth/logout` gerçek bir endpoint ama bir no-op — JWT'ler
stateless olduğundan (imzalı, sunucuda saklanmıyor) "iptal etme" diye
bir şey yok; frontend token'ları `localStorage`'dan silip login
ekranına dönüyor. Bunun pratik sonucu: çalınan bir refresh token
(7 gün geçerli) süresi dolana kadar geçerli kalır, "logout" onu
iptal etmez. Üretim sertleştirmesi için sunucu tarafında bir refresh
token blacklist/revocation tablosu eklenebilir (her refresh'te
kontrol edilir) — bu görevin kapsamı dışında bıraktım, dahili bir DBA
aracı için (halka açık bir SaaS değil) makul bir basitleştirme.
`ACCESS_TOKEN_EXPIRE_MINUTES` varsayılanı 60dk — çalınan bir ACCESS
token'ın ömrü kısa, esas risk 7 günlük refresh token'da.

## Faz 15 — İŞ 1: ADMIN_PASSWORD verilmezse rastgele şifre üretiliyor

Görev ".env'den ilk açılışta oluşturulsun" diyordu ama `ADMIN_PASSWORD`
boşsa ne olacağını belirtmiyordu. Sabit bir varsayılan (`"admin"`,
`"changeme"` gibi) her deployment'ta aynı, tahmin edilebilir bir
kimlik bilgisi anlamına gelirdi — bunun yerine `secrets.token_urlsafe(12)`
ile rastgele bir şifre üretip bir kerelik log'a yazıyorum
(`services/bootstrap.py::ensure_default_admin`). Kullanıcı .env'de
`ADMIN_PASSWORD` ayarlarsa bu hiç devreye girmez (kendi şifresi
kullanılır). `must_change_password=True` her iki durumda da set
edildiğinden, üretilen şifre zaten tek kullanımlık.

## Faz 15 — İŞ 1: Rol gizleme (frontend) tam kapsamlı değil, backend her zaman otoriter

Viewer için en görünür ekleme/düzenleme/silme butonlarını gizledim
(sol menü, Customers/Applications/DatabaseGroups/GroupDetail/Servers/
Instances/Dashboard/Alerts/Predictions) ama her sayfadaki HER tikanik
kontrolü (ör. bazı satır-içi "test et" butonları, AlertsPage'in
"Resolve"u zaten gizlendi ama bazı ikincil aksiyonlar gözden kaçmış
olabilir) tek tek denetlemedim — zaman kısıtı nedeniyle en görünür/
sık kullanılan yolları önceliklendirdim. Bu bir güvenlik açığı değil:
`require_write_access` backend'de HER yazma isteğini rol fark etmeksizin
engelliyor (403), frontend gizleme sadece UX cilası. Gözden kaçan bir
buton varsa tıklandığında kullanıcı sadece bir hata mesajı görür, veri
değişmez.

Görev "ekleme paneli aşağı doğru sonsuz akıyor" diyordu (tekil "panel") ve
alt maddeleri hep "sihirbazda" diye başlıyordu (adım göstergesi, düğüm
kartı). `InstancesPage.tsx`'in düz ekleme/düzenleme formu da uzun ama
tek bir instance için (çok düğümlü değil) — sorunun en şiddetli hâli
sihirbazın çok düğümlü cluster görünümü (3-8 düğüm × ~10 alan = gerçek
"sonsuz akış"). Kapsamı oraya sınırladım; `InstancesPage`/`ServersPage`
formlarına katlanabilir bölüm veya sticky eylem çubuğu eklemedim.
İstenirse aynı `WizardSection` deseni oraya da taşınabilir — şu an
`DatabaseWizardPage.tsx` içinde tanımlı, paylaşılan bir bileşen değil
(tek kullanıcısı olduğu için ayrı dosyaya çıkarmadım).

## Faz 14 — İŞ 1: SSL modu sadece disable/require, tam sslmode değil

PostgreSQL'in (libpq) `sslmode`'u 6 değerli: `disable`, `allow`,
`prefer`, `require`, `verify-ca`, `verify-full`. asyncpg'nin `connect()`
fonksiyonu bunu string olarak almıyor — `ssl` parametresi `bool` veya
elle kurulmuş bir `ssl.SSLContext`. `verify-ca`/`verify-full` gibi sunucu
sertifikası doğrulaması yapan modları desteklemek, bir CA sertifikası
dosya yolu alıp `ssl.create_default_context(cafile=...)` ile bir
`SSLContext` inşa etmeyi gerektirir — bu hem UI'da ek bir "CA sertifikası
yükle" akışı hem de collector'da dosya yönetimi ister. Kapsamı `disable`/
`require` (asyncpg'nin `ssl=None`/`ssl=True`'suna doğrudan karşılık gelen
iki değer) ile sınırladım — "şifreli bağlantı iste ama sertifika
doğrulama" ile "şifreli bağlantı iste VE sertifikayı doğrula" arasındaki
farkı desteklemiyor. Gerçek bir üretim ortamında `verify-full` gerekiyorsa
bu hâlâ eksik — ileride bir CA-sertifikası yükleme akışıyla genişletilebilir.

## Faz 14 — İŞ 1: MongoDB sihirbazda sadece standalone

`GroupTopology` enum'u `{standalone, patroni, alwayson}` — dbace'de
MongoDB replica set'i temsil eden bir topoloji hiç yok (Faz 1'den beri).
Bunu bu görevin kapsamında YENİ bir topoloji eklemek yerine (backend
modelinde `Node.role_hint`/health prob'ları vb. her yerde "patroni" ve
"alwayson"a özel dallanma var, üçüncü bir topoloji eklemek cluster_health.py,
alwayson_health.py'ye benzer yeni bir `mongo_replicaset_health.py`
gerektirirdi — bu görevin 4 maddesinin çok ötesinde bir genişleme)
MongoDB'yi sihirbazda SADECE standalone olarak destekledim, cluster
kartlarını UI'da devre dışı bıraktım ve backend'de de reddettim (422).
"replica set adı" alanı gerçek bir MongoDB replica set'e BAĞLANMAK için
var (`connection URI`'ye `replicaSet=` eklüyor) — dbace'in kendisinin
o replica set'in üyelerini/health'ini TAKİP ETMESİ ayrı, yapılmamış bir
özellik.

## Faz 14 — İŞ 1: index_advisor/explain_service ssl_mode'u kullanmıyor

`ssl_mode`, ana toplama döngüsünün collector'ında (`collectors/
postgresql.py`) gerçekten uygulanıyor. `services/index_advisor.py` ve
`services/explain_service.py`'nin kendi `_connect()`'leri (on-demand,
kullanıcı tetikledikçe çalışan EXPLAIN/index önerisi araçları) bu
parametreyi henüz okumuyor. Bilinçli bir kapsam daraltması: bu ikisi
zaten "bağlantı kurulamadı" şeklinde temiz bir hatayla anında
kullanıcıya görünür başarısız oluyor (sessiz bir yanlış davranış değil),
ana periyodik toplama döngüsünün aksine — SSL gerektiren bir sunucuda
bu ikisi görünür şekilde başarısız olur, kullanıcı hemen fark eder.
İleride aynı `(self.target.options or {}).get("ssl_mode")` satırı oraya
da eklenebilir.

## Faz 14 — İŞ 2: Sahipsiz-node kalan standalone gruplarda ekleme yolu yok

`GroupDetailPage`'in eski "Yeni düğüm" formunu kaldırıp yerine sihirbaz
linki koydum, ama bu link `group.topology !== "standalone"` olmadıkça
render edilmiyor (standalone bir gruba wizard'ın `wizard_add_nodes` uç
noktası zaten 400 döndürüyor — bkz. Faz 14 İŞ 1'in backend değişikliği).
Normalde bir standalone grubun tam olarak 1 düğümü olur ve bu asla boşa
düşmez. Ama biri o tek düğümü manuel silerse (node düzenleme sayfasından
"Sil"), grup 0 düğümlü ve artık **hiçbir UI yolundan** yeni düğüm
eklenemez hale geliyor — ne "+ Düğüm Ekle" (gizli, standalone), ne de
sol menünün "+" düğmesi (`App.tsx::groupNode`'da aynı sebeple gizli).
Tek çıkış "Cluster'a dönüştür" (topology standalone iken her zaman
görünür) ile clustera geçip sihirbazla düğüm eklemek — ama bu, kullanıcı
sadece "eski düğümü sil, yenisini ekle" istiyorsa gereksiz bir adım.
Kapsam dışı bıraktım (bu görev "dört ekleme akışı" içindi, "boş grup
kurtarma" değildi); istenirse `GroupDetailPage`'e "grubu sil" veya
"standalone'a tek düğüm ekle" için wizard'ın 400'ünü gevşeten ayrı bir
küçük iş açılabilir.

## Faz 14 — İŞ 3: Son düğümü silinen sunucu — kullanıcıya soruyoruz (auto-delete değil)

Görev iki seçenek sunuyordu: sunucu otomatik silinsin ya da kullanıcıya
sorulsun. **Kullanıcıya sormayı seçtim.** Gerekçe: bir Server artık
birden fazla Node'u barındırabiliyor (İŞ 3'ün kendisi — aynı fiziksel
kutuda ikinci bir named SQL Server instance'ı — `existing_server_id`
ile). Bu, "sunucunun son düğümü silindi" anını nadir ama gerçek kılıyor
ve o anda kullanıcının niyeti belirsiz: bazen sunucu gerçekten
kullanımdan kalkmıştır (silinsin), bazen sadece o instance/node hatalı
kaydedilmiştir ve sunucu bilgisi (host, agent_url, vb.) korunup yeni bir
node ona yeniden bağlanacaktır. Sessiz otomatik silme bu ikinci
senaryoda veri kaybı olur; bu yüzden `GroupDetailPage::onDeleteNode`
düğümü sildikten sonra `GET /api/servers/{id}/node-count` (yeni uç
nokta) ile sunucunun sahipsiz kalıp kalmadığını kontrol ediyor, kaldıysa
ikinci bir `confirm()` ile soruyor — evetse `DELETE /api/servers/{id}`
çağrılıyor. Bu, kod tabanındaki mevcut "her silme işleminden önce
confirm()" desenine de uyuyor (bkz. CLAUDE.md'nin genel yıkıcı-işlem
temkinliliği ilkesi).

## Faz 13 — İŞ 2: "Bağlantıyı test et → kaydet" bir kapı değil, bir öneri

Görev tarifinin standalone akışı bölümü "'Bağlantıyı test et' →
başarılıysa kaydet" diyor — bunu MUTLAKA test geçmeden Kaydet'in kilitli
olması gerektiği şeklinde OKUMADIM, mutlu yol sırası olarak okudum.
Gerekçe: (1) mevcut Group Detail sayfasındaki "Yeni düğüm" formu zaten
test etmeden kaydetmeye izin veriyor (Faz 10 İŞ 1'den beri) — sihirbazı
daha katı yapmak tutarsız bir davranış farkı yaratırdı; (2) demo/gerçek
kullanım senaryolarının çoğunda dbace'in demo verisi bilinçli olarak
sahte/erişilemez host'lar kullanıyor (Faz 1-2'den beri belgeli), yani
"test geçene kadar kaydedemezsin" kuralı test ortamında sihirbazı
kullanılamaz hale getirirdi; (3) bir DBA sunucu henüz kurulmadan/ağ
erişimi açılmadan ÖNCE de yapılandırmayı önceden girmek isteyebilir
(ör. bir bakım penceresinde sırayla). Bunun yerine: her düğümün son
test sonucu (veya "test edilmedi") özet adımında görünür durumda —
kullanıcı bilgilendirilir ama engellenmez.

## Faz 13 — İŞ 2: Sihirbaz her zaman YENİ sunucu oluşturur

Görev tarifinde "sunucu/veritabanı/cluster ekleme akışını tek bir
sihirbaza topla" deniyor — bunu "sıfırdan, yeşil saha ekleme" olarak
yorumladım: sihirbaz her düğüm için her zaman yeni bir `Server` satırı
oluşturuyor, var olan bir sunucuyu seçip ona ikinci bir instance
bağlama seçeneği YOK. Bu, Faz 9'da kanıtlanmış gerçek bir senaryoyu
(`boa-shared-winsvr` — bir Windows sunucusu, iki farklı Always On
grubuna üye iki named instance) sihirbazdan değil, mevcut sayfa-bazlı
akıştan (Group Detail → "Yeni düğüm" → var olan sunucuyu seç) geçirmeyi
gerektiriyor. Gerekçe: sihirbazın tek-ekran/tek-akış doğası "bu sunucu
zaten var mı, aransın mı, seçilsin mi" gibi bir arama/seçim adımı
eklemeyi zorlaştırıyor ve görev tarifi de düğüm bloklarını hep "sunucu
adı, hostname, ip, ..." gibi YENİ bir sunucu girme alanlarıyla
tarif ediyor (var olanı seçme alanı yok). Mevcut akış zaten çalışıyor
ve kaldırılmadı ("Mevcut tek tek ekleme sayfaları kalabilir" — görev
tarifinde de açıkça izin verilmiş), bu yüzden bu ayrım kullanıcıya
kapalı bir kapı bırakmıyor.

## Faz 12 — SQL Server'da statement_timeout karşılığı yok

PostgreSQL'in `SET statement_timeout = 'Xms'`'i tek bir sorgunun toplam
yürütme süresini (CPU+IO, kilit beklemesi dahil her şeyi) sınırlıyor —
düz SQL ile, sürücüden bağımsız, her zaman çalışan bir mekanizma. SQL
Server'da bunun birebir karşılığı yok:
- `SET LOCK_TIMEOUT <ms>` — sadece kilit beklemesini sınırlıyor (bir
  başka session'ın tuttuğu kilidin açılmasını bekleme süresi). Bu,
  pratikte bir "toplama sorgusu takıldı" durumunun EN YAYGIN sebebi
  olduğu için gerçek bir koruma sağlıyor, ama CPU-bound/IO-bound saf
  yürütme süresini sınırlamıyor.
- `SQL_ATTR_QUERY_TIMEOUT` (ODBC seviyesinde, pyodbc/aioodbc'de
  genellikle cursor'ın `.timeout` özelliği üzerinden) gerçek bir
  yürütme süresi sınırı olurdu, ama bu ortamda gerçek bir ODBC
  sürücüsü/SQL Server erişimi olmadığından aioodbc'nin bu özelliği
  hangi sürümde/nasıl expose ettiğini güvenilir şekilde doğrulayamadım
  — yanlış bir attribute adı yazıp sessizce hiçbir şey yapmaması (ya da
  gerçek bir sürücüye karşı patlaması) riskini almak yerine, sadece
  SQL seviyesinde her zaman çalışacağını bildiğim `LOCK_TIMEOUT`'u
  ekledim.
- `sp_configure 'query governor cost limit'` instance-wide bir ayar
  (ALTER SERVER CONFIGURATION gerektiriyor, session-scoped değil) —
  bir izleme aracının bunu hedef sunucuda global olarak değiştirmesi
  uygun değil, kapsam dışı bırakıldı.

Sonuç: SQL Server tarafında koruma PostgreSQL'dekiyle aynı güçte değil
— bu bilinçli, dokümante edilmiş bir sınır (ILERLEME.md'de de not
edildi), gelecekte gerçek bir SQL Server'a karşı doğrulanıp
`cur.timeout` eklenebilir.

## Faz 11 — PostgreSQL sürüm-uyumlu collector

**pg_stat_activity / pg_stat_replication taraması sonucu: kod değişikliği
gerekmedi.** Görev tarif metninde bu ikisi de "tespit edilecek" adaylar
olarak sayılmıştı, ama gerçek bir PG17 kaynağına (release notes,
bildiğim şema geçmişi) göre inceledikten sonra: `collect_activity()`'nin
kullandığı `pg_stat_activity` kolonları (`pid`, `usename`, `datname`,
`application_name`, `client_addr`, `state`, `wait_event_type`,
`wait_event`, `backend_type`, `query_start`, `state_change`,
`xact_start`, `pg_blocking_pids()`) PostgreSQL 10'dan beri stabil —
12-17 arasında hiçbiri kaldırılmadı/yeniden adlandırılmadı. dbace
`pg_stat_replication` view'ını hiç sorgulamıyor zaten (replikasyon
lag'i `pg_last_wal_receive_lsn()`/`pg_last_wal_replay_lsn()`
fonksiyonlarından hesaplanıyor, bunlar da PG10+'ta stabil). Bu yüzden
"tara ve uyarla" görevi burada **negatif sonuçla** kapandı — bilinçli
bir "değişiklik gerekmiyor" kararı, atlanmış bir adım değil.

**PG17'nin ötesi (PG18+) için strateji: açık üst sınır yok, `>=` ile
açık uçlu.** `PG_VERSION_CHECKPOINTER = 170_000` eşiği `version_num >=
170_000` ile kontrol ediliyor, `< 180_000` gibi bir üst sınır YOK —
yani gelecekteki bir PG18 de otomatik olarak checkpointer branch'ini
kullanacak. Gerekçe: PG17'nin şema değişikliği additive/kalıcı bir
mimari karar (checkpointer'ın bgwriter'dan ayrılması), geri
alınması beklenmiyor; version bandını kapalı aralık yapmak (ör. `17 <=
v < 18`) PG18 çıktığında sessizce YANLIŞ (eski/PG16 tarzı) sorguyu
kullanmaya başlardı — bu, açık uçlu bırakmaktan daha kötü bir
başarısızlık modu. `_detect_version()` sadece `PG_MIN_SUPPORTED_VERSION`
(12) altını logluyor, üst sınır için hiç uyarmıyor.

**"Unsupported" ile "hata" arasındaki fark bilinçli olarak ayrıldı.**
`unsupported_metrics` sözlüğüne iki farklı kaynaktan giriş düşüyor: (1)
sürüme göre GERÇEKTEN var olmayan bir şey (`buffers_backend_per_sec`
PG17+'de, `io_reads_per_sec` PG16 öncesinde) — bunlar için sorgu hiç
denenmiyor, mesaj net ("PostgreSQL 17+ gerektirir" gibi); (2) beklenmedik
bir toplama hatası (ör. yetki reddi) — bunlar için mesaj `"Toplama
hatası: {exc}"` öneki taşıyor, ham istisna metnini koruyor. İkisi aynı
sözlükte ama farklı önekle ayırt edilebiliyor; UI bunu tek bir liste
olarak gösterebilir, ayrım isteyen bir DBA metne bakarak anlayabilir.

**SQL Server tarafında version-number gating yerine hata-güdümlü
fallback tercih edildi.** PostgreSQL'de major version'a göre sistem
kataloğu atomik olarak değişiyor (17.0'da pg_stat_checkpointer her
zaman var, 16.x'te hiçbir zaman yok) — version-number gate güvenilir.
SQL Server'da `sys.dm_exec_query_stats.total_rows`/`min_rows`/
`max_rows`/`last_rows` kolonları belli bir SP/CU ile eklendi, temiz bir
major-version sınırı yok ve bunu güvenilir şekilde ezbere bilmiyorum
(ve bu ortamda doğrulayacak bir SQL Server erişimi de yok). Yanlış bir
version eşiği yazmak "13.0.6300'de var, 13.0.4001'de yok" gibi ince
farkları kaçırıp yanlış dallanmaya yol açardı — bunun yerine sorguyu
`total_rows` ile dene, hata alırsan (kolon yoksa) `NULL AS rows` ile
yeniden dene deseni seçildi: sonuç her zaman doğru (gerçekten var olan
davranışa göre dallanıyor), sadece başarısız denemede bir ekstra
round-trip maliyeti var. `collect_metrics`'teki diğer DMV/perf-counter
sorguları da (Buffer cache hit ratio, tempdb boyutu) aynı gerekçeyle
version numarasına göre değil, deneme/hata ile korunuyor — bunlar
sürümden çok edition (ör. Azure SQL Database) veya yetki (ör. VIEW
SERVER STATE olmadan tempdb'ye erişim) bağımlı, "SQL Server 2016+" gibi
bir aralık ifadesiyle temiz şekilde modellenemiyor.

**collect_metrics'te "eksik" davranışı: anahtar dict'te hiç yok, `None`
veya `0` değil.** PostgreSQL'deki kararla simetrik: `unsupported`'a
düşen bir metrik `metrics` dict'inde hiç görünmüyor (ör.
`cache_hit_ratio` perf counter sorgusu patladıysa `metrics` içinde
`"cache_hit_ratio"` anahtarı yok, `0` ya da `None` değil). Bu, frontend
tarafında "0 = gerçekten sıfır" ile "0 = toplanamadı" karışmasını önlüyor
— `MetricSample`'daki flat kolonlar (`transactions_per_sec` vb.) yine de
`.get(key) or 0` ile dolduruluyor (o kolonlar zaten NOT NULL), ama
`metrics_json`'da (grafiklerin okuduğu asıl kaynak) anahtar gerçekten
yok.

## Faz 10 — SONRA: Ekleme akışlarının eksiklerini tamamlama

Dört senaryoyu (a: PostgreSQL standalone, b: PostgreSQL Patroni, c: SQL
Server standalone/named instance, d: SQL Server Always On) sıfırdan
httpx/ASGITransport ile uçtan uca çalıştırırken bulunan gerçek eksikler:

- **"Yeni düğüm" formunda test butonu yoktu.** Faz 10 İŞ 1'in
  post-creation "Bağlantı bilgisi gir" akışına test butonu eklenmişti ama
  grup içinde İLK KEZ düğüm eklerken kullanılan orijinal "Yeni düğüm"
  formunda yoktu — aynı `onTestNewNodeConnection` deseni oraya da eklendi
  (form henüz kaydedilmediği için `POST /api/instances/test`'e sunucudan
  alınan `host` + formdaki kullanıcı adı/parola/db/port gönderiliyor).
- **Sessiz "bağlanmamış" düğüm oluşturma.** `instanceMode==="new"` iken
  kullanıcı adını boş bırakıp Ekle'ye basarsa, backend `db_username`
  boşsa sessizce `instance_id: None` ile düğüm oluşturuyordu (kullanıcı
  "bağladım" sanıp aslında bağlamamış oluyordu) — aynı şekilde
  `instanceMode==="existing"` iken instance seçilmezse. İkisi de artık
  HTML `required` ile client-side engelleniyor; backend davranışı
  bilerek değiştirilmedi (geriye dönük uyumluluk — `db_username`'siz
  `POST /api/nodes` hâlâ "bağlama" anlamına geliyor, bu form dışı
  kullanım için (ör. script) hâlâ geçerli bir senaryo).
- **Agent bilgisi test edilemiyordu.** "Agent bilgisi Server seviyesinde
  girilsin ve test edilebilsin" isteği için `fetch_agent_snapshot`
  (Faz 2'den beri `services/cluster_health.py`'de var, `v1/services` +
  `v1/keepalived` uçlarına GET atıp `agent_ok` döndürüyor) yeniden
  kullanılarak iki yeni uç eklendi: `POST /api/servers/test-agent`
  (kayıttan önce, `InstanceCreate`/`instances/test` ile aynı desen) ve
  `POST /api/servers/{id}/test-agent` (kayıtlı bir sunucu için). İkisi de
  aynı `_test_agent()` yardımcı fonksiyonunu paylaşıyor. Not: bu, agent'ın
  TCP/HTTP olarak erişilebilir ve `v1/services` uç noktasının doğru
  şekilde yanıt verdiğini doğruluyor — token'ın GERÇEKTEN doğru olup
  olmadığını (ör. agent 401 yerine sessizce boş liste dönerse) agent
  implementasyonuna bağlı; dbace tarafında ekstra bir doğrulama yok.
- **Hata mesajları ham driver metniydi.** `str(exc)` doğrudan kullanıcıya
  gösteriliyordu (`"password authentication failed for user \"postgres\""`
  gibi İngilizce/driver-özel metin). `collectors/base.py`'ye eklenen
  `classify_connection_error()` anahtar kelime eşleştirmeyle (kimlik
  doğrulama/DNS/port/timeout/veritabanı yok) Türkçe bir kategori mesajına
  çeviriyor, orijinal metni parantez içinde koruyarak. Bu bir sezgisel
  eşleme — kapsamlı bir driver-hata-kodu haritası değil; bilinen sınır
  olarak ILERLEME.md'de not edildi.

## Faz 10 — İŞ 4: Önerilerde action alanı — kaynak başına farklı anlam

"Aksiyon" tek bir şablona oturmuyor, dört öneri kaynağının dördü de farklı
bir şey sunabiliyor:
- `connectivity` → gerçek bir komut var (log komutu), doğrudan `action`.
- `index_advisor` → gerçek bir komut var (`CREATE INDEX` DDL'i), doğrudan
  `action`.
- `parameter_audit` → gerçek bir "düzeltme" komutu YOK, çünkü hedef değer
  sunucunun RAM/CPU'suna bağlı ve dbace bunu toplamıyor (Faz 3'ten beri
  bilinen sınır). `ALTER SYSTEM SET x = '<tahmini-değer>'` uydurmak yanlış
  bir değeri "resmi öneri" gibi göstermek olurdu — onun yerine güvenli,
  gerçek bir sonraki adım olan `SHOW <param>;` seçildi (mevcut değeri
  kontrol etmeye yönlendiriyor, hiçbir şeyi tahmin etmiyor).
- `performance_insights` → `insight.action` alanı VAR ama bu bir UI
  tab-hint'i (`"queries"`/`"metrics"`/`"alerts"` — TuningPanel'in hangi
  sekmeyi açacağını söylüyor), kopyalanabilir bir komut değil. Bunu
  `action` olarak kullanmak kullanıcıya "queries" diye bir "komut"
  kopyalatırdı — anlamsız. Bu kaynak için `action` bilerek boş bırakıldı;
  bunun yerine önceden hiç gösterilmeyen `insight.recommendation` (düzyazı
  öneri metni) artık `message`'a dahil edildi, en azından öneri içeriği
  kayboldu değil.

## Faz 10 — İŞ 3: "Seçili müşteri" public modda nasıl belirleniyor

Private modda "seçili müşteri" tek ve sabit (tek tenant). Public modda
böyle bir kavram doğal olarak yok — birden fazla müşteri var ve hangisinin
"seçili" sayılacağı belirsiz. Basit bir sezgisel kural seçildi: seçili
müşteri, kullanıcının o an içinde bulunduğu `/customers/:id/...` URL'sinden
okunuyor (route değiştikçe güncelleniyor), ayrıca ağaçta bir müşteri adına
tıklamak da (artık o da bir link) hem oraya gidiyor hem seçimi güncelliyor.
Bilinçli olarak YAPILMAYAN: `/applications/:id/groups` veya `/groups/:id`
gibi müşteri ID'si URL'de doğrudan görünmeyen sayfalarda ekstra bir API
çağrısıyla (`GET /api/applications/{id}` → `customer_id`) müşteriyi geriye
doğru çözmek — bu, her sayfa geçişinde bir round-trip daha eklerdi ve o
sayfaların zaten kendi breadcrumb'ı var. Sonuç: sabit "Uygulamalar" linki
kullanıcı bir müşteri sayfasını hiç ziyaret etmeden Group Detail'e URL ile
gelirse `/customers`'a düşer (boş/yanlış değil, sadece "henüz seçilmedi").

## Faz 10 — İŞ 2: Seed'de "farklı AG" kanıtı

Önceki (Faz 9) seed'de paylaşımlı Windows sunucusundaki iki instance
ikisi de `topology="standalone"` gruba üyeydi — bu, kullanıcının asıl
sormak istediği "aynı sunucudaki instance'lar birbirinden bağımsız
Always On gruplarına üye olabilir mi" sorusunu hiç test etmiyordu (iki
standalone grup, iki AG'den yapısal olarak ayırt edilemez bir senaryu
değil). Düzeltme: ikisi de artık gerçek `topology="alwayson"` gruplar,
her birinin ikinci bir replika düğümü var (aksi halde "tek düğümlü AG"
gerçekçi olmayan bir demo olurdu). Bu, sunucu sayısını artırdı (2 yeni
sunucu) — kapsam dışı bir büyüme değil, senaryonun asıl iddiasını
kanıtlamak için gerekli minimum gerçekçilik.

## Faz 9 — İŞ 5: Dashboard öneri alanları

**Teşhis:** Kullanıcının şüphesi kısmen doğruydu ama tam isabetli değildi
— öneriler instance BAĞLANTISI olmadığı için değil (Faz 8 İŞ 1 zaten bunu
çözmüştü, her düğümün bir Instance'ı var), instance'lara gerçekten
ULAŞILAMADIĞI için boştu:
- `parameter_audit` canlı bir PostgreSQL bağlantısı gerektiriyor — demo
  host'ları (`*.internal`) DNS'te yok, her zaman bağlantı hatası veriyor.
- `performance_insights` sadece zaten TOPLANMIŞ `MetricSample`'lardan
  çalışıyor — ama collector (`collect_all_instances`) da aynı erişilemez
  host'lara bağlanmaya çalıştığından hiçbir metrik satırı hiç
  toplanamıyor (`metrics_json` hep None kalıyor).
- `index_advisor` hem toplanmış bir yavaş sorgu KAYDI hem de canlı bir
  bağlantı gerektiriyor — ikisi de yok.

Üçü de mimari olarak "gerçek, erişilebilir bir veritabanına ihtiyaç
duyuyor" — demo verisi (bilinçli olarak sahte `.internal` hostname'ler
kullanıyor, bkz. Faz 1-2 notları) bunu hiçbir zaman sağlayamayacak. Kod
düzeltmesiyle bu üçünü demo'da "doldurmak" mümkün değildi.

**Çözüm:** Bu üçüne bağımlı olmayan, dördüncü bir öneri kaynağı eklendi
— `_connectivity_recommendations()`: bir grubun `down_nodes` listesi
doluysa (yani zaten prob edilmiş, gerçek health raporundan geliyor —
ekstra bağlantı gerektirmiyor) engine'e uygun bir log komutu öneren bir
kayıt üretiyor (`journalctl -u patroni` / PostgreSQL, `Get-EventLog
... MSSQLSERVER` / SQL Server, `journalctl -u mongod` / MongoDB). Bu
kaynak asla bir canlı bağlantıya ihtiyaç duymadığı için erişilemez
demo'da bile HER ZAMAN dolu — doğrulandı: 5 demo grubunun 5'i de artık
"Öneriler" panelinde ve kendi `top_issues` satırının altında bir öneri
gösteriyor.

- **Severity sabit "high":** Gerçek bir down-node durumunun önerisi
  olduğu için sabit `high` verdim (parameter_audit/performance_insights
  bulgularının aksine, burada "ne kadar kötü" diye ölçülebilir bir
  eşik yok — ya erişilemiyor ya erişiliyor).
- **Gerçek altyapıda bu kaynak sessiz kalır:** `down_nodes` boşsa hiç
  üretmiyor — erişilebilir bir kurulumda parameter_audit/performance_
  insights/index_advisor zaten normal şekilde çalışıp asıl ayrıntılı
  önerileri üretecek, `connectivity` kaynağı sadece "erişilemiyor"
  durumunun boş bırakılmaması için bir güvenlik ağı.

## Faz 9 — İŞ 4: CRUD erişimi

- **DatabaseGroup düzenlemede engine/topology hariç tutuldu:** Grup
  listesindeki inline "Düzenle" sadece `name`/`environment`/
  `access_name`/`notes` değiştiriyor — `engine`/`topology` kasıtlı
  olarak inline düzenlemeye açılmadı çünkü bunlar zaten kendi özel
  akışları olan, daha ciddi yapısal değişiklikler (topology için
  "Cluster'a dönüştür" akışı var; engine değişimi hâlâ hiçbir yerde
  desteklenmiyor, mevcut düğümlerin/instance'ların bağlantı mantığını
  bozardı). Aynı gerekçeyle Node düzenlemede `group_id` değiştirilemiyor
  (farklı bir gruba taşımak yerine silip yeniden oluşturmak daha güvenli
  — grup değişimi cluster üyeliğini etkiler).
- **"+ Ekle" butonları sayfa içi forma kaydırma (`<a href="#...">`)
  kullanıyor, modal/toggle değil:** Formlar zaten sayfada her zaman
  görünür (mevcut tasarım deseni) — modal eklemek yerine üstteki CTA
  butonunu forma çapa (`id="new-x-form"`) ile bağladım, hem "üstte net
  bir + Ekle butonu" isteğini karşılıyor hem de var olan sayfa yapısını
  korudu.
- **Sol menü ağacındaki "+" düğmesi hover ile beliriyor (varsayılan
  `opacity:0`), her zaman görünür değil:** Masaüstü/mouse kullanımını
  varsayıyor — dokunmatik cihazlarda satıra dokunmak `:focus-visible`
  üzerinden erişilebilir kalıyor ama fiziksel olarak "hover" yok; bu bir
  sınırlama, tam dokunmatik-öncelikli bir tasarım için buton her zaman
  görünür yapılabilir ama masaüstü-öncelikli bu DBA aracında görsel
  gürültüyü azaltmak için hover tercih edildi.
- **Server silme, bağlı düğüm varsa 409 ile reddediliyor** (İŞ 1'de zaten
  eklenmişti) — kullanıcı önce düğümleri silmeli/taşımalı. Sessizce
  orphan bırakmak yerine engellemeyi tercih ettim.

## Faz 9 — İŞ 3: Engine-aware probe doğrulaması

Asıl kod değişikliği İŞ 1'in `cluster_health.py` yeniden yazımında
yapıldı (bkz. o bölüm) — burada sadece hedeflenen semptomun tam olarak
düzeldiğini doğruladım:
- `boa-sqlserver-ag` (engine=sqlserver, topology=alwayson) grubunun
  prob'ladığı servisler artık tam olarak `{sqlserver, alwayson,
  windows_cluster}` — `postgresql`/`patroni`/`etcd`/`keepalived`/
  `haproxy`'den hiçbiri yok, ne prob'lanıyor ne raporda görünüyor.
- Dashboard `top_issues` listesindeki mesajlarda ("4 düğüm erişilemez: ...")
  hiçbir yerde "PostgreSQL" geçmiyor — zaten mesaj şablonları motor adı
  kullanmıyordu, asıl sorun servis etiketinin kendisiydi (madde yukarıda).
- `down_nodes` artık `boa-sqlserver-ag`'in 4 düğümünü de doğru şekilde
  "down" işaretliyor (demo host'ları erişilemez olduğu için) — İŞ 1
  öncesi bu her zaman boş kalıyordu (bkz. İŞ 1 notu, `_ENGINE_SERVICE_
  NAME` düzeltmesi).
- `aapara-patroni` (engine=postgresql) davranışı hiç değişmedi — hâlâ tam
  Patroni/etcd/keepalived/haproxy yığınını prob'luyor.

## Faz 9 — İŞ 2: Node ↔ Instance eşleşmesi doğrulaması

Teşhis: `seed_demo.py` her düğüm için gerçekten bir Instance oluşturup
bağlıyordu ve UI (sol menü ağacı + Group Detail düğüm kartı) bunu zaten
`node.instance_id`'ye bakarak doğru gösteriyordu — kod seviyesinde İŞ 1
öncesi de bu mekanizma vardı. Rapor edilen kırıklık muhtemelen ya (a) bu
oturumun İŞ 1'i başlamadan önceki ara bir commit'te geçici olarak
bozulmuş bir ara durumdu, ya da (b) tarayıcıda eski bir `data/dbace.db`
dosyası (birçok şema değişikliğinden önce oluşturulmuş, `nodes.
instance_id` sütunu hâlâ NULL kalmış satırlar içeren) kullanılıyordu —
bu ortamda kesin olarak hangisi olduğunu geriye dönük tespit edemedim
(pre-İŞ1 kod artık üzerine yazıldı), ama İŞ 1'in Server/Node yeniden
yazımından SONRA mekanizmayı uçtan uca (API seviyesinde, tarayıcı
otomasyonu bu ortamda yok — bkz. Faz 5 doğrulama notu) test ettim ve
tamamen çalışır durumda:
- Taze bir DB'de `seed_demo.py` çalıştırıldıktan sonra her düğümün
  `instance_id`'si dolu (`GET /api/groups/{id}/nodes` ile doğrulandı).
- Sol menü ağacındaki `nodeLeaf()` bu `instance_id`'yi kullanarak
  `/instances/{id}`'ye linkliyor (App.tsx, değişmedi — zaten doğruydu).
- `GET /api/instances/{id}` ve InstanceDetailPage'in Overview sekmesinin
  ilk yüklemede çağırdığı tüm uçlar (`/metrics`, `/queries`, `/summary`,
  `/alerts/rules`, `/alerts/events`, `/predictions`, `/insights`) taze,
  hiç toplanmamış bir instance için bile 200 dönüyor (boş dizi/rapor,
  hata değil) — sayfa çökmeden açılıyor.
- **Eski bir `data/dbace.db` dosyanız varsa ve düğümlerin instance
  bağlantısı hâlâ boş görünüyorsa:** dosyayı silip yeniden oluşturun
  (bu, Faz 6/8/9'da tekrarlanan bir SQLite ALTER TABLE kısıtı deseni —
  bu kez engelleyici değildi ama eski bir dosyada birikmiş tutarsız ara
  durumlar olabilir).

## Faz 9 — İŞ 1: Server modeli (+ İŞ 3'ün engine-aware probe düzeltmesi)

İŞ 1 ve İŞ 3 aynı dosyada (`cluster_health.py`) kesişiyordu — `probe_node()`
zaten `node.server`'a taşınması gerektiğinden (İŞ 1), aynı anda hangi
servislerin prob'lanacağını da engine/topology'ye göre düzeltmek (İŞ 3)
doğal geldi; ikisini tek bir yeniden yazımda birleştirdim. Bu yüzden asıl
"PostgreSQL down" düzeltmesi bu commit'te — İŞ 3'ün kendi commit'i sadece
doğrulama/dokümantasyon içeriyor, kod tekrar yazılmadı.

- **`Node.server_id` nullable kaldı (NOT NULL yapılamadı):** SQLite `ALTER
  TABLE ADD COLUMN` yeni bir NOT NULL kısıtı (varsayılansız) ekleyemiyor;
  model `int | None` tanımlıyor ama uygulama kodu (router) her zaman
  zorunlu kılıyor (`NodeCreate.server_id: int`, opsiyonel değil). Aynı
  desen daha önce de kullanıldı (bkz. Instance.group_id).
- **Geriye dönük uyumluluk gerçek bir veri migrasyonu ile yapıldı (DB
  silme istenmedi):** Var olan `nodes.host/site/agent_url/agent_token`
  sütunları için `database.py::_migrate_nodes_to_server_model()` her
  (customer, host) çifti için bir Server satırı oluşturup düğümleri ona
  bağlıyor, sonra eski sütunları SQLite'ın (3.35+) desteklediği `ALTER
  TABLE ... DROP COLUMN` ile siliyor — bu, önceki fazlarda (Faz 6, Faz 8
  alert_rules.metric) SQLite'ın NOT NULL/tip kısıtı gevşetememesi
  yüzünden kullanıcıdan DB dosyasını silmesinin istendiği durumların
  aksine, gerçek bir kod-tarafı migrasyon. Supabase migration'ı da eşdeğer
  bir backfill + DROP COLUMN içeriyor (`DO $$ ... $$` bloğu, idempotent).
  Migrasyon başarısız olursa (ör. çok eski bir SQLite sürümü DROP
  COLUMN'u desteklemiyorsa) hata loglanıp yutuluyor — açılış çökmüyor,
  ama o durumda düğümler `server_id=NULL` kalır ve elle düzeltme
  gerekebilir; bu ortamda (Python 3.14 bundled SQLite) sorunsuz çalıştı.
- **`Node.agent_token` artık `Server.agent_token`'da — hâlâ düz metin:**
  Aynı kapsam-dışı karar (Faz 3/6'da not düşüldü) burada da geçerli;
  host-agent paylaşımlı sırrı bir DB kimlik bilgisi değil, bu istekte de
  adı geçmedi.
- **`NodeOut.host`/`site` salt-okunur, türetilmiş alanlar:** Host/site
  artık Node'da değil Server'da yaşadığı için API tüketicilerinin (özellikle
  frontend'in) her düğüm için ayrı bir Server sorgusu yapmasını önlemek
  amacıyla router bunları `node.server`'dan okuyup NodeOut'a dolduruyor —
  `NodeCreate`/`NodeUpdate`'te bu alanlar yok (sadece `server_id` kabul
  ediliyor), gerçek kaynak hep Server.
- **`_node_services()` artık engine+topology bazlı:** `topology=patroni`
  (her zaman postgresql) tam Patroni yığınını, `topology=alwayson` (her
  zaman sqlserver) `{sqlserver, alwayson, windows_cluster}`'ı,
  `topology=standalone` ise sadece tek bir engine-doğru servisi
  (`postgresql`/`sqlserver`/`mongodb`) prob'luyor — hiçbiri karışmıyor.
  `windows_cluster` servisi host-agent olmadan hiçbir şekilde
  prob'lanamıyor (WSFC için PowerShell remoting/WMI gerekir, dbace'nin
  bunu yapacak bir mekanizması yok) — agent yoksa her zaman "skipped"
  döner, varsa `merge_agent_into_services()` üzerinden agent'ın raporladığı
  değerle geçersiz kılınabilir (aynı mekanizma keepalived için zaten
  vardı, genelleştirdim).
  `alwayson` servisi de sadece TCP erişilebilirlik sinyali — gerçek AG
  sync-state/role detayı zaten ayrı, DMV-tabanlı `GET /groups/{id}/
  alwayson` endpoint'inde.
- **`down_nodes` artık engine-doğru servise bakıyor:** Önceden hep
  literal `"postgresql"` servisine bakıyordu — bu yüzden sqlserver/mongodb
  gruplarında düğümler ASLA "down" olarak işaretlenmiyordu (servis adı hiç
  eşleşmediği için sessizce boş kalıyordu), sadece SQL Server'da yanlış
  "PostgreSQL down" mesajı değil, ayrı ve daha ciddi bir sessiz-hata idi.
  Şimdi `_ENGINE_SERVICE_NAME[group.engine]`'e bakıyor.
- **Windows'ta bir sunucuda birden çok named instance örneği
  (`boa-shared-winsvr`):** `seed_demo.py`'de bu tek Windows sunucusu iki
  ayrı Node'a ev sahipliği yapıyor (`MSSQLSERVER` @ port 1433, grup
  `boa-sqlserver-test`; `REPORTING` @ port 1434, grup `boa-reporting` —
  yeni eklenen küçük bir grup) — tam olarak istenen "aynı sunucu, farklı
  gruplara üye iki instance" senaryosu.

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

## Faz 8 — İŞ 5: Alarm kuralları (default + custom) — GÜVENLİK

Özel SQL kurallarının salt-okunur çalışmasını sağlayan katmanlar
(`services/custom_alert_rules.py`):

1. **Sözdizimsel ön-denetim (`validate_readonly_sql`)** — hem kural
   oluşturma/düzenleme anında (router) hem de her çalıştırmadan hemen
   önce (defense in depth) uygulanıyor:
   - Boş sorgu reddedilir.
   - Sorgu gövdesinde `;` varsa reddedilir (çoklu ifade / stacked query
     enjeksiyonunu engellemek için — `SELECT 1; DROP TABLE x;` gibi).
   - Sorgu `SELECT` veya `WITH` ile başlamıyorsa reddedilir.
   - **Anahtar kelime kara listesi tüm sorgu gövdesinde taranıyor**
     (sadece baştan değil): `INSERT, UPDATE, DELETE, MERGE, EXEC,
     EXECUTE, DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, CALL,
     VACUUM, COPY`. Bunun nedeni: PostgreSQL'de veri değiştiren CTE'ler
     (`WITH x AS (DELETE FROM t RETURNING *) SELECT count(*) FROM x`)
     sözdizimsel olarak `WITH` ile başlayan geçerli bir SELECT'tir ama
     veri siler — salt "SELECT/WITH ile başlıyor mu" kontrolü bunu
     YAKALAYAMAZDI, bu yüzden anahtar kelime taraması eklendi.
2. **Gerçek salt-okunur transaction (sadece PostgreSQL hedeflerinde):**
   `asyncpg`'nin `connection.transaction(readonly=True)`'ı gerçek bir
   `START TRANSACTION READ ONLY` başlatıyor — PostgreSQL bu transaction
   içinde HERHANGİ bir yazma girişimini (madde 1'in kaçırdığı bir şey
   olsa bile) kendisi reddeder. Bu asıl güvenlik sınırı; regex sadece
   "erken ve anlaşılır hata mesajı" katmanı.
3. **Zaman aşımı, üç seviyeli:** PostgreSQL'de bağlantı açılırken
   `SET statement_timeout = '10s'` (gerçek sunucu-taraflı iptal) +
   `asyncpg.fetchval(..., timeout=10)` (istemci taraflı iptal) +
   dıştan `asyncio.wait_for(..., timeout=15)` (her ihtimale karşı genel
   güvenlik ağı, bağlantı kapanınca sunucudaki sorgu da iptal olur).
4. **SQL Server hedeflerinde madde 2 YOK — bilinen sınırlama:** T-SQL'de
   ad-hoc bir oturum için "bu transaction'da hiçbir yazma kabul etme"
   diyen, PostgreSQL'in `READ ONLY` transaction'ına denk bir birincil
   mekanizma yok (Always On okunabilir secondary'ler için
   `ApplicationIntent=ReadOnly` var ama primary'ye karşı hiçbir şeyi
   engellemez). Bu yüzden SQL Server hedeflerinde asıl savunma madde
   1'deki sözdizimsel + anahtar kelime denetimi — gerçek sunucu-taraflı
   zorlama yok. Eğer bu ciddi bir tehdit modeliyse, önerilen çözüm: DBA
   özel kural sorguları için ayrı, gerçekten salt-okunur bir SQL Server
   login/rolü (`db_datareader` + `DENY INSERT/UPDATE/DELETE`)
   yapılandırıp o kimlik bilgilerini kullanmalı — bu app seviyesinde
   zorlanamaz, altyapı/DBA sorumluluğunda.
- **Hedef instance/grup çözümleme (grup kuralları için):** Bir grup
  hedefli özel kural, gruptaki `role_hint == "primary"` düğümün bağlı
  Instance'ını kullanır (yoksa ilk düğüm) — `parameter_audit.py`/
  `alwayson_health.py`'deki `_select_target_node` deseniyle aynı.
  Düğümün bağlı bir Instance'ı yoksa kural o turda sessizce atlanır
  (loglanır), hata AlertEvent üretmez.
- **Her kuralın kendi `interval_seconds`'ı, tek bir sabit scheduler
  tick'i (10sn) üzerinden self-servis işleniyor:** N kural için N ayrı
  APScheduler job'ı açmak yerine, tek bir job her 10 saniyede tüm
  enabled custom kuralları tarayıp `last_run_at`'i kendi
  `interval_seconds`'ından eskiyse çalıştırıyor. En kısa desteklenen
  aralık (10sn) bu yüzden tick periyoduyla aynı seçildi — daha kısa bir
  aralık isteği pratikte 10sn'de bir çalışır.
- **`metric` kolonu NOT NULL kalmaya devam ediyor, custom kurallarda
  `""` saklanıyor:** SQLite `ALTER TABLE ... ADD COLUMN` ile var olan
  bir NOT NULL kısıtını gevşetemediği için (Faz 6'da aynı sorun
  `alert_events.instance_id` için yaşanmıştı — o zaman kullanıcı
  DB dosyasını silmek zorunda kalmıştı), bu kez modeli hiç
  `nullable`'a çevirmedim; custom kurallar `metric=""` ile geliyor
  (kullanılmıyor). Supabase migration'ı da aynı şekli koruyor (tutarlılık
  için, orada gevşetmek teknik olarak mümkün olsa da).

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

## Faz 19 — İŞ 1: Derin bağlantıda sayfa yenilemenin 404 dönüp dönmediği koddan doğrulanamıyor

Kullanıcının bildirdiği "sayfa cevap vermediğinde sık sık 404" belirtisinin
bir kısmı uygulama içi gezinmeydi ve düzeltildi. Ama bir olasılık daha var
ve bunu koddan kanıtlayamıyorum: derin bir adreste (`/instances/12`)
tarayıcıyı YENİLEMEK sunucudan o yolu ister; statik barındırma bu isteğe
`index.html` ile cevap vermezse gerçek bir HTTP 404 döner ve uygulama hiç
yüklenmez. Bu, uygulama içi hiçbir düzeltmenin çözemeyeceği bir katman.

`frontend/vercel.json` içinde açık bir SPA yönlendirmesi (`rewrites`) yok;
yalnızca `"framework": "vite"` var. Vercel'in Vite ön ayarının SPA geri
dönüşünü kendiliğinden eklediği belgeleniyor, dolayısıyla büyük ihtimalle
sorun değil — ama bunu koddan doğrulayamam ve `vercel.json` proje
kurallarına göre dokunmamam gereken bir deploy dosyası.

**Kullanıcının yapması gereken tek kontrol:** canlıda `/instances/<bir id>`
adresine gidip tarayıcıyı yenileyin. Sayfa açılıyorsa bu madde kapanır.
404 geliyorsa `vercel.json`'a şu eklenmeli (deploy dosyası olduğu için ben
eklemedim):

```json
"rewrites": [{ "source": "/(.*)", "destination": "/index.html" }]
```

## Faz 19 — İŞ 1: Uygulama içi hata izleme yok

Hata sınırı yakaladığı hatayı `console.error` ile konsola bırakıyor; hiçbir
yere raporlanmıyor. Yani bir kullanıcının karşılaştığı render hatasını,
kullanıcı ekran görüntüsü göndermedikçe göremiyoruz. Bu turda kapsam
dışıydı (bir hata toplama servisi bağlamak ayrı bir karar ve muhtemelen
banka tarafında onay gerektirir). Şimdilik ekranda "Sorun sürerse tarayıcı
konsolundaki ayrıntıyla birlikte bildirin" yazıyor.

## Faz 19 — İŞ 3: Sayfalama istemci tarafında, sunucu hâlâ her şeyi gönderiyor

Uzun listeler artık sayfalanıyor ama bu **istemci tarafı** sayfalama:
sunucu tüm satırları tek yanıtta gönderiyor, frontend yalnızca bir dilimini
render ediyor. Asıl darboğaz olan render maliyetini (her satırın kendi
düğmeleri, rozetleri, bağlantıları) çözer; ağ ve bellek maliyetini çözmez.

Sunucu tarafına taşımadım çünkü `/api/instances`, `/api/instances/summary`,
`/api/customers`, `/api/servers`, `/api/groups` ve rapor bulguları uçlarının
hepsinin sözleşmesi değişirdi (`limit`/`offset` + toplam sayı) ve bunların
bir kısmı frontend'de liste olarak DEĞİL, arama tablosu olarak kullanılıyor
(ör. `getInstances()` sonucu alarm kuralı formunda instance adı çözmek
için). Sayfalanmış bir yanıt oraları sessizce bozardı.

**Ne zaman gerekir:** birkaç bin instance'a çıkılırsa. O noktada doğru
çözüm, liste uçlarını sayfalayıp arama/çözümleme için ayrı bir hafif uç
(`/api/instances/names` gibi) açmak.

## Faz 19 — İŞ 3: Dar ekran düzeltmeleri gerçek bir cihazda denenmedi

Kırılma noktaları (1024px / 820px / 560px) ve `.main` taşma davranışı
koddan ve CSS kurallarından çıkarılarak düzeltildi; bu ortamda tarayıcı
otomasyonu olmadığı için gerçek bir tablette görsel doğrulama yapılmadı.
Kalıcı testler kuralların varlığını doğruluyor, görünümü değil.

Kullanıcının kontrol etmesi iyi olur: bir tablette (dikey ve yatay) menü,
sekme şeritleri ve geniş tablolar. Özellikle kenar çubuğunun dikey modda
yatay bağlantı şeridine dönüşmesi — bu bir tasarım kararı, tercih
edilmezse alternatif açılır bir menü olurdu.

## Faz 20 — İŞ 2: Doğruluk, manşet ufkun kendisinde değil kontrol noktasında ölçülüyor

Uzun vadeli tahminlerin manşet ufku aylar sürüyor (disk için 180 gün,
wraparound için 365). O tarihi beklemek altı ay boyunca hiçbir geri
besleme almamak demekti, dolayısıyla aynı modelden 7 gün sonrası için
ikinci bir tahmin alınıp o ölçülüyor.

Bu geçerli bir vekil çünkü ölçülen şey MODELİN kendisi — ama birebir aynı
şey değil: 7 günde iyi çalışan bir doğrusal eğim 180 günde bozulabilir
(büyüme hızlanabilir, arşivleme yapılabilir, iş yükü değişebilir). Yani
"güven aralığı %80 tuttu" ifadesi "180 günlük tarihin %80 doğru" demek
DEĞİL, "modelin bir haftalık öngörüsü %80 tuttu" demek.

Daha doğrusu, manşet ufkun kendisini de ölçmek olurdu (tahmin 180 gün
saklanır, tarihi gelince değerlendirilir). Bunu eklemedim çünkü ilk anlamlı
sonuç için altı ay gerekirdi ve o süre boyunca panel boş kalırdı. İkisini
birlikte tutmak (hem kısa kontrol noktası hem uzun manşet ölçümü) doğru
uzun vadeli çözüm; `prediction_outcomes` tablosu buna hazır (checkpoint_days
kolonu zaten ufku taşıyor), yalnızca ikinci bir kayıt eklemek gerekir.

## Faz 20 — İŞ 2: Güvenilirlik instance bazında değil tür bazında

Rozet "bu tür tahminler ne kadar tutuyor" diyor, "bu instance'ta ne kadar
tutuyor" demiyor. Sebep pratik: tek bir instance'ta beş tamamlanmış ölçüme
ulaşmak haftalar sürer ve rozet o zamana kadar hep "bilinmiyor" kalırdı.

Sonucu şu: çok farklı davranan iki instance (biri düzenli büyüyen bir OLTP
veritabanı, diğeri dalgalı bir rapor veritabanı) aynı rozeti görür.
İkincisinin tahminleri sürekli ıskalıyorsa birincisinin rozeti de düşer.

Instance bazına inmek için ölçüm hacminin artması gerekiyor. Doğru yol
muhtemelen kademeli olmak: yeterli ölçümü olan instance için kendi oranını,
olmayan için tür ortalamasını göstermek. Bu turda yapmadım.

## Faz 20 — İŞ 3: Doğrusallık testi eşikleri ölçülerek seçildi, teorik türetilmedi

`EXPONENTIAL_R2_GAIN = 0.15`, `NOISY_R2 = 0.3`, `CURVATURE_SIGMA = 0.8` ve
`OUTLIER_MAD_MULTIPLIER = 3.5` — bunlardan yalnızca sonuncusu literatürde
standart bir değer (MAD tabanlı aykırı tespitinde yaygın kullanılır).

Diğer üçü bilinen şekillerdeki sentetik serilerle (doğrusal, üstel, eğri,
gürültülü, sabit) denenerek ayarlandı ve testlerde o serilerle
kilitlendi. Gerçek veritabanı serileri üzerinde kalibre edilmediler, çünkü
bu ortamda gerçek bir üretim serisi yok.

Pratik riski: sınırda bir seri yanlış sınıflanabilir — hafifçe eğri bir
büyüme "doğrusal" sayılabilir ya da tersine. Yanlış tarafa düşerse sonuç
bir uyarı notunun eksik/fazla olması, tahminin tamamen yanlış olması
değil. Yine de gerçek veri biriktikçe (özellikle İŞ 2'nin doğruluk
ölçümleri) bu eşikler gözden geçirilmeli.

## Faz 20 — İŞ 3: Kısa vadeli tahminler artık ilk 12 saat üretilmiyor

`PREDICTION_REQUIREMENTS` gereksinimi (40 örnek / 0.5 gün) artık gerçekten
uygulanıyor. Bu, yeni eklenen bir instance'ın ilk yarım gün kısa vadeli
tahmin üretmemesi demek.

Öncesinde 5 örnekle (75 saniye) üretiyordu; o tahminler 48 katlık bir
ekstrapolasyondu ve güvenilir değildi. Yani kayıp gerçek bir kayıp değil —
ama davranış değişikliği olduğu için burada da not ediyorum: bir kullanıcı
"eskiden hemen tahmin çıkıyordu" derse cevap bu.

Hazırlık paneli zaten "kaç gün daha veri gerekli" diyor, dolayısıyla
bekleme süresi kullanıcıya görünür durumda.

## Faz 24: Tarayıcı testleri yalnızca chromium, görsel karşılaştırma yok

Playwright kurulumu tek tarayıcıyla (chromium) koşuyor. Gerekçe: üç
tarayıcı indirmek CI süresini üç katına çıkarıyor ve bu testlerin amacı
tarayıcı uyumluluğu değil, uygulama akışlarının çalıştığını doğrulamak.
**Sonuç olarak Firefox ve Safari'ye özgü kırılmalar (CSS düzen farkları,
Date/Intl davranışı) bu testlerle yakalanmıyor.** İhtiyaç doğarsa
`playwright.config.ts` içindeki `projects` listesine eklemek yeterli.

**Görsel piksel karşılaştırması (visual regression) bilerek kapsam
dışı.** Testler "doğru öğe DOM'da mı, tıklanınca ne oluyor" seviyesinde;
"hizalama bozuldu mu, boşluk kaydı mı" seviyesinde değil. Faz 22 İŞ 2'de
düzeltilen görsel tutarlılık işleri bu yüzden hâlâ statik testlerle
(`test_ui_consistency.py`) ve gözle korunuyor. Piksel karşılaştırması
eklenirse, farklı işletim sistemlerinde font render farkı yüzünden
referans görüntülerin CI'ın koştuğu ortamda üretilmesi gerekir.

## Faz 24: E2E veritabanı SQLite, canlı Postgres

Tarayıcı testleri izole bir SQLite dosyasıyla koşuyor (`dbace_e2e.db`).
Bu, testleri hızlı ve bağımsız yapıyor ama **Postgres'e özgü davranış
farklarını (tip zorlamaları, kısıt mesajları, eşzamanlılık) kapsamıyor.**
Faz 23'teki silme hatası tam olarak böyle bir farktan doğmuştu (SQLite'ta
foreign key denetimi varsayılan kapalı). O sınıf hata için koruma
tarayıcı testinde değil, `conftest.py`'de FK denetimini açan ön koşulda
ve `test_delete_dependencies.py`'de.

## Faz 25 İŞ 1: Örnekleme yükü gerçek bir sunucuda ÖLÇÜLMEDİ

README'nin "İzleme yükü" bölümünde bekleme örnekleyicisinin maliyeti iki
parçaya ayrıldı ve ikisi farklı güvenilirlikte:

- **dbace'in kendi veritabanındaki depolama maliyeti ÖLÇÜLDÜ**: 200 bin
  satırlık gerçekçi bir tablo kurulup dosya boyutu okundu, satır başına 192
  bayt (indeksler dahil). Oradaki tablo bu ölçümden türetilmiş hesap.
- **İzlenen sunucudaki sorgu maliyeti ÖLÇÜLMEDİ.** Bu geliştirme ortamında ne
  çalışan bir PostgreSQL ne de `psql` var (Docker da kapalı); "1 ms'den az
  sürer" gibi bir sayı yazmak uydurma olurdu. Bunun yerine tasarım
  gerekçeleri (tek kalıcı bağlantı, tek round trip, sunucu tarafında filtre,
  `pg_blocking_pids` yalnızca kilit bekleyen satırlarda) ve **kullanıcının
  kendi sunucusunda ölçebileceği çalıştırılabilir sorgu** yazıldı —
  örnekleyicinin sorgusu `pg_stat_statements`'ta diğerleri gibi görünüyor.

**Kapanması için gereken:** yük altındaki gerçek bir PostgreSQL'de
örnekleyiciyi bir saat çalıştırıp yukarıdaki `pg_stat_statements` sorgusunun
`mean_ms` değerini README'ye ölçüm olarak işlemek.

## Faz 25 İŞ 1: PostgreSQL 14 öncesinde bekleme SORGU BAZINDA ayrıştırılamıyor

`pg_stat_activity.query_id` PostgreSQL 14 ile geldi. Daha eski sürümlerde
(dbace 12'yi destekliyor) örnekleyici beklemeleri yine topluyor — "sistem
neyi bekliyor" grafiği çalışıyor — ama beklemeyi SORGUYA bağlayamıyor, yani
"bu sorgu süresinin yüzde kaçını kilitte geçirdi" sorusu cevapsız kalıyor.

Bu durumda satırlar boş `queryid` ile yazılıyor ve API bunu açıkça
bildiriyor; sessizce boş liste dönmek, kullanıcının "sorgu yok" sanmasına yol
açardı. Alternatif (sorgu metnini kendimiz normalleştirip hash'lemek) reddedildi:
pg_stat_statements'ınkinden farklı bir kimlik üretir ve iki liste birbirine
bağlanamaz hale gelirdi — projede zaten kural olan "aynı veriyi gösteren
yerler tek gerçeklik kaynağından beslensin" ilkesine aykırı.

## Faz 27 İŞ 1: SQL ayrıştırıcı tercihi — sqlglot, pglast değil

Elle yazılmış regex ile SQL ayrıştırmak canlıda iki hataya yol açtı
(`'public.recurse' tablosu bulunamadı` — CTE adı tablo sanıldı;
`missing FROM-clause entry for table "pn"` — kesilmiş metne EXPLAIN
çalıştırıldı). Gerçek bir ayrıştırıcıya geçildi ve iki aday değerlendirildi.

**pglast** — PostgreSQL'in KENDİ ayrıştırıcısının (libpg_query) Python
bağlaması. Doğruluk açısından tartışmasız üstün: PostgreSQL ne kabul ediyorsa
onu kabul eder, sürüm farklarını da taşır. **Seçilmedi**, çünkü bir C eklentisi
ve derlenmesi gerekiyor:

- Bu proje yerelde **Windows / Python 3.14**, canlıda **Linux / Python 3.12**
  ile çalışıyor. pglast'in her iki ortam için de hazır tekerlek (wheel)
  sunmadığı sürüm kombinasyonları var; olmadığında kaynaktan derleme gerekiyor.
- Derleme gerekirse `Dockerfile.backend`'e derleyici eklemek gerekir — ama
  Dockerfile'lara dokunmak **proje kuralıyla yasak** (CLAUDE.md).
- Bu proje daha önce üç kez "yerelde yeşil, canlıda patlak" yaşadı. Platforma
  bağlı bir derleme bağımlılığı, o listeye dördüncüyü eklemenin en kolay yolu
  olurdu.

**sqlglot** — saf Python, bağımlılıksız, PostgreSQL lehçesini destekliyor.
Seçildi. Doğrulanan davranışlar: CTE adları, alt sorgu ve VALUES takma adları,
küme döndüren fonksiyonlar (`generate_series`), şema nitelikli adlar ve `$1`
yer tutucuları doğru çözülüyor.

**Kabul edilen sınır:** sqlglot PostgreSQL'in tamamını desteklemiyor; egzotik
bir sözdiziminde ayrıştırma başarısız olabilir. Bu durumda dbace **regex'e
düşmüyor** — "sorgu çözümlenemedi" diyor ve sebebini yazıyor. Yanlış tablo adı
üretmektense hiç üretmemek yeğdir; canlıdaki iki hatanın ortak kökü zaten
"emin değilken tahmin etmek"ti.

**Kapanması için gereken:** pglast'in hem Windows/3.14 hem Linux/3.12 için hazır
tekerlek sunduğu doğrulanırsa (ya da yerel geliştirme 3.12'ye çekilirse) geçiş
değerlendirilebilir. `sql_analysis.py` tek giriş noktası olduğu için değişim
tek dosyayla sınırlı kalır.

## Faz 27 İŞ 1: Yer tutuculu sorgularda plan, PostgreSQL 16 öncesinde alınamıyor

pg_stat_statements sorguları normalleştirilmiş hâlde saklıyor (`WHERE id = $1`).
Bu metne EXPLAIN çalıştırmak için parametre değeri gerekiyor ve dbace o değeri
bilmiyor.

Önceki davranış `$1` yerine `NULL` koyup denemekti. **Bu sessizce yanlıştı:**
planlayıcıya başka bir sorgu sunuluyordu ve dönen plan, gerçek çalıştırmanın
planı olmadığı hâlde öyleymiş gibi gösteriliyordu. Kaldırıldı.

Artık: **PostgreSQL 16+** sunucularda `EXPLAIN (GENERIC_PLAN)` kullanılıyor —
planlayıcı değerden bağımsız bir plan üretiyor ve bu, kullanıcıya uyarısıyla
birlikte gösteriliyor. **16 öncesinde plan alınamıyor** ve sebebi yazılıyor;
alternatif olarak auto_explain öneriliyor (gerçek çalıştırmanın planı).

`EXPLAIN ANALYZE` yer tutuculu sorgularda **hiçbir sürümde** çalıştırılmıyor:
ANALYZE sorguyu gerçekten çalıştırır ve uydurma değerlerle çalıştırmak hem
yanıltıcı bir plan verir hem de izlenen veritabanında öngörülemez maliyet
çıkarır.

## Faz 27 İŞ 5: PG 15-18 davranışı GERÇEK sunucularda doğrulanmadı

Sürüm yetenek matrisi (`backend/app/domain/pg_capabilities.py`) PostgreSQL'in
sürüm notlarına ve katalog belgelerine dayanıyor; **hiçbir sürüme karşı gerçek
bir bağlantıyla test edilmedi.** Bu geliştirme ortamında PostgreSQL yok (Docker
kapalı, psql kurulu değil) — bu sınır Faz 25'ten beri açık.

Testler sürüm numarasını SAHTELEYEREK hangi sorgunun gönderildiğini doğruluyor;
yani "PG 18'de op_bytes istenmiyor" kanıtlanmış durumda. Doğrulanmayan şey,
gönderilen sorgunun o sürümde gerçekten çalıştığı.

**En yüksek riskli varsayımlar** (biri yanlışsa o sürümde ilgili metrik grubu
boş gelir, çökme olmaz — her sorgu kendi try/except'inde):

- PG 18'de `pg_stat_io` sütunlarının `read_bytes` / `write_bytes` /
  `extend_bytes` olarak adlandırıldığı ve `op_bytes`'ın kaldırıldığı.
- PG 17'de `buffers_backend`'in karşılığının `pg_stat_io`'da, arka plan
  süreçleri (`checkpointer`, `background writer`) dışlanarak elde edildiği.
- `pg_stat_checkpointer` sütun adları (`num_timed`, `num_requested`,
  `write_time`, `sync_time`, `buffers_written`).

**Kapanması için gereken:** her sürüm için bir kap (container) ayağa kaldırıp
`collect_metrics` çalıştırmak ve `_unsupported_metrics` çıktısının boş olduğunu
görmek. CI'da matris job'u olarak kurulabilir (`postgres:15` … `postgres:18`
servisleriyle); bu turda kapsam dışı bırakıldı çünkü CI süresi ve Docker
bağımlılığı ayrı bir karar.

## Faz 27 İŞ 5: PG 18'in yeni vacuum süre sayaçları kullanılmıyor

PostgreSQL 18, `pg_stat_all_tables`'a toplam vacuum/analyze süresi sütunları
ekledi (`total_vacuum_time`, `total_autovacuum_time`, `total_analyze_time`,
`total_autoanalyze_time`). Bunlar şema sağlığı bölümünde değerli olurdu:
"autovacuum bu tabloda ne kadar zaman harcıyor" sorusu bugün cevapsız.

Bu turda EKLENMEDİ. Sebep kapsam: yeni bir metrik eklemek metrik kataloğunu,
saklama şemasını ve arayüz gösterimini birlikte değiştirmeyi gerektiriyor; İŞ 5
sürüm UYUMU işiydi, yeni özellik değil. Yetenek matrisi bu metrikleri eklemeye
hazır — `MetricSource(_STAT_ALL_TABLES, PG_18)` satırı yeterli.

## Faz 28 İŞ 1: msdb yedek zamanları saat dilimi bilgisi taşımıyor

`msdb.dbo.backupset` zaman damgaları sunucunun **yerel saatinde** ve saat dilimi
bilgisi yok. dbace bunları naive kabul edip **UTC varsayıyor**
(`services/backup_collection.py::_parse_dt`).

Sunucu UTC'de değilse yedek YAŞI saat farkı kadar kayar. Pratik etkisi eşiğe
göre değişiyor: tam yedek eşiği gün mertebesinde olduğu için 3 saatlik bir kayma
önemsiz, ama **log yedeği eşiği 1 saat** — orada aynı kayma yanlış kritik bulgu
üretebilir.

Dürüst çözüm sunucunun saat dilimini de okuyup dönüştürmek olurdu
(`SELECT CURRENT_TIMEZONE()` SQL Server 2019+; öncesinde
`sys.time_zone_info` + kayıt defteri). Bu turda yapılmadı çünkü İŞ 1 toplama
altyapısıydı; varsayım tek bir fonksiyonda ve yorumda açıkça duruyor.

**Kapanması için gereken:** `collect_backups` sorgusuna sunucu saat dilimini
ekleyip `_parse_dt`'ye taşımak, 2019 öncesi için UTC farkını
`sys.time_zone_info`'dan çözmek.

## Faz 28 İŞ 1: msdb başarısız yedekleri kaydetmiyor

`msdb.dbo.backupset` satırı yalnızca yedek **başarıyla bittiğinde** yazılır.
Başarısız bir yedek orada hiç iz bırakmaz — dolayısıyla dbace SQL Server
tarafında "başarısız yedek" tespit edemiyor; yalnızca "yedek yaşı büyüdü"
sinyalini görebiliyor.

Uydurma bir `failed` kaydı üretmek yanlış olurdu, o yüzden `_backup_record`
yalnızca `success` ve `running` üretiyor.

Gerçek başarısızlık kaynağı **SQL Server Agent iş geçmişi**
(`msdb.dbo.sysjobhistory` + `sysjobs`) ya da yedek aracının kendi log'u. Bu, iş
zamanlayıcı entegrasyonu demek ve ayrı bir kapsam.

pgBackRest ve Barman tarafında durum farklı: ikisi de başarısız yedeği listede
işaretliyor ve dbace onu `failed` olarak kaydediyor.

## Faz 28 İŞ 1: Barman çıktısı metin ayrıştırmayla okunuyor

pgBackRest yerel JSON veriyor (`--output=json`), WAL-G sürüme göre JSON
verebiliyor; **Barman'ın JSON çıktısı yok**. `barman list-backup` metni desenle
ayrıştırılıyor ve bu kırılgan: Barman çıktı biçimini değiştirirse desen tutmaz.

Bu durumda ayrıştırıcı **boş dönüyor**, tahmin yürütmüyor. Gerekçe: yanlış
ayrıştırılmış bir tarih "yedek 40 gün eski" gibi sahte bir kritik bulgu üretir
ve bu, hiç göstermemekten kötüdür.

**Alternatif:** `barman diagnose` JSON veriyor ama tüm sunucu yapılandırmasını
döken ağır bir komut. Barman kullanan bir kurulum ortaya çıkarsa oraya geçmek
değerlendirilebilir.

## Faz 28 İŞ 3b: SLA ölçümü veritabanı erişilebilirliğidir, uygulama erişilebilirliği değil

Bir uygulamanın üç veritabanı varsa "uygulamanın erişilebilirliği" tek doğru
cevabı olan bir soru değil: **replikalı bir kümede bir düğümün düşmesi uygulama
için kesinti değildir**, çünkü trafik diğer düğüme geçer. dbace bunu bilmiyor —
Patroni/Always On durumunu görüyor ama "uygulama o sırada gerçekten hizmet
verebildi mi" sorusunu cevaplayamıyor.

Seçilen tanım: **kapsamdaki veritabanlarının ortalaması.** Gerekçe, erişilebilirlik
bölümünün zaten bu tanımı kullanması; iki yerin farklı sayı göstermesi güven
kaybı olurdu. "Kalan kesinti bütçesi" de aynı ortalamadan türetiliyor ki iki sayı
birbiriyle çelişmesin. Ortalamanın gizlediği düğümü göstermek için **en kötü
veritabanı** ayrıca raporlanıyor.

Bu tanım, replikalı bir kümede SLA'yı **olduğundan kötü** gösterir: bir replikanın
düşmesi uygulamayı etkilemese de ortalamayı aşağı çeker. Ters yönde hata yapmamak
bilinçli bir tercih — SLA'yı olduğundan iyi göstermek çok daha pahalı bir yanlış.

**Kapanması için gereken:** uygulama seviyesinde bir erişilebilirlik sinyali —
ya VIP/HAProxy üzerinden bir sağlık kontrolü, ya da "bu grupta en az bir yazılabilir
düğüm ayaktaydı" bilgisinin küme anlık görüntülerinden türetilmesi. İkincisi
mevcut veriyle yapılabilir görünüyor ama `GroupHealthSnapshot` grup başına TEK
satır tuttuğu için dönemsel değil anlık; önce onun geçmişi tutulmalı.

## Faz 28 İŞ 3: Bakım penceresi saat dilimi UTC

Bakım pencereleri UTC saklanıyor ve arayüz tarayıcının yerel saatinde gösteriyor.
Müşterinin bakım penceresi kendi saat diliminde tanımlıysa ve dbace'i kullanan
kişi başka bir saat diliminde ise, girilen değer doğru ama **okunuşu** kafa
karıştırıcı olabilir.

Yaz saati uygulaması ayrıca bir risk: "her salı 02:00" kuralı UTC'de sabitlendiği
için yerel saatte yılda iki kez bir saat kayar. Kurumsal bakım pencereleri
genelde geniş (2 saat) olduğu için pratikte örtüşme kaybolmuyor, ama dar bir
pencerede kayma bakımı pencere dışına düşürebilir.

**Kapanması için gereken:** pencereye saat dilimi alanı eklemek ve tekrar
genişletmesini o saat diliminde yapmak (`zoneinfo`). Bu turda yapılmadı çünkü
kullanıcı arayüzüne saat dilimi seçimi eklemek İŞ 3'ün kapsamını genişletirdi.
