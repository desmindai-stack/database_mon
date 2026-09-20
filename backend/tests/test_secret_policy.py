"""Sır yönetimi (Faz 31 Commit 9, madde 1).

Canlıda `JWT_SECRET` verilmemişti: jetonlar kodda YAZILI geliştirme sırrıyla imzalanıyordu (kaynağa erişen
herkes geçerli jeton üretebilir) ve `ADMIN_PASSWORD` eski bir değerde kalmıştı. Üçü de yalnızca uyarı
logluyordu. Artık üretimde (meta veritabanı PostgreSQL) bu alanlar tanımsızsa ya da varsayılana eşitse
uygulama AÇILIŞTA duruyor.

Hangi alanların zorunlu olduğu elle yazılmıyor: güvenlik modüllerinin okuduğu sır alanları koddan (AST).
Negatif kontroller: varsayılan sırla açılış kırmızı, tanımlıyken yeşil; varsayılan sırla üretilmiş jeton
kabul edilmiyor; şifre değişince eski jetonlar düşüyor.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.models import User
from app.services import security
from app.services.secret_policy import (
    ALLOW_INSECURE_ENV,
    enforce_secret_policy,
    insecure_fields,
    is_production,
    secret_fields,
)
from tests.auth_helper import _TEST_PASSWORD, authed_client

SECURE = {
    "jwt_secret": "x" * 64,
    "credentials_master_key": "k" * 44,
    "admin_password": "Gercek_parola_2026",
    "database_url": "postgresql+asyncpg://dbace:pw@db:5432/dbace",
}


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


def _settings(**over) -> Settings:
    return Settings(**{**SECURE, **over})


# --- 1. Hangi alanlar sır, hangileri zorunlu (koddan) -------------------------------------------------


def test_secret_fields_are_discovered_from_code_with_their_defaults():
    fields = {f.env_var: f for f in secret_fields()}
    log("Settings'teki sır alanları",
        {name: {"varsayılan": f.default, "zorunlu": f.required, "kullanan": f.used_by} for name, f in fields.items()})
    # Güvenlik yolundan OKUNANLAR zorunlu; okunmayan (Supabase) alanlar değil — kural koddan çıkıyor.
    assert {name for name, f in fields.items() if f.required} == {"JWT_SECRET", "CREDENTIALS_MASTER_KEY", "ADMIN_PASSWORD"}
    assert fields["JWT_SECRET"].default == security._DEV_SECRET
    assert {name for name, f in fields.items() if not f.required} == {"SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"}
    # Süre ayarları adında "token" geçse de sır değil.
    assert "ACCESS_TOKEN_EXPIRE_MINUTES" not in fields


# --- 2. Açılış: varsayılan sır = kırmızı, tanımlı = yeşil --------------------------------------------


@pytest.mark.parametrize(("over", "expected"), [
    ({}, []),
    ({"jwt_secret": security._DEV_SECRET}, ["JWT_SECRET koddaki geliştirme varsayılanına eşit"]),
    ({"credentials_master_key": None}, ["CREDENTIALS_MASTER_KEY tanımsız"]),
    ({"admin_password": ""}, ["ADMIN_PASSWORD tanımsız"]),
    ({"jwt_secret": security._DEV_SECRET, "admin_password": None},
     ["JWT_SECRET koddaki geliştirme varsayılanına eşit", "ADMIN_PASSWORD tanımsız"]),
])
def test_startup_refuses_insecure_secrets_in_production(over, expected):
    problems = insecure_fields(_settings(**over))
    log("üretim denetimi", {"ayar": over or "hepsi tanımlı", "sonuç": problems or "sorun yok"})
    assert [p.split(" (")[0] for p in problems] == expected
    if expected:
        with pytest.raises(RuntimeError) as error:
            enforce_secret_policy(_settings(**over))
        assert all(e.split(" ")[0] in str(error.value) for e in expected)
    else:
        enforce_secret_policy(_settings(**over))  # yeşil: hata yok


def test_sqlite_development_is_not_production_and_env_override_works(monkeypatch):
    development = _settings(database_url="sqlite+aiosqlite:///./data/dbace.db", jwt_secret=security._DEV_SECRET)
    assert not is_production(development)
    enforce_secret_policy(development)  # yerelde geliştirme varsayılanı serbest

    production = _settings(jwt_secret=security._DEV_SECRET)
    assert is_production(production)
    monkeypatch.setenv(ALLOW_INSECURE_ENV, "1")
    assert not is_production(production)
    enforce_secret_policy(production)  # bilinçli muafiyet


# --- 3. Varsayılan sırla üretilmiş jeton kabul edilmiyor ---------------------------------------------


async def test_token_signed_with_the_dev_secret_is_rejected(monkeypatch):
    await init_db()
    async with await authed_client() as client:
        me = (await client.get("/api/auth/me")).json()
        forged = jwt.encode(
            {"sub": str(me["id"]), "role": "admin", "type": "access",
             "iat": datetime.now(UTC), "exp": datetime.now(UTC) + timedelta(minutes=30)},
            security._DEV_SECRET, algorithm="HS256")
        monkeypatch.setattr(security.settings, "jwt_secret", "gercek-uretim-sirri-" + "y" * 40)
        response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    log("varsayılan sırla üretilmiş jeton", response.status_code)
    assert response.status_code == 401


# --- 4. Şifre değişince eski oturumlar ---------------------------------------------------------------


async def test_password_change_invalidates_tokens_issued_before_it():
    """ÖLÇÜM + düzeltme: JWT durumsuz olduğu için şifre değişimi eski jetonu kendiliğinden düşürmüyordu
    (access 60 dk, refresh 7 gün). Artık `iat` şifre değişim anından eskiyse jeton reddediliyor."""
    await init_db()
    async with await authed_client() as client:
        before = client.headers["Authorization"]
        me_before = (await client.get("/api/auth/me")).json()
        old_refresh = (await client.post("/api/auth/login",
                                         json={"username": me_before["username"], "password": _TEST_PASSWORD})
                       ).json()["refresh_token"]
        assert (await client.get("/api/auth/me")).status_code == 200
        # Gerçek bir oturum gibi: jeton şifre değişiminden ÖNCEKİ bir saniyede üretilmiş olsun
        # (jetonun `iat` değeri saniye hassasiyetinde).
        await asyncio.sleep(1.1)
        changed = await client.post("/api/auth/change-password",
                                    json={"current_password": _TEST_PASSWORD, "new_password": "Yeni_Parola_2026"})
        assert changed.status_code == 200
        old_token_after_change = await client.get("/api/auth/me", headers={"Authorization": before})
        username = changed.json()["username"]
        fresh = await client.post("/api/auth/login", json={"username": username, "password": "Yeni_Parola_2026"})
        new_token_works = await client.get("/api/auth/me",
                                           headers={"Authorization": f"Bearer {fresh.json()['access_token']}"})
        # Refresh jetonu da aynı kuralla düşüyor mu (7 gün geçerliydi)? Değişimden ÖNCE alınmış refresh jetonu:
        refreshed = await client.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
    log("şifre değişimi", {"değişim anı": user.password_changed_at, "eski access jetonu": old_token_after_change.status_code,
                           "yeni jeton": new_token_works.status_code, "eski jetonla refresh": refreshed.status_code})
    assert user.password_changed_at is not None
    assert old_token_after_change.status_code == 401
    assert new_token_works.status_code == 200
    assert refreshed.status_code == 401


async def test_admin_password_reset_also_drops_the_users_sessions():
    await init_db()
    async with await authed_client() as admin_client:
        username = f"viewer-{uuid.uuid4().hex[:8]}"
        created = await admin_client.post("/api/admin/users",
                                          json={"username": username, "password": "Ilk_Parola_2026", "role": "viewer"})
        assert created.status_code in (200, 201), created.text
        login = await admin_client.post("/api/auth/login", json={"username": username, "password": "Ilk_Parola_2026"})
        viewer_token = login.json()["access_token"]
        await asyncio.sleep(1.1)
        assert (await admin_client.get("/api/auth/me", headers={"Authorization": f"Bearer {viewer_token}"})).status_code == 200
        reset = await admin_client.post(f"/api/admin/users/{created.json()['id']}/reset-password")
        assert reset.status_code == 200
        after = await admin_client.get("/api/auth/me", headers={"Authorization": f"Bearer {viewer_token}"})
    log("yönetici şifre sıfırlaması sonrası eski jeton", after.status_code)
    assert after.status_code == 401
