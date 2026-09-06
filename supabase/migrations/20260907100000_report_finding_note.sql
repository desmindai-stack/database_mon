-- Faz 18 İŞ 3: bulgunun kısa sınırlılık notu.
-- "Darboğaz belirlenemedi çünkü CPU verisi yok" gibi bilgiler bulgu metninin içine gömülü
-- uzun bir cümle olarak yazılıyordu ve asıl bulgunun önüne geçiyordu. Artık ayrı, kısa ve
-- sönük gösterilen bir not alanı var.

ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS note VARCHAR(512);
