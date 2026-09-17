-- Faz 31 Commit 5: index önerisinin ÖLÇÜLMÜŞ etkisi.
--
-- hypopg yokken fayda yüzdesi gösterilmiyor (istatistik formülü gerçek sunucuda 2,6 kat saptı).
-- Asıl fayda yolu: öneri üretilince sorgunun planı ("önce") saklanıyor; index hedefte
-- kurulduktan sonra AYNI sorgunun planı yeniden alınıyor ("sonra"). İkisi de değerden bağımsız
-- EXPLAIN — sorgu çalıştırılmıyor, planlayıcı maliyeti karşılaştırılıyor.
--
-- status: waiting_for_index | measured | not_measurable
-- existing_indexes: kayıt anında tabloda VAR OLAN index adları — sonradan kurulanı ayırmak için.

CREATE TABLE IF NOT EXISTS index_advice_outcomes (
    id SERIAL PRIMARY KEY,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    queryid VARCHAR(64),
    query_fingerprint VARCHAR(64) NOT NULL,
    query_text TEXT NOT NULL DEFAULT '',
    table_name VARCHAR(256) NOT NULL,
    index_columns JSON NOT NULL,
    index_ddl TEXT NOT NULL,
    index_kind VARCHAR(32) NOT NULL DEFAULT 'btree',
    existing_indexes JSON,
    status VARCHAR(32) NOT NULL DEFAULT 'waiting_for_index',
    before_cost DOUBLE PRECISION,
    before_indexes_used JSON,
    before_measured_at TIMESTAMPTZ,
    after_cost DOUBLE PRECISION,
    after_indexes_used JSON,
    after_index_name VARCHAR(256),
    after_measured_at TIMESTAMPTZ,
    last_error TEXT,
    registered_at TIMESTAMPTZ DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    CONSTRAINT uq_index_advice_outcome UNIQUE (instance_id, query_fingerprint, index_ddl)
);

CREATE INDEX IF NOT EXISTS ix_index_advice_outcomes_instance_id ON index_advice_outcomes (instance_id);
CREATE INDEX IF NOT EXISTS ix_index_advice_outcomes_status ON index_advice_outcomes (status);
