-- Faz 31 Commit 10c-B: bekleme örnekleyicisinin (wait_sampling.py) bağlantı durumu — ana toplama döngüsünden ayrı.
--
-- "Bu aralıkta örnek yok" iki farklı şeyi karıştırıyordu: örnekleyici bağlanamıyor/zaman aşımına uğruyor/yetkisiz
-- (hiçbir şey ölçülemedi) ile örnekleyici sağlıklı ve gerçekten aktif oturum görmedi. Yalnızca durum değişiminde
-- yazılır (arıza başlangıcı / toparlanma); nullable kolonlar, instances küçük bir tablo, backfill yok. CONCURRENTLY YOK.
ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_sample_ok_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_sample_error TEXT;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_sample_error_at TIMESTAMPTZ;
