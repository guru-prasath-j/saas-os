"""Optional bearer-token auth. Enforced only when AMY_AUTH_TOKEN is set."""
from __future__ import annotations
from fastapi import Header, HTTPException
from . import config


async def require_auth(authorization: str | None = Header(default=None)):
    if not config.AUTH_TOKEN:
        return  # open in local dev
    expected = f"Bearer {config.AUTH_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="missing or invalid token")
