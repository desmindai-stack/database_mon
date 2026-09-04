-- Faz 17 İŞ 2: günlük parametre / ön koşul durum fotoğrafı.
-- Sağlık raporundaki "dün'e göre değişen parametreler" ve "ön koşul eksikliği" bölümleri
-- geçmişe dönük veri ister; bu tablo günlük rollup işi tarafından doldurulur.
-- SQLite'ta create_all bu tabloyu kendiliğinden oluşturur; Supabase'de migration elle uygulanır.

CREATE TABLE IF NOT EXISTS daily_state_snapshots (
    id          SERIAL PRIMARY KEY,
    instance_id INTEGER     NOT NULL REFERENCES instances(id),
    day         DATE        NOT NULL,
    kind        VARCHAR(32) NOT NULL,
    payload     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_daily_state_instance_kind_day UNIQUE (instance_id, kind, day)
);

CREATE INDEX IF NOT EXISTS ix_daily_state_instance ON daily_state_snapshots (instance_id);
