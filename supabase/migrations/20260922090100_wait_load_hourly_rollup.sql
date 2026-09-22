-- Faz 31 Commit 10c-C: bekleme yükünün SAATLİK toplulaştırması — ham dakikalık veri kısa (7 gün) saklanmaya geçerken
-- eski dönemler buradan okunuyor (services/wait_load_rollup.py, services/database_load.py).
--
-- Ölçüldü (Faz 31 Commit 10a): 20 instance için ham yazım ~10 MB/saat, 30 günde ~7 GB. Bu iki tablo YENİ ve BOŞ
-- başlıyor; backfill YOK — `services/retention.run_retention_cleanup` ilk çalıştığında ham veriyi silmeden önce
-- kendisi dolduruyor (bkz. `ensure_wait_load_rollup`), migration'ın kendisi bir şey taşımıyor. CONCURRENTLY YOK.
CREATE TABLE IF NOT EXISTS active_session_rollup_hourly (
    id                       BIGSERIAL PRIMARY KEY,
    instance_id              BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    hour                     TIMESTAMPTZ NOT NULL,
    samples_taken            INTEGER NOT NULL DEFAULT 0,
    active_sessions_sampled  INTEGER NOT NULL DEFAULT 0,
    blocked_sessions_sampled INTEGER NOT NULL DEFAULT 0,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_active_session_rollup_hourly UNIQUE (instance_id, hour)
);
CREATE INDEX IF NOT EXISTS ix_active_session_rollup_hourly_instance_hour
    ON active_session_rollup_hourly (instance_id, hour);

CREATE TABLE IF NOT EXISTS wait_load_rollup_hourly (
    id            BIGSERIAL PRIMARY KEY,
    instance_id   BIGINT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
    hour          TIMESTAMPTZ NOT NULL,
    wait_category VARCHAR(16) NOT NULL,
    sample_count  INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_wait_load_rollup_hourly UNIQUE (instance_id, hour, wait_category)
);
CREATE INDEX IF NOT EXISTS ix_wait_load_rollup_hourly_instance_hour
    ON wait_load_rollup_hourly (instance_id, hour);
