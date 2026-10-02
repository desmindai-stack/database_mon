-- Faz 32 Commit 12c: tek ölçümle "cluster bozuk" alarmı YOK — ardışık bozulma bir süre (varsayılan
-- 60 sn, server_topology.py::DEGRADED_CONFIRM_AFTER) sürerse alarm üretiliyor. Gerçek bir PostgreSQL
-- sunucusunda doğrudan ölçülen, ağır yazma yükü altında kendiliğinden-iyileşen 1-4 sn'lik geçici
-- wal_receiver boşalmaları (Faz 31 Commit 10g) YANLIŞLIKLA alarm üretmesin diye.
--
--   topology_degraded_since             mevcut bozulma serisinin BAŞLADIĞI an (iyileşince NULL)
--   topology_transient_disconnect_count eşiğe ulaşmadan kendiliğinden iyileşen (alarm üretmeyen)
--                                        bozulma serilerinin SAYISI — DBA'ya görünür, kalıcı sayaç

ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_degraded_since TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS topology_transient_disconnect_count INTEGER NOT NULL DEFAULT 0;
