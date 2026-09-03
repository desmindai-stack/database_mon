-- Faz 16 İŞ 6 (feature/multi-tenant-cluster): tahmin motoru — güven aralığı + günlük rollup.
-- app/models.py::PredictionInsight.lower_bound/.upper_bound/.seasonality,
-- MetricRollupDaily, SchemaObjectDailySample için Supabase karşılığı. SQLite'ta
-- migrate_schema() ile eşdeğer kolonlar ekleniyor / yeni tablolar create_all ile oluşuyor, ama
-- migrate_schema() Postgres için no-op olduğundan (bkz. 20260901090000_users_table.sql'in aynı
-- gerekçesi) burada da açık migration gerekiyor.

ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS lower_bound DOUBLE PRECISION;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS upper_bound DOUBLE PRECISION;
ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS seasonality VARCHAR(16);

CREATE TABLE IF NOT EXISTS metric_rollup_daily (
  id BIGSERIAL PRIMARY KEY,
  instance_id BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  metric_key VARCHAR(64) NOT NULL,
  day DATE NOT NULL,
  avg_value DOUBLE PRECISION NOT NULL,
  min_value DOUBLE PRECISION NOT NULL,
  max_value DOUBLE PRECISION NOT NULL,
  last_value DOUBLE PRECISION NOT NULL,
  sample_count INTEGER NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (instance_id, metric_key, day)
);

CREATE INDEX IF NOT EXISTS idx_metric_rollup_daily_instance_key ON metric_rollup_daily (instance_id, metric_key, day DESC);

CREATE TABLE IF NOT EXISTS schema_object_daily_samples (
  id BIGSERIAL PRIMARY KEY,
  instance_id BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
  day DATE NOT NULL,
  object_kind VARCHAR(16) NOT NULL,
  schema_name VARCHAR(128) NOT NULL,
  object_name VARCHAR(128) NOT NULL,
  size_bytes DOUBLE PRECISION NOT NULL,
  extra JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (instance_id, object_kind, schema_name, object_name, day)
);

CREATE INDEX IF NOT EXISTS idx_schema_object_daily_instance ON schema_object_daily_samples (instance_id, object_kind, day DESC);
