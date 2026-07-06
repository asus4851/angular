"""Encryption of account credentials at rest (Fernet)."""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.fernet import Fernet

from clipfactory.config import get_settings


class CryptoError(RuntimeError):
    pass


def _fernet() -> Fernet:
    secret = get_settings().secret_key
    if not secret:
        raise CryptoError(
            "SECRET_KEY is not set. Generate one with `clipfactory gen-key` and put it in .env"
        )
    # Accept both a proper Fernet key and an arbitrary passphrase.
    try:
        return Fernet(secret.encode())
    except ValueError:
        digest = hashlib.sha256(secret.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))


def generate_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_credentials(credentials: dict) -> str:
    return _fernet().encrypt(json.dumps(credentials).encode()).decode()


def decrypt_credentials(token: str) -> dict:
    if not token:
        return {}
    return json.loads(_fernet().decrypt(token.encode()))
