ALTER TABLE nodes ADD COLUMN IF NOT EXISTS instance_id BIGINT REFERENCES instances(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_nodes_instance_id ON nodes (instance_id);
