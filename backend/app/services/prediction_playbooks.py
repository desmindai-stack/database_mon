"""Tahminler için adım adım çözüm planları (Faz 16-B İŞ 7).

Önceki öneriler tek cümlelik ve genel geçerdi ("arşivleme/partitioning değerlendirin").
Kullanıcı bunun yerine "önce şunu çalıştır, çıktısına göre şunu yap" diyen numaralı adımlar
istedi. Her adım {title, detail, command} — komut varsa arayüzde ayrı satırda, kopyalanabilir.

Kurallar:

* Komutlar tam ve çalıştırılabilir; şema adı gerektiğinde yazılı, noktalı virgülle bitiyor.
* Yıkıcı/kilitleyen komutlar (VACUUM FULL, ALTER SYSTEM + restart) adım metninde açık uyarıyla
  geliyor; hiçbiri "körlemesine çalıştır" diye sunulmuyor.
* Sayısal eşikler uydurulmuyor: dbace'in gerçekten ölçtüğü değerler (boyut, günlük artış,
  tahmini tarih) adım metnine geçiyor, ölçmediklerimiz (gerçek disk kapasitesi) için
  kullanıcıya "kendi kapasitenle karşılaştır" deniyor.
"""

from __future__ import annotations

from typing import Any

PlaybookStep = dict[str, Any]


def _step(title: str, detail: str, command: str | None = None) -> PlaybookStep:
    return {"title": title, "detail": detail, "command": command}


def database_size_playbook(current_human: str, per_day_human: str, doubling_date: str) -> list[PlaybookStep]:
    return [
        _step(
            "Mevcut kullanımı ve en çok yer kaplayan tabloları çıkarın",
            f"Veritabanı şu an {current_human}, günlük ~{per_day_human} büyüyor. Önce yerin nereye "
            "gittiğini görün — büyümenin neredeyse tamamı genelde birkaç tablodan gelir.",
            "SELECT n.nspname AS sema,\n"
            "       c.relname AS tablo,\n"
            "       pg_size_pretty(pg_total_relation_size(c.oid)) AS toplam,\n"
            "       pg_size_pretty(pg_relation_size(c.oid)) AS veri,\n"
            "       pg_size_pretty(pg_total_relation_size(c.oid) - pg_relation_size(c.oid)) AS index_toast\n"
            "FROM pg_class c\n"
            "JOIN pg_namespace n ON n.oid = c.relnamespace\n"
            "WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')\n"
            "ORDER BY pg_total_relation_size(c.oid) DESC\n"
            "LIMIT 20;",
        ),
        _step(
            "Ölü satırların kapladığı yeri ölçün",
            "Büyüme gerçek veri değil, temizlenmemiş ölü satırlar olabilir. Oran yüksekse (>%20) "
            "önce VACUUM, sonra arşivleme düşünülür.",
            "SELECT schemaname AS sema, relname AS tablo, n_live_tup, n_dead_tup,\n"
            "       round(100.0 * n_dead_tup / NULLIF(n_live_tup, 0), 1) AS olu_yuzde,\n"
            "       last_autovacuum\n"
            "FROM pg_stat_user_tables\n"
            "ORDER BY n_dead_tup DESC\n"
            "LIMIT 20;",
        ),
        _step(
            "Rutin VACUUM ile boşluğu tekrar kullanılabilir hale getirin",
            "Bu, diski işletim sistemine geri VERMEZ ama alanı tablonun kendi içinde yeniden "
            "kullanılabilir yapar ve büyümeyi durdurur. Tabloyu kilitlemez.",
            "VACUUM (ANALYZE, VERBOSE) <sema>.<tablo>;",
        ),
        _step(
            "Gerçekten disk geri almanız gerekiyorsa VACUUM FULL — dikkatli",
            "VACUUM FULL tabloyu ACCESS EXCLUSIVE kilitler (o süre boyunca tamamen erişilemez) ve "
            "tablo boyutu kadar geçici disk ister. Sadece bakım penceresinde, yeterli boş disk "
            "varken çalıştırın. Kilitsiz alternatif: pg_repack.",
            "VACUUM FULL <sema>.<tablo>;",
        ),
        _step(
            "Eski veriyi arşivleyin veya partition'a geçin",
            "Zaman serisi/log benzeri tablolarda kalıcı çözüm partitioning: eski partition'ı "
            "DETACH edip arşive taşımak saniyeler sürer, DELETE ise tabloyu daha da şişirir.",
            "-- Örnek: aylık partition'lı yeni tablo\n"
            "CREATE TABLE <sema>.<tablo>_yeni (LIKE <sema>.<tablo> INCLUDING ALL)\n"
            "  PARTITION BY RANGE (<zaman_kolonu>);\n"
            "CREATE TABLE <sema>.<tablo>_2026_01 PARTITION OF <sema>.<tablo>_yeni\n"
            "  FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');\n"
            "-- Eski partition'ı arşive almak (kilitsize yakın):\n"
            "ALTER TABLE <sema>.<tablo>_yeni DETACH PARTITION <sema>.<tablo>_2026_01;",
        ),
        _step(
            "Disk büyütme kararı için eşik",
            f"Bu trend sürerse boyut ~{doubling_date} civarında iki katına çıkar. dbace gerçek disk "
            "kapasitesini ölçmüyor; kendi diskinizin boş alanını bu tarihe kadarki artışla "
            "karşılaştırın. Pratik eşik: bu tarihte doluluk %85'i geçecekse disk büyütmeyi ŞİMDİ "
            "planlayın — PostgreSQL disk dolduğunda yazma işlemlerini tamamen durdurur.",
            "-- Sunucuda (host-agent yoksa elle):\n"
            "df -h $(psql -tAc \"SHOW data_directory;\")",
        ),
    ]


def connections_playbook(engine: str) -> list[PlaybookStep]:
    if engine == "postgresql":
        return [
            _step(
                "Mevcut ve azami bağlantı sayısını görün",
                "Önce gerçek doluluk oranını ve boşta bekleyen bağlantıları ölçün — sorun çoğu zaman "
                "yetersiz max_connections değil, kapatılmayan idle bağlantılardır.",
                "SELECT (SELECT setting::int FROM pg_settings WHERE name = 'max_connections') AS azami,\n"
                "       count(*) AS toplam,\n"
                "       count(*) FILTER (WHERE state = 'active') AS aktif,\n"
                "       count(*) FILTER (WHERE state = 'idle') AS bosta,\n"
                "       count(*) FILTER (WHERE state = 'idle in transaction') AS islem_icinde_bosta\n"
                "FROM pg_stat_activity;",
            ),
            _step(
                "Bağlantıyı kim tutuyor?",
                "Uygulama/kullanıcı bazında dağılım, hangi tarafın havuz ayarının bozuk olduğunu "
                "gösterir.",
                "SELECT usename, application_name, client_addr, state, count(*)\n"
                "FROM pg_stat_activity\n"
                "GROUP BY 1, 2, 3, 4\n"
                "ORDER BY count(*) DESC;",
            ),
            _step(
                "Önce pooler — max_connections'ı büyütmeden",
                "Her PostgreSQL bağlantısı ayrı bir işlemdir ve boştayken bile bellek tüketir. "
                "max_connections'ı büyütmek sorunu bellek sorununa çevirir. Doğru çözüm PgBouncer "
                "(transaction mode) ya da uygulama tarafında havuz boyutunu SINIRLAMAK.",
                "# PgBouncer örnek yapılandırma (pgbouncer.ini)\n"
                "[databases]\n"
                "* = host=127.0.0.1 port=5432\n"
                "[pgbouncer]\n"
                "pool_mode = transaction\n"
                "max_client_conn = 1000\n"
                "default_pool_size = 25",
            ),
            _step(
                "Sızdıran bağlantıları kesin",
                "'idle in transaction' durumunda takılı kalan oturumlar hem bağlantı hem de VACUUM'u "
                "engeller. Önce uygulamayı düzeltin; geçici çözüm olarak zaman aşımı koyun.",
                "ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';\n"
                "SELECT pg_reload_conf();",
            ),
            _step(
                "Gerçekten gerekiyorsa max_connections'ı artırın — yeniden başlatma gerekir",
                "DİKKAT: bu ayar reload ile devreye GİRMEZ, PostgreSQL yeniden başlatılmalıdır "
                "(kesinti). Ayrıca her bağlantı work_mem kadar (hatta sorgu başına birkaç katı) "
                "bellek isteyebilir — artırmadan önce toplam belleği hesaplayın.",
                "ALTER SYSTEM SET max_connections = 300;\n"
                "-- Ardından PostgreSQL'i YENİDEN BAŞLATIN. Kontrol:\n"
                "SHOW max_connections;",
            ),
        ]
    if engine == "sqlserver":
        return [
            _step(
                "Mevcut oturumları ve havuz kullanımını görün",
                "SQL Server'da bağlantı limiti pratikte uygulama havuzlarının toplamıdır.",
                "SELECT program_name, login_name, host_name, status, COUNT(*) AS oturum\n"
                "FROM sys.dm_exec_sessions\n"
                "WHERE is_user_process = 1\n"
                "GROUP BY program_name, login_name, host_name, status\n"
                "ORDER BY oturum DESC;",
            ),
            _step(
                "Uygulama tarafı havuz boyutunu sınırlayın",
                "Bağlantı dizesindeki Max Pool Size (varsayılan 100) her uygulama örneği için ayrı "
                "ayrı geçerlidir — 10 örnek = 1000 bağlantı. Önce burayı düşürün.",
                "Server=...;Database=...;Max Pool Size=50;Min Pool Size=5;",
            ),
            _step(
                "Sunucu tarafı limiti kontrol edin",
                "'user connections' 0 ise dinamik (pratikte sınırsız) demektir; 0 dışında bir değer "
                "elle konmuşsa sorun burada olabilir.",
                "EXEC sp_configure 'show advanced options', 1;\nRECONFIGURE;\nEXEC sp_configure 'user connections';",
            ),
        ]
    return [
        _step(
            "Sürücü tarafı bağlantı havuzunu gözden geçirin",
            "MongoDB sürücüsünde maxPoolSize her uygulama örneği için ayrıdır; toplam bağlantı = "
            "örnek sayısı × maxPoolSize.",
            "mongodb://host:27017/?maxPoolSize=50&minPoolSize=5",
        ),
        _step(
            "Sunucudaki mevcut bağlantıları görün",
            "Boştaki bağlantı sayısı yüksekse havuz gereğinden büyük demektir.",
            "db.serverStatus().connections",
        ),
        _step(
            "Uzun süren işlemleri ve boşta bekleyen oturumları bulun",
            "Bağlantı doluluğunun yaygın sebebi, biten ama kapatılmayan ya da uzun süren "
            "işlemlerdir; havuzu büyütmeden önce bunları temizleyin.",
            "db.currentOp({ \"secs_running\": { $gt: 30 } })",
        ),
    ]


def table_growth_playbook(schema_name: str, table_name: str, per_day_human: str) -> list[PlaybookStep]:
    q = f'"{schema_name}"."{table_name}"'
    return [
        _step(
            "Büyümenin veri mi şişme mi olduğunu ayırın",
            f"{schema_name}.{table_name} günde ~{per_day_human} büyüyor. Ölü satır oranı yüksekse "
            "bu gerçek veri artışı değil, temizlenmemiş şişmedir — çözümü de farklıdır.",
            "SELECT n_live_tup, n_dead_tup,\n"
            "       round(100.0 * n_dead_tup / NULLIF(n_live_tup, 0), 1) AS olu_yuzde,\n"
            "       last_autovacuum, last_autoanalyze\n"
            "FROM pg_stat_user_tables\n"
            f"WHERE schemaname = '{schema_name}' AND relname = '{table_name}';",
        ),
        _step(
            "Tablonun ve indexlerinin boyut dağılımı",
            "Büyüme index tarafındaysa çözüm arşivleme değil, index bakımı/temizliğidir.",
            f"SELECT pg_size_pretty(pg_relation_size('{schema_name}.{table_name}')) AS veri,\n"
            f"       pg_size_pretty(pg_indexes_size('{schema_name}.{table_name}')) AS indexler,\n"
            f"       pg_size_pretty(pg_total_relation_size('{schema_name}.{table_name}')) AS toplam;",
        ),
        _step(
            "Şişme ise: rutin VACUUM",
            "Tabloyu kilitlemez, alanı tablo içinde yeniden kullanılabilir yapar.",
            f"VACUUM (ANALYZE, VERBOSE) {q};",
        ),
        _step(
            "Gerçek veri artışı ise: eski kayıtları arşivleyin",
            "Silmeden önce arşiv tablosuna taşıyın. Tek seferde milyonlarca satır silmek yerine "
            "partiler halinde çalıştırın — uzun süren tek bir DELETE hem WAL'i şişirir hem de "
            "VACUUM'u engeller.",
            f"CREATE TABLE IF NOT EXISTS {schema_name}.{table_name}_arsiv "
            f"(LIKE {schema_name}.{table_name} INCLUDING ALL);\n\n"
            "-- Partiler halinde taşı (10.000'lik):\n"
            f"WITH tasinacak AS (\n"
            f"    SELECT ctid FROM {q}\n"
            "    WHERE <zaman_kolonu> < now() - interval '90 days'\n"
            "    LIMIT 10000\n"
            ")\n"
            f"DELETE FROM {q} t USING tasinacak s WHERE t.ctid = s.ctid\n"
            f"RETURNING t.*;  -- çıktıyı {schema_name}.{table_name}_arsiv'e INSERT edin",
        ),
        _step(
            "Kalıcı çözüm: partitioning",
            "Zaman bazlı büyüyen tablolarda eski partition'ı DETACH etmek saniyeler sürer ve "
            "şişme bırakmaz; DELETE ise her seferinde VACUUM işi yaratır.",
            f"-- Yeni tabloyu range partition olarak kurup veriyi taşıyın:\n"
            f"CREATE TABLE {schema_name}.{table_name}_p (LIKE {schema_name}.{table_name} INCLUDING ALL)\n"
            "  PARTITION BY RANGE (<zaman_kolonu>);\n"
            f"CREATE TABLE {schema_name}.{table_name}_p_2026_01 PARTITION OF {schema_name}.{table_name}_p\n"
            "  FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');",
        ),
    ]


def wraparound_playbook(current_age: float, freeze_max_age: int, eta_date: str) -> list[PlaybookStep]:
    return [
        _step(
            "Veritabanı ve tablo bazında mevcut yaşı ölçün",
            f"Şu anki yaş {current_age:,.0f}; bu hızla ~{eta_date} civarında "
            f"autovacuum_freeze_max_age ({freeze_max_age:,}) eşiğine ulaşır.",
            "SELECT datname, age(datfrozenxid) AS yas\n"
            "FROM pg_database\n"
            "ORDER BY yas DESC;",
        ),
        _step(
            "Yaşı hangi tablolar taşıyor?",
            "Genellikle tek bir eski/az yazılan tablo tüm veritabanının yaşını yukarı çeker; "
            "sadece onu dondurmak yeterlidir.",
            "SELECT n.nspname AS sema, c.relname AS tablo, age(c.relfrozenxid) AS yas,\n"
            "       pg_size_pretty(pg_total_relation_size(c.oid)) AS boyut\n"
            "FROM pg_class c\n"
            "JOIN pg_namespace n ON n.oid = c.relnamespace\n"
            "WHERE c.relkind IN ('r', 'm')\n"
            "ORDER BY age(c.relfrozenxid) DESC\n"
            "LIMIT 20;",
        ),
        _step(
            "autovacuum'u engelleyen bir şey var mı?",
            "Uzun süren transaction, 'idle in transaction' oturumlar, kullanılmayan replication "
            "slot'ları ve hazırlanmış (prepared) transaction'lar freeze işlemini tamamen "
            "durdurabilir. Yaş artmaya devam ediyorsa sebep neredeyse her zaman burasıdır.",
            "SELECT pid, state, age(backend_xmin) AS xmin_yasi, now() - xact_start AS islem_suresi, query\n"
            "FROM pg_stat_activity\n"
            "WHERE backend_xmin IS NOT NULL\n"
            "ORDER BY age(backend_xmin) DESC;\n\n"
            "SELECT slot_name, active, age(xmin) AS slot_xmin_yasi FROM pg_replication_slots;\n"
            "SELECT gid, prepared, age(transaction) AS yas FROM pg_prepared_xacts;",
        ),
        _step(
            "Yaşı en yüksek tabloları elle dondurun",
            "VACUUM FREEZE tabloyu kilitlemez (ACCESS SHARE yeterlidir) ama yoğun I/O üretir — "
            "büyük tablolarda düşük trafikli bir saatte, tek tek çalıştırın.",
            "VACUUM (FREEZE, VERBOSE, ANALYZE) <sema>.<tablo>;",
        ),
        _step(
            "autovacuum'un yetişebilmesi için ayarları gözden geçirin",
            "Yaş sürekli tırmanıyorsa autovacuum yetişemiyor demektir: işçi sayısını artırmak ve "
            "gecikmeyi azaltmak (maliyet gecikmesini düşürmek) freeze işlerini hızlandırır. "
            "Değerleri kendi sunucu kapasitenize göre ayarlayın; bunlar başlangıç noktasıdır.",
            "ALTER SYSTEM SET autovacuum_max_workers = 5;\n"
            "ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 2000;\n"
            "ALTER SYSTEM SET autovacuum_vacuum_cost_delay = '2ms';\n"
            "SELECT pg_reload_conf();\n"
            "-- Not: autovacuum_max_workers YENİDEN BAŞLATMA gerektirir, diğer ikisi reload ile geçer.",
        ),
    ]


def index_bloat_playbook(schema_name: str, index_name: str, per_day_human: str) -> list[PlaybookStep]:
    q = f'"{schema_name}"."{index_name}"'
    return [
        _step(
            "Index'in boyutunu ve kullanımını doğrulayın",
            f"{schema_name}.{index_name} günde ~{per_day_human} büyüyor. Önce bu index'in gerçekten "
            "kullanılıp kullanılmadığına bakın — hiç kullanılmıyorsa doğru hamle REINDEX değil DROP.",
            "SELECT s.schemaname AS sema, s.relname AS tablo, s.indexrelname AS index_adi,\n"
            "       s.idx_scan AS tarama, pg_size_pretty(pg_relation_size(s.indexrelid)) AS boyut\n"
            "FROM pg_stat_user_indexes s\n"
            f"WHERE s.schemaname = '{schema_name}' AND s.indexrelname = '{index_name}';",
        ),
        _step(
            "Şişme oranını ölçün",
            "Index'in kaç yaprağının gerçekten dolu olduğunu pgstattuple gösterir "
            "(avg_leaf_density %50'nin altındaysa REINDEX kazançlıdır). Uzantı kurulu değilse "
            "önce onu kurun; sorgu tabloyu okur, büyük indexlerde zaman alabilir.",
            "CREATE EXTENSION IF NOT EXISTS pgstattuple;\n"
            f"SELECT * FROM pgstatindex('{schema_name}.{index_name}');",
        ),
        _step(
            "Kullanılmıyorsa kaldırın",
            "idx_scan = 0 ise index hem disk hem de her INSERT/UPDATE'te yazma maliyeti demektir. "
            "CONCURRENTLY tabloyu kilitlemez.",
            f"DROP INDEX CONCURRENTLY IF EXISTS {q};",
        ),
        _step(
            "Kullanılıyorsa yerinde yeniden oluşturun",
            "REINDEX CONCURRENTLY tabloyu yazmaya kapatmaz (PostgreSQL 12+). İşlem sırasında "
            "index'in iki kopyası birden diskte durur — yeterli boş alan olduğundan emin olun. "
            "Yarıda kalırsa geride 'INVALID' bir index kalabilir; onu DROP INDEX ile temizleyin.",
            f"REINDEX INDEX CONCURRENTLY {q};",
        ),
        _step(
            "Tekrarlamaması için fillfactor'ü düşünün",
            "Sık güncellenen tablolarda index yaprakları sürekli bölünür. fillfactor'ü düşürmek "
            "(varsayılan 90) bölünmeleri azaltır; değişiklik bir sonraki REINDEX'te devreye girer.",
            f"ALTER INDEX {q} SET (fillfactor = 80);\nREINDEX INDEX CONCURRENTLY {q};",
        ),
    ]
