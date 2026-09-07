-- Faz 20 İŞ 3: tahmin yöntemi şeffaflığı ve kalite işaretleri.
--
-- Öncesinde tahminde yalnızca `confidence` (regresyonun R²'si) vardı. Bu, modelin GEÇMİŞ
-- veriye oturma iyiliğini söyler; verinin doğrusal bir modele UYUP uymadığını söylemez. Üstel
-- büyüyen bir seri de, tek bir sıçramayla bozulmuş bir seri de yüksek "güven" gösterebiliyordu.
-- Bu kolonlar tahminin neye dayandığını açık ediyor: hangi model, kaç ölçüm, hangi dönem, kaç
-- aykırı ölçüm çıkarıldı, veri doğrusal mı — ve tarih tek nokta yerine aralık olarak.

ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS method VARCHAR(128);
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS sample_count INTEGER;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS span_days DOUBLE PRECISION;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS outliers_removed INTEGER;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS fit_kind VARCHAR(16);
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS fit_note TEXT;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS eta_days_min DOUBLE PRECISION;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS eta_days_max DOUBLE PRECISION;
