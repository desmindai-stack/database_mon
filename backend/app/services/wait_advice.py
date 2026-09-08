"""Bekleme tipine göre öneri (Faz 25 İŞ 4).

Bekleme analizinin değeri ölçümde değil, ölçümün EYLEME dönüşmesinde. "Yükün %78'i disk g/ç"
tek başına bir bilgi; "shared_buffers şu an 128 MB, RAM'in %25'ine çıkarın, komutu şu, riski
şu, doğrulaması şu" bir eylem.

Bu modül `services/advice.py`'deki BEŞ PARÇALI standarda uyuyor (CLAUDE.md kuralı):
neden (iş etkisiyle) → numaralı adımlar → adım başına komut → dikkat notları → doğrulama.

İKİ DÜRÜSTLÜK KURALI:

1. **Öneri üretilemiyorsa NEDENİ yazılır.** Kategori tanınmıyorsa ya da veri yetersizse
   `unavailable(...)` dönüyor; boş kutu göstermek yasak.
2. **`client` baskınsa "veritabanında yapacak bir şey yok" denir.** Bu, ürünün verebileceği
   en değerli cevaplardan biri: DBA'yı olmayan bir sorunu aramaktan kurtarır. Bunu gizleyip
   yerine genel bir "sorgularınızı gözden geçirin" önerisi üretmek, ölçümü çöpe atmak olurdu.

Komutlar motora göre değişiyor (PostgreSQL / SQL Server); desteklenmeyen motorda öneri
uydurulmuyor.
"""

from __future__ import annotations

from app.domain.engines import DatabaseEngine
from app.domain.waits import WaitCategory, category_label
from app.services.advice import Advice, AdviceStep, unavailable

#: Öneri üretmek için gereken en düşük pay. Altındaki bir kategoriye göre eylem önermek,
#: kullanıcıyı yükün yarısından azını açıklayan bir işe yönlendirmek olurdu.
#: `database_load.py` ve `query_diagnostics.py` ile aynı eşik.
ADVICE_MIN_SHARE_PCT = 40.0


def _pg_io_advice(share_pct: float, top_query: str | None) -> Advice:
    steps = [
        AdviceStep(
            action=(
                "Önce cache oranını ölçün: %99'un altındaki bir oran, çalışma kümesinin "
                "shared_buffers'a sığmadığını gösterir."
            ),
            command=(
                "SELECT datname,\n"
                "       round(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2) AS cache_hit_pct\n"
                "FROM pg_stat_database\n"
                "WHERE datname = current_database();"
            ),
        ),
        AdviceStep(
            action=(
                "En çok disk okuyan tabloları bulun — sorun genelde tek bir tabloda "
                "yoğunlaşır ve tek bir index onu bitirir."
            ),
            command=(
                "SELECT relname,\n"
                "       heap_blks_read, heap_blks_hit,\n"
                "       round(100.0 * heap_blks_read / NULLIF(heap_blks_read + heap_blks_hit, 0), 1) AS disk_pct\n"
                "FROM pg_statio_user_tables\n"
                "WHERE heap_blks_read > 0\n"
                "ORDER BY heap_blks_read DESC\n"
                "LIMIT 10;"
            ),
        ),
        AdviceStep(
            action=(
                "Yükü üreten sorgunun planına bakın: Seq Scan görüyorsanız çözüm index, "
                "Index Scan görüp yine çok okuyorsanız çözüm bellek."
            ),
            command=(
                "EXPLAIN (ANALYZE, BUFFERS)\n"
                + (f"{top_query.strip()};" if top_query else "<yük üreten sorgu>;")
            ),
        ),
        AdviceStep(
            action=(
                "Index gerekiyorsa CONCURRENTLY ile oluşturun — normal CREATE INDEX tabloyu "
                "yazmaya kapatır."
            ),
            command="CREATE INDEX CONCURRENTLY ix_<tablo>_<kolon> ON <tablo> (<kolon>);",
        ),
        AdviceStep(
            action=(
                "Bellek gerekiyorsa shared_buffers'ı toplam RAM'in %25'ine çıkarın "
                "(mevcut değeri önce okuyun)."
            ),
            command=(
                "SHOW shared_buffers;\n"
                "-- postgresql.conf: shared_buffers = '<RAM/4>'\n"
                "-- Patroni kullanıyorsanız: patronictl edit-config -p postgresql.parameters.shared_buffers=<değer>"
            ),
        ),
    ]
    return Advice(
        title="Disk okumasını azaltın (index ya da bellek)",
        why=(
            f"Veritabanı yükünün %{share_pct:.0f}'i diskten okuma bekleyerek geçiyor. Disk "
            "okuması bellekten okumadan yüzlerce kat yavaştır; bu oran yüksekken sorgu "
            "süreleri disk kuyruğuna bağlı hale gelir ve yük arttıkça doğrusal değil ÜSTEL "
            "kötüleşir. Kullanıcı tarafında bu, yoğun saatlerde aniden uzayan yanıt "
            "süreleri olarak görünür."
        ),
        steps=steps,
        cautions=[
            "CREATE INDEX CONCURRENTLY tabloyu kilitlemez ama uzun sürer ve başarısız olursa "
            "geçersiz (invalid) bir index bırakır — sonrasında `\\d <tablo>` ile kontrol edin.",
            "shared_buffers değişikliği PostgreSQL'i YENİDEN BAŞLATMAYI gerektirir; bakım "
            "penceresi planlayın.",
            "shared_buffers'ı RAM'in %40'ının üstüne çıkarmak genelde ters teper — işletim "
            "sistemi cache'i için yer kalmaz.",
        ],
        estimated_duration=(
            "Index: tablo boyutuna göre dakikalar–saatler. Parametre değişikliği: "
            "yeniden başlatma süresi (saniyeler), ama bakım penceresi gerektirir."
        ),
        rollback=(
            "Index: DROP INDEX CONCURRENTLY ix_<tablo>_<kolon>;\n"
            "Parametre: eski değeri geri yazıp yeniden başlatın."
        ),
        verification=(
            "-- Değişiklikten sonra aynı pencerede cache oranı yükselmeli, Veritabanı Yükü\n"
            "-- sekmesinde disk g/ç payı düşmeli.\n"
            "SELECT round(100.0 * blks_hit / NULLIF(blks_hit + blks_read, 0), 2) AS cache_hit_pct\n"
            "FROM pg_stat_database WHERE datname = current_database();"
        ),
    )


def _pg_lock_advice(share_pct: float, category: WaitCategory) -> Advice:
    if category == WaitCategory.LWLOCK:
        return Advice(
            title="İç çekişmeyi (LWLock) azaltın",
            why=(
                f"Yükün %{share_pct:.0f}'i LWLock beklemesinde geçiyor. LWLock, "
                "PostgreSQL'in kendi iç veri yapılarına (buffer haritası, WAL yazımı, "
                "transaction durumu) erişim sırasıdır — kullanıcı kilidi değildir ve sorgu "
                "değişikliğiyle çözülmez. Yüksek LWLock, genellikle sunucunun aynı anda "
                "kaldırabileceğinden fazla bağlantıyla çalıştığını gösterir; bu noktadan "
                "sonra bağlantı EKLEMEK verimi DÜŞÜRÜR."
            ),
            steps=[
                AdviceStep(
                    action="Hangi LWLock'un beklendiğini görün — çözüm buna göre değişir.",
                    command=(
                        "SELECT wait_event, count(*)\n"
                        "FROM pg_stat_activity\n"
                        "WHERE wait_event_type = 'LWLock'\n"
                        "GROUP BY wait_event ORDER BY count(*) DESC;"
                    ),
                ),
                AdviceStep(
                    action=(
                        "Bağlantı sayısını ölçün. CPU çekirdek sayısının 2-4 katından fazla "
                        "AKTİF bağlantı varsa bir bağlantı havuzu (PgBouncer) tek başına "
                        "sorunu çözebilir."
                    ),
                    command=(
                        "SELECT count(*) FILTER (WHERE state = 'active') AS active,\n"
                        "       count(*) AS total,\n"
                        "       current_setting('max_connections') AS max_connections\n"
                        "FROM pg_stat_activity;"
                    ),
                ),
                AdviceStep(
                    action=(
                        "WALWrite/WALInsert baskınsa WAL yazımı darboğaz demektir: "
                        "wal_buffers'ı artırın ve WAL'ı ayrı bir diske alın."
                    ),
                    command="SHOW wal_buffers;\n-- postgresql.conf: wal_buffers = '16MB'",
                ),
            ],
            cautions=[
                "max_connections'ı ARTIRMAK bu sorunu büyütür, küçültmez — çözüm havuzlamadır.",
                "wal_buffers değişikliği yeniden başlatma gerektirir.",
            ],
            estimated_duration="PgBouncer kurulumu: 1-2 saat. Parametre: yeniden başlatma.",
            rollback="PgBouncer'ı devre dışı bırakıp uygulamayı doğrudan bağlayın; parametreyi eski değerine alın.",
            verification=(
                "SELECT wait_event, count(*) FROM pg_stat_activity\n"
                "WHERE wait_event_type = 'LWLock' GROUP BY wait_event;"
            ),
        )

    return Advice(
        title="Kilit çakışmasını çözün (uzun transaction'ları kısaltın)",
        why=(
            f"Yükün %{share_pct:.0f}'i başka bir transaction'ın tuttuğu kilidi bekleyerek "
            "geçiyor. Kilit beklemesi kaynak sorunu DEĞİLDİR: sunucuya CPU ya da disk eklemek "
            "hiçbir şeyi değiştirmez, çünkü oturumlar sırada boş bekliyor. İş etkisi doğrudan: "
            "bekleyen her oturum bir kullanıcı isteğidir ve zincirleme büyür — bir uzun "
            "transaction onlarca isteği durdurabilir."
        ),
        steps=[
            AdviceStep(
                action="Şu anda kimin kimi beklettiğini görün — zincirin başındaki oturum suçludur.",
                command=(
                    "SELECT blocked.pid AS bekleyen, blocking.pid AS bloklayan,\n"
                    "       blocking.state, blocking.query AS bloklayan_sorgu,\n"
                    "       now() - blocking.xact_start AS bloklayan_transaction_suresi\n"
                    "FROM pg_stat_activity blocked\n"
                    "JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS b(pid) ON true\n"
                    "JOIN pg_stat_activity blocking ON blocking.pid = b.pid\n"
                    "WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;"
                ),
            ),
            AdviceStep(
                action=(
                    "Bloklayan oturum 'idle in transaction' ise sorun uygulamada: transaction "
                    "açılıp iş yapılmadan bekletiliyor. Ne kadar sürdüğünü ölçün."
                ),
                command=(
                    "SELECT pid, usename, application_name,\n"
                    "       now() - xact_start AS transaction_suresi, query\n"
                    "FROM pg_stat_activity\n"
                    "WHERE state = 'idle in transaction'\n"
                    "ORDER BY xact_start;"
                ),
            ),
            AdviceStep(
                action=(
                    "Kalıcı çözüm: sunucu tarafında boşta transaction'a süre sınırı koyun. "
                    "Bu, uygulama hatasının veritabanını kilitlemesini engeller."
                ),
                command=(
                    "-- Oturum bazında test edin, sonra postgresql.conf'a alın:\n"
                    "SET idle_in_transaction_session_timeout = '60s';\n"
                    "-- postgresql.conf: idle_in_transaction_session_timeout = '60s'\n"
                    "-- Uygulanması için: SELECT pg_reload_conf();"
                ),
            ),
            AdviceStep(
                action=(
                    "Acil durumda tek bir bloklayan oturumu sonlandırın — ÖNCE iptal deneyin, "
                    "sonra sonlandırın."
                ),
                command=(
                    "SELECT pg_cancel_backend(<pid>);   -- sorguyu iptal eder, oturum yaşar\n"
                    "SELECT pg_terminate_backend(<pid>); -- oturumu kapatır (son çare)"
                ),
            ),
        ],
        cautions=[
            "pg_terminate_backend bloklayan oturumun transaction'ını GERİ ALIR — yarım kalmış "
            "bir iş varsa kaybolur. Önce pg_cancel_backend deneyin.",
            "idle_in_transaction_session_timeout uygulamada beklenmedik hatalara yol açabilir; "
            "önce test ortamında deneyin ve uygulamanın yeniden bağlanma davranışını kontrol edin.",
            "Değeri çok kısa tutmayın: uzun süren toplu işler (batch) de bu sınıra takılır.",
        ],
        estimated_duration=(
            "Acil müdahale (cancel/terminate): saniyeler. Kalıcı çözüm (uygulama tarafı "
            "transaction kapsamının daraltılması): geliştirme işi."
        ),
        rollback=(
            "idle_in_transaction_session_timeout = 0 (sınırsız) yazıp SELECT pg_reload_conf();"
        ),
        verification=(
            "-- Beklenen: sonuç boş dönmeli (kimse kimseyi beklemiyor).\n"
            "SELECT count(*) AS bloklanan_oturum\n"
            "FROM pg_stat_activity\n"
            "WHERE cardinality(pg_blocking_pids(pid)) > 0;"
        ),
    )


def _pg_cpu_advice(share_pct: float, top_query: str | None) -> Advice:
    return Advice(
        title="CPU'da geçen süreyi azaltın (gereksiz hesaplamayı kaldırın)",
        why=(
            f"Yükün %{share_pct:.0f}'i CPU'da geçiyor: oturumlar bir şey BEKLEMİYOR, iş "
            "yapıyor. Bu iyi bir haber gibi görünse de yüksek CPU payı genellikle gereksiz "
            "işin belirtisidir — filtrelenebilecek satırların okunup atılması, index yerine "
            "sıralama, ya da fonksiyonun her satır için yeniden çalışması. İş etkisi: CPU "
            "doyduğunda yeni istekler sıraya girer ve yanıt süreleri hep birlikte bozulur; "
            "donanım eklemek pahalı ve geçici bir çözümdür."
        ),
        steps=[
            AdviceStep(
                action=(
                    "Yükü üreten sorgunun planını ölçerek alın. Rows Removed by Filter yüksekse "
                    "gereksiz satır okunuyor demektir."
                ),
                command=(
                    "EXPLAIN (ANALYZE, BUFFERS, VERBOSE)\n"
                    + (f"{top_query.strip()};" if top_query else "<yük üreten sorgu>;")
                ),
            ),
            AdviceStep(
                action=(
                    "Sıralama (Sort) adımı varsa ve disk kullanıyorsa work_mem yetersizdir; "
                    "index ile sıralamayı tamamen kaldırmak daha iyidir."
                ),
                command=(
                    "SHOW work_mem;\n"
                    "-- Oturum bazında deneyin (kalıcı yapmadan önce):\n"
                    "SET work_mem = '64MB';"
                ),
            ),
            AdviceStep(
                action=(
                    "Paralellik açık mı kontrol edin. Büyük taramalarda paralel işçi sayısını "
                    "artırmak duvar saati süresini düşürür (toplam CPU'yu değil)."
                ),
                command=(
                    "SHOW max_parallel_workers_per_gather;\n"
                    "SHOW max_parallel_workers;"
                ),
            ),
            AdviceStep(
                action=(
                    "İstatistikler eskiyse planlayıcı yanlış plan seçer ve gereksiz CPU harcar. "
                    "Son analiz zamanını kontrol edin."
                ),
                command=(
                    "SELECT relname, last_autoanalyze, last_analyze, n_live_tup\n"
                    "FROM pg_stat_user_tables\n"
                    "ORDER BY n_live_tup DESC LIMIT 10;\n"
                    "-- Eskiyse: ANALYZE <tablo>;"
                ),
            ),
        ],
        cautions=[
            "work_mem OTURUM VE İŞLEM BAŞINA ayrılır: 100 bağlantı × 3 sıralama × 64MB = 19GB. "
            "Global değeri artırmadan önce bu çarpımı yapın.",
            "ANALYZE kısa süreli okuma yükü bindirir ama kilitlemez; yoğun saatte de "
            "çalıştırılabilir.",
            "Paralel işçi sayısını artırmak CPU zaten doluysa işleri KÖTÜLEŞTİRİR.",
        ],
        estimated_duration="Plan incelemesi: dakikalar. Sorgu/index değişikliği: geliştirme işi.",
        rollback="SET work_mem = DEFAULT; parametre değişikliklerini eski değerine alın.",
        verification=(
            "-- Değişiklikten sonra aynı sorgunun toplam süresi düşmeli.\n"
            "SELECT queryid, calls, round(mean_exec_time::numeric, 2) AS mean_ms,\n"
            "       round(total_exec_time::numeric, 1) AS total_ms\n"
            "FROM pg_stat_statements\n"
            "ORDER BY total_exec_time DESC LIMIT 5;"
        ),
    )


def _client_advice(share_pct: float) -> Advice:
    return Advice(
        title="Veritabanında yapılacak bir şey yok — uygulama tarafına bakın",
        why=(
            f"Yükün %{share_pct:.0f}'i, veritabanının UYGULAMAYI beklemesiyle geçiyor. "
            "Sorgu hazır, sonuç hazır; veritabanı istemcinin veriyi çekmesini ya da bir "
            "sonraki komutu göndermesini bekliyor. Bu bir veritabanı sorunu DEĞİLDİR: index "
            "eklemek, parametre değiştirmek ya da donanım büyütmek bu süreyi kısaltmaz. "
            "Bunu söylemek, ekibi haftalarca yanlış yerde arama yapmaktan kurtarır."
        ),
        steps=[
            AdviceStep(
                action=(
                    "Hangi uygulamanın beklettiğini bulun — application_name alanı doğru "
                    "ayarlanmışsa bu tek adımda çıkar."
                ),
                command=(
                    "SELECT application_name, client_addr, count(*)\n"
                    "FROM pg_stat_activity\n"
                    "WHERE wait_event_type = 'Client'\n"
                    "GROUP BY 1, 2 ORDER BY count(*) DESC;"
                ),
            ),
            AdviceStep(
                action=(
                    "Uygulama tarafında sonuç kümesinin nasıl işlendiğine bakın: satır satır "
                    "işlenip her satırda başka bir iş yapılıyorsa (N+1, ağ çağrısı, dosya "
                    "yazımı) bağlantı boşuna açık kalır."
                ),
                command=None,
            ),
            AdviceStep(
                action=(
                    "Uygulama ile veritabanı arasındaki ağ gecikmesini ölçün — farklı bölge/AZ "
                    "yerleşimi tek başına bu tabloyu üretebilir."
                ),
                command="-- Uygulama sunucusundan:\nping -c 20 <veritabanı-host>",
            ),
            AdviceStep(
                action=(
                    "Sonucun tamamı gerekmiyorsa sunucu tarafında sınırlayın: LIMIT, sayfalama "
                    "ya da toplama işini veritabanına verin."
                ),
                command=None,
            ),
        ],
        cautions=[
            "Bu tabloyu görüp veritabanı parametreleriyle oynamak zaman kaybıdır ve risk "
            "üretir — ölçüm sorunun burada olmadığını söylüyor.",
            "İstisna: uygulama sunucusu veritabanıyla aynı ağda değilse gecikme gerçek bir "
            "altyapı sorunudur ve uygulama kodunda çözülmez.",
        ],
        estimated_duration="Teşhis: dakikalar. Çözüm uygulama tarafında, geliştirme işi.",
        rollback=None,
        verification=(
            "-- Uygulama düzeltmesinden sonra Client payı düşmeli.\n"
            "SELECT wait_event_type, count(*) FROM pg_stat_activity\n"
            "WHERE state = 'active' GROUP BY 1;"
        ),
    )


def _pg_ipc_advice(share_pct: float) -> Advice:
    return Advice(
        title="Paralellik ayarlarını gözden geçirin",
        why=(
            f"Yükün %{share_pct:.0f}'i süreçler arası bekleme (IPC) — pratikte paralel plan "
            "çalıştıran sorgularda lider sürecin işçileri beklemesi. Küçük bir pay normaldir; "
            "yüksek pay ya işçilerin iş dağılımının dengesiz olduğunu ya da paralel işçi "
            "sayısının sunucunun kaldırabileceğinden fazla olduğunu gösterir. İş etkisi: "
            "paralellik hızlandırmak yerine yavaşlatmaya başlar."
        ),
        steps=[
            AdviceStep(
                action="Mevcut paralellik ayarlarını okuyun.",
                command=(
                    "SHOW max_parallel_workers_per_gather;\n"
                    "SHOW max_parallel_workers;\n"
                    "SHOW max_worker_processes;"
                ),
            ),
            AdviceStep(
                action=(
                    "Paralel işçi talebinin karşılanıp karşılanmadığına bakın — reddedilen "
                    "talepler ayarların yetersiz olduğunu gösterir."
                ),
                command="EXPLAIN (ANALYZE) <yük üreten sorgu>;  -- 'Workers Planned' ile 'Workers Launched' farkına bakın",
            ),
            AdviceStep(
                action=(
                    "Paralellik fayda etmiyorsa tek tek sorgu bazında kapatmak, global ayarı "
                    "değiştirmekten güvenlidir."
                ),
                command="SET max_parallel_workers_per_gather = 0;  -- yalnızca bu oturumda",
            ),
        ],
        cautions=[
            "max_worker_processes değişikliği YENİDEN BAŞLATMA gerektirir; diğer iki parametre "
            "reload ile uygulanır.",
            "Paralelliği global kapatmak, ondan gerçekten fayda gören raporlama sorgularını "
            "yavaşlatır — önce sorgu bazında deneyin.",
        ],
        estimated_duration="Ayar denemesi: dakikalar. Yeniden başlatma gerekiyorsa bakım penceresi.",
        rollback="SET max_parallel_workers_per_gather = DEFAULT;",
        verification="EXPLAIN (ANALYZE) <sorgu>;  -- süre ve Workers Launched karşılaştırın",
    )


def _pg_memory_advice(share_pct: float) -> Advice:
    return Advice(
        title="Bellek tahsisini artırın (work_mem / bellek beklemesi)",
        why=(
            f"Yükün %{share_pct:.0f}'i bellek tahsisi bekleyerek geçiyor. Sorgular sıralama "
            "veya hash için gereken belleği alamayınca ya bekliyor ya da işi diske taşıyor; "
            "ikisi de sürenin katlanması demek."
        ),
        steps=[
            AdviceStep(
                action="Mevcut work_mem değerini ve geçici dosya kullanımını okuyun.",
                command=(
                    "SHOW work_mem;\n"
                    "SELECT datname, temp_files, pg_size_pretty(temp_bytes) AS temp_boyut\n"
                    "FROM pg_stat_database WHERE datname = current_database();"
                ),
            ),
            AdviceStep(
                action="Oturum bazında artırıp etkisini ölçün (global değiştirmeden önce).",
                command="SET work_mem = '64MB';\nEXPLAIN (ANALYZE, BUFFERS) <sorgu>;",
            ),
        ],
        cautions=[
            "work_mem OTURUM VE İŞLEM BAŞINA ayrılır: bağlantı sayısı × işlem sayısı ile "
            "çarpın, aksi halde sunucu belleği tükenir.",
        ],
        estimated_duration="Dakikalar (reload yeterli, yeniden başlatma gerekmez).",
        rollback="SET work_mem = DEFAULT;",
        verification=(
            "SELECT temp_files, pg_size_pretty(temp_bytes) AS temp_boyut\n"
            "FROM pg_stat_database WHERE datname = current_database();"
        ),
    )


def _sqlserver_advice(category: WaitCategory, share_pct: float) -> Advice:
    """SQL Server karşılıkları. Ayrı fonksiyon: komutlar tamamen farklı ve PostgreSQL
    komutlarını SQL Server'a önermek çalışmayan bir öneri üretmek olurdu."""
    common_probe = AdviceStep(
        action="Bekleme dağılımını sunucudan doğrulayın.",
        command=(
            "SELECT TOP 10 wait_type, wait_time_ms, waiting_tasks_count\n"
            "FROM sys.dm_os_wait_stats\n"
            "WHERE wait_type NOT IN ('CLR_SEMAPHORE','SLEEP_TASK','BROKER_TASK_STOP')\n"
            "ORDER BY wait_time_ms DESC;"
        ),
    )
    if category == WaitCategory.IO:
        return Advice(
            title="Disk okumasını azaltın (index ya da bellek)",
            why=(
                f"Yükün %{share_pct:.0f}'i disk g/ç beklemesinde (PAGEIOLATCH/WRITELOG). "
                "Veri buffer pool'da bulunamıyor ya da log yazımı darboğaz; ikisi de yük "
                "arttıkça hızla kötüleşir."
            ),
            steps=[
                common_probe,
                AdviceStep(
                    action="Dosya bazında g/ç gecikmesini ölçün — sorun veri dosyasında mı log'da mı?",
                    command=(
                        "SELECT DB_NAME(vfs.database_id) AS veritabani, mf.physical_name,\n"
                        "       vfs.io_stall_read_ms / NULLIF(vfs.num_of_reads, 0) AS okuma_gecikme_ms,\n"
                        "       vfs.io_stall_write_ms / NULLIF(vfs.num_of_writes, 0) AS yazma_gecikme_ms\n"
                        "FROM sys.dm_io_virtual_file_stats(NULL, NULL) vfs\n"
                        "JOIN sys.master_files mf\n"
                        "  ON mf.database_id = vfs.database_id AND mf.file_id = vfs.file_id\n"
                        "ORDER BY okuma_gecikme_ms DESC;"
                    ),
                ),
                AdviceStep(
                    action="Eksik index önerilerini okuyun (körü körüne uygulamayın, doğrulayın).",
                    command=(
                        "SELECT TOP 10 d.statement, d.equality_columns, d.inequality_columns,\n"
                        "       d.included_columns, s.avg_user_impact\n"
                        "FROM sys.dm_db_missing_index_details d\n"
                        "JOIN sys.dm_db_missing_index_groups g ON g.index_handle = d.index_handle\n"
                        "JOIN sys.dm_db_missing_index_group_stats s ON s.group_handle = g.index_group_handle\n"
                        "ORDER BY s.avg_user_impact DESC;"
                    ),
                ),
                AdviceStep(
                    action="Bellek yetersizse Max Server Memory'yi gözden geçirin.",
                    command=(
                        "SELECT name, value_in_use FROM sys.configurations\n"
                        "WHERE name = 'max server memory (MB)';"
                    ),
                ),
            ],
            cautions=[
                "sys.dm_db_missing_index_details önerileri ÇAKIŞIR ve fazla index yazma "
                "performansını düşürür — önce mevcut index'lerle örtüşmeyi kontrol edin.",
                "ONLINE = ON olmadan index oluşturmak tabloyu kilitler (Enterprise sürüm gerekir).",
                "Max Server Memory'yi işletim sistemine yer bırakmadan ayarlamak sunucuyu "
                "takla attırır.",
            ],
            estimated_duration="Index: tablo boyutuna göre. Bellek ayarı: anında (yeniden başlatma gerekmez).",
            rollback="DROP INDEX <ad> ON <tablo>; bellek ayarını eski değerine alın.",
            verification=(
                "SELECT TOP 5 wait_type, wait_time_ms FROM sys.dm_os_wait_stats\n"
                "WHERE wait_type LIKE 'PAGEIOLATCH%' ORDER BY wait_time_ms DESC;"
            ),
        )
    if category in (WaitCategory.LOCK, WaitCategory.LWLOCK, WaitCategory.BUFFER_PIN):
        return Advice(
            title="Kilit çakışmasını çözün",
            why=(
                f"Yükün %{share_pct:.0f}'i kilit beklemesinde (LCK_M_*). Kaynak sorunu "
                "değildir: CPU ya da disk eklemek bekleyen oturumları serbest bırakmaz."
            ),
            steps=[
                common_probe,
                AdviceStep(
                    action="Kimin kimi beklettiğini görün.",
                    command=(
                        "SELECT r.session_id AS bekleyen, r.blocking_session_id AS bloklayan,\n"
                        "       r.wait_type, r.wait_time, t.text AS bekleyen_sorgu\n"
                        "FROM sys.dm_exec_requests r\n"
                        "OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t\n"
                        "WHERE r.blocking_session_id <> 0;"
                    ),
                ),
                AdviceStep(
                    action="Açık kalmış transaction var mı bakın.",
                    command=(
                        "SELECT s.session_id, s.login_name, s.status,\n"
                        "       t.transaction_begin_time, DB_NAME(tsn.database_id) AS veritabani\n"
                        "FROM sys.dm_tran_active_transactions t\n"
                        "JOIN sys.dm_tran_session_transactions tsn ON tsn.transaction_id = t.transaction_id\n"
                        "JOIN sys.dm_exec_sessions s ON s.session_id = tsn.session_id\n"
                        "ORDER BY t.transaction_begin_time;"
                    ),
                ),
                AdviceStep(
                    action=(
                        "Okuma sorguları yazarları beklemesin diye READ COMMITTED SNAPSHOT'ı "
                        "değerlendirin."
                    ),
                    command=(
                        "-- ÖNCE bakım penceresi planlayın: tek kullanıcı modu gerektirir.\n"
                        "ALTER DATABASE <veritabani> SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;"
                    ),
                ),
                AdviceStep(
                    action="Acil durumda bloklayan oturumu sonlandırın (son çare).",
                    command="KILL <session_id>;",
                ),
            ],
            cautions=[
                "READ_COMMITTED_SNAPSHOT tempdb kullanımını ARTIRIR ve satır sürümleme ekler; "
                "tempdb'nin yerini ve boyutunu önce kontrol edin.",
                "WITH ROLLBACK IMMEDIATE açık transaction'ları geri alır — bakım penceresinde "
                "yapın.",
                "KILL, bloklayan oturumun işini geri alır; yarım kalan iş kaybolur.",
            ],
            estimated_duration="Acil müdahale: saniyeler. RCSI: bakım penceresi.",
            rollback="ALTER DATABASE <veritabani> SET READ_COMMITTED_SNAPSHOT OFF WITH ROLLBACK IMMEDIATE;",
            verification=(
                "SELECT COUNT(*) AS bloklanan FROM sys.dm_exec_requests\n"
                "WHERE blocking_session_id <> 0;"
            ),
        )
    if category == WaitCategory.CPU:
        return Advice(
            title="CPU'da geçen süreyi azaltın",
            why=(
                f"Yükün %{share_pct:.0f}'i CPU'da geçiyor (SOS_SCHEDULER_YIELD dahil — bu bir "
                "bekleme gibi görünse de CPU kotasının dolduğunu, yani işlemci baskısını "
                "gösterir). CPU doyduğunda tüm istekler birlikte yavaşlar."
            ),
            steps=[
                common_probe,
                AdviceStep(
                    action="En çok CPU harcayan sorguları bulun.",
                    command=(
                        "SELECT TOP 10 qs.total_worker_time / qs.execution_count AS ortalama_cpu_us,\n"
                        "       qs.execution_count, SUBSTRING(t.text, 1, 200) AS sorgu\n"
                        "FROM sys.dm_exec_query_stats qs\n"
                        "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) t\n"
                        "ORDER BY qs.total_worker_time DESC;"
                    ),
                ),
                AdviceStep(
                    action="Planı inceleyin; tahmini ve gerçek satır sayıları çok ayrışıyorsa istatistikler eskimiştir.",
                    command="UPDATE STATISTICS <tablo>;",
                ),
            ],
            cautions=[
                "UPDATE STATISTICS okuma yükü bindirir; büyük tablolarda yoğun saatte "
                "çalıştırmayın.",
                "MAXDOP ayarını değiştirmeden önce mevcut değerini not edin.",
            ],
            estimated_duration="İstatistik güncelleme: tablo boyutuna göre dakikalar.",
            rollback="İstatistik güncellemesi geri alınmaz; MAXDOP'u eski değerine alın.",
            verification=(
                "SELECT TOP 5 total_worker_time / execution_count AS ortalama_cpu_us\n"
                "FROM sys.dm_exec_query_stats ORDER BY total_worker_time DESC;"
            ),
        )
    if category == WaitCategory.MEMORY:
        return Advice(
            title="Bellek tahsisi (memory grant) beklemesini giderin",
            why=(
                f"Yükün %{share_pct:.0f}'i RESOURCE_SEMAPHORE ailesinde: sorgular çalışmak "
                "için bellek tahsisi bekliyor. Genelde aşırı büyük tahmini bellek talebinden "
                "kaynaklanır (eski istatistik, uygun olmayan plan)."
            ),
            steps=[
                common_probe,
                AdviceStep(
                    action="Bekleyen bellek taleplerini görün.",
                    command=(
                        "SELECT session_id, requested_memory_kb, granted_memory_kb, wait_time_ms\n"
                        "FROM sys.dm_exec_query_memory_grants\n"
                        "ORDER BY wait_time_ms DESC;"
                    ),
                ),
                AdviceStep(
                    action="İstatistikleri güncelleyin — yanlış tahmin, şişmiş bellek talebinin ana sebebidir.",
                    command="UPDATE STATISTICS <tablo>;",
                ),
            ],
            cautions=["Max Server Memory'yi artırmadan önce işletim sistemine yer bıraktığınızdan emin olun."],
            estimated_duration="Dakikalar.",
            rollback="Ayar değişikliklerini eski değerine alın.",
            verification="SELECT COUNT(*) FROM sys.dm_exec_query_memory_grants WHERE wait_time_ms > 0;",
        )
    return unavailable(
        f"'{category_label(str(category))}' beklemesi için SQL Server'a özgü hazır bir eylem "
        "planı tanımlı değil. sys.dm_os_wait_stats çıktısındaki baskın wait_type'ı Microsoft "
        "belgelerinde arayarak devam edin; genel bir öneri üretmek yanlış yönlendirme olurdu.",
        title="Bu bekleme tipi için hazır plan yok",
    )


def advice_for_wait_category(
    category: str,
    share_pct: float,
    *,
    engine: str,
    top_query: str | None = None,
) -> Advice:
    """Baskın bekleme kategorisi için beş parçalı öneri.

    `share_pct` eşiğin altındaysa ya da kategori tanınmıyorsa NEDENİ yazılı bir
    `unavailable` döner — boş öneri hiçbir zaman dönmez.
    """
    try:
        wait_category = WaitCategory(category)
    except ValueError:
        return unavailable(
            f"'{category}' bilinen bir bekleme kategorisi değil; buna dayanarak eylem "
            "önerilemez.",
        )

    if share_pct < ADVICE_MIN_SHARE_PCT:
        return unavailable(
            f"En yüksek paya sahip kategori ({category_label(category)}) yükün yalnızca "
            f"%{share_pct:.0f}'ini açıklıyor. Bu orana göre eylem önermek, yükün yarısından "
            f"azını hedefleyen bir işe yönlendirmek olurdu — önce yükün daha uzun bir aralıkta "
            "nasıl dağıldığına bakın.",
            title="Tek bir baskın kaynak yok",
        )

    # Client beklemesi motordan bağımsız: cevap her iki motorda da aynı — sorun burada değil.
    if wait_category == WaitCategory.CLIENT:
        return _client_advice(share_pct)

    if engine == str(DatabaseEngine.POSTGRESQL):
        if wait_category == WaitCategory.IO:
            return _pg_io_advice(share_pct, top_query)
        if wait_category in (WaitCategory.LOCK, WaitCategory.LWLOCK):
            return _pg_lock_advice(share_pct, wait_category)
        if wait_category == WaitCategory.CPU:
            return _pg_cpu_advice(share_pct, top_query)
        if wait_category == WaitCategory.IPC:
            return _pg_ipc_advice(share_pct)
        if wait_category == WaitCategory.MEMORY:
            return _pg_memory_advice(share_pct)
        return unavailable(
            f"'{category_label(category)}' beklemesi için hazır bir eylem planı tanımlı değil. "
            "pg_stat_activity'deki wait_event değerine bakıp PostgreSQL belgelerindeki bekleme "
            "olayları tablosundan devam edin; genel bir öneri üretmek yanlış yönlendirme olurdu.",
            title="Bu bekleme tipi için hazır plan yok",
        )

    if engine == str(DatabaseEngine.SQLSERVER):
        return _sqlserver_advice(wait_category, share_pct)

    return unavailable(
        f"Bekleme tabanlı öneri bu motor için üretilmiyor ({engine}).",
    )
