# auto_explain — gerçek çalıştırmanın planını yakalamak

## Neden gerekli

dbace bir sorgunun planını iki yoldan gösterebiliyor ve **ikisi aynı şey değil**:

| Kaynak | Ne veriyor | Sınırı |
|---|---|---|
| **auto_explain** | Sorgunun yavaş çalıştığı andaki GERÇEK planı | Sunucu log'una erişim gerekir |
| Sonradan `EXPLAIN` | Planlayıcının ŞU AN seçeceği plan | Yavaşlık anındaki plan olmayabilir |

İkincisinin neden yetersiz olduğu:

- **Parametre bilinmiyor.** `pg_stat_statements` sorguyu normalleştirir (`WHERE id = $1`).
  Sonradan EXPLAIN alırken dbace `$1` yerine `NULL` koyar. Planlayıcı `id = 5` için index
  scan, `id = NULL` için bambaşka bir plan seçebilir — üstelik asıl sorun genelde tam da
  budur: *bazı* parametre değerleri için plan çöküyordur.
- **Veri değişmiş olabilir.** Tablo büyümüş, `ANALYZE` çalışmış, index eklenmiş olabilir.
- **Sunucu durumu farklı.** Sorgu yavaş çalıştığında sunucu yük altındaydı; şimdi değil.

Bu yüzden dbace her planın kaynağını ekranda yazıyor. "Gerçek çalıştırmadan yakalandı" ile
"sonradan EXPLAIN ile alındı" farklı güvenilirlikte iki şeydir.

## Kurulum (kendi sunucunuz / on-prem)

`auto_explain` PostgreSQL ile birlikte gelir, ayrıca kurulum gerektirmez — yalnızca
yüklenmesi ve ayarlanması gerekir.

### 1. Kütüphaneyi yükleyin (yeniden başlatma gerektirir)

```conf
# postgresql.conf
shared_preload_libraries = 'pg_stat_statements,auto_explain'
```

Patroni kullanıyorsanız:

```bash
patronictl edit-config -p postgresql.parameters.shared_preload_libraries='pg_stat_statements,auto_explain'
# ardından rolling restart
```

> **Dikkat:** `shared_preload_libraries` değişikliği PostgreSQL'in yeniden başlatılmasını
> gerektirir. Mevcut değeri **okuyup üzerine ekleyin** — üzerine yazarsanız
> `pg_stat_statements` gibi hâlihazırda yüklü kütüphaneler düşer ve dbace'in yavaş sorgu
> listesi tamamen boşalır.

### 2. Ayarları yapın (yeniden başlatma gerektirmez)

```sql
-- Eşik: bu süreyi aşan sorguların planı log'a yazılır.
ALTER SYSTEM SET auto_explain.log_min_duration = '1s';

-- dbace yalnızca JSON biçimini ayrıştırabiliyor.
ALTER SYSTEM SET auto_explain.log_format = 'json';

-- Gerçek satır sayıları ve düğüm süreleri. Bunlar olmadan tahmini/gerçek sapma
-- analizi yapılamaz.
ALTER SYSTEM SET auto_explain.log_analyze = on;
ALTER SYSTEM SET auto_explain.log_buffers = on;

-- Fonksiyon içinden çağrılan sorgular da yakalansın (isteğe bağlı, maliyeti var).
-- ALTER SYSTEM SET auto_explain.log_nested_statements = on;

SELECT pg_reload_conf();
```

### 3. Eşik seçimi — maliyet buradadır

| Eşik | Sonuç |
|---|---|
| `-1` | Kapalı. auto_explain yüklü ama **hiçbir şey yapmıyor** |
| `0` | **HER** sorgunun planı yazılır. Log diskini doldurur, her sorguya plan üretme maliyeti biner. Canlıda kullanmayın |
| `1s` | Makul başlangıç: yalnızca gerçekten yavaş olanlar |

`log_analyze = on` ölçüm maliyeti ekler (her düğüm için zaman ölçümü). PostgreSQL bunu
yalnızca eşiği aşan sorgular için değil, **her sorgu için** yapmak zorundadır — çünkü sorgu
bitmeden ne kadar süreceği bilinemez. Yavaş sistem saatine sahip sunucularda bu fark edilir
bir yük getirebilir; ölçmek için:

```sql
-- Sunucunuzda zaman ölçümünün maliyeti (PostgreSQL ile gelen araç):
-- pg_test_timing
```

`log_timing = off` ile düğüm süreleri olmadan yalnızca satır sayıları toplanabilir; bu, ölçüm
maliyetini büyük ölçüde kaldırır ve tahmini/gerçek sapma analizi için yeterlidir.

### 4. dbace tarafında

Planlar sunucu log'undan okunuyor ve log'a erişim **host-agent** üzerinden sağlanıyor.
Instance'ın `agent_url` seçeneği tanımlı olmalı. Agent yoksa dbace planı toplayamaz ve DPA'da
bunun sebebini yazar.

Ön koşullar panelinde dört ayrı kontrol var (`auto_explain`, `auto_explain_threshold`,
`auto_explain_format`, `auto_explain_analyze`) — çünkü "kütüphane yüklü" tek başına hiçbir şey
söylemiyor: eşik kapalıysa hiç plan yazılmaz, `log_format` metinse dbace ayrıştıramaz,
`log_analyze` kapalıysa plan gerçektir ama gerçek satır sayısı yoktur.

## Yönetilen servisler (Supabase, RDS, Azure Database, Cloud SQL)

Bu ortamlarda **sunucu log dosyasına erişim yoktur**, dolayısıyla dbace auto_explain planlarını
toplayamaz. İki seçenek var:

### Seçenek 1 — auto_explain'i yine açın, planları sağlayıcının arayüzünden okuyun

Çoğu yönetilen servis `auto_explain` parametrelerini destekler (RDS parameter group, Supabase
`ALTER SYSTEM`, Azure server parameters). Planlar sağlayıcının kendi log görüntüleyicisinde
görünür; dbace'e akmaz ama yine de elinizde olur.

### Seçenek 2 — planı sonradan EXPLAIN ile alın (SECURITY DEFINER fonksiyon)

dbace'in izleme kullanıcısı bilinçli olarak salt-okunurdur. Bu kullanıcının `EXPLAIN`
çalıştırabilmesi ama başka bir şey yapamaması için, sahibi ayrıcalıklı olan bir
`SECURITY DEFINER` fonksiyon kullanılır.

> **Bu bir yetki yükseltmesidir ve dikkatle kurulmalıdır.** Aşağıdaki fonksiyon yalnızca
> `EXPLAIN` çalıştırır ve `search_path`'i sabitler; `search_path` sabitlenmezse çağıran
> kullanıcı kendi şemasındaki sahte bir fonksiyonu araya sokabilir.

```sql
-- 1) Fonksiyon ayrıcalıklı bir rolün sahipliğinde oluşturulur.
CREATE OR REPLACE FUNCTION public.dbace_explain(p_query text)
RETURNS json
LANGUAGE plpgsql
SECURITY DEFINER
-- search_path SABİTLENİYOR: SECURITY DEFINER bir fonksiyonda bu satır olmadan,
-- çağıran kullanıcı kendi şemasını öne alıp fonksiyonun kullandığı nesneleri
-- değiştirebilir (klasik ayrıcalık yükseltme yolu).
SET search_path = pg_catalog, public
AS $$
DECLARE
    v_result json;
BEGIN
    -- Yalnızca SELECT/WITH kabul ediliyor. Bu kontrol fonksiyonun İÇİNDE olmak zorunda:
    -- dışarıda yapılan kontrol, fonksiyonu doğrudan çağıran biri için geçersizdir.
    IF p_query !~* '^\s*(select|with)\s' THEN
        RAISE EXCEPTION 'Yalnızca SELECT/WITH sorguları için EXPLAIN alınabilir';
    END IF;
    IF p_query ~ ';' THEN
        RAISE EXCEPTION 'Çoklu statement kabul edilmiyor';
    END IF;

    -- ANALYZE YOK: sorguyu gerçekten çalıştırmak, salt-okunur bir kullanıcıya beklenmedik
    -- maliyet (ve yan etkili fonksiyonlar üzerinden yazma) kapısı açardı.
    EXECUTE 'EXPLAIN (FORMAT JSON) ' || p_query INTO v_result;
    RETURN v_result;
END;
$$;

-- 2) Varsayılan olarak herkese açık olmasın.
REVOKE ALL ON FUNCTION public.dbace_explain(text) FROM PUBLIC;

-- 3) Yalnızca dbace'in izleme kullanıcısına verilsin.
GRANT EXECUTE ON FUNCTION public.dbace_explain(text) TO dbace_monitor;
```

Doğrulama:

```sql
-- dbace_monitor olarak bağlanıp:
SELECT public.dbace_explain('SELECT 1');
-- Beklenen: JSON plan döner.

SELECT public.dbace_explain('DELETE FROM orders');
-- Beklenen: "Yalnızca SELECT/WITH sorguları için EXPLAIN alınabilir" hatası.
```

Geri alma:

```sql
DROP FUNCTION IF EXISTS public.dbace_explain(text);
```

> **Not:** dbace bugün bu fonksiyonu otomatik olarak KULLANMIYOR; izleme kullanıcısı yeterli
> yetkiye sahipse EXPLAIN'i doğrudan çalıştırıyor. Yukarıdaki fonksiyon, izleme kullanıcısını
> daha da kısıtlamak isteyen kurulumlar için belgelenmiştir. Kullanmak istiyorsanız
> `SORULAR.md`'deki ilgili maddeye bakın.

## Saklama

Yakalanan planlar `captured_plans` tablosunda tutuluyor ve **saklama politikasına dahil**
(`Yönetim → Saklama süresi`). Plan JSON'u satır başına kilobaytlar tuttuğu için bu tablo,
politikanın dışında bırakılsa en hızlı büyüyen tablo olurdu.
