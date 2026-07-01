"""Auth, account settings, and vault-settings routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import User, get_db
from .. import paths, security, tenancy
from ..deps import current_user, _private_prefixes

router = APIRouter()


class Credentials(BaseModel):
    email: str
    password: str


class OpenAIKey(BaseModel):
    key: str


class PrivateFolders(BaseModel):
    folders: list[str]


class VaultSettingsBody(BaseModel):
    cloud_sync: bool | None = None
    cloud_path: str | None = None
    local_path: str | None = None


class AAToggle(BaseModel):
    enabled: bool


@router.post("/auth/signup")
def signup(c: Credentials, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email == c.email)):
        raise HTTPException(status_code=409, detail="email already registered")
    if len(c.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    user = User(email=c.email, password_hash=security.hash_password(c.password))
    db.add(user)
    db.commit()
    tenancy.ensure_dirs(user.id)
    return {"token": security.create_token(user.id), "user": {"id": user.id, "email": user.email}}


@router.post("/auth/login")
def login(c: Credentials, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == c.email))
    if not user or not security.verify_password(c.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    return {"token": security.create_token(user.id), "user": {"id": user.id, "email": user.email}}


@router.get("/api/me")
def me(user: User = Depends(current_user)):
    return {
        "id": user.id,
        "email": user.email,
        "has_openai_key": bool(user.openai_key_enc),
        "aa_enabled": bool(user.aa_enabled if user.aa_enabled is not None else True),
    }


@router.post("/api/settings/openai-key")
def set_openai_key(body: OpenAIKey, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    if not body.key.strip():
        raise HTTPException(status_code=400, detail="empty key")
    user.openai_key_enc = security.encrypt_secret(body.key.strip())
    db.add(user)
    db.commit()
    tenancy.invalidate(user.id)
    return {"ok": True, "has_openai_key": True}


@router.delete("/api/settings/openai-key")
def clear_openai_key(user: User = Depends(current_user), db: Session = Depends(get_db)):
    user.openai_key_enc = None
    db.add(user)
    db.commit()
    tenancy.invalidate(user.id)
    return {"ok": True, "has_openai_key": False}


@router.get("/api/settings/private-folders")
def get_private_folders(user: User = Depends(current_user)):
    return {"folders": _private_prefixes(user)}


@router.put("/api/settings/private-folders")
def set_private_folders(body: PrivateFolders, user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    cleaned = [f.strip().strip("/") for f in body.folders if f.strip()]
    user.sensitive_folders = ",".join(cleaned)
    db.add(user)
    db.commit()
    tenancy.invalidate(user.id)
    return {"ok": True, "folders": cleaned}


@router.get("/api/settings/vault")
def get_vault_settings(user: User = Depends(current_user)):
    from ...vault_settings import VaultSettings
    vs = VaultSettings(tenancy.vault_settings_path(user.id))
    return vs.status(default=paths.vault_dir(user.id))


@router.post("/api/settings/vault")
def set_vault_settings(body: VaultSettingsBody, user: User = Depends(current_user)):
    from ...vault_settings import VaultSettings
    vs = VaultSettings(tenancy.vault_settings_path(user.id))
    vs.set(cloud_sync=body.cloud_sync, cloud_path=body.cloud_path, local_path=body.local_path)
    tenancy.invalidate(user.id)
    return vs.status(default=paths.vault_dir(user.id))


@router.get("/api/settings/aa-enabled")
def get_aa_enabled(user: User = Depends(current_user)):
    return {"aa_enabled": bool(user.aa_enabled if user.aa_enabled is not None else True)}


@router.post("/api/settings/aa-enabled")
def set_aa_enabled(body: AAToggle, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    user.aa_enabled = body.enabled
    db.add(user)
    db.commit()
    return {"ok": True, "aa_enabled": user.aa_enabled}
