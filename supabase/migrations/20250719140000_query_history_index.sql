-- Speed up query history lookups by queryid over time
--
-- Faz 31 Commit 10b: CONCURRENTLY. slow_query_samples büyük bir tablo (canlıda ~393 bin satır); düz CREATE INDEX
-- index kurulurken tabloyu YAZMAYA kapatır (toplayıcı 15 saniyede bir yazıyor). Yarıda kesilirse geçersiz index
-- kalır: `python -m app.migrations_runner` bunu kendisi temizler; elle uygularken DEPLOY.md'deki kontrol sorgusu.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_slow_query_samples_instance_queryid_collected
  ON slow_query_samples (instance_id, queryid, collected_at DESC);
