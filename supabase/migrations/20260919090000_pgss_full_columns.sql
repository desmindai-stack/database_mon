-- Faz 29 İŞ 2a: pg_stat_statements'ın kullanılmayan sütunları.
--
-- Önceden 12 sütun okunuyordu; tanı için en değerli olanlar dışarıda kalmıştı. En önemlisi
-- I/O SÜRESİ: `blk_read_time` sorgunun diskte beklediği süreyi DOĞRUDAN ölçüyor. Onsuz
-- "bu sorgu I/O mu bekliyor, CPU mu yakıyor" sorusu blok SAYILARINDAN çıkarımla
-- cevaplanıyordu — ölçüm varken çıkarım yapmak, yanlış teşhis riskini boşuna almak.
--
-- DEPOLAMA: bu tabloya toplama döngüsü başına 20 satır yazılıyor (~3,5 milyon satır/ay/
-- instance). 16 yeni sayısal sütun kabaca +400 MB/ay/instance demek; saklama süresi
-- (retention) bu büyümeyi sınırlıyor. Karşılığı, tanının ölçüme dayanması.
--
-- BIGINT kullanılıyor: bunlar kümülatif sayaçlar ve uzun çalışan bir sunucuda int32
-- sınırını (2,1 milyar) aşarlar.

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS stddev_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS min_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS max_time_ms DOUBLE PRECISION;

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS shared_blks_dirtied BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS shared_blks_written BIGINT;

-- track_io_timing = on gerektirir. Kapalıyken 0 gelir ve bu "I/O yok" DEĞİL "ölçülmedi"
-- demektir; ayrım arayüzde korunuyor.
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS blk_read_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS blk_write_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS temp_blk_read_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS temp_blk_write_time_ms DOUBLE PRECISION;

-- wal_fpi ayrı: FPI ağırlıklı bir sorgu checkpoint aralığının çok sık olduğunu gösterir,
-- bu WAL hacminden anlaşılmaz.
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS wal_records BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS wal_fpi BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS wal_bytes DOUBLE PRECISION;

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS plans BIGINT;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS total_plan_time_ms DOUBLE PRECISION;

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS jit_time_ms DOUBLE PRECISION;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS jit_functions BIGINT;
