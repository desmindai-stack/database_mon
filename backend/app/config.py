import json
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "dbace"
    database_url: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parents[2] / 'data' / 'dbace.db'}"
    collect_interval_seconds: int = 15
    # Yavaş sorgular METRİKLERDEN daha seyrek toplanır (Faz 21 İŞ 3). Sebep hacim: her toplama
    # döngüsü 20 satır yazıyor, 15 saniyelik aralıkta bu instance başına AYDA ~3,5 milyon satır
    # demek. pg_stat_statements kümülatif olduğu için 15 saniyelik çözünürlük yavaş sorgu
    # analizine hiçbir şey katmıyor — pencere farkları 5 dakikalık örneklerle de aynı sonucu
    # veriyor. 300 sn ile aylık satır sayısı ~173 bine, yani metric_samples ile aynı mertebeye
    # iniyor. Metrik toplama (bağlantı zirvesi gibi ani olaylar için) 15 saniyede kalıyor.
    slow_query_interval_seconds: int = 300
    # dbace'in KENDİ veritabanı bağlantıları için sorgu zaman aşımı (saniye, PostgreSQL).
    # 0 = sınırsız. Kaçak bir sorgunun isteği süresiz asılı bırakmasını (ve gateway'in 502
    # döndürmesini) engelleyen son savunma hattı; izlenen hedef veritabanlarını ETKİLEMEZ
    # (onların kendi sınırı collectors/postgresql.py içinde).
    db_statement_timeout_seconds: int = 120
    dashboard_refresh_interval_seconds: int = 60
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = '["http://localhost:5173", "http://127.0.0.1:5173"]'

    # api = REST only, worker = collector only, all = local dev
    run_mode: Literal["api", "worker", "all"] = "all"
    credentials_master_key: str | None = None

    # public = multi-customer dashboard; private = single-customer isolated
    deployment_mode: Literal["public", "private"] = "public"
    default_customer_name: str | None = None

    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None

    # Auth (Faz 15 İŞ 1). jwt_secret MUST be overridden in production via .env — the default
    # is only safe for local dev (see services/security.py's startup warning).
    jwt_secret: str = "dev-insecure-secret-change-me-in-production"
    access_token_expire_minutes: int = 60
    refresh_token_expire_days: int = 7
    # First-boot admin bootstrap (services/bootstrap.py::ensure_default_admin) — only used
    # when the users table is empty. If admin_password is left unset, a random one is
    # generated and logged once so the deployment doesn't ship a guessable default credential.
    admin_username: str = "admin"
    admin_password: str | None = None

    def get_cors_origins(self) -> list[str]:
        value = self.cors_origins.strip()
        # Railway bazen değeri çift tırnak içine alır; temizle
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1].strip()
        if value.startswith("["):
            return json.loads(value)
        return [x.strip() for x in value.split(",") if x.strip()]


settings = Settings()
