-- Faz 31 Commit 6: sunucunun ÖLÇÜLEN topolojisi.
--
-- Tek sunucuda "cluster down" alarmı çıkıyordu: topoloji ölçülmüyor, yapılandırma alanlarından
-- (cluster_name, services) tahmin ediliyordu ve probun göremediği her şey "down" sayılıyordu.
-- Toplayıcı artık her döngüde salt okunur kaynaklardan okuyor (PostgreSQL: pg_is_in_recovery,
-- pg_stat_replication, pg_stat_wal_receiver; SQL Server: IsHadrEnabled, sys.dm_hadr_*).
--
--   topology_kind            standalone | cluster | unmeasured
--   topology_state           healthy | degraded (yalnızca cluster)
--   topology_role            primary | replica
--   topology_reason          gerekçe (kullanıcıya gösteriliyor)
--   topology_required_grant  ölçülemediyse gereken yetki
--   topology_members         replika/AG üyeleri (ad, durum)
--   topology_checked_at      son tespit
--   topology_cluster_seen_at cluster'ın EN SON gözlendiği an — birincilde replika kaybolunca
--                            "tek sunucu" ile "replikası kopmuş cluster"ı ayırmak için (24 saat)

ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_kind VARCHAR(16);
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_state VARCHAR(16);
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_role VARCHAR(16);
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_reason TEXT;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_required_grant TEXT;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_members JSONB;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_checked_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_cluster_seen_at TIMESTAMPTZ;
