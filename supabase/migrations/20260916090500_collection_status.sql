-- Faz 30 İŞ 1: veritabanı başına toplama durumu.
--
-- Toplama döngüsü bütün veritabanları için tek oturum ve sonda tek commit kullanıyordu; bir
-- veritabanındaki hata turun tamamını düşürüyordu. Artık her veritabanı kendi oturumunda ve
-- kendi commit'inde toplanıyor — ve hata SESSİZCE GEÇİLMİYOR, buraya yazılıyor.
--
-- "Son başarılı toplama" ayrı bir alan: son DENEME zamanıyla karıştırılırsa, üç gündür hata
-- veren bir veritabanı "az önce toplandı" gibi görünürdü.
--
-- last_collect_error_kind iki değer alıyor:
--   'instance' — o veritabanına özel (bağlantı, yetki, hedefteki view).
--   'schema'   — kod kendi şemamızla uyumsuz; tek bir veritabanının değil KURULUMUN sorunu
--                (çalıştırılmamış migration). Arayüzde tek tek hata değil, tek bir sistemik
--                uyarı olarak gösteriliyor.

ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_collect_ok_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_collect_error TEXT;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_collect_error_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS last_collect_error_kind VARCHAR(32);
