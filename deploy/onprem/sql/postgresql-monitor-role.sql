-- dbace izleme rolü — PostgreSQL 13+ (Faz 31 Commit 8)
--
-- DBA çalıştırır (süper kullanıcı ya da CREATEROLE yetkili bir hesapla). Yalnızca OKUMA yetkisi verir.
-- Değiştirin: :'monitor_password' (psql değişkeni), <izlenen_veritabanı>, <şema>.
--   psql -v monitor_password='...' -f postgresql-monitor-role.sql
--
-- Her satırın sonundaki not: o yetki olmadan hangi özellik "ölçülemedi + gerekçe" döner.
-- Ön koşul (yetki değil, DBA kurulumu): shared_preload_libraries = 'pg_stat_statements' ve
-- izlenen veritabanında CREATE EXTENSION pg_stat_statements. İsteğe bağlı: hypopg eklentisi.

CREATE ROLE dbace_monitor LOGIN PASSWORD :'monitor_password';  -- bağlantı: dbace'in hedefe oturum açması
GRANT pg_monitor TO dbace_monitor;  -- metrikler, pg_stat_statements tam metni (pg_read_all_stats), ayarlar ve ön koşullar (pg_read_all_settings), aktivite, bloklama, replikasyon/topoloji, yedek (pg_stat_archiver)
GRANT CONNECT ON DATABASE <izlenen_veritabanı> TO dbace_monitor;  -- bağlantı: her izlenen veritabanı için tekrarlayın

-- Aşağıdakiler HER izlenen veritabanında (\c <izlenen_veritabanı>) — index önerisinin ÖLÇÜMLERİ için:
GRANT USAGE ON SCHEMA <şema> TO dbace_monitor;  -- index önerisi: tabloya erişim (katalog okuması yetkisiz de çalışır)
GRANT SELECT ON ALL TABLES IN SCHEMA <şema> TO dbace_monitor;  -- index önerisi fayda ölçümü (EXPLAIN, hypopg), istatistik denetimi (pg_stats), önce/sonra plan karşılaştırması, gerçek değerli EXPLAIN ANALYZE (READ ONLY işlemde, admin onayıyla)

-- BU DOSYA BİLEREK İÇERMEZ (bankada izleme kullanıcısı yalnızca okur):
--   SUPERUSER, CREATEDB, CREATEROLE, REPLICATION, BYPASSRLS;
--   TEMPORARY/CREATE (dbace geçici tablo ya da nesne oluşturmuyor — ifade index'i denetimi hypopg ile);
--   pg_read_server_files, pg_write_server_files, pg_execute_server_program (sunucu log'u host-agent ile okunur;
--   agent yoksa auto_explain planı ve deadlock ayrıntısı "ölçülemedi", deadlock SAYISI pg_stat_database'den);
--   INSERT, UPDATE, DELETE, TRUNCATE.
