"""Encrypt-at-rest for user-saved BYOK provider API keys (app/models.py's
UserApiKey). First reversible-encryption code in this codebase — existing
secret handling (User.set_password/check_password) is one-way hashing,
which doesn't apply here since a saved key must be recoverable to actually
call the provider.

Uses Fernet (authenticated symmetric encryption) rather than itsdangerous
(already a transitive Flask dep via session/CSRF signing) — itsdangerous's
serializers are tamper-evident, not confidential: their output is signed
but readable, so a DB dump would leak keys in the clear.

The Fernet key is derived from API_KEY_ENCRYPTION_KEY (an arbitrary
operator-supplied string, same ergonomics as SECRET_KEY) via SHA-256, so
the operator doesn't need to hand-generate a 32-byte urlsafe-base64 value.
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class DecryptionError(Exception):
    """Raised when a stored key can't be decrypted — wrong/rotated
    API_KEY_ENCRYPTION_KEY, or corrupted data. Callers should treat this as
    "this saved key is unusable," not crash (see graph.py's
    _resolve_user_llm_keys)."""


@lru_cache(maxsize=1)
def _fernet_for(raw_secret: str) -> Fernet:
    digest = hashlib.sha256(raw_secret.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    return _fernet_for(current_app.config['API_KEY_ENCRYPTION_KEY'])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode('ascii')).decode('utf-8')
    except InvalidToken as exc:
        raise DecryptionError('stored key could not be decrypted') from exc
