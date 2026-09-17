-- Faz 29 İŞ 2c: motordan bağımsız sorgu metrikleri.
--
-- SQL Server tarafında `sys.dm_exec_query_stats` yalnızca 5 sütunla okunuyordu ve
-- `total_time_ms` alanına `total_worker_time` (CPU süresi) yazılıyordu. `total_elapsed_time`
-- ise duvar saatidir; ikisinin FARKI BEKLEMEDİR (kilit, I/O, ağ).
--
-- Yani "yavaş sorgu" listesi aslında "CPU yiyen sorgu" listesiydi: kilitte 10 saniye bekleyip
-- 5 ms CPU kullanan bir sorgu listede HIZLI görünüyordu — oysa kullanıcının şikâyet ettiği
-- tam olarak odur. Artık total_time_ms = elapsed ve CPU ayrı sütunda.
--
-- Alan adları motora değil KAVRAMA göre: "CPU süresi" her iki motorda da aynı şeyi soruyor.
-- PostgreSQL'de pg_stat_kcache kuruluysa cpu_time_ms oradan da dolabilir.

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS cpu_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS logical_reads BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS physical_reads BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS logical_writes BIGINT;

-- tempdb'ye taşma (SQL Server) — PostgreSQL'deki temp_blks_* karşılığı.
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS spills BIGINT;

-- Bellek izni: talep edilen ile kullanılan arasındaki fark, sorgunun gereğinden fazla bellek
-- rezerve edip diğer sorguları beklettiğini gösterir.
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS grant_kb BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS used_grant_kb BIGINT;
