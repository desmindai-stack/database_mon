-- Faz 28 İŞ 1: yedek izleme.
--
-- dbace'te yedek izleme hiç yoktu; bir DBA aracında bu temel direklerden biri ve bankada ilk
-- sorulacak sorulardan biri "son yedek ne zaman alındı".
--
-- TEMEL KURAL: yedek alındığı VARSAYILMAZ. Kayıt bulunamadığında "yedek yok" denmiyor —
-- `backup_probes` hangi yöntemlere bakıldığını ve neyin engellediğini tutuyor, çünkü
-- "yedek yok" ile "dbace bulamadı" çok farklı iki şey ve ikincisini birincisi gibi sunmak
-- gerçekten yedeği olan kurumu paniğe, olmayanı sahte güvene sürükler.

CREATE TABLE IF NOT EXISTS backup_records (
    id                BIGSERIAL PRIMARY KEY,
    instance_id       BIGINT NOT NULL REFERENCES instances(id),
    -- full | differential | log | wal_archive | base_backup
    backup_type       VARCHAR(24) NOT NULL,
    -- SQL Server veritabanı başına yedekliyor; PostgreSQL küme geneli (NULL).
    database_name     VARCHAR(255),
    -- msdb | pg_stat_archiver | pgbackrest | barman | wal_g | pg_basebackup
    source            VARCHAR(32) NOT NULL,
    -- Kaynağın kendi kimliği (backupset id, pgBackRest etiketi). Aynı yedek her sondada
    -- yeniden görülüyor; bu kısıt tekrar yazmayı engelliyor.
    external_id       VARCHAR(128) NOT NULL DEFAULT '',

    started_at        TIMESTAMPTZ NOT NULL,
    -- Devam eden yedekte NULL. "Bitmedi" durumu ayrı bir bayrakla değil bu alanla temsil
    -- ediliyor; iki alanın çelişmesi imkânsız olsun diye.
    finished_at       TIMESTAMPTZ,
    duration_seconds  DOUBLE PRECISION,
    size_bytes        DOUBLE PRECISION,
    -- success | failed | running
    status            VARCHAR(16) NOT NULL DEFAULT 'success',
    error_message     TEXT,
    detail            JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_backup_record UNIQUE (instance_id, source, external_id)
);

CREATE INDEX IF NOT EXISTS ix_backup_records_instance_started
    ON backup_records (instance_id, started_at);
CREATE INDEX IF NOT EXISTS ix_backup_records_instance_id
    ON backup_records (instance_id);

-- Sondanın KENDİSİ: ne arandı, ne bulundu, ne engelledi. Instance başına tek satır.
CREATE TABLE IF NOT EXISTS backup_probes (
    id                BIGSERIAL PRIMARY KEY,
    instance_id       BIGINT NOT NULL REFERENCES instances(id),
    probed_at         TIMESTAMPTZ NOT NULL,
    -- Kullanıcıya "nereye baktık" olarak gösteriliyor.
    methods_checked   JSONB,
    methods_found     JSONB,
    -- pg_stat_archiver anlık durumu. Arşivleme durmuşsa bu sayaçlardan değil ZAMAN
    -- DAMGALARINDAN anlaşılıyor: archived_count sabit kalsa da last_archived_time eskiyse
    -- arşivleme durmuş demektir.
    archiver          JSONB,
    -- Replikasyon slotları: kullanılmayan bir slot WAL biriktirip diski doldurur.
    slots             JSONB,
    -- Yöntem başına hata (yetki yok, araç kurulu değil, agent yok).
    errors            JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_backup_probe_instance UNIQUE (instance_id)
);

CREATE INDEX IF NOT EXISTS ix_backup_probes_instance_id
    ON backup_probes (instance_id);
