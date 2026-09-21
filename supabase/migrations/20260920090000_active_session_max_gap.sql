-- Faz 31 Commit 10a: bekleme örnekleyicisinin ÖLÇÜLEN en uzun örnekleme boşluğu (ms), dakika başına.
--
-- Neden: `samples_taken` bir dakikada kaç örnek alındığını söylüyor ama örneklerin ARALIĞINI söylemiyor. 60 örnek
-- ilk 30 saniyede toplanıp sonraki 30 saniye boş kalmış olabilir; AAS ortalaması aynı görünür, oysa o yarım dakikada
-- ne olduğu hiç ölçülmemiştir. En uzun boşluk bu düzensizliği görünür kılıyor; ekran "örnekleme aralığı
-- tutturulamadı" uyarısını buradan üretiyor.
--
-- NULL = bu satır kolon eklenmeden önce yazıldı (boşluk ölçülmemiş); 0 değil — "ölçüm yok" ile "boşluk yok" farklı.
-- Idempotent: elle çalıştırılsa da, paketin başlangıçta uyguladığı sırayla çalışsa da aynı sonuç.
ALTER TABLE active_session_minutes ADD COLUMN IF NOT EXISTS max_gap_ms INTEGER;
