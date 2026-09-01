-- Faz 15 İŞ 1 (feature/multi-tenant-cluster): kimlik doğrulama.
-- app/models.py::User için Supabase karşılığı — bu tablo şu ana kadar sadece
-- SQLite'ta init_db()'nin Base.metadata.create_all'ı üzerinden (checkfirst=True, tablo yoksa
-- örtük biçimde) var oluyordu; migrate_schema() Postgres için no-op olduğundan (sadece
-- sqlite:// URL'lerinde çalışıyor) hiçbir kolon eklemesi Supabase'e hiç uygulanmadı. Bu dosya
-- olmadan üretimde ilk admin bootstrap'ı ya create_all'ın örtük davranışına ya da DB rolünün
-- CREATE TABLE yetkisine bel bağlamış olurdu — projedeki her diğer tablo/kolon gibi burada da
-- açık bir migration'la izleniyor.

CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  username VARCHAR(64) NOT NULL UNIQUE,
  email VARCHAR(255),
  password_hash VARCHAR(255) NOT NULL,
  role VARCHAR(16) NOT NULL DEFAULT 'viewer',
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_login_at TIMESTAMPTZ
);
