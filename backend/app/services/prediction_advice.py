"""Tahminler için beş parçalı standart öneri (Faz 20 İŞ 1).

Tahminlerin adım adım planı (`prediction_playbooks.py`) zaten vardı ama `PredictionInsight`'ın
`advice` alanı HİÇ doldurulmuyordu: `PredictionOut.advice` şemada tanımlıydı, modelde karşılığı
yoktu, dolayısıyla API her tahmin için `advice: null` dönüyordu. Arayüz de bu yüzden standart
öneri kartı yerine tek cümlelik `recommendation`'a düşüyordu — kullanıcının gördüğü buydu:
"öneri var ama çalıştırılacak komut yok".

Bu modül her tahmin türünü tam standarda çeviriyor:

    Öneri: <kısa eylem>   → title (eylem, durum tespiti değil)
    Neden                 → why — İŞ ETKİSİYLE: yapılmazsa ne olur
    Adımlar               → steps[].action (numaralı, playbook'tan)
      └ komut             → steps[].command (tam, kopyalanabilir)
    Dikkat                → cautions[] — risk, kilitleme, bakım penceresi
    Tahmini süre          → estimated_duration
    Geri alma             → rollback
    Doğrulama             → verification — "düzeldi mi" sorgusu

Öneri üretilemeyen tahmin türleri için `unavailable()` kullanılıyor: neden üretilemediği
yazılıyor, boş bırakılmıyor.
"""

from __future__ import annotations

from app.services.advice import Advice, advice_from_playbook, unavailable
from app.services.prediction_playbooks import (
    connections_playbook,
    database_size_playbook,
    index_bloat_playbook,
    table_growth_playbook,
    wraparound_playbook,
)

# --- Uzun vadeli kapasite tahminleri -------------------------------------------------------


def database_size_advice(
    *, current_human: str, per_day_human: str, doubling_date: str, horizon_label: str
) -> Advice:
    return advice_from_playbook(
        "Veritabanı büyümesini yavaşlatın ve disk kapasitesini planlayın",
        why=(
            f"Veritabanı şu an {current_human} ve günde ~{per_day_human} büyüyor; bu hızla "
            f"{horizon_label} iki katına çıkar (~{doubling_date}). PostgreSQL disk dolduğunda "
            "yazma işlemlerini TAMAMEN durdurur — veritabanı okunur ama hiçbir işlem kabul "
            "edilmez. Bu, uygulamanın tamamen durması demektir ve disk büyütme çoğu ortamda "
            "planlı bir kesinti gerektirdiği için son anda yapılamaz."
        ),
        playbook=database_size_playbook(
            current_human=current_human, per_day_human=per_day_human, doubling_date=doubling_date
        ),
        cautions=[
            "VACUUM FULL tabloyu ACCESS EXCLUSIVE kilitler — o süre boyunca tablo tamamen "
            "erişilemez. Yalnızca bakım penceresinde çalıştırın; kilitsiz alternatif pg_repack.",
            "VACUUM FULL, tablo boyutu kadar EK boş disk ister. Diskin dolmasına az kalmışken "
            "çalıştırmak durumu kötüleştirebilir — önce boş alanı doğrulayın.",
            "Toplu DELETE ile yer açmaya çalışmak tabloyu daha da şişirir ve WAL üretir; "
            "arşivleme/partition DETACH tercih edilmeli.",
            "dbace gerçek disk kapasitesini ölçmüyor (host-agent OS metriği toplamıyor) — "
            "buradaki tarih veri büyüme trendidir, doluluk tahmini değildir.",
        ],
        estimated_duration=(
            "Ölçüm sorguları saniyeler; rutin VACUUM tablo boyutuna göre dakikalar; "
            "VACUUM FULL / partition'a geçiş büyük tablolarda saatler sürebilir."
        ),
        rollback=(
            "Ölçüm ve VACUUM adımları geri alınamaz bir değişiklik yapmaz (veri kaybı yok). "
            "Arşivlemeden önce arşiv tablosuna kopyalayın: taşıma yanlışsa arşivden geri "
            "INSERT edilir. Partition'a geçişte eski tabloyu DROP etmeden önce yeni yapının "
            "doğruluğunu doğrulayın."
        ),
        verification=(
            "-- Büyüme durdu mu / yer geri alındı mı (birkaç gün sonra tekrar bakın):\n"
            "SELECT pg_size_pretty(pg_database_size(current_database())) AS toplam_boyut;\n"
            "SELECT relname, n_dead_tup, last_autovacuum\n"
            "FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 10;"
        ),
    )


def wraparound_advice(*, current_age: float, freeze_max_age: int, eta_date: str) -> Advice:
    return advice_from_playbook(
        "Transaction ID yaşını düşürün (VACUUM FREEZE)",
        why=(
            f"Transaction ID yaşı {current_age:,.0f} ve artıyor; bu hızla ~{eta_date} civarında "
            f"autovacuum_freeze_max_age ({freeze_max_age:,}) eşiğine ulaşır. O noktada "
            "PostgreSQL, siz istemeseniz de zorunlu bir freeze VACUUM başlatır: yoğun I/O, "
            "sorgu yavaşlaması ve iptal edilemeyen uzun bir bakım. Daha da ilerlerse "
            "(~2.1 milyar) veritabanı yazmaları tamamen reddeder ve tek çıkış tek kullanıcılı "
            "modda kurtarmadır. Şimdi planlı yapılırsa düşük trafikli bir saate alınabilir."
        ),
        playbook=wraparound_playbook(
            current_age=current_age, freeze_max_age=freeze_max_age, eta_date=eta_date
        ),
        cautions=[
            "VACUUM FREEZE tabloyu KİLİTLEMEZ (ACCESS SHARE yeterli) ama yoğun I/O üretir — "
            "büyük tablolarda düşük trafikli bir saatte, tek tek çalıştırın.",
            "Yaş artmaya devam ediyorsa sebep neredeyse her zaman freeze'i ENGELLEYEN bir şeydir: "
            "uzun transaction, 'idle in transaction' oturum, kullanılmayan replication slot ya da "
            "prepared transaction. Bunlar temizlenmeden VACUUM çalıştırmak yaşı düşürmez.",
            "`autovacuum_max_workers` değişikliği YENİDEN BAŞLATMA gerektirir; cost_limit ve "
            "cost_delay reload ile devreye girer.",
            "Bir replication slot'u silmek, ona bağlı replikayı kalıcı olarak bozar — silmeden "
            "önce slot'un gerçekten sahipsiz olduğunu doğrulayın.",
        ],
        estimated_duration=(
            "Teşhis sorguları saniyeler; tek bir büyük tablonun VACUUM FREEZE'i tablo boyutuna "
            "ve disk hızına göre dakikalardan saatlere."
        ),
        rollback=(
            "VACUUM FREEZE'in geri alınması gerekmez ve gerekmemelidir — veriyi değiştirmez, "
            "yalnızca satırları dondurulmuş olarak işaretler. Parametre değişiklikleri "
            "`ALTER SYSTEM RESET <parametre>; SELECT pg_reload_conf();` ile geri alınır."
        ),
        verification=(
            "-- Yaş düştü mü? (VACUUM sonrası birkaç dakika içinde görünür)\n"
            "SELECT datname, age(datfrozenxid) AS yas FROM pg_database ORDER BY yas DESC;\n"
            "-- Hangi tablo hâlâ yaşlı:\n"
            "SELECT n.nspname, c.relname, age(c.relfrozenxid) AS yas\n"
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
            "WHERE c.relkind IN ('r','m') ORDER BY yas DESC LIMIT 10;"
        ),
    )


def table_growth_advice(
    *, schema_name: str, table_name: str, per_day_human: str, horizon_label: str, predicted_human: str
) -> Advice:
    return advice_from_playbook(
        f"{schema_name}.{table_name} tablosunun büyümesini kontrol altına alın",
        why=(
            f"Tablo günde ~{per_day_human} büyüyor; {horizon_label} ~{predicted_human} olur. "
            "Büyüyen bir tablo yalnızca disk tüketmez: index'leri de büyür, sorgu planları "
            "seq scan'e kayar, VACUUM süresi uzar ve yedek alma/geri yükleme süresi artar. "
            "Erken davranılırsa arşivleme kilitsiz yapılabilir; geç kalınırsa tek çare "
            "bakım penceresinde uzun süren bir taşımadır."
        ),
        playbook=table_growth_playbook(schema_name, table_name, per_day_human),
        cautions=[
            "Tek seferde milyonlarca satır silmek hem WAL'i şişirir hem de VACUUM'u engeller — "
            "partiler halinde (10.000'lik) çalıştırın.",
            "Silmeden önce arşiv tablosuna taşıyın; DELETE geri alınamaz.",
            "Partition'a geçiş tablo tanımını değiştirir — uygulamanın yazma yolunu ve "
            "foreign key'leri etkileyebilir, önce test ortamında deneyin.",
            "Ölü satır oranı yüksekse bu büyüme gerçek veri artışı DEĞİL şişmedir; o durumda "
            "arşivleme yanlış çözümdür, VACUUM doğrudur.",
        ],
        estimated_duration=(
            "Ölçüm saniyeler; parti parti arşivleme satır sayısına göre saatlere yayılabilir "
            "(ama her parti kısa sürer, kilit tutmaz)."
        ),
        rollback=(
            "Arşiv tablosuna taşınan satırlar arşivden geri INSERT edilerek döndürülür — bu "
            "yüzden DELETE'ten önce arşive yazmak zorunludur. VACUUM'un geri alınması gerekmez."
        ),
        verification=(
            f"-- Büyüme yavaşladı mı / şişme geri alındı mı:\n"
            f"SELECT pg_size_pretty(pg_total_relation_size('{schema_name}.{table_name}')) AS toplam,\n"
            f"       (SELECT n_dead_tup FROM pg_stat_user_tables\n"
            f"         WHERE schemaname = '{schema_name}' AND relname = '{table_name}') AS olu_satir;"
        ),
    )


def index_bloat_advice(
    *, schema_name: str, index_name: str, per_day_human: str, horizon_label: str, predicted_human: str
) -> Advice:
    return advice_from_playbook(
        f"{schema_name}.{index_name} index'ini yeniden oluşturun veya kaldırın",
        why=(
            f"Index günde ~{per_day_human} büyüyor; {horizon_label} ~{predicted_human} olur. "
            "Şişmiş bir index yalnızca disk tüketmez: her INSERT/UPDATE'te yazma maliyeti "
            "artar, index taraması daha çok sayfa okur ve önbelleğe sığmaz hale gelir. "
            "Hiç kullanılmayan bir index ise saf maliyettir — hiçbir sorguyu hızlandırmadan "
            "her yazmayı yavaşlatır."
        ),
        playbook=index_bloat_playbook(schema_name, index_name, per_day_human),
        cautions=[
            "REINDEX CONCURRENTLY sırasında index'in İKİ kopyası birden diskte durur — yeterli "
            "boş alan olduğundan emin olun.",
            "REINDEX CONCURRENTLY yarıda kalırsa geride 'INVALID' bir index kalır; onu "
            "`DROP INDEX` ile temizlemeden yeniden denemeyin.",
            "DROP etmeden önce `idx_scan = 0` olduğunu doğrulayın — istatistikler son "
            "`pg_stat_reset()` çağrısından beri sayar; yakın zamanda sıfırlandıysa 0 değeri "
            "'kullanılmıyor' anlamına GELMEZ.",
            "UNIQUE/PRIMARY KEY index'leri kısıtlamayı taşır; onları DROP etmek veri "
            "bütünlüğünü bozar.",
        ],
        estimated_duration=(
            "REINDEX CONCURRENTLY index boyutuna göre dakikalardan saate; tabloyu yazmaya "
            "kapatmaz (PostgreSQL 12+). DROP INDEX CONCURRENTLY saniyeler."
        ),
        rollback=(
            "REINDEX geri alınmaz ve alınması gerekmez (aynı index, yeniden kurulmuş hali). "
            "DROP edilen bir index'i geri almak için CREATE INDEX CONCURRENTLY ile aynı "
            "tanımı yeniden kurun — bu yüzden DROP'tan önce tanımı saklayın:\n"
            f"SELECT indexdef FROM pg_indexes\n"
            f"WHERE schemaname = '{schema_name}' AND indexname = '{index_name}';"
        ),
        verification=(
            f"-- Boyut düştü mü, index geçerli mi:\n"
            f"SELECT pg_size_pretty(pg_relation_size('{schema_name}.{index_name}')) AS boyut,\n"
            f"       (SELECT indisvalid FROM pg_index\n"
            f"         WHERE indexrelid = '{schema_name}.{index_name}'::regclass) AS gecerli;"
        ),
    )


# --- Kısa vadeli eşik tahminleri -----------------------------------------------------------


def connection_advice(*, engine: str, current: float, predicted: float, horizon_minutes: int) -> Advice:
    hours = horizon_minutes / 60
    horizon_label = f"{hours:.0f} saat" if hours >= 1 else f"{horizon_minutes} dakika"
    engine_caution = {
        "postgresql": (
            "`max_connections` reload ile devreye GİRMEZ — PostgreSQL yeniden başlatılmalıdır "
            "(kesinti). Ayrıca her bağlantı ayrı bir işlemdir ve work_mem kadar bellek "
            "isteyebilir; artırmadan önce toplam belleği hesaplayın."
        ),
        "sqlserver": (
            "`Max Pool Size` her uygulama ÖRNEĞİ için ayrı geçerlidir — 10 örnek × 100 = 1000 "
            "bağlantı. Sunucu tarafını değiştirmeden önce uygulama tarafını düşürün."
        ),
        "mongodb": (
            "`maxPoolSize` her uygulama örneği için ayrıdır; toplam bağlantı = örnek sayısı × "
            "maxPoolSize. Sürücü ayarı değişikliği uygulamanın yeniden başlatılmasını ister."
        ),
    }.get(engine, "")
    return advice_from_playbook(
        "Bağlantı havuzunu sınırlayın, limiti büyütmeden önce sızıntıyı kesin",
        why=(
            f"Bağlantı kullanımı şu an {current:.0f}, {horizon_label} içinde ~{predicted:.0f} "
            "olması bekleniyor. Bağlantı limiti dolduğunda veritabanı YENİ bağlantıyı reddeder — "
            "uygulama 'too many connections' ile hata verir ve bu genelde tüm istekleri etkiler. "
            "Limitin dolması çoğu zaman gerçek yük artışı değil, kapatılmayan boşta bağlantılardır; "
            "limiti büyütmek sorunu bellek sorununa çevirir."
        ),
        playbook=connections_playbook(engine),
        cautions=[
            c
            for c in [
                engine_caution,
                "'idle in transaction' oturumları yalnızca bağlantı tüketmez, VACUUM'u da "
                "engeller — önce uygulamayı düzeltin, zaman aşımı geçici çözümdür.",
                "Oturum sonlandırmak (pg_terminate_backend) çalışan bir işlemi geri alır; "
                "uygulamanın hata alacağını bilerek yapın.",
            ]
            if c
        ],
        estimated_duration=(
            "Ölçüm ve pooler yapılandırması dakikalar; `max_connections` değişikliği yeniden "
            "başlatma penceresi gerektirir."
        ),
        rollback=(
            "Pooler ayarları yapılandırma dosyasından geri alınır (PgBouncer reload). "
            "`ALTER SYSTEM SET` ile yapılan her değişiklik "
            "`ALTER SYSTEM RESET <parametre>; SELECT pg_reload_conf();` ile geri alınır."
        )
        if engine == "postgresql"
        else "Havuz boyutu değişikliği bağlantı dizesinden/sürücü ayarından geri alınır.",
        verification=(
            "SELECT (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS azami,\n"
            "       count(*) AS toplam,\n"
            "       count(*) FILTER (WHERE state = 'idle in transaction') AS islem_icinde_bosta\n"
            "FROM pg_stat_activity;"
            if engine == "postgresql"
            else None
        ),
    )


def cache_hit_advice(*, current: float, predicted: float) -> Advice:
    return advice_from_playbook(
        "Cache hit oranındaki düşüşün kaynağını bulun",
        why=(
            f"Cache hit oranı {current:.1f}%'ten ~{predicted:.1f}%'e düşüyor. Bu, aynı sorguların "
            "giderek daha çok veriyi diskten okuduğu anlamına gelir: yanıt süreleri artar, disk "
            "I/O'su ve dolayısıyla tüm instance'ın yükü büyür. Sebep genelde üçünden biridir — "
            "veri kümesi belleğe sığmayacak kadar büyüdü, yeni bir sorgu tabloyu baştan sona "
            "tarıyor, ya da shared_buffers gerçek çalışma kümesine göre küçük kaldı."
        ),
        playbook=[
            {
                "title": "Hangi tablo/index diskten okunuyor?",
                "detail": "Önbellekten kaçan okumanın nereden geldiğini görün — tek bir tablo "
                "oranı aşağı çekiyor olabilir.",
                "command": "SELECT schemaname AS sema, relname AS tablo,\n"
                "       heap_blks_read AS diskten, heap_blks_hit AS onbellekten,\n"
                "       round(100.0 * heap_blks_hit / NULLIF(heap_blks_hit + heap_blks_read, 0), 2) AS hit_yuzde\n"
                "FROM pg_statio_user_tables\n"
                "WHERE heap_blks_read > 0\n"
                "ORDER BY heap_blks_read DESC\n"
                "LIMIT 20;",
            },
            {
                "title": "Yeni bir seq scan başladı mı?",
                "detail": "Kaybolan bir index ya da değişen bir sorgu planı oranı hızla düşürür.",
                "command": "SELECT schemaname AS sema, relname AS tablo, seq_scan, seq_tup_read,\n"
                "       idx_scan, n_live_tup\n"
                "FROM pg_stat_user_tables\n"
                "WHERE seq_scan > 0\n"
                "ORDER BY seq_tup_read DESC\n"
                "LIMIT 20;",
            },
            {
                "title": "Çalışma kümesi belleğe sığıyor mu?",
                "detail": "shared_buffers'ı veritabanı boyutu ve sunucu RAM'iyle karşılaştırın. "
                "Yaygın başlangıç noktası sunucu RAM'inin ~%25'idir; dbace sunucunun RAM'ini "
                "ölçmediği için değeri siz doğrulamalısınız.",
                "command": "SELECT name, setting, unit FROM pg_settings\n"
                "WHERE name IN ('shared_buffers', 'effective_cache_size', 'work_mem');\n"
                "SELECT pg_size_pretty(pg_database_size(current_database())) AS db_boyutu;",
            },
            {
                "title": "Sık okunan veriyi önbellekte tutun",
                "detail": "pg_prewarm ile kritik tabloları yeniden başlatma sonrası önbelleğe "
                "alabilirsiniz — bu bir çözüm değil, ısınma süresini kısaltan bir yardımcıdır.",
                "command": "CREATE EXTENSION IF NOT EXISTS pg_prewarm;\n"
                "SELECT pg_prewarm('<sema>.<tablo>');",
            },
        ],
        cautions=[
            "`shared_buffers` değişikliği YENİDEN BAŞLATMA gerektirir ve fazlası zararlıdır: "
            "işletim sisteminin kendi dosya önbelleğine yer bırakmalısınız.",
            "Cache hit oranı yedek alma, toplu içe aktarma ya da gece raporu gibi TEK SEFERLİK "
            "işlerde de düşer — düşüş kalıcı mı, önce bunu doğrulayın.",
            "Bu oran `pg_stat_reset()` çağrısından beri kümülatiftir; yakın zamanda "
            "sıfırlandıysa kısa vadeli değişim abartılı görünür.",
        ],
        estimated_duration="Teşhis sorguları saniyeler; parametre değişikliği yeniden başlatma penceresi ister.",
        rollback="`ALTER SYSTEM RESET <parametre>;` ardından yeniden başlatma. Teşhis adımları "
        "hiçbir değişiklik yapmaz.",
        verification=(
            "SELECT round(100.0 * sum(blks_hit) / NULLIF(sum(blks_hit) + sum(blks_read), 0), 2) AS hit_yuzde\n"
            "FROM pg_stat_database WHERE datname = current_database();"
        ),
    )


def replication_lag_advice(*, current_bytes: float, predicted_bytes: float) -> Advice:
    return advice_from_playbook(
        "Replikasyon gecikmesinin kaynağını bulun",
        why=(
            f"Replikasyon gecikmesi {current_bytes:,.0f} bayt ve ~{predicted_bytes:,.0f} bayta "
            "çıkması bekleniyor. Gecikme büyüdükçe iki şey riske girer: bir failover'da "
            "kaybedilecek veri miktarı (RPO) artar ve replikadan okuyan raporlar giderek daha "
            "eski veri döndürür. Gecikme replikanın yetişemediği noktayı geçerse primary'deki "
            "WAL birikir ve disk dolabilir."
        ),
        playbook=[
            {
                "title": "Gecikme nerede: gönderim mi, uygulama mı?",
                "detail": "write/flush/replay farkları hangi aşamanın geride kaldığını söyler — "
                "ağ mı yavaş, replika mı WAL'i uygulayamıyor.",
                "command": "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,\n"
                "       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS uygulama_farki,\n"
                "       write_lag, flush_lag, replay_lag\n"
                "FROM pg_stat_replication;",
            },
            {
                "title": "Replikada uygulamayı bloklayan bir sorgu var mı?",
                "detail": "Hot standby'da uzun süren bir okuma sorgusu WAL uygulamasını "
                "bekletebilir (replikada çalıştırın).",
                "command": "SELECT pid, now() - query_start AS sure, state, query\n"
                "FROM pg_stat_activity\n"
                "WHERE state <> 'idle'\n"
                "ORDER BY query_start\n"
                "LIMIT 20;",
            },
            {
                "title": "Slot birikimi ve WAL disk baskısı",
                "detail": "Sahipsiz bir replication slot WAL'i sonsuza kadar tutar ve primary'nin "
                "diskini doldurur — gecikmenin en tehlikeli hali budur.",
                "command": "SELECT slot_name, active,\n"
                "       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS tutulan_wal\n"
                "FROM pg_replication_slots\n"
                "ORDER BY pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) DESC;",
            },
        ],
        cautions=[
            "Sahipsiz görünen bir slot'u silmek, ona bağlı replikayı KALICI olarak bozar "
            "(yeniden temel yedek gerekir) — silmeden önce replikanın gerçekten kullanım dışı "
            "olduğunu doğrulayın.",
            "`max_standby_streaming_delay` artırmak replikadaki sorguların iptal edilmesini "
            "azaltır ama gecikmeyi BÜYÜTÜR — ikisi arasında bilinçli bir denge kurun.",
            "Replikada sorgu sonlandırmak raporların hata almasına yol açar.",
        ],
        estimated_duration="Teşhis dakikalar; kök neden ağ/IO ise düzeltme altyapı tarafında, süresi değişken.",
        rollback="Teşhis adımları değişiklik yapmaz. Parametre değişiklikleri "
        "`ALTER SYSTEM RESET <parametre>; SELECT pg_reload_conf();` ile geri alınır.",
        verification=(
            "SELECT client_addr,\n"
            "       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS gecikme\n"
            "FROM pg_stat_replication;"
        ),
    )


def throughput_advice(*, metric_key: str, current: float, predicted: float) -> Advice:
    """Yük artışı tahmini: kapasite planlaması, acil bir arıza değil.

    Buraya somut bir "şunu çalıştır" adımı yazmıyoruz çünkü artan yükün doğru cevabı ortamın
    kendisine bağlı (donanım, uygulama mimarisi, iş takvimi) — uydurulmuş bir komut vermek
    yerine hangi ölçümlere bakılacağını söylüyoruz.
    """
    return advice_from_playbook(
        "Artan yük için kapasiteyi ve darboğazı ölçün",
        why=(
            f"{metric_key} şu an {current:.1f}, ~{predicted:.1f} seviyesine çıkması bekleniyor. "
            "Yük artışının kendisi bir arıza değil; sorun, artışın hangi kaynağı önce "
            "tüketeceğinin bilinmemesidir. Darboğaz önceden ölçülmezse ilk belirti genelde "
            "kullanıcıya yansıyan yavaşlama olur."
        ),
        playbook=[
            {
                "title": "Yük gerçekten mi artıyor, yoksa aynı iş mi yavaşlıyor?",
                "detail": "Çağrı sayısı sabitken toplam süre artıyorsa bu yük artışı değil, "
                "performans gerilemesidir — çözümü de farklıdır.",
                "command": "SELECT calls, round(total_exec_time::numeric, 1) AS toplam_ms,\n"
                "       round(mean_exec_time::numeric, 2) AS ortalama_ms, query\n"
                "FROM pg_stat_statements\n"
                "ORDER BY total_exec_time DESC\n"
                "LIMIT 20;",
            },
            {
                "title": "Hangi kaynakta bekleniyor?",
                "detail": "Bekleme tipleri darboğazın CPU mu, kilit mi, I/O mu olduğunu söyler.",
                "command": "SELECT wait_event_type, wait_event, count(*)\n"
                "FROM pg_stat_activity\n"
                "WHERE wait_event IS NOT NULL\n"
                "GROUP BY 1, 2\n"
                "ORDER BY count(*) DESC;",
            },
            {
                "title": "Bağlantı havuzu artan yükü karşılayabiliyor mu?",
                "detail": "Yük artışı çoğu zaman önce bağlantı limitinde hissedilir.",
                "command": "SELECT (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS azami,\n"
                "       count(*) AS toplam FROM pg_stat_activity;",
            },
        ],
        cautions=[
            "Bu bir kapasite planlama sinyalidir, acil bir arıza değil — bakım penceresi "
            "gerektiren bir değişikliği tek bir tahmine dayanarak yapmayın.",
            "dbace sunucunun CPU/RAM/disk kullanımını ÖLÇMÜYOR (host-agent OS metriği "
            "toplamıyor); darboğazın donanımda olup olmadığını buradan doğrulayamazsınız.",
        ],
        estimated_duration="Ölçüm dakikalar; kapasite kararı ayrı bir planlama işi.",
        rollback="Bu adımlar yalnızca ölçüm yapar, geri alınacak bir değişiklik üretmez.",
        verification=(
            "-- Birkaç gün sonra trendin gerçekten sürdüğünü doğrulayın:\n"
            "SELECT now(), calls, round(mean_exec_time::numeric, 2) AS ortalama_ms\n"
            "FROM pg_stat_statements ORDER BY calls DESC LIMIT 5;"
        ),
    )


# --- Kısa vadeli metrikler için dağıtıcı ---------------------------------------------------


def short_horizon_advice(
    *, metric_key: str, engine: str, current: float, predicted: float, horizon_minutes: int
) -> Advice:
    if metric_key in ("connection_utilization_pct", "active_connections"):
        return connection_advice(
            engine=engine, current=current, predicted=predicted, horizon_minutes=horizon_minutes
        )
    if metric_key == "cache_hit_ratio":
        if engine != "postgresql":
            return unavailable(
                f"Cache hit oranı için adım adım plan yalnızca PostgreSQL'de üretiliyor; "
                f"bu instance {engine} ve karşılığı olan DMV/komut seti doğrulanmadı.",
                title="Cache hit düşüşünü inceleyin",
            )
        return cache_hit_advice(current=current, predicted=predicted)
    if metric_key == "replication_lag_bytes":
        if engine != "postgresql":
            return unavailable(
                f"Replikasyon gecikmesi planı yalnızca PostgreSQL için yazıldı; bu instance "
                f"{engine} ve Always On/replica set karşılıkları farklı komutlar ister.",
                title="Replikasyon gecikmesini inceleyin",
            )
        return replication_lag_advice(current_bytes=current, predicted_bytes=predicted)
    if metric_key in ("transactions_per_sec", "ops_per_sec"):
        if engine != "postgresql":
            return unavailable(
                f"Yük artışı için ölçüm adımları yalnızca PostgreSQL komutlarıyla yazıldı; "
                f"bu instance {engine}.",
                title="Artan yükü ölçün",
            )
        return throughput_advice(metric_key=metric_key, current=current, predicted=predicted)
    return unavailable(
        f"{metric_key} için standart bir çözüm adımı tanımlı değil — bu metriğin doğru cevabı "
        "ortama özgü olduğundan uydurulmuş bir komut vermek yanıltıcı olurdu.",
    )
