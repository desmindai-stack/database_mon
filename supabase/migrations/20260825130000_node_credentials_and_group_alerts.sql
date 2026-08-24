-- Faz 6 (feature/multi-tenant-cluster): group-scoped alert persistence.
-- Node credential encryption (node.options.db_password) is handled entirely
-- in application code (services/credentials.py) — no schema change needed
-- for that, node.options was already JSONB.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS group_id BIGINT REFERENCES database_groups(id);
CREATE INDEX IF NOT EXISTS idx_alert_rules_group_id ON alert_rules(group_id);

ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS group_id BIGINT REFERENCES database_groups(id);
CREATE INDEX IF NOT EXISTS idx_alert_events_group_id ON alert_events(group_id);

-- A group-scoped AlertEvent has no instance_id, so the old NOT NULL no
-- longer holds. instance_id stays a normal (nullable) FK otherwise.
ALTER TABLE alert_events ALTER COLUMN instance_id DROP NOT NULL;
