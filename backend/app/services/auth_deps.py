from __future__ import annotations

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.domain.topology import UserRole
from app.models import User
from app.services.security import decode_token

_bearer = HTTPBearer(auto_error=False)

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Oturum gerekli",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if creds is None:
        raise unauthorized
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise unauthorized from None
    if payload.get("type") != "access":
        raise unauthorized
    user = await db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise unauthorized
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bu işlem için admin yetkisi gerekli")
    return user


async def require_write_access(request: Request, user: User = Depends(get_current_user)) -> User:
    """Applied at router-include time (see main.py) instead of per-endpoint — any authenticated
    user can GET, only admin can POST/PUT/PATCH/DELETE. This is the "viewer salt-okunur" rule
    (Faz 15 İŞ 1): deliberately blanket, including diagnostic-flavored POSTs like connection
    tests or EXPLAIN — see SORULAR.md for why that's the safer default over an allowlist."""
    if request.method in _MUTATING_METHODS and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Viewer rolü salt-okunur — bu işlem admin gerektirir")
    return user
