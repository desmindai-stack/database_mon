# Index önerisi onarımı ve plan kaynağı önceliklendirmesi

**Tarih:** 2026-09-16
**Kapsam:** İŞ 1 (index önerisi üretilemiyor — üç sebep), İŞ 2 (gerçek parametre
değerleriyle plan analizi)
**Commit planı:** üç commit — (1) mekanik `/* dbace */` marker + `application_name`,
(2) İŞ 1, (3) İŞ 2.

---

## Neden bu iş var

Index önerisi canlıda üç ayrı sebeple hiç üretilemiyor ve üçü de farklı katmanda:

1. Sistem kataloğu sorguları analize giriyor ve `'public.pg_class' tablosu bu
   veritabanında bulunamadı` hatası üretiyor.
2. Gerçek uygulama sorgularında filtre kolonu tespit edilemiyor
   (`WHERE/JOIN/ORDER BY/GROUP BY ile filtrelenen bir kolon tespit edilemedi`).
3. Çağrı eşiğini karşılamayan sorgular kullanıcıyı elle tekrar denemeye zorluyor.

Ayrıca EXPLAIN ANALYZE reddediliyor — gerekçe doğru (normalize metinde gerçek değer yok)
ama dbace'in elinde ZATEN iki gerçek kaynak var ve ikisi de kullanılmıyor.

---

## İŞ 1a — Şema çözümlemesi ve sistem sorgusu filtresi

### Kök neden: `public` varsayımı

`services/sql_analysis.py::analyze_query` içinde:

    schema = table.db or default_schema   # default_schema = "public"

Nitelenmemiş her ad `public` damgası yiyor. `pg_class` `pg_catalog` şemasındadır, yani
`pg_tables` araması kaçınılmaz olarak boş döner ve kullanıcı anlamsız bir hata görür.

### Çözüm

`TableRef` yeni bir alan kazanıyor:

    @dataclass
    class TableRef:
        schema: str
        name: str
        alias: str
        #: Şema sorguda YAZMIYORDU, çıkarım yapıldı. Tablo bulunamazsa mesaj buna göre
        #: değişir: "yok" demek ile "varsaydığım şemada yok" demek farklı şeyler.
        schema_assumed: bool = False

Çözümleme sırası:

1. `table.db` doluysa (`pg_catalog.pg_class`, `information_schema.tables`,
   `sales.orders`) olduğu gibi kullanılır, `schema_assumed=False`.
2. Nitelenmemiş ad bir sistem kataloğuna benziyorsa (`pg_` öneki, ya da bilinen bir
   `information_schema` görünüm adı) şema `pg_catalog` / `information_schema` olur.
   Bu, `search_path`'in gerçekte yaptığı şeydir — `pg_catalog` her zaman örtük olarak
   en öndedir.
3. Aksi hâlde `default_schema`, `schema_assumed=True`.

### Varsayılan şemada bulunamayan tablo

`index_advisor._build_advice` içinde tablo bulunamadığında ve `schema_assumed` ise TEK bir
katalog sorgusu çalışır:

    SELECT n.nspname
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = $1 AND c.relkind IN ('r', 'p', 'm')

* Tek eşleşme → o şema kullanılır, öneri üretilir.
* Birden çok eşleşme → hangi şemalarda olduğu söylenir, tahmin YÜRÜTÜLMEZ
  (yanlış şemaya index önermek canlıda gereksiz index demek).
* Sıfır eşleşme → bugünkü mesaj.

### Sistem sorgusu filtresi

`services/slow_query_selection.py::classify_system_query` Faz 18'den beri var ve index
önerisinden **hiç çağrılmıyor**. Üç değişiklik:

1. **Bağlama.** `advise()` en başta çağırır; sistem sorgusu ise
   `NoAdviceReason(code="system_query", ...)` ile erken döner. Mesaj sorunun ne olduğunu
   söyler: bu bir veritabanı iç sorgusudur, DBA'nın optimize edebileceği bir şey değildir.

2. **Desen listesi genişletiliyor.** Eksik olan çıplak katalog adları ekleniyor:
   `pg_class`, `pg_namespace`, `pg_attribute`, `pg_index`, `pg_indexes`, `pg_tables`,
   `pg_proc`, `pg_type`, `pg_locks`, `pg_roles`, `pg_constraint`.
   Bu, yavaş sorgu listesini de etkiler — ve bu DOĞRU olan: CLAUDE.md'nin "aynı veriyi
   gösteren yerler tek gerçeklik kaynağından beslensin" kuralı gereği iki yerde iki ayrı
   sistem sorgusu tanımı olamaz.

3. **Yapısal denetim.** Index danışmanında sqlglot ağacı ZATEN ayrıştırılmış durumda.
   Sorgudaki gerçek tabloların HEPSİ `pg_catalog`/`information_schema` şemasındaysa bu bir
   sistem sorgusudur. Desenden daha doğru ve ek maliyeti yok (ağaç elimizde).

   Desen tabanlı denetim yine de kalıyor: listeleme yolunda (`select_slow_queries`)
   ayrıştırma yapılmıyor ve sorgu başına sqlglot çalıştırmak orada pahalı olurdu.

### dbace'in kendi toplama sorguları

`collectors/postgresql.py` içindeki sorgu metinleri `/* dbace */` ile öneklenir ve
`_SYSTEM_PATTERNS`'e karşılık gelen desen eklenir. Yaklaşımın bu projede çalıştığı zaten
kanıtlı: mevcut `-- ext:` deseni aynı mekanizmaya dayanıyor (pg_stat_statements sorgu
metnindeki yorumları korur).

Bağlantılara ayrıca `application_name=dbace` veriliyor. pg_stat_statements
`application_name` tutmaz, yani bu tek başına filtre değil; ama `pg_stat_activity`'de
görünür ve DBA'nın "bu bağlantı kimin" sorusunu cevaplar.

**Commit ayrımı:** marker'ın sorgu metinlerine eklenmesi ve `application_name` tamamen
mekanik bir diff olduğu için AYRI ve İLK commit'te. Marker'ı tüketen desen ikinci
commit'te (İŞ 1) — böylece mekanik değişiklik mantık değişikliğinden ayrı okunabilir.
Birinci commit tek başına davranış değiştirmez.

---

## İŞ 1b — Filtre kolonu tespiti

### Ölçüm: `$1` yer tutucuları suçlu DEĞİL

Görev tanımı yer tutucuların predicate çıkarımını bozup bozmadığını sormuştu. Kontrol
edildi, bozmuyor:

* Mevcut regex `([a-zA-Z_][a-zA-Z0-9_$]*(?:\.…)?)\s*(=|<|>|…)` ifadesi
  `WHERE a.status = $1` metninde `a.status` + `=` eşleşmesini sorunsuz buluyor.
* sqlglot `$1` yer tutucularını doğru ayrıştırıyor (`sql_analysis.py` modül açıklamasında
  yazılı ve doğrulanmış).

Gerçek sebepler başka ve daha ağır:

| Kalıp | Bugün | Neden |
|---|---|---|
| `lower(email) = $1` | **görünmez** | regex karakter sınıfında `(` yok, hiçbir eşleşme olmuyor |
| `col::text = $1` | **görünmez** | `:` operatör listesinde yok |
| `date_trunc('day', c) = $1` | **görünmez** | ifade durumunun aynısı |
| nitelenmemiş kolon, 2+ tablo | **düşüyor** | `__unknown`'a gidiyor, yalnızca TEK tablo varsa kurtarılıyor |
| CTE içindeki filtre | **düşüyor** | CTE takma adına yazılıyor, sonra `is_not_a_table()` siliyor |
| `ON a.x=b.y AND a.z=$1` | **eksik** | `ON` regex'i yalnızca ilk eşitliği alıyor |
| `EXISTS (SELECT … WHERE b.a_id=a.id)` | **görünmez** | alt sorgu işlenmiyor |

Nitelenmemiş kolon durumu muhtemelen en sık görülen: iki tablolu HER join, WHERE
kolonları niteliksizse bugün sıfır aday üretiyor.

### Çözüm: regex yerine AST

`_extract_candidates` siliniyor. Predicate çıkarımı `sql_analysis.py`'ye taşınıyor —
ayrıştırma oraya ait, `index_advisor.py` öneri üretmeye odaklanıyor.

    @dataclass
    class ColumnPredicate:
        #: Takma ad ya da tablo adı. None = nitelenmemiş (kolon sahibi katalogdan çözülecek).
        table_ref: str | None
        column: str
        #: eq | range | join | in | like_prefix | like_unanchored | sort | group | is_null
        kind: str
        #: Kolon bir ifadeyle sarılıysa ifadenin metni ("lower(email)"), değilse None.
        expression: str | None
        #: where | join_on | cte | subquery | exists | having | order_by | group_by
        source: str
        usable: bool
        unusable_reason: str | None

`QueryAnalysis` yeni alan kazanıyor: `predicates: list[ColumnPredicate]`.

Kapsam kuralları:

* **Kapsam farkındalığı**: CTE ve alt sorgu içindeki predicate, o kapsamdaki GERÇEK
  tabloya bağlanır; CTE adına değil. Bugünkü kaybın kaynağı tam olarak bu.
* **JOIN ON**: koşul ağacındaki tüm eşitlikler; `AND` ile bağlı filtreler de dahil.
* **IN / EXISTS**: `col IN ($1,$2)` eşitlik gibi. Bağıntılı `EXISTS` içindeki
  `b.a_id = a.id` iki taraflı join predicate'i üretir.
* **Aralık**: `BETWEEN`, `>`, `<`, `>=`, `<=`. Aynı kolon üzerindeki `> $1 AND < $2`
  tek bir aralık kolonuna indirgenir.

### Nitelenmemiş kolonların çözülmesi

Zaten açık bir bağlantı var. Nitelenmemiş kolonlar için tek bir katalog sorgusu
(`pg_attribute` üzerinden, sorgudaki tablolarla sınırlı):

* Tek tablo o kolona sahipse → ona bağlanır.
* Birden çok tablo sahipse → bağlanmaz, `unusable_reason` ile raporlanır
  ("`status` kolonu hem `orders` hem `invoices` tablosunda var; sorguda nitelenmemiş").
  Tahmin yürütmek, yanlış tabloya index önermek demek olurdu.

### İfade index'i ARTIK ÖNERİLİYOR

Bugün yalnızca "böyle olabilir" deniyor. Artık DDL üretiliyor:

    CREATE INDEX idx_dbace_users_lower_email ON public.users ((lower(email)));

**Kanıt şartı — değişmezlik (immutability).** PostgreSQL yalnızca IMMUTABLE ifadeleri
indeksler. Öneri üretmeden önce katalogdan (`pg_proc.provolatile`) doğrulanıyor:

* `lower(text)` → `i` (immutable) → **önerilir**.
* `date_trunc(text, timestamptz)` → `s` (stable) → **önerilmez**; sebep yazılır:
  bu fonksiyon saat dilimi ayarına bağlı olduğu için index'lenemez, `timestamp`
  sürümüne cast ederek ya da üretilmiş kolonla çözülebilir.
* Cast'ler aynı denetimden geçer: `col::text` indekslenebilir, `timestamptz::date`
  indekslenemez.

Çalıştırılamayan bir DDL önermek, hiç önermemekten kötüdür — kullanıcı onu canlıda
deneyip hata alır.

### LIKE

* `LIKE 'abc%'` (önek) → btree kullanılabilir. Veritabanı collation'ı `C` değilse
  `text_pattern_ops` gerektiği notu eklenir, yoksa index kullanılmaz.
* `LIKE '%abc'` / `'%abc%'` → btree işe yaramaz; `pg_trgm` GIN index önerilir.
  `pg_trgm` kurulu değilse DDL üretilmez, kurulum adımı sebep olarak yazılır.
* `LIKE $1` (normalize metin) → kalıp bilinmiyor. İki ihtimal de açıklanır, DDL
  üretilmez.

### Kullanıcıya görünürlük

`IndexAdviceReportOut` yeni alan kazanıyor:

    predicates: list[PredicateOut]   # column, kind, source, usable, unusable_reason

Arayüzde "Bulunan filtreler" başlığı altında liste. Böylece "öneri yok" sonucu
denetlenebilir hâle geliyor: hangi filtreler görüldü, hangisi neden index'e
dönüştürülemedi. Hiç predicate bulunmadıysa bu da açıkça söyleniyor — "ölçüm yok" ile
"sorun yok" farklı şeyler.

---

## İŞ 1c — Çağrı eşiği ve izleme listesi

### Model

    class IndexAdviceWatch(Base):
        __tablename__ = "index_advice_watches"
        __table_args__ = (
            UniqueConstraint("instance_id", "query_fingerprint", name="uq_index_advice_watch"),
        )
        id, instance_id, queryid (nullable), query_fingerprint, query_text
        threshold: int
        calls_at_registration: int
        calls_seen: int
        status: str            # waiting | ready | unavailable
        advice_json: JSON | None
        reasons_json: JSON | None
        registered_at, last_checked_at, ready_at

`query_fingerprint` mevcut `plan_capture.fingerprint()` ile aynı normalleştirmeyi
kullanır — iki ayrı parmak izi tanımı iki ayrı eşleşme davranışı demek olurdu.

### Akış

1. `advise()` eşik nedeniyle öneri üretemezse uç nokta bir izleme kaydı açar
   (varsa günceller).
2. Dönen sebep sayıları taşır: `calls_now`, `threshold`. Arayüz **"Şu anda 2/5 çağrı —
   izlemeye alındı, eşik dolunca öneri burada görünecek."** der. Tekrar dene butonu yok.
3. `collectors/scheduler.py` içine `index_advice_watch_tick` ekleniyor (mevcut
   `plan_capture_tick` ile aynı desen). `waiting` kayıtlar için `SlowQuerySample`'dan
   güncel çağrı sayısı okunur; eşik dolduysa danışman çalıştırılır, sonuç kaydedilir,
   `status=ready` olur.
4. `GET /instances/{id}/advice-watches` izlenen sorguları listeler — kaydı açan satıra
   geri dönmek zorunda kalmadan tek yerden görülebilsin diye.

Bildirim bilinçli olarak `AlertEvent`'e BAĞLANMIYOR: "öneri hazırlandı" operasyonel bir
olay değil, alarm geçmişini operasyonel olmayan kayıtlarla kirletirdi.

### Ayarlar

Yeni modül `services/analysis_settings.py` (`noise_settings.py` ile aynı şekil,
`AppSetting` tabanlı):

| Anahtar | Varsayılan | Ne yapar |
|---|---|---|
| `index_advice_min_calls` | 5 | Öneri için gereken asgari çağrı sayısı |
| `index_advice_watch_enabled` | true | İzleme listesi ve arka plan turu |
| `store_real_query_samples` | **false** | İŞ 2 — gerçek değerli örnek saklama |

Uç nokta: `GET/PUT /api/admin/analysis-settings`. Üç anahtar için üç ayrı modül
açmaktansa tek tutarlı modül; üçü de "dbace ne kadar derin analiz eder ve ne saklar"
sorusunu cevaplıyor.

`index_advisor.MIN_SAMPLE_CALLS` sabiti kalkıyor, değer ayardan okunuyor.

---

## İŞ 2 — Plan kaynağı önceliklendirmesi

### Zaten var olan iki kaynak

* **auto_explain planları**: `CapturedPlan` tablosu, `plan_capture.py` topluyor,
  `GET /captured-plans/{id}` gösteriyor. Eksik olan tek şey: EXPLAIN istenirken bu
  kaynağın ÖNCE aranması.
* **Gerçek değerli sorgu metni**: `WaitQuerySignature.query_text` bekleme örnekleyicisinden
  geliyor ve `pg_stat_activity`'den okunduğu için **normalize değil, gerçek değerli**.

### Gizlilik: mevcut ve sessiz bir maruziyet

`WaitQuerySignature.query_text` bugün zaten gerçek değerli metin saklıyor ve veritabanı
yükü kırılımında gösteriliyor. Yani bu yeni bir maruziyet değil, var olan ve
belgelenmemiş bir maruziyet. Tek anahtar hepsini yönetiyor:

`store_real_query_samples` — **varsayılan kapalı**.

| Anahtar | `query_text` | `sample_*` kolonları |
|---|---|---|
| kapalı (varsayılan) | **normalize edilerek** yazılır | hiç yazılmaz |
| açık | gerçek değerli yazılır | yazılır |

Anahtar kapatıldığında mevcut `sample_*` değerleri **NULL'lanır**. Aksi hâlde "kapattım"
kullanıcının sandığı şeyi ifade etmezdi.

Rapor ve yönetici görünümleri `sample_query_text` alanını hiçbir koşulda okumaz; bu bir
testle sabitleniyor (CLAUDE.md: yönetici raporunda sorgu metni asla görünmez).

Karar ve gerekçesi `SORULAR.md`'ye yazılıyor.

### Şema değişikliği

`wait_query_signatures` tablosuna üç nullable kolon:

* `sample_query_text: Text | None` — temsili gerçek değerli örnek
* `sample_duration_ms: Float | None` — o örneğin ölçülmüş süresi
* `sample_captured_at: DateTime | None`

### Örnekleyici değişikliği

`collectors/postgresql.py::sample_active_sessions`:

* `now() - a.query_start` farkı milisaniye olarak eklenir — **en yavaş** çalıştırmanın
  tercih edilebilmesi için. Saklanan örnek, o queryid için görülmüş en uzun süreli
  çalıştırmadır.
* Metin sınırı 400 → 4000 karakter. 400 karakter sözlük gösterimi için yeterliydi,
  EXPLAIN girdisi için değil.
* `detect_truncation()` örnek kabul edilmeden önce çalışır. Sunucunun
  `track_activity_query_size` sınırında kesilmiş bir metin gösterim için saklanır ama
  **EXPLAIN girdisi olarak reddedilir** ve sebebi söylenir. Kesik SQL'e EXPLAIN
  çalıştırmak, sorguyla ilgisiz bir sözdizimi hatası üretir — bu hata bu projede zaten
  bir kez yaşandı.

### `services/plan_source.py`

    @dataclass
    class PlanSourceOption:
        kind: str              # captured | sample_analyze | generic | unavailable
        available: bool
        label: str             # kullanıcıya görünen Türkçe ad
        caveat: str | None     # güvenilirlik sınırı
        reason: str | None     # kullanılamıyorsa NEDEN
        detail: dict           # plan_id, sample_captured_at, sample_duration_ms, …

    async def resolve_plan_sources(session, instance, query, queryid) -> list[PlanSourceOption]

**Dört seçenek de her zaman döner**, öncelik sırasında, `available` ve `reason` ile.
Kullanılamayan bir kaynağı listeden gizlemek, kullanıcıya neyin eksik olduğunu
söylememek olurdu.

| Öncelik | Kaynak | Koşul | Etiket |
|---|---|---|---|
| 1 | `captured` | Bu queryid/fingerprint için `CapturedPlan` var | "Gerçek çalıştırmadan yakalandı (auto_explain)" |
| 2 | `sample_analyze` | Gerçek değerli örnek var, kesik değil, anahtar açık | "Gerçek değerlerle EXPLAIN ANALYZE" (onay + maliyet uyarısı) |
| 3 | `generic` | Sunucu PG 12+ | "Değerden bağımsız plan (ANALYZE'siz)" |
| 4 | `unavailable` | — | Neden hiçbirinin olmadığı |

Öncelik 1 varsa EXPLAIN hiç çalıştırılmaz — görev tanımının açık isteği.

Etiket ve uyarı metinleri mevcut `auto_explain.plan_source_label()` /
`plan_source_caveat()` fonksiyonlarından gelir; `sample_analyze` için yeni bir dal
eklenir. İkinci bir etiket kaynağı açmak, aynı planın iki yerde farklı adlandırılması
demek olurdu.

### Uçlar

* `GET /instances/{id}/plan-sources` — sıralı seçenek listesi.
* `POST /instances/{id}/explain` gövdesine `use_sample: bool = False` eklenir. True ise
  sunucu saklanan gerçek değerli örneği kullanır ve `ANALYZE, BUFFERS, FORMAT JSON`
  çalıştırır; sonuç `source="sample_analyze"` ile döner.

**Yetki**: ek bir mekanizma gerekmiyor. `require_write_access` zaten her POST'u admin'e
kapatıyor (viewer 403 alır), yani EXPLAIN ANALYZE hâlihazırda admin'e kısıtlı. Arayüz de
viewer'a teklifi göstermez. Bu, var olan kuralı tekrar etmek yerine ona dayanıyor.

Onay ve maliyet uyarısı arayüzde, POST'tan önce: örneğin ölçülmüş süresi gösterilir
("bu örnek çalıştırma 4.2 sn sürmüştü — sorgu izlenen sunucuda yeniden çalıştırılacak").

---

## Test stratejisi

Görev tanımı açık: sahte bağlantı testleri bu özellik sınıfında üç tur boyunca yanıltıcı
oldu. Doğrulama **gerçek PostgreSQL'e** karşı, mevcut `DBACE_TEST_PG_DSN` düzeneği
(`tests/test_explain_live_postgres.py`) genişletilerek yapılır. Yerelde `dbace-pg17`.

Gerçek tablolar, gerçek satırlar, gerçek `ANALYZE`:

**İŞ 1**
* 1b tablosundaki her kalıp için bir test: ya index üretilir ya da ADI KONMUŞ bir sebep
  döner. "Bir şey döndü" yeterli sayılmaz.
* Önerilen ifade index DDL'i gerçekten **sunucuda çalıştırılır** ve ardından planlayıcının
  onu kullandığı gösterilir. Sahteye karşı değil, gerçeğe karşı.
* `date_trunc` gibi STABLE bir ifade için DDL üretilmediği ve sebebin yazıldığı.
* `pg_class` / `information_schema` sorguları `system_query` sebebiyle döner ve katalog
  aramasına HİÇ girmez.
* Nitelenmemiş kolonun katalogdan doğru tabloya çözüldüğü; iki tabloda da varsa
  çözülmeyip raporlandığı.

**İŞ 2**
* Yakalanmış plan varken öncelik 1 kazanır, EXPLAIN çalıştırılmaz.
* Gerçek değerli örnekle `EXPLAIN ANALYZE` çalışır ve planda `Actual Rows` bulunur.
* Anahtar kapalıyken öncelik 2 `available=False` ve doğru sebeple döner.
* Anahtar kapatıldığında mevcut `sample_*` değerlerinin NULL'landığı.
* Kesik örnek metnin EXPLAIN girdisi olarak reddedildiği.
* Rapor/yönetici payload'ında gerçek değerli örnek metnin hiç görünmediği.

**Statik denetimler (CLAUDE.md gereği)**
* `test_definition_order.py` yeni şema tiplerini kapsar (yerel 3.14 / canlı 3.12 farkı).
* Arayüz kuralları için backend'deki statik testlere ekleme
  (`test_navigation_integrity.py`, `test_ui_terminology.py`).
* `python -c "from app.main import app"` ve `npm run build` yeşil.

---

## Migration ve dokümantasyon

`supabase/migrations/` altına eklenecek ve DEPLOY.md tablosuna işlenecek:

1. `wait_query_signatures` → `sample_query_text`, `sample_duration_ms`,
   `sample_captured_at` kolonları.
2. `index_advice_watches` tablosu.

`AppSetting` satırları veri olduğu için migration gerektirmiyor.

`SORULAR.md`: gerçek değerli örnek saklamanın gizlilik gerekçesi ve açık varsayımları.
`ILERLEME.md`: her iki işin kararları ve gerekçeleri.

---

## Bilinçli olarak YAPILMAYANLAR

* **Sistem sorgusu tespiti hâlâ kusursuz değil.** Yapısal denetim yalnızca index
  danışmanında çalışıyor (ağaç orada zaten var); listeleme yolu desen tabanlı kalıyor,
  çünkü sorgu başına sqlglot çalıştırmak listeleme için pahalı. Bu sınır zaten
  CLAUDE.md'de yazılı.
* **İzleme listesi alarm üretmiyor** (yukarıda gerekçeli).
* **`sample_analyze` yalnızca PostgreSQL.** SQL Server'da karşılığı ayrı bir iş.
* **Örnek başına tek metin saklanıyor**, geçmiş tutulmuyor. Farklı parametrelerin farklı
  planlar üretmesi gerçek bir konu ama örnek geçmişi ayrı bir tasarım kararı.
