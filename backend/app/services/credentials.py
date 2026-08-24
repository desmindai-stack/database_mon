from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

# node.options keys that hold connection secrets (Faz 3/4's db_username/
# db_password convention — Node has no dedicated password column like
# Instance). Only db_password is a secret; db_username/db_database follow
# Instance's own precedent of storing the username unencrypted.
NODE_SECRET_OPTION_KEYS = ("db_password",)


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


def encrypt_node_options(options: dict[str, Any] | None) -> dict[str, Any] | None:
    """Encrypt secret keys (db_password) in a Node.options payload before it's persisted."""
    if not options:
        return options
    result = dict(options)
    for key in NODE_SECRET_OPTION_KEYS:
        value = result.get(key)
        if value:
            result[key] = encrypt_secret(str(value))
    return result


def decrypt_node_options(options: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decrypt secret keys in a Node.options payload for collectors/audits that need to connect."""
    if not options:
        return options
    result = dict(options)
    for key in NODE_SECRET_OPTION_KEYS:
        value = result.get(key)
        if value:
            try:
                result[key] = decrypt_secret(str(value))
            except Exception:
                logger.warning("Node option '%s' could not be decrypted; using stored value as-is", key)
    return result


def redact_node_options(options: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip secret values from a Node.options payload before it's returned in an API response."""
    if not options:
        return options
    result = dict(options)
    for key in NODE_SECRET_OPTION_KEYS:
        if result.get(key):
            result[key] = "***"
    return result
