CREATE TABLE IF NOT EXISTS group_health_snapshots (
    id BIGSERIAL PRIMARY KEY,
    group_id BIGINT NOT NULL UNIQUE REFERENCES database_groups(id) ON DELETE CASCADE,
    overall VARCHAR(16) NOT NULL DEFAULT 'unknown',
    report_json JSONB,
    recommendations_json JSONB,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_group_health_snapshots_group_id ON group_health_snapshots (group_id);
