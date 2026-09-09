-- Faz 28 İŞ 3b: SLA hedefleri.
--
-- Hedef olmadan erişilebilirlik sayısı bir bilgi ama bir KARAR değil: %99.7 iyi mi kötü mü,
-- ancak taahhüde göre söylenebilir.
--
-- Kapsam hiyerarşisi bulgu kararları ve bakım pencereleriyle aynı (instance/group/
-- application/customer/global) — aynı kavramın üç farklı kapsam modeli olması, üçünü de
-- yanlış hatırlamaya yol açardı.

CREATE TABLE IF NOT EXISTS sla_targets (
    id          BIGSERIAL PRIMARY KEY,
    scope_type  VARCHAR(16) NOT NULL DEFAULT 'customer',
    scope_id    BIGINT,
    -- Hedef yüzde (99.9, 99.95 gibi).
    target_pct  DOUBLE PRECISION NOT NULL DEFAULT 99.9,
    -- monthly | quarterly. Yıllık bilinçli olarak yok: bir yılın ortasında "kalan kesinti
    -- bütçesi" sayısı o kadar büyük çıkıyor ki uyarı değeri kalmıyor.
    period      VARCHAR(16) NOT NULL DEFAULT 'monthly',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_by  VARCHAR(128) NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ,
    CONSTRAINT uq_sla_target_scope UNIQUE (scope_type, scope_id)
);

CREATE INDEX IF NOT EXISTS ix_sla_targets_scope ON sla_targets (scope_type, scope_id);
