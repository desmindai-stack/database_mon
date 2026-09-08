-- Faz 25 İŞ 1: bekleme (wait event) örnekleme tabloları.
--
-- pg_stat_statements bize "bu sorgu 206 ms sürdü" diyor ama bu sürenin NEREDE geçtiğini
-- söylemiyor. Aktif oturum örnekleyicisi saniyede bir `pg_stat_activity` (SQL Server'da
-- `dm_exec_requests`) fotoğrafı alıp beklemeyi kaydediyor.
--
-- HAM ÖRNEK SAKLANMIYOR: 1 saniyelik örnekleme, aktif oturum başına saniyede bir satır
-- demektir — orta yüklü tek bir instance'ta günde milyonlarca satır. Örnekler süreç
-- belleğinde dakikalık kovalarda toplanıp dakika kapandığında tek satır olarak yazılıyor.

-- Dakikalık PAYDA: AAS = aktif oturum toplamı / alınan örnek sayısı.
-- Örnek sayısı sabit varsayılamaz (gecikme, yeniden başlatma, erişilemeyen sunucu) — sabit
-- varsaymak, tam da sorunun yaşandığı dakikaları olduğundan sakin gösterirdi.
CREATE TABLE IF NOT EXISTS active_session_minutes (
    id                        BIGSERIAL PRIMARY KEY,
    instance_id               BIGINT NOT NULL REFERENCES instances(id),
    minute                    TIMESTAMPTZ NOT NULL,
    samples_taken             INTEGER NOT NULL DEFAULT 0,
    active_sessions_sampled   INTEGER NOT NULL DEFAULT 0,
    blocked_sessions_sampled  INTEGER NOT NULL DEFAULT 0,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_active_session_minute UNIQUE (instance_id, minute)
);

CREATE INDEX IF NOT EXISTS ix_active_session_minutes_instance_minute
    ON active_session_minutes (instance_id, minute);
CREATE INDEX IF NOT EXISTS ix_active_session_minutes_instance_id
    ON active_session_minutes (instance_id);

-- Dakikalık KIRILIM: (dakika, sorgu, kategori, olay) başına tek satır.
-- `queryid` NULL DEĞİL boş dizgi olabilir: NULL, UNIQUE kısıtında her satırı benzersiz yapar
-- ve dakikalık toplamayı sessizce bozardı (aynı sorgu için 60 ayrı satır).
CREATE TABLE IF NOT EXISTS wait_sample_minutes (
    id             BIGSERIAL PRIMARY KEY,
    instance_id    BIGINT NOT NULL REFERENCES instances(id),
    minute         TIMESTAMPTZ NOT NULL,
    queryid        VARCHAR(64) NOT NULL DEFAULT '',
    wait_category  VARCHAR(16) NOT NULL,
    wait_event     VARCHAR(64) NOT NULL DEFAULT '',
    sample_count   INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_wait_sample_minute UNIQUE (instance_id, minute, queryid, wait_category, wait_event)
);

CREATE INDEX IF NOT EXISTS ix_wait_sample_minutes_instance_minute
    ON wait_sample_minutes (instance_id, minute);
CREATE INDEX IF NOT EXISTS ix_wait_sample_minutes_instance_id
    ON wait_sample_minutes (instance_id);

-- queryid → sorgu metni sözlüğü. Metni her kırılım satırına yazmak tabloyu on katına
-- çıkarırdı; `slow_query_samples`'tan okumak da yetmez, çünkü örnekleyici en pahalı 20 sorguyu
-- değil O AN ÇALIŞAN her sorguyu görüyor (tek tek ucuz ama binlerce kez çalışan bir sorgu o
-- listede hiç bulunmaz).
CREATE TABLE IF NOT EXISTS wait_query_signatures (
    id             BIGSERIAL PRIMARY KEY,
    instance_id    BIGINT NOT NULL REFERENCES instances(id),
    queryid        VARCHAR(64) NOT NULL,
    query_text     TEXT NOT NULL DEFAULT '',
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_wait_query_signature UNIQUE (instance_id, queryid)
);

CREATE INDEX IF NOT EXISTS ix_wait_query_signatures_instance_id
    ON wait_query_signatures (instance_id);
