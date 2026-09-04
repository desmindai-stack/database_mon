-- Faz 17 Ek İŞ B: standart öneri yapısı.
-- Bulgunun önerisi artık düz metin + komut listesi değil; başlık / neden / numaralı adımlar /
-- her adımın komutu / dikkat notları / tahmini süre / geri alma / doğrulama sorgusu taşıyan
-- yapılandırılmış bir nesne. Eski `recommendation` ve `commands` kolonları geriye dönük
-- uyumluluk için duruyor.

ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS advice JSONB;
