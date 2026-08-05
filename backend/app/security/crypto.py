"""Symmetric encryption for secrets at rest (webhook signing secret).

Fernet keyed from SECRET_ENCRYPTION_KEY (any high-entropy string; we derive the
32-byte key via sha256). Rotating SECRET_ENCRYPTION_KEY invalidates stored
ciphertexts — the budgeting app must re-POST its webhook config after rotation.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    key_material = get_settings().secret_encryption_key
    if not key_material:
        raise RuntimeError("SECRET_ENCRYPTION_KEY is not set")
    derived = hashlib.sha256(key_material.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
