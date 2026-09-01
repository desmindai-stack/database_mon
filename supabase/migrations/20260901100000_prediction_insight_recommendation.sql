-- Faz 15 İŞ 6 (feature/multi-tenant-cluster): predictions çözüm önerisi.
-- app/models.py::PredictionInsight.recommendation/.action için Supabase karşılığı — SQLite'ta
-- migrate_schema() ile eklendi ama migrate_schema() Postgres için no-op olduğundan bu iki kolon
-- Supabase'deki prediction_insights tablosuna (ilk migration'dan beri var olan bir tabloya)
-- hiç eklenmedi. create_all bunu telafi ETMEZ: tablo zaten var olduğu için checkfirst=True onu
-- atlar, sadece eksik TABLOLARI oluşturur — eksik KOLONLARI değil. Bu migration olmadan
-- predictions okuyan her sorgu Supabase'de "column recommendation does not exist" ile patlar.

ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS recommendation TEXT;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS action VARCHAR(255);
