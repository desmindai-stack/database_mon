-- Faz 31 Commit 10c-A: SQL Server bekleme istatistiklerinin (dm_os_wait_stats) kümülatif sayaç TABANI paylaşılan yerde.
--
-- Sayaç sunucu açılışından beri birikiyor; anlamlı olan iki okuma arasındaki FARK. Taban süreç belleğindeydi: dbace yeniden
-- başlayınca ilk okuma "fark hesaplanamadı" diyordu, birden çok süreçte (uvicorn --workers N / WEB_CONCURRENCY /
-- çoğaltılmış servis) ardışık iki istek farklı sürece düşerse fark "o sürecin son okumasından bu yana" oluyordu.
-- Instance başına TEK satır (en çok dakikada bir güncellenir): yeni tablo, büyük tabloya dokunmaz.
CREATE TABLE IF NOT EXISTS wait_stats_baselines (
    instance_id       BIGINT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
    server_start_time TIMESTAMPTZ,
    sampled_at        TIMESTAMPTZ NOT NULL,
    counters          JSONB NOT NULL,
    writer            VARCHAR(64)
);
