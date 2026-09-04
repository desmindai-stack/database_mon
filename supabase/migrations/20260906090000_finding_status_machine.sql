-- Faz 17 Ek İŞ A: bulgu durum makinesi.
-- Tek "kabul edildi" bayrağı yerine durumlar: open | ignored | deferred | risk_accepted |
-- planned | resolved_pending_verification | resolved. Karar artık bir kapsama (instance /
-- group / application / customer / global) uygulanabiliyor ve her değişiklik geçmişe yazılıyor.
-- Tablo adı finding_acknowledgements KORUNDU — yeniden adlandırmak veri taşıma gerektirirdi.

ALTER TABLE finding_acknowledgements ADD COLUMN IF NOT EXISTS finding_type VARCHAR(96);
ALTER TABLE finding_acknowledgements ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'ignored';
ALTER TABLE finding_acknowledgements ADD COLUMN IF NOT EXISTS reference VARCHAR(255);

CREATE INDEX IF NOT EXISTS ix_finding_ack_type ON finding_acknowledgements (finding_type);

ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS finding_type VARCHAR(96) NOT NULL DEFAULT '';
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'open';
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS verification_failed BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS decision_note TEXT;
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS decision_reference VARCHAR(255);
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS decision_until TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS ix_report_findings_type   ON report_findings (finding_type);
CREATE INDEX IF NOT EXISTS ix_report_findings_status ON report_findings (status);

CREATE TABLE IF NOT EXISTS finding_status_history (
    id           SERIAL PRIMARY KEY,
    fingerprint  VARCHAR(64) NOT NULL,
    finding_type VARCHAR(96),
    scope_type   VARCHAR(16) NOT NULL DEFAULT 'instance',
    scope_id     INTEGER,
    from_status  VARCHAR(32),
    to_status    VARCHAR(32) NOT NULL,
    note         TEXT,
    reference    VARCHAR(255),
    expires_at   TIMESTAMPTZ,
    changed_by   VARCHAR(64) NOT NULL DEFAULT '',
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_finding_history_fingerprint ON finding_status_history (fingerprint);
CREATE INDEX IF NOT EXISTS ix_finding_history_changed_at  ON finding_status_history (changed_at);

-- Geriye dönük doldurma: bu değişiklikten ÖNCE "kabul edildi" işaretlenmiş bulgular, yeni
-- modelde status='open' varsayılanıyla kalırsa yükseltmeden sonra tekrar kritik sayılır ve
-- daha önce susturulmuş konular topluca geri döner. Eski bayrağı yeni duruma çeviriyoruz.
UPDATE report_findings SET status = 'ignored' WHERE acknowledged = true AND status = 'open';
UPDATE report_findings SET status = 'resolved' WHERE change_state = 'resolved' AND status = 'open';
UPDATE report_findings SET finding_type = section || ':' || section WHERE finding_type = '';

-- Var olan kabul kayıtları "yoksayıldı" durumuna karşılık geliyor (kolon varsayılanı zaten
-- 'ignored', bu satır NULL kalmış olabilecek eski satırlar için).
UPDATE finding_acknowledgements SET status = 'ignored' WHERE status IS NULL;
