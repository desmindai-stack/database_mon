ALTER TABLE instances ADD COLUMN IF NOT EXISTS server_version VARCHAR(255);
ALTER TABLE instances ADD COLUMN IF NOT EXISTS unsupported_metrics JSONB;
