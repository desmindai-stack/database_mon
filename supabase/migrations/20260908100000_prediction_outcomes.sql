-- Faz 20 İŞ 2: tahmin doğruluğunun ölçüldüğü tablo.
--
-- Öncesinde her tahminde bir `confidence` vardı ama bu regresyonun R² değeriydi: "model geçmiş
-- veriye ne kadar iyi oturdu" demek, "tahmin tuttu mu" demek değil. Doğruluk iddia edilemez,
-- ölçülür — her tahmin üretildiğinde buraya bir satır yazılır, hedef tarih geldiğinde
-- gerçekleşen değer okunup sapma hesaplanır.

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    id                  BIGSERIAL PRIMARY KEY,
    prediction_id       BIGINT REFERENCES prediction_insights(id) ON DELETE SET NULL,
    instance_id         BIGINT NOT NULL REFERENCES instances(id),
    kind                VARCHAR(32) NOT NULL,
    metric_key          VARCHAR(64) NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    target_at           TIMESTAMPTZ NOT NULL,
    checkpoint_days     DOUBLE PRECISION NOT NULL DEFAULT 0,

    predicted_value     DOUBLE PRECISION NOT NULL,
    lower_bound         DOUBLE PRECISION,
    upper_bound         DOUBLE PRECISION,

    method              VARCHAR(64) NOT NULL DEFAULT 'linear_regression',
    sample_count        INTEGER NOT NULL DEFAULT 0,
    span_days           DOUBLE PRECISION NOT NULL DEFAULT 0,
    r_squared           DOUBLE PRECISION NOT NULL DEFAULT 0,

    -- Gerçekleşen değerin okunacağı kaynak: sample | rollup | schema_object
    source              VARCHAR(16) NOT NULL,
    schema_name         VARCHAR(128),
    object_name         VARCHAR(128),

    -- pending | evaluated | expired ("expired" = hedef tarih geçti ama değer okunamadı;
    -- bunu hatalı tahmin saymak modeli haksız yere cezalandırırdı)
    status              VARCHAR(16) NOT NULL DEFAULT 'pending',
    evaluated_at        TIMESTAMPTZ,
    actual_value        DOUBLE PRECISION,
    absolute_error      DOUBLE PRECISION,
    percent_error       DOUBLE PRECISION,
    within_interval     BOOLEAN,
    unevaluable_reason  VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS ix_prediction_outcomes_instance ON prediction_outcomes(instance_id);
CREATE INDEX IF NOT EXISTS ix_prediction_outcomes_kind ON prediction_outcomes(kind);
CREATE INDEX IF NOT EXISTS ix_prediction_outcomes_prediction ON prediction_outcomes(prediction_id);
-- Değerlendirme işi "hedefi gelmiş ve hâlâ bekleyen" satırları tarar.
CREATE INDEX IF NOT EXISTS ix_prediction_outcomes_due ON prediction_outcomes(status, target_at);
