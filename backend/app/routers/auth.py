from __future__ import annotations

from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.schemas import (
    AccessTokenOut,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenOut,
    UserOut,
)
from app.services.auth_deps import get_current_user
from app.services.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenOut:
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    # Same generic message whether the username doesn't exist or the password is wrong — don't
    # let the error itself confirm which usernames are registered.
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Kullanıcı adı veya şifre hatalı")
    if user is None or not verify_password(payload.password, user.password_hash):
        raise invalid
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bu hesap pasif durumda")

    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(user)

    return TokenOut(
        access_token=create_access_token(user.id, user.role),
        refresh_token=create_refresh_token(user.id, user.role),
        user=UserOut.model_validate(user),
    )


@router.post("/refresh", response_model=AccessTokenOut)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)) -> AccessTokenOut:
    invalid = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Geçersiz veya süresi dolmuş refresh token")
    try:
        decoded = decode_token(payload.refresh_token)
    except jwt.PyJWTError:
        raise invalid from None
    if decoded.get("type") != "refresh":
        raise invalid
    user = await db.get(User, int(decoded["sub"]))
    if user is None or not user.is_active:
        raise invalid
    return AccessTokenOut(access_token=create_access_token(user.id, user.role))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(_: User = Depends(get_current_user)) -> None:
    # Stateless JWTs — nothing to invalidate server-side (see SORULAR.md for the tradeoff).
    # Kept as a real endpoint so the frontend has a symmetric call, and so server-side session
    # revocation can be added later without an API shape change.
    return None


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/change-password", response_model=UserOut)
async def change_password(
    payload: ChangePasswordRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> UserOut:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mevcut şifre hatalı")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    await db.commit()
    await db.refresh(user)
    return UserOut.model_validate(user)
