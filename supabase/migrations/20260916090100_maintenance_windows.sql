-- Faz 28 İŞ 3: bakım pencereleri.
--
-- Bakım penceresi olmadan erişilebilirlik sayıları dürüst değil: planlı bir bakım için
-- alınan 40 dakikalık kesinti, plansız bir arızayla aynı kefeye giriyor ve aylık %99.9
-- hedefini tek başına deliyor. Müşteriye "SLA'yı tutturamadınız" demek, o kesintiyi
-- müşterinin kendisi onayladıysa yanlış bir suçlama.
--
-- Ters yönü de aynı ölçüde önemli: her kesintiyi "planlıydı" diye etiketlemek sayıyı
-- yalancı yapar. Bu yüzden pencere ÖNCEDEN tanımlanmış olmak zorunda ve created_by
-- kaydediliyor — "kesinti planlıydı" iddiası denetlenebilir olmalı.

CREATE TABLE IF NOT EXISTS maintenance_windows (
    id               BIGSERIAL PRIMARY KEY,
    -- instance | group | application | customer | global (bulgu kararlarıyla aynı hiyerarşi)
    scope_type       VARCHAR(16) NOT NULL DEFAULT 'instance',
    scope_id         BIGINT,
    title            VARCHAR(255) NOT NULL,
    description      TEXT,
    starts_at        TIMESTAMPTZ NOT NULL,
    ends_at          TIMESTAMPTZ NOT NULL,
    -- Tekrar KURALI saklanıyor, tek tek örnekler değil: altı aylık haftalık bir bakımı 26
    -- satır olarak açmak, biri değiştiğinde hepsini düzeltmek demekti.
    recurrence       VARCHAR(16) NOT NULL DEFAULT 'none',
    recurrence_until TIMESTAMPTZ,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    created_by       VARCHAR(128) NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_maintenance_windows_scope
    ON maintenance_windows (scope_type, scope_id);
CREATE INDEX IF NOT EXISTS ix_maintenance_windows_starts_at
    ON maintenance_windows (starts_at);
