-- dbace izleme login'i — SQL Server 2016+ (Faz 31 Commit 8)
--
-- DBA çalıştırır. Yalnızca OKUMA yetkisi verir. Değiştirin: <güçlü_parola>, <izlenen_veritabanı>.
-- Her satırın sonundaki not: o yetki olmadan hangi özellik "ölçülemedi + gerekçe" döner.

CREATE LOGIN [dbace_monitor] WITH PASSWORD = N'<güçlü_parola>', CHECK_POLICY = ON;  -- bağlantı: dbace'in sunucuya oturum açması
GRANT VIEW SERVER STATE TO [dbace_monitor];  -- DMV'ler: oturum/istek/kilit, sorgu istatistikleri, bekleme, performans sayaçları, Always On topolojisi (sys.dm_hadr_*), deadlock (system_health)

USE [master];
CREATE USER [dbace_monitor] FOR LOGIN [dbace_monitor];  -- master'daki sistem görünümleri
GRANT VIEW DATABASE STATE TO [dbace_monitor];  -- master veritabanı DMV'leri

USE [msdb];
CREATE USER [dbace_monitor] FOR LOGIN [dbace_monitor];  -- yedek geçmişi: msdb.dbo.backupset / backupmediafamily (public'e açık, ölçüldü)

USE [<izlenen_veritabanı>];
CREATE USER [dbace_monitor] FOR LOGIN [dbace_monitor];  -- bağlantı: her izlenen veritabanı için tekrarlayın
GRANT VIEW DATABASE STATE TO [dbace_monitor];  -- index kullanımı, eksik index önerileri, dosya/alan DMV'leri, Query Store durumu

-- BU DOSYA BİLEREK İÇERMEZ (bankada izleme kullanıcısı yalnızca okur):
--   sysadmin, db_owner, CONTROL SERVER, ALTER ANY ..., db_datawriter;
--   SQLAgentReaderRole: dbace msdb.dbo.sysjobs / sysjobhistory OKUMUYOR; bu tablolar yalnızca yedek önerisinin
--   DBA'nın KENDİ yetkisiyle çalıştıracağı komutlarında geçiyor.
