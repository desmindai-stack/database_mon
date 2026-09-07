-- CANLI 502 DÜZELTMESİ: metric_samples / slow_query_samples bileşik indeksleri.
--
-- Belirti: GET /api/instances/{id}/insights Railway'de 502 Bad Gateway (ve yan etki olarak
-- CORS hatası — 502'de CORS başlığı eklenmiyor).
--
-- Sebep: bu uç ve benzerleri sürekli şu soruyu soruyor:
--     SELECT ... FROM <tablo> WHERE instance_id = ? ORDER BY collected_at DESC LIMIT 1
-- Tabloda yalnızca AYRI AYRI `instance_id` ve `collected_at` indeksleri vardı. Planlayıcının
-- iki seçeneği de kötü:
--   (a) instance_id indeksiyle o instance'ın TÜM satırlarını çekip sıralamak,
--   (b) collected_at indeksini sondan tarayıp instance_id ile elemek — veri göndermeyi
--       durdurmuş bir instance için bu, tablonun tamamını taramaya dönüşür.
-- 15 saniyelik toplama aralığında slow_query_samples'a döngü başına 20 satır yazılıyor:
-- instance başına ayda ~3.5 milyon satır. Sorgu dakikalarca asılı kalıyor, gateway 502 veriyor.
--
-- ⚠️ ÖNEMLİ — BU DOSYA SQL EDITOR'DEN ÇALIŞTIRILAMAZ.
--
-- `CREATE INDEX CONCURRENTLY` bir transaction bloğunun İÇİNDE çalışmaz. Supabase SQL Editor
-- her gönderimi transaction'a sardığı için oradan denemek şu hatayı verir:
--     ERROR: CREATE INDEX CONCURRENTLY cannot run inside a transaction block
-- Satırları tek tek göndermek bunu ÇÖZMEZ (sorun satır sayısı değil, editörün sarmalaması);
-- `supabase db push` de aynı sebeple çalışmaz.
--
-- Doğru yol psql ile, her komut AYRI bir `-c` çağrısı olarak — tam komutlar ve doğrulama için
-- DEPLOY.md'deki "CONCURRENTLY kullanan migration'lar" bölümüne bakın:
--     psql "$DBACE_DB" -c "CREATE INDEX CONCURRENTLY IF NOT EXISTS ... ;"
--
-- CONCURRENTLY tercih edilme sebebi: tablolar milyonlarca satır ve normal `CREATE INDEX`
-- tamamlanana kadar tabloya YAZMAYI KİLİTLER, yani toplama döngüsü durur.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_metric_samples_instance_collected
    ON metric_samples (instance_id, collected_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_slow_query_samples_instance_collected
    ON slow_query_samples (instance_id, collected_at);

-- Alternatif, KİLİTLEYEN sürüm — yalnızca tablo küçükse ya da planlı bakım penceresinde:
--   CREATE INDEX IF NOT EXISTS ix_metric_samples_instance_collected
--       ON metric_samples (instance_id, collected_at);
--   CREATE INDEX IF NOT EXISTS ix_slow_query_samples_instance_collected
--       ON slow_query_samples (instance_id, collected_at);
--
-- Doğrulama (indeksin gerçekten kullanıldığını görmek için):
--   EXPLAIN ANALYZE SELECT collected_at FROM slow_query_samples
--   WHERE instance_id = <id> ORDER BY collected_at DESC LIMIT 1;
-- Planda "Index Only Scan using ix_slow_query_samples_instance_collected" görünmeli;
-- "Sort" ya da "Seq Scan" görünüyorsa indeks oluşmamış demektir.
