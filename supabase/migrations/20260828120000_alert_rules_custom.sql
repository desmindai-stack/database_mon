ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS rule_type VARCHAR(16) NOT NULL DEFAULT 'metric';
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS severity VARCHAR(16) NOT NULL DEFAULT 'warning';
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS engine VARCHAR(32);
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS sql_query TEXT;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS interval_seconds INTEGER NOT NULL DEFAULT 60;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS last_run_at TIMESTAMPTZ;
-- metric stays NOT NULL (matches the SQLAlchemy model, which keeps it a plain str) —
-- custom rules just store '' since sqlite can't relax an existing NOT NULL via ADD COLUMN
-- and we want the same schema shape in both environments (see SORULAR.md).
ALTER TABLE alert_rules ALTER COLUMN metric SET DEFAULT '';
