-- Faz 16-B İŞ 6: ön koşul kontrollerini instance bazında yoksayabilme.
-- app/models.py::Instance.ignored_prerequisites için Supabase karşılığı. SQLite tarafında
-- migrate_schema() ekliyor; migrate_schema() Postgres için no-op olduğundan ve create_all
-- var olan bir tabloya eksik KOLON eklemediğinden (checkfirst=True tabloyu atlar) bu kolon
-- bu migration olmadan Supabase'de hiç oluşmaz ve instance okuyan her sorgu
-- "column ignored_prerequisites does not exist" ile patlar.

ALTER TABLE instances ADD COLUMN IF NOT EXISTS ignored_prerequisites JSONB;
