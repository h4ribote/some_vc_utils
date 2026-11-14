"""Encryption of credentials stored in the database, and random tokens, both derived from SVU_SECRET_KEY."""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["InvalidToken", "SecretBox", "new_token", "token_digest"]


class SecretBox:
    """Fernet encryption under a key derived from the secret key, so rotating SVU_SECRET_KEY makes every stored credential unreadable."""

    def __init__(self, secret_key: str) -> None:
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"svu stored credentials").derive(secret_key.encode("utf-8"))
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """The plaintext; raises InvalidToken when the value was not encrypted under this key."""
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")


def new_token() -> str:
    """A random URL-safe token with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """The SHA-256 hex digest under which a bearer token is stored, so a database copy does not hold usable tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
