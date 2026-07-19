-- Speed up query history lookups by queryid over time
CREATE INDEX IF NOT EXISTS idx_slow_query_samples_instance_queryid_collected
  ON slow_query_samples (instance_id, queryid, collected_at DESC);
