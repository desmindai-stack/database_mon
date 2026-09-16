-- Faz 31 İŞ 1c: çağrı eşiğini henüz karşılamayan index önerisi sorgularının izlenmesi.
--
-- Önceden eşik altındaki sorgu için "tekrar deneyin" deniyordu ve geri dönmek kullanıcıya
-- kalıyordu. Artık sorgu buraya yazılıyor; zamanlayıcı turu çağrı sayısını izleyip eşik
-- dolunca öneriyi üretiyor (advice_json) ve durumu 'ready' yapıyor.
--
-- query_fingerprint: plan_capture.fingerprint() ile aynı normalleştirme. queryid ayrıcalıksız
-- rolde NULL gelebildiği için anahtar o değil.
-- status: waiting | ready | failed

CREATE TABLE IF NOT EXISTS index_advice_watches (
    id SERIAL PRIMARY KEY,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    queryid VARCHAR(64),
    query_fingerprint VARCHAR(64) NOT NULL,
    query_text TEXT NOT NULL DEFAULT '',
    threshold INTEGER NOT NULL,
    calls_at_registration INTEGER NOT NULL DEFAULT 0,
    calls_seen INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'waiting',
    advice_json JSON,
    last_error TEXT,
    registered_at TIMESTAMPTZ DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    ready_at TIMESTAMPTZ,
    CONSTRAINT uq_index_advice_watch UNIQUE (instance_id, query_fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_index_advice_watches_instance_id ON index_advice_watches (instance_id);
CREATE INDEX IF NOT EXISTS ix_index_advice_watches_status ON index_advice_watches (status);
