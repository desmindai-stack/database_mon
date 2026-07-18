-- pgwatch control plane schema for Supabase (PostgreSQL)
-- Run via Supabase SQL editor or: supabase db push

CREATE TABLE IF NOT EXISTS instances (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(128) NOT NULL UNIQUE,
  engine VARCHAR(32) NOT NULL DEFAULT 'postgresql',
  host VARCHAR(255) NOT NULL,
  port INTEGER NOT NULL DEFAULT 5432,
  database VARCHAR(128) NOT NULL DEFAULT 'postgres',
  username VARCHAR(128) NOT NULL,
  password VARCHAR(512) NOT NULL,
  options JSONB,
  customer_name VARCHAR(128),
  environment VARCHAR(32) NOT NULL DEFAULT 'public',
  application VARCHAR(128),
  cluster_name VARCHAR(128),
  role VARCHAR(32),
  services JSONB,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS metric_samples (
  id BIGSERIAL PRIMARY KEY,
  instance_id BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metrics_json JSONB,
  active_connections INTEGER NOT NULL DEFAULT 0,
  max_connections INTEGER NOT NULL DEFAULT 0,
  transactions_per_sec DOUBLE PRECISION NOT NULL DEFAULT 0,
  cache_hit_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
  replication_lag_bytes DOUBLE PRECISION,
  database_size_bytes DOUBLE PRECISION NOT NULL DEFAULT 0,
  deadlocks INTEGER NOT NULL DEFAULT 0,
  temp_bytes DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_metric_samples_instance_time
  ON metric_samples (instance_id, collected_at DESC);

CREATE TABLE IF NOT EXISTS slow_query_samples (
  id BIGSERIAL PRIMARY KEY,
  instance_id BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  queryid VARCHAR(64),
  query TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  total_time_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
  mean_time_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
  rows INTEGER NOT NULL DEFAULT 0,
  shared_blks_hit INTEGER,
  shared_blks_read INTEGER,
  local_blks_hit INTEGER,
  local_blks_read INTEGER,
  temp_blks_read INTEGER,
  temp_blks_written INTEGER,
  plan_user_time DOUBLE PRECISION,
  plan_sys_time DOUBLE PRECISION,
  exec_user_time DOUBLE PRECISION,
  exec_sys_time DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS alert_rules (
  id BIGSERIAL PRIMARY KEY,
  instance_id BIGINT REFERENCES instances(id) ON DELETE CASCADE,
  name VARCHAR(128) NOT NULL,
  metric VARCHAR(64) NOT NULL,
  operator VARCHAR(8) NOT NULL,
  threshold DOUBLE PRECISION NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alert_events (
  id BIGSERIAL PRIMARY KEY,
  rule_id BIGINT NOT NULL REFERENCES alert_rules(id) ON DELETE CASCADE,
  instance_id BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  metric_value DOUBLE PRECISION NOT NULL,
  message TEXT NOT NULL,
  triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS prediction_insights (
  id BIGSERIAL PRIMARY KEY,
  instance_id BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  metric_key VARCHAR(64) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  horizon_minutes INTEGER NOT NULL DEFAULT 60,
  current_value DOUBLE PRECISION NOT NULL,
  predicted_value DOUBLE PRECISION NOT NULL,
  threshold DOUBLE PRECISION NOT NULL,
  confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
  severity VARCHAR(16) NOT NULL DEFAULT 'info',
  message TEXT NOT NULL,
  acknowledged_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_prediction_open
  ON prediction_insights (instance_id, acknowledged_at);

-- Optional: enable RLS when Supabase Auth is wired (dashboard users)
-- ALTER TABLE instances ENABLE ROW LEVEL SECURITY;
