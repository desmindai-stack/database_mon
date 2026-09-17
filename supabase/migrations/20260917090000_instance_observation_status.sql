-- Faz 31 Commit 5: veritabanı başına iki gözlem durumu.
--
-- 1. İZLEME ROLÜ PAYLAŞIMI. dbace'in bağlandığı rolde dbace DIŞI oturum (application_name
--    'dbace' değil ya da son sorgusu imzasız) görülürse "dbace'in kendi sorgusu" ayrımı
--    ölçülemez: pg_stat_statements satırı userid ile anahtarlı ve aynı roldeki uygulama
--    çağrıları dbace'in satırına karışır. Toplayıcı her döngüde pg_stat_activity'ye bakıyor.
--      monitoring_role_checked_at   son kontrol (NULL = hiç ölçülmedi)
--      monitoring_role_shared_at    dbace dışı oturumun EN SON görüldüğü an
--      monitoring_role_shared_apps  o oturumların application_name'leri (en fazla 5; sorgu metni YOK)
--
-- 2. auto_explain PLAN YAKALAMA DURUMU. "Yakalanan plan yok" üç ayrı durumu karıştırıyordu:
--    hedefte auto_explain kapalı / log okunamıyor ya da iş çalışmıyor / henüz eşiği aşan sorgu yok.
--      auto_explain_loaded        toplayıcının okuduğu shared_preload_libraries'te auto_explain var mı (NULL = okunamadı)
--      plan_capture_checked_at    plan yakalama işinin son log okuma denemesi
--      plan_capture_error         o denemenin hatası (NULL = log okundu)
--      plan_capture_found         okunan log'da bulunan plan sayısı

ALTER TABLE instances ADD COLUMN IF NOT EXISTS monitoring_role_checked_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS monitoring_role_shared_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS monitoring_role_shared_apps JSONB;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS auto_explain_loaded BOOLEAN;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS plan_capture_checked_at TIMESTAMPTZ;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS plan_capture_error TEXT;
ALTER TABLE instances ADD COLUMN IF NOT EXISTS plan_capture_found INTEGER;
