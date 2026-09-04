-- Faz 17 İŞ 1: Sağlık Raporu (Daily Health Check + Yönetici Raporu) tabloları.
-- SQLite tarafında create_all bu üç TABLOYU kendiliğinden oluşturur (checkfirst=True eksik
-- tabloları yaratır); Supabase'de migration'lar elle uygulandığı için burada da yazılı olmalı.
-- Kolon tipleri app/models.py::HealthReport / ReportFinding / FindingAcknowledgement ile birebir.

CREATE TABLE IF NOT EXISTS health_reports (
    id                 SERIAL PRIMARY KEY,
    scope_type         VARCHAR(16)  NOT NULL,
    scope_id           INTEGER,
    scope_label        VARCHAR(255) NOT NULL DEFAULT '',
    period_start       TIMESTAMPTZ  NOT NULL,
    period_end         TIMESTAMPTZ  NOT NULL,
    generated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    generated_by       VARCHAR(16)  NOT NULL DEFAULT 'manual',
    overall_status     VARCHAR(16)  NOT NULL DEFAULT 'ok',
    sections           JSONB,
    previous_report_id INTEGER REFERENCES health_reports(id),
    duration_ms        INTEGER      NOT NULL DEFAULT 0,
    status             VARCHAR(16)  NOT NULL DEFAULT 'done',
    progress_pct       INTEGER      NOT NULL DEFAULT 100,
    progress_label     VARCHAR(255),
    error              TEXT
);

CREATE INDEX IF NOT EXISTS ix_health_reports_scope_type  ON health_reports (scope_type);
CREATE INDEX IF NOT EXISTS ix_health_reports_scope_id    ON health_reports (scope_id);
CREATE INDEX IF NOT EXISTS ix_health_reports_generated_at ON health_reports (generated_at);
CREATE INDEX IF NOT EXISTS ix_health_reports_status      ON health_reports (status);

CREATE TABLE IF NOT EXISTS report_findings (
    id                  SERIAL PRIMARY KEY,
    report_id           INTEGER      NOT NULL REFERENCES health_reports(id) ON DELETE CASCADE,
    section             VARCHAR(32)  NOT NULL,
    severity            VARCHAR(16)  NOT NULL,
    title               VARCHAR(255) NOT NULL,
    detail              TEXT         NOT NULL DEFAULT '',
    evidence            JSONB,
    recommendation      TEXT,
    commands            JSONB,
    related_object_type VARCHAR(32),
    related_object_id   INTEGER,
    fingerprint         VARCHAR(64)  NOT NULL,
    priority            DOUBLE PRECISION NOT NULL DEFAULT 0,
    open_since_days     INTEGER      NOT NULL DEFAULT 0,
    change_state        VARCHAR(16)  NOT NULL DEFAULT 'new',
    acknowledged        BOOLEAN      NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS ix_report_findings_report_id   ON report_findings (report_id);
CREATE INDEX IF NOT EXISTS ix_report_findings_section     ON report_findings (section);
CREATE INDEX IF NOT EXISTS ix_report_findings_fingerprint ON report_findings (fingerprint);

CREATE TABLE IF NOT EXISTS finding_acknowledgements (
    id              SERIAL PRIMARY KEY,
    fingerprint     VARCHAR(64) NOT NULL,
    scope_type      VARCHAR(16) NOT NULL DEFAULT 'global',
    scope_id        INTEGER,
    acknowledged_by VARCHAR(64) NOT NULL DEFAULT '',
    acknowledged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    note            TEXT,
    CONSTRAINT uq_ack_fingerprint_scope UNIQUE (fingerprint, scope_type, scope_id)
);

CREATE INDEX IF NOT EXISTS ix_finding_ack_fingerprint ON finding_acknowledgements (fingerprint);
