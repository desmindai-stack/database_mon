-- Faz 26 İŞ 1: auto_explain ile GERÇEK çalıştırmadan yakalanan sorgu planları.
--
-- Sonradan alınan EXPLAIN, sorgunun yavaş çalıştığı andaki planı göstermez: pg_stat_statements
-- sorguyu normalleştirdiği için parametre bilinmez ve `NULL` konur — planlayıcı bambaşka bir
-- plan seçebilir. Ayrıca veri, istatistikler ve sunucu yükü o günden beri değişmiş olabilir.
-- auto_explain ise eşiği aşan sorguların gerçekten kullanılan planını log'a yazar.
--
-- `source` alanı planın nereden geldiğini taşır ve arayüzde gösterilir; iki kaynağın aynı
-- ekranda aynı görünmesi, tahmini bir planı ölçüm sanmaya yol açardı.

CREATE TABLE IF NOT EXISTS captured_plans (
    id                 BIGSERIAL PRIMARY KEY,
    instance_id        BIGINT NOT NULL REFERENCES instances(id),
    captured_at        TIMESTAMPTZ NOT NULL,
    -- auto_explain | manual_analyze | manual_estimate
    source             VARCHAR(24) NOT NULL DEFAULT 'auto_explain',
    duration_ms        DOUBLE PRECISION NOT NULL DEFAULT 0,
    query_text         TEXT NOT NULL DEFAULT '',
    -- Normalleştirilmiş sorgunun özeti. auto_explain log'u queryid YAZMADIĞI için
    -- pg_stat_statements ile eşleştirme metin üzerinden yapılmak zorunda.
    query_fingerprint  VARCHAR(64) NOT NULL DEFAULT '',
    -- Eşleştirme kesin değil; bulunamazsa NULL kalır ve bu bir hata değildir — plan tek
    -- başına da değerlidir, yanlış bir sorguya bağlamaktansa bağlamamak yeğdir.
    queryid            VARCHAR(64),
    -- auto_explain.log_analyze kapalıyken plan gerçektir ama gerçek satır sayısı yoktur.
    has_actual_rows    BOOLEAN NOT NULL DEFAULT false,
    plan_json          JSONB,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Her çekim log'un son N satırını okuyor, yani pencereler örtüşüyor ve aynı plan birden
    -- çok kez görülüyor. Bu kısıt tekrarları engelliyor; "en son ne zaman çektik" saymacı
    -- kullanmak worker yeniden başladığında çöker ve planlar ikinci kez yazılırdı.
    CONSTRAINT uq_captured_plan_occurrence
        UNIQUE (instance_id, captured_at, duration_ms, query_fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_captured_plans_instance_captured
    ON captured_plans (instance_id, captured_at);
CREATE INDEX IF NOT EXISTS ix_captured_plans_instance_id
    ON captured_plans (instance_id);
CREATE INDEX IF NOT EXISTS ix_captured_plans_queryid
    ON captured_plans (queryid);
