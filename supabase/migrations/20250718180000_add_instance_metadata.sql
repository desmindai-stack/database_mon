-- Add customer/environment/application/cluster/role/services metadata to existing instances table
-- Run this in Supabase SQL Editor if you already created the tables with the previous migration.

ALTER TABLE instances
  ADD COLUMN IF NOT EXISTS customer_name VARCHAR(128),
  ADD COLUMN IF NOT EXISTS environment VARCHAR(32) NOT NULL DEFAULT 'public',
  ADD COLUMN IF NOT EXISTS application VARCHAR(128),
  ADD COLUMN IF NOT EXISTS cluster_name VARCHAR(128),
  ADD COLUMN IF NOT EXISTS role VARCHAR(32),
  ADD COLUMN IF NOT EXISTS services JSONB;
