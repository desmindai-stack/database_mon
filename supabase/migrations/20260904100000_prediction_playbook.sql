-- Faz 16-B İŞ 7: tahminler için adım adım çözüm planı.
-- app/models.py::PredictionInsight.playbook için Supabase karşılığı. SQLite tarafında
-- migrate_schema() ekliyor; migrate_schema() Postgres için no-op ve create_all var olan bir
-- tabloya eksik KOLON eklemediğinden bu kolon bu migration olmadan Supabase'de hiç oluşmaz ve
-- predictions okuyan her sorgu "column playbook does not exist" ile patlar.

ALTER TABLE prediction_insights ADD COLUMN IF NOT EXISTS playbook JSONB;
