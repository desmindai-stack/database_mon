-- Faz 31 İŞ 2: gerçek değerli temsili sorgu örneği (bekleme örnekleyicisi, pg_stat_activity).
--
-- Plan kaynağı önceliklendirmesinin 2. basamağı: auto_explain planı yoksa, bu örnekle
-- EXPLAIN ANALYZE teklif ediliyor (salt-okunur işlem, zaman aşımı, geri alma; yalnızca admin).
--
-- GİZLİLİK: örnek metin uygulamanın gömdüğü gerçek değerleri (kimlik no, e-posta, tutar)
-- içerebilir. Yazılması `analysis_store_real_query_samples` ayarına bağlı ve ayar VARSAYILAN
-- KAPALI; kapatıldığında mevcut örnekler siliniyor. `query_text` artık HER ZAMAN değerlerden
-- arındırılmış yazılıyor (yük kırılımında ve teknik raporda görünüyor). Worker ilk yazımında,
-- bu migration'dan önce saklanmış ham `query_text` değerlerini de arındırıyor.
-- Gerekçe: SORULAR.md.
--
-- seen_bind_parameters: örnekleyici sorguyu bind parametreli ($1) gördü mü — değer içermeyen
-- bir işaret; "neden örnek yok" sorusunun cevabı için.
--
-- sample_duration_ms: örneğin alındığı andaki çalışma süresi — queryid başına EN YAVAŞ
-- çalıştırma saklanıyor.

ALTER TABLE wait_query_signatures ADD COLUMN IF NOT EXISTS sample_query_text TEXT;
ALTER TABLE wait_query_signatures ADD COLUMN IF NOT EXISTS sample_duration_ms DOUBLE PRECISION;
ALTER TABLE wait_query_signatures ADD COLUMN IF NOT EXISTS sample_captured_at TIMESTAMPTZ;
ALTER TABLE wait_query_signatures ADD COLUMN IF NOT EXISTS seen_bind_parameters BOOLEAN NOT NULL DEFAULT FALSE;
