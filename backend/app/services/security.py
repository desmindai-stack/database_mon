from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

from app.config import settings

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_DEV_SECRET = "dev-insecure-secret-change-me-in-production"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash — never a match, but don't 500 on it.
        return False


def _jwt_secret() -> str:
    if settings.jwt_secret == _DEV_SECRET:
        logger.warning(
            "JWT_SECRET not set (using the insecure dev default) — every restart invalidates "
            "existing sessions and anyone with the source can forge tokens. Set JWT_SECRET in "
            "production."
        )
    return settings.jwt_secret


def _create_token(user_id: int, role: str, token_type: Literal["access", "refresh"], expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_ALGORITHM)


def create_access_token(user_id: int, role: str) -> str:
    return _create_token(user_id, role, "access", timedelta(minutes=settings.access_token_expire_minutes))


def create_refresh_token(user_id: int, role: str) -> str:
    return _create_token(user_id, role, "refresh", timedelta(days=settings.refresh_token_expire_days))


def token_is_stale(payload: dict[str, Any], password_changed_at) -> bool:
    """Jeton, şifrenin değiştirildiği andan ÖNCE mi üretildi (Faz 31 Commit 9)?

    JWT durumsuz: şifre değişince eski jetonlar kendiliğinden düşmüyordu — access 60 dakika, refresh 7 gün
    daha geçerliydi. `iat` ile şifre değişim anı karşılaştırılıyor; hiç şifre değiştirmemiş kullanıcıda
    (NULL) kontrol yok.
    """
    if password_changed_at is None:
        return False
    issued_at = payload.get("iat")
    if issued_at is None:
        return True
    changed = password_changed_at if password_changed_at.tzinfo else password_changed_at.replace(tzinfo=timezone.utc)
    # Tolerans YOK: `iat` saniyeye yuvarlanıyor, şifre değişimi aynı saniyede olsa bile jeton düşsün
    # (şüpheli durumda kapalı taraf: bir kullanıcıya yeniden giriş yaptırmak, çalınmış jetonu yaşatmaktan iyi).
    return float(issued_at) < changed.timestamp()


def decode_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError (expired/invalid/malformed) — callers turn that into a 401."""
    return jwt.decode(token, _jwt_secret(), algorithms=[_ALGORITHM])
