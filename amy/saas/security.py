"""Auth + crypto helpers: password hashing, JWT tokens, API-key encryption.

Notes for production:
- Passwords use PBKDF2-HMAC-SHA256 (stdlib). Argon2/bcrypt is stronger — swap in
  passlib[argon2] before launch.
- JWT signing: set AMY_JWT_SECRET (≥32 chars) in env. If it's shorter it is
  stretched to a 256-bit key via SHA-256; if unset, a strong secret is
  generated once and persisted at saas_data/.jwt_secret so sessions survive
  restarts. HS256 never runs with a weak (<32-byte) key.
- API-key encryption: set AMY_ENC_SECRET in env. Its fallback stays the
  legacy constant on purpose — deriving it from the (now auto-generated) JWT
  secret would silently make previously-encrypted user keys undecryptable.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import os
import secrets

import jwt  # PyJWT

_LEGACY_DEV_SECRET = "dev-insecure-change-me"


def _load_jwt_secret() -> str:
    env = os.getenv("AMY_JWT_SECRET", "").strip()
    if env:
        if len(env.encode()) >= 32:
            return env
        # stretch a short env secret to a full 256-bit key (deterministic,
        # so tokens stay valid across restarts with the same env value)
        return hashlib.sha256(env.encode()).hexdigest()
    # No env secret: generate once and persist so sessions survive restarts.
    try:
        from . import paths
        secret_path = paths.SAAS_DATA / ".jwt_secret"
        if secret_path.exists():
            existing = secret_path.read_text(encoding="utf-8").strip()
            if len(existing.encode()) >= 32:
                return existing
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        secret_path.write_text(generated, encoding="utf-8")
        return generated
    except Exception:
        # can't persist (read-only fs?) — strong process-lifetime key;
        # tokens just won't survive a restart
        return secrets.token_urlsafe(48)


JWT_SECRET = _load_jwt_secret()
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

    # Fallback is intentionally the legacy constant, NOT the (auto-generated)
    # JWT secret: existing installs encrypted user API keys under it, and a
    # changed key would make them silently undecryptable. Set AMY_ENC_SECRET.
    secret = os.getenv("AMY_ENC_SECRET", _LEGACY_DEV_SECRET)
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
