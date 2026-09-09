-- Faz 28 İŞ 1b: sondaya recovery model bilgisi.
--
-- İŞ 1a'da SQL Server collector'ı recovery model + son tam/log yedeği bilgisini zaten
-- topluyordu ama saklanacak yer yoktu ve veri atılıyordu. Değerlendirme aşaması bu bilgiye
-- ihtiyaç duyuyor: FULL recovery model'de log yedeği alınmıyorsa transaction log sınırsız
-- büyür ve diski doldurur — SQL Server'da en sık görülen "disk doldu" sebebi.
--
-- Yedek KAYITLARINDAN türetilemez: bilgi sunucu yapılandırmasında duruyor, yedek geçmişinde
-- değil. Log yedeği hiç alınmamış bir veritabanının geçmişinde hiç satır olmaz — yani
-- "kayıt yok" durumunun kendisi burada anlamlı ve ancak yapılandırmayla birlikte okunabilir.

ALTER TABLE backup_probes ADD COLUMN IF NOT EXISTS recovery_models JSONB;
