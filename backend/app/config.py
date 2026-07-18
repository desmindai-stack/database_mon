import json
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "pgwatch"
    database_url: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parents[2] / 'data' / 'pgwatch.db'}"
    collect_interval_seconds: int = 15
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = '["http://localhost:5173", "http://127.0.0.1:5173"]'

    # api = REST only, worker = collector only, all = local dev
    run_mode: Literal["api", "worker", "all"] = "all"
    credentials_master_key: str | None = None

    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None

    def get_cors_origins(self) -> list[str]:
        value = self.cors_origins.strip()
        if value.startswith("["):
            return json.loads(value)
        return [x.strip() for x in value.split(",") if x.strip()]


settings = Settings()
