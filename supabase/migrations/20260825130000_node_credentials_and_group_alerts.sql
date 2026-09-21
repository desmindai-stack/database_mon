-- Faz 6 (feature/multi-tenant-cluster): group-scoped alert persistence.
-- Node credential encryption (node.options.db_password) is handled entirely
-- in application code (services/credentials.py) — no schema change needed
-- for that, node.options was already JSONB.

ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS group_id BIGINT REFERENCES database_groups(id);
CREATE INDEX IF NOT EXISTS idx_alert_rules_group_id ON alert_rules(group_id);

-- Faz 31 Commit 10b: alert_events büyük bir tablo (saklama politikasının temizlediği). `ADD COLUMN ... REFERENCES` var
-- olan her satırı doğrulamak için tabloyu tarar ve doğrulama sürerken tabloyu kilitler; index de düz CREATE INDEX ile
-- yazmayı kapatırdı. Aynı sonuç, kilitsiz sırayla: sütun (yalnızca katalog değişikliği) → kısıt NOT VALID (yeni satırlar
-- denetlenir, var olanlar taranmaz) → VALIDATE (yazmayı ENGELLEMEDEN tarar) → CONCURRENTLY index.
ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS group_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'alert_events_group_id_fkey' AND conrelid = 'alert_events'::regclass) THEN
        ALTER TABLE alert_events ADD CONSTRAINT alert_events_group_id_fkey
            FOREIGN KEY (group_id) REFERENCES database_groups(id) NOT VALID;
    END IF;
END
$$;

ALTER TABLE alert_events VALIDATE CONSTRAINT alert_events_group_id_fkey;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_alert_events_group_id ON alert_events(group_id);

-- A group-scoped AlertEvent has no instance_id, so the old NOT NULL no
-- longer holds. instance_id stays a normal (nullable) FK otherwise.
ALTER TABLE alert_events ALTER COLUMN instance_id DROP NOT NULL;
