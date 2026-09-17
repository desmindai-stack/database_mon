-- Faz 31 Commit 4: yavaş sorgu satırının KİMDEN geldiği ve iç içe olup olmadığı.
--
-- `/* dbace */` imzası sorgu ŞEKLİNİ işaretliyor, çalıştırmayı değil. Gerçek sunucuda ölçüldü
-- (PG 15/16/17): dbace ile uygulama farklı rollerdeyse aynı queryid pg_stat_statements'ta iki
-- AYRI satır; dbace'in yavaş sorgu seçimi ise satırları yalnızca queryid ile birleştiriyordu —
-- dbace'in imzalı satırının metni uygulama yükünü "sistem sorgusu" diye gizleyebiliyordu.
--
-- from_monitoring_role: pg_stat_statements.userid, toplayıcının bağlandığı rol mü. İmzalı metin
--   ama FALSE ise satır filtrelenmiyor, işaretleniyor. NULL = bilinmiyor (eski satır).
-- toplevel: pg_stat_statements.toplevel (PG 14+). Üst düzey ve iç içe satırlar ayrı sayaç.

ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS from_monitoring_role BOOLEAN;
ALTER TABLE slow_query_samples ADD COLUMN IF NOT EXISTS toplevel BOOLEAN;
