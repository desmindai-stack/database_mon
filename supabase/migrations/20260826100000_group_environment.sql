-- Faz 7 (feature/multi-tenant-cluster): environment ayrımı (prod/preprod/test/dev)
-- database_groups için.

ALTER TABLE database_groups ADD COLUMN IF NOT EXISTS environment VARCHAR(16) NOT NULL DEFAULT 'prod';
