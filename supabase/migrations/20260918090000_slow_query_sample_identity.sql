-- Faz 31 Commit 9 (madde 0, egress): yavaş sorgu örneğinin KİMLİĞİ ve SINIFI satırda saklanıyor.
--
-- Supabase egress kotası 17 kat aşıldı. En ağır yol yavaş sorgu seçimiydi: liste, Tuning içgörüsü,
-- teşhis ve geçmiş, pencerede (24 saat) her örneği TAM KOLON çekip — sorgu metni dahil, satır başına
-- ~840 bayt — gruplama, fark ve sistem sorgusu sınıflandırmasını Python'da yapıyordu (çağrı başına
-- ~14 bin satır, arayüz 15 saniyede bir yeniliyor). Gruplama anahtarı (metin parmak izi) ve sistem
-- sorgusu sınıfı METİNDEN türüyordu; metni çekmeden SQL'de gruplamak ve saymak için ikisi satırda.
--
--   query_hash   'q:' + normalize edilmiş metnin sha256'sının ilk 24 hanesi — Python
--                slow_query_selection.query_fingerprint ile aynı (boşluklar tek boşluk, küçük harf).
--   query_class  sistem/platform sorgusu sınıflandırması (classify_system_query): '' uygulama
--                sorgusu, dolu = gerekçe etiketi, NULL = henüz sınıflandırılmadı (uygulama bu satırları
--                metin başına BİR kez sınıflandırıp toplu UPDATE ile yazıyor).
--
-- Yeni satırlarda ikisini uygulama yazıyor. Aşağıdaki UPDATE var olan satırların parmak izini SQL'de
-- hesaplıyor (sunucu içinde; egress yok). Büyük tabloda tabloyu bir kez yeniden yazar — canlıda
-- ~393 bin satır: bakım penceresinde çalıştırın. CONCURRENTLY YOK.

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS query_hash VARCHAR(40);
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS query_class TEXT;

UPDATE slow_query_samples
   SET query_hash = 'q:' || substr(encode(sha256(convert_to(
           lower(regexp_replace(btrim(query, E' \t\n\r\f\v'), E'[ \t\n\r\f\v]+', ' ', 'g')), 'UTF8')), 'hex'), 1, 24)
 WHERE query_hash IS NULL;

CREATE INDEX IF NOT EXISTS ix_slow_query_samples_instance_hash ON slow_query_samples (instance_id, query_hash);
