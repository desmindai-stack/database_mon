-- Faz 27 İŞ 4/İŞ 5: metrik kaynağı ve sunucu sürüm numarası.
--
-- `metric_sources`: hangi metriğin hangi sistem view'ından alındığı. Aynı metrik sürüme göre
-- FARKLI kaynaktan gelebiliyor — `buffers_backend` PostgreSQL 17'den itibaren
-- `pg_stat_bgwriter` yerine `pg_stat_io`'dan alınıyor — ve kullanıcı ekrandaki sayının
-- nereden geldiğini bilmeli. Öncesinde ekranda "bu metriğin karşılığı yok" yazıyordu; oysa
-- karşılığı vardı, sadece başka bir view'daydı.
--
-- `server_version_num`: sürüm NUMARASI (170004). Metin sürüm ("PostgreSQL 17.4 on x86_64...")
-- karşılaştırma için elverişsiz. Sürüme bağlı her karar (yetenek matrisi, ön koşul
-- kontrolleri, EXPLAIN stratejisi) bu sayıya bakıyor ve her seferinde sunucuya yeniden
-- sormak gereksiz bir round trip demek.

ALTER TABLE instances ADD COLUMN IF NOT EXISTS metric_sources JSONB;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS server_version_num INTEGER;
