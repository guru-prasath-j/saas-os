"""Shared FastAPI dependencies and per-user helpers used across multiple routers."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .db import User, get_db
from . import paths, security, tenancy


# ---------------------------------------------------------------------------
# Shared Pydantic schemas (used by 2+ routers)
# ---------------------------------------------------------------------------

class Query(BaseModel):
    text: str
    channel: str = "text"


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    uid = security.decode_token(authorization.split(" ", 1)[1])
    if not uid:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status_code=401, detail="user not found")
    return user


# ---------------------------------------------------------------------------
# Per-user helpers
# ---------------------------------------------------------------------------

def _user_key(user: "User") -> str:
    """Decrypt the user's OpenAI key, or '' so SaaS never uses a shared key."""
    if not user.openai_key_enc:
        return ""
    try:
        return security.decrypt_secret(user.openai_key_enc)
    except Exception:
        return ""


def _private_prefixes(user: "User") -> list[str]:
    return [p.strip() for p in (user.sensitive_folders or "").split(",") if p.strip()]


def _engine_for(user: "User"):
    return tenancy.get_engine(user.id, _user_key(user), _private_prefixes(user))


def _collab_db_path(user: "User") -> str:
    return str(paths.index_dir(user.id) / "collab.db")


def _collab_light(user: "User"):
    from ..collab import CollabDB, MemoryManager, PlannerAgent, ReflectionAgent, LearningAgent
    db = CollabDB(_collab_db_path(user))
    mem = MemoryManager(db)
    planner = PlannerAgent(db)
    reflection = ReflectionAgent(db, planner, mem)
    learning = LearningAgent(db, mem)
    return db, mem, planner, reflection, learning


def _connector_dir(user: "User"):
    return paths.index_dir(user.id) / "connectors"


def _knowledge_for(user: "User"):
    from ..knowledge import KnowledgeBase, make_embedder
    from ..llm import LLMRouter
    key = _user_key(user)
    embedder = make_embedder(openai_key=key or None)
    llm = LLMRouter(openai_api_key=key, use_global_keys=False)
    data_dir = paths.index_dir(user.id) / "knowledge"
    return KnowledgeBase(data_dir, embedder=embedder, llm=llm)


def _journal_user(user: "User") -> dict:
    """Catch up the vault journal from the persisted events table. Idempotent."""
    from ..memory import JournalSync
    from ..collab import CollabDB
    db = CollabDB(_collab_db_path(user))
    try:
        notes = _engine_for(user).notes
        return JournalSync(db, paths.vault_dir(user.id), notes=notes).sync()
    finally:
        db.close()
