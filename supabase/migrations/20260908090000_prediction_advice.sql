-- Faz 20 İŞ 1: tahminler için beş parçalı standart öneri.
-- `PredictionOut.advice` Faz 17 Ek İŞ B'de şemaya eklenmişti ama `prediction_insights`
-- tablosunda karşılığı yoktu — API her tahmin için `advice: null` dönüyor, arayüz standart
-- öneri kartı yerine tek cümlelik `recommendation` metnine düşüyordu. Kolon, öneriyi
-- üretildiği anda saklıyor ki tahmin geçmişi kendi önerisini taşısın.

ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS advice JSONB;
