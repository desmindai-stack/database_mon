from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)


def _fernet() -> Fernet | None:
    key = settings.credentials_master_key
    if not key:
        return None
    digest = hashlib.sha256(key.encode()).digest()
    fkey = base64.urlsafe_b64encode(digest)
    return Fernet(fkey)


def encrypt_secret(plain: str) -> str:
    f = _fernet()
    if f is None:
        logger.warning("CREDENTIALS_MASTER_KEY not set; storing password without encryption (dev only)")
        return f"plain:{plain}"
    return f.encrypt(plain.encode()).decode()


def decrypt_secret(stored: str) -> str:
    if stored.startswith("plain:"):
        return stored[6:]
    f = _fernet()
    if f is None:
        raise RuntimeError("Encrypted credentials require CREDENTIALS_MASTER_KEY")
    try:
        return f.decrypt(stored.encode()).decode()
    except InvalidToken:
        # Legacy rows stored before encryption was enabled
        return stored
