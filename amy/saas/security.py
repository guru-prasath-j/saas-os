"""Auth + crypto helpers: password hashing, JWT tokens, API-key encryption.

Notes for production:
- Passwords use PBKDF2-HMAC-SHA256 (stdlib). Argon2/bcrypt is stronger — swap in
  passlib[argon2] before launch.
- JWT secret and the key-encryption secret MUST be set via env in production
  (AMY_JWT_SECRET, AMY_ENC_SECRET). The dev fallbacks are insecure.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import os
import secrets

import jwt  # PyJWT

JWT_SECRET = os.getenv("AMY_JWT_SECRET", "dev-insecure-change-me")
JWT_ALGO = "HS256"
TOKEN_TTL_HOURS = int(os.getenv("AMY_JWT_TTL_HOURS", "168"))  # 7 days

_PBKDF2_ROUNDS = 200_000


# --- passwords --------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, b64salt, b64hash = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(b64salt)
        expected = base64.b64decode(b64hash)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# --- JWT --------------------------------------------------------------------
def create_token(user_id: str) -> str:
    now = _dt.datetime.utcnow()
    payload = {"sub": user_id, "iat": now, "exp": now + _dt.timedelta(hours=TOKEN_TTL_HOURS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> str | None:
    try:
        data = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return data.get("sub")
    except Exception:
        return None


# --- API-key encryption (Fernet) -------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet

    secret = os.getenv("AMY_ENC_SECRET", JWT_SECRET)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
