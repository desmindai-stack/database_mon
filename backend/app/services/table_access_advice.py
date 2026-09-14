"""Tablo erişim sinyalleri için beş parçalı öneriler (Faz 29 İŞ 2b).

Sinyal "şu durum var" diyor; öneri "ne yapacaksın" diyor. İkisi ayrı modülde çünkü sinyalin
kendisi saf bir ölçüm (domain/table_access.py), öneri ise motora ve sürüme bağlı komutlar
içeriyor.

Bu bölümdeki önerilerin ortak bir zorluğu var: **dbace hangi kolona index gerektiğini tablo
istatistiklerinden bilemez.** `seq_scan` sayacı hangi WHERE koşuluyla tarandığını taşımıyor.
Bu yüzden öneri "şu index'i kur" demiyor — "index önerisi ekranına git, o sorgu bazında
çalışıyor" diyor. Bilinmeyen bir şeyi biliyormuş gibi sunmak, yanlış index kurdurmaktan
başka işe yaramaz.
"""

from __future__ import annotations

from app.services.advice import Advice, AdviceStep

_QUALIFIED = '"{schema}"."{table}"'


def _table(signal_evidence: dict, schema: str, table: str) -> str:
    return _QUALIFIED.format(schema=schema, table=table)


def advice_for_signal(key: str, schema: str, table: str, evidence: dict) -> Advice:
    """Sinyal anahtarına göre beş parçalı öneri."""
    qualified = _table(evidence, schema, table)

    if key == "seq_scan_dominant":
        rows = evidence.get("value")
        return Advice(
            title=f"{schema}.{table} için index ihtiyacını sorgu bazında inceleyin",
            why=(
                f"Tabloya yapılan erişimlerin çoğu index kullanmıyor ve her taramada ortalama "
                f"{rows:,.0f} satır okunuyor. ".replace(",", ".")
                + "Bu maliyet tablo büyüdükçe DOĞRUSAL artar: bugün kabul edilebilir olan "
                "süre, veri iki katına çıktığında iki katına çıkar. Uygun bir index aynı "
                "işi sabit maliyete indirir."
            ),
            steps=[
                AdviceStep(
                    "Bu tabloya dokunan en pahalı sorguları listeleyin — index kararı sorgunun "
                    "WHERE/ORDER BY koşuluna göre verilir, tablo sayacına göre değil.",
                    "SELECT queryid, calls, mean_exec_time, query\n"
                    "FROM pg_stat_statements\n"
                    f"WHERE query ILIKE '%{table}%'\n"
                    "ORDER BY total_exec_time DESC LIMIT 10;",
                ),
                AdviceStep(
                    "dbace'in index önerisi ekranını kullanın (Veritabanı → Sorgular → index "
                    "önerisi): orada öneri, sorgunun gerçek planı ve hypopg ile ölçülmüş "
                    "fayda tahminiyle birlikte geliyor."
                ),
                AdviceStep(
                    "Sıralı tarama BEKLENEN olabilir: tablo raporlama amaçlı tam taranıyorsa "
                    "index eklemek fayda etmez, hatta yazma maliyetini artırır. Bu durumda "
                    "bulguyu yoksayın."
                ),
            ],
            cautions=[
                "Index kurmadan önce fayda tahminine bakın: her index yazma maliyeti ve disk "
                "getirir; kullanılmayan bir index zarardır.",
                "Üretimde `CREATE INDEX CONCURRENTLY` kullanın — normal CREATE INDEX tabloyu "
                "yazmalara karşı kilitler.",
            ],
            estimated_duration="Index oluşturma tablo boyutuna göre dakikalar.",
            rollback=f"DROP INDEX CONCURRENTLY <index_adı>;",
            verification=(
                "SELECT seq_scan, idx_scan FROM pg_stat_user_tables\n"
                f"WHERE schemaname = '{schema}' AND relname = '{table}';\n"
                "-- idx_scan artmalı, seq_scan sabitlenmeli."
            ),
        )

    if key == "low_table_cache_hit":
        return Advice(
            title=f"{schema}.{table} için bellek/erişim dengesini gözden geçirin",
            why=(
                f"Tablo bloklarının %{evidence.get('value')}'i bellekten karşılanamadı ve "
                "diskten okundu. Disk erişimi bellekten kat kat yavaştır; bu oran kalıcıysa "
                "sorgu süreleri doğrudan diskin hızına bağlı hale gelir."
            ),
            steps=[
                AdviceStep(
                    "Önce sorguların gereğinden fazla veri okuyup okumadığına bakın: düşük "
                    "cache isabetinin en sık sebebi yetersiz bellek değil, index eksikliği "
                    "yüzünden gereksiz yere okunan bloklardır."
                ),
                AdviceStep(
                    "Sunucunun genel cache isabetini ve `shared_buffers` değerini kontrol edin.",
                    "SELECT name, setting, unit FROM pg_settings\n"
                    "WHERE name IN ('shared_buffers', 'effective_cache_size');",
                ),
                AdviceStep(
                    "Tek seferlik büyük bir tarama da bu görüntüyü verir (ör. gece raporu). "
                    "Kalıcı olup olmadığını birkaç gün izleyin."
                ),
            ],
            cautions=[
                "`shared_buffers` değerini artırmak yeniden başlatma gerektirir ve sunucu "
                "RAM'inin ~%25'ini aşmak genelde fayda etmez — işletim sistemi cache'i de "
                "aynı veriyi tutuyor.",
            ],
            estimated_duration="İnceleme dakikalar; ayar değişikliği bakım penceresi ister.",
            rollback="ALTER SYSTEM RESET shared_buffers; ve yeniden başlatma.",
            verification=(
                "SELECT heap_blks_hit, heap_blks_read FROM pg_statio_user_tables\n"
                f"WHERE schemaname = '{schema}' AND relname = '{table}';"
            ),
        )

    if key == "low_hot_update_ratio":
        return Advice(
            title=f"{schema}.{table} üzerindeki yazma amplifikasyonunu azaltın",
            why=(
                f"Güncellemelerin yalnızca %{evidence.get('value')}'i HOT olarak yapılabildi; "
                "kalanı tablodaki TÜM index'leri de yeniden yazdı. Bu, sorgu listesinde "
                "GÖRÜNMEYEN bir yazma yüküdür: bir satırlık güncelleme, index sayısı kadar "
                "ek yazma ve o kadar ek WAL üretir."
            ),
            steps=[
                AdviceStep(
                    "Kullanılmayan index'leri silin — her biri her güncellemeyi pahalılaştırıyor. "
                    "Şema sağlığı ekranındaki 'kullanılmayan index' listesi bunu veriyor."
                ),
                AdviceStep(
                    "Güncellenen kolonun index'li olup olmadığını kontrol edin: index'li bir "
                    "kolonu güncellemek HOT'u imkânsız kılar.",
                    f"SELECT indexdef FROM pg_indexes\n"
                    f"WHERE schemaname = '{schema}' AND tablename = '{table}';",
                ),
                AdviceStep(
                    "Sayfalarda HOT için yer bırakın (sık güncellenen tablolarda fillfactor "
                    "düşürmek doğrudan HOT oranını artırır).",
                    f'ALTER TABLE "{schema}"."{table}" SET (fillfactor = 85);\n'
                    f'VACUUM FULL "{schema}"."{table}";  -- yeni fillfactor mevcut sayfalara '
                    "ancak yeniden yazılınca uygulanır",
                ),
            ],
            cautions=[
                "`VACUUM FULL` tabloyu ACCESS EXCLUSIVE kilitler ve tablo boyutu kadar geçici "
                "disk ister — bakım penceresi şart. Kilitsiz alternatif için pg_repack.",
                "Fillfactor düşürmek tabloyu büyütür: %85 ile tablo ~%18 daha fazla yer kaplar. "
                "Yazma kazancı bu maliyeti karşılamıyorsa yapmayın.",
            ],
            estimated_duration="Ayar anlık; yeniden yazma tablo boyutuna göre.",
            rollback=f'ALTER TABLE "{schema}"."{table}" SET (fillfactor = 100);',
            verification=(
                "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_user_tables\n"
                f"WHERE schemaname = '{schema}' AND relname = '{table}';\n"
                "-- hot_upd / upd oranı yükselmeli."
            ),
        )

    if key == "stale_statistics":
        return Advice(
            title=f"{schema}.{table} istatistiklerini güncelleyin",
            why=(
                f"Son ANALYZE'dan beri canlı satır sayısının %{evidence.get('value')}'i kadar "
                "değişiklik yapıldı. Planlayıcı kararlarını istatistiklere göre veriyor; "
                "eskimiş istatistik yanlış plan seçimine yol açar ve aynı sorgu bir gün hızlı, "
                "ertesi gün kat kat yavaş çalışır — sebebi sorguda aranır, oysa sorgu değişmemiştir."
            ),
            steps=[
                AdviceStep(
                    "İstatistikleri hemen güncelleyin.",
                    f'ANALYZE "{schema}"."{table}";',
                ),
                AdviceStep(
                    "Tekrar etmemesi için bu tabloya özel autovacuum eşiği verin: varsayılan "
                    "eşik (%10 + 50 satır) çok yazılan büyük tablolarda geç tetikleniyor.",
                    f'ALTER TABLE "{schema}"."{table}"\n'
                    "  SET (autovacuum_analyze_scale_factor = 0.02, "
                    "autovacuum_analyze_threshold = 1000);",
                ),
                AdviceStep(
                    "Dağılımı çarpık bir kolon varsa istatistik hedefini artırın — varsayılan "
                    "100 kova, çok değerli kolonlarda yetersiz kalır.",
                    f'ALTER TABLE "{schema}"."{table}" ALTER COLUMN <kolon> SET STATISTICS 500;\n'
                    f'ANALYZE "{schema}"."{table}";',
                ),
            ],
            cautions=[
                "ANALYZE tabloyu kilitlemez ama I/O üretir; çok büyük tablolarda yoğun saat "
                "dışında çalıştırın.",
                "Autovacuum eşiğini düşürmek analiz sıklığını artırır; bu da CPU/I/O demek. "
                "Çok sayıda tabloya uygulamadan önce etkisini izleyin.",
            ],
            estimated_duration="ANALYZE genelde saniyeler-dakikalar.",
            rollback=(
                f'ALTER TABLE "{schema}"."{table}"\n'
                "  RESET (autovacuum_analyze_scale_factor, autovacuum_analyze_threshold);"
            ),
            verification=(
                "SELECT last_analyze, last_autoanalyze, n_mod_since_analyze\n"
                "FROM pg_stat_user_tables\n"
                f"WHERE schemaname = '{schema}' AND relname = '{table}';"
            ),
        )

    # Bilinmeyen sinyal: öneri UYDURULMUYOR, neden verilemediği yazılıyor.
    return Advice(
        title="Öneri üretilemedi",
        unavailable_reason=(
            f"'{key}' sinyali için tanımlı bir öneri yok. Bu bir hata; sinyal eklenirken "
            "önerisi de eklenmeliydi."
        ),
    )
