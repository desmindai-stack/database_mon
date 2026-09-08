-- Faz 26 İŞ 3: bloklama olayları ve deadlock kayıtları.
--
-- Canlı ağaç "şu anda kim kimi blokluyor" sorusunu cevaplıyor; bu tablolar "dün gece 03:14'te
-- ne oldu" sorusunu. En kötü olaylar kimsenin ekrana bakmadığı saatlerde yaşanır ve sabah
-- geriye kalan tek şey "gece sistem yavaştı" cümlesidir.

-- Bir olay = aynı kök engelleyicinin KESİNTİSİZ bekletme dönemi. Aynı pid tekrar bekletmeye
-- başlarsa bu YENİ bir olaydır; arada sistem düzelmiş demektir ve iki dönemi tek olay saymak
-- süreyi olduğundan uzun gösterirdi.
CREATE TABLE IF NOT EXISTS blocking_episodes (
    id                    BIGSERIAL PRIMARY KEY,
    instance_id           BIGINT NOT NULL REFERENCES instances(id),
    started_at            TIMESTAMPTZ NOT NULL,
    -- Olay sürerken NULL. "Hâlâ devam ediyor" durumunu ayrı bir bayrakla değil bu alanla
    -- temsil etmek, iki alanın birbiriyle çelişmesini imkânsız kılıyor.
    ended_at              TIMESTAMPTZ,
    duration_seconds      DOUBLE PRECISION NOT NULL DEFAULT 0,

    root_pid              INTEGER NOT NULL,
    root_query            TEXT NOT NULL DEFAULT '',
    root_username         VARCHAR(128),
    root_application      VARCHAR(255),
    -- Kök engelleyici sorgu ÇALIŞTIRMIYOR muydu — sessiz blok. Raporun en değerli ayrımı:
    -- bu durumda sorun veritabanında değil uygulamadadır.
    root_was_idle         BOOLEAN NOT NULL DEFAULT false,

    -- Olay boyunca görülen EN YÜKSEK değerler: etkinin ölçüsü anlık değil zirvedir.
    max_blocked_sessions  INTEGER NOT NULL DEFAULT 0,
    max_chain_depth       INTEGER NOT NULL DEFAULT 0,

    lock_type             VARCHAR(64),
    lock_object           VARCHAR(255),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_blocking_episodes_instance_started
    ON blocking_episodes (instance_id, started_at);
CREATE INDEX IF NOT EXISTS ix_blocking_episodes_instance_id
    ON blocking_episodes (instance_id);

-- Deadlock bloklamadan FARKLI bir olaydır: veritabanı döngüyü kendisi kırar ve bir tarafı
-- iptal eder. Anlıktır — canlı ekranda hiçbir izi kalmaz, yalnızca log'dan görülebilir.
CREATE TABLE IF NOT EXISTS deadlock_events (
    id             BIGSERIAL PRIMARY KEY,
    instance_id    BIGINT NOT NULL REFERENCES instances(id),
    detected_at    TIMESTAMPTZ NOT NULL,
    -- postgresql_log | sqlserver_system_health
    source         VARCHAR(32) NOT NULL DEFAULT 'postgresql_log',
    -- Sorgu metinlerinden türetiliyor, pid'lerden DEĞİL: pid'ler her deadlock'ta farklıdır
    -- ama aynı deadlock tekrar ettiğinde sorgular aynıdır. Bu sayede hem tekrar yazma
    -- engelleniyor hem de "aynı deadlock 40 kez oldu" sorusu cevaplanabiliyor.
    fingerprint    VARCHAR(64) NOT NULL DEFAULT '',
    -- Kurban: veritabanının iptal ettiği taraf. Kazanan: işine devam eden. Yalnızca kurbanı
    -- göstermek yarım teşhistir — döngüyü oluşturan kilit sırası genelde kazananındır.
    victim_pid     INTEGER,
    victim_query   TEXT NOT NULL DEFAULT '',
    winner_pid     INTEGER,
    winner_query   TEXT NOT NULL DEFAULT '',
    participants   INTEGER NOT NULL DEFAULT 2,
    raw_detail     TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_deadlock_event UNIQUE (instance_id, detected_at, fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_deadlock_events_instance_detected
    ON deadlock_events (instance_id, detected_at);
CREATE INDEX IF NOT EXISTS ix_deadlock_events_instance_id
    ON deadlock_events (instance_id);
