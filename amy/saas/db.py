"""Database layer (SQLAlchemy 2.0).

Defaults to a local SQLite file so it runs with zero infra. Set DATABASE_URL to a
Postgres URL in production (e.g. postgresql+psycopg://user:pass@host/db) with no
code changes.
"""
from __future__ import annotations

import datetime as _dt
import os
import uuid
from pathlib import Path

from sqlalchemy import Boolean, String, DateTime, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .paths import SAAS_DATA

SAAS_DATA.mkdir(parents=True, exist_ok=True)
DEFAULT_SQLITE = f"sqlite:///{(SAAS_DATA / 'amy_saas.db').as_posix()}"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE)

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, echo=False, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # user's own OpenAI key, encrypted at rest (BYO-key).
    openai_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # comma-separated vault folder prefixes the user marked private -> their notes
    # are treated as sensitive (kept on the local model, never sent to a cloud key).
    sensitive_folders: Mapped[str | None] = mapped_column(Text, nullable=True)
    # kill-switch for Account Aggregator — can disable regardless of env config.
    aa_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # free-text country/city, set at signup or later in profile — powers
    # locale-aware budget suggestions (cost-of-living norms differ by country).
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=lambda: _dt.datetime.now(_dt.timezone.utc))


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|running|done|failed
    markdown_notes: Mapped[int] = mapped_column(default=0)
    notes_loaded: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=lambda: _dt.datetime.now(_dt.timezone.utc))
    finished_at: Mapped[_dt.datetime | None] = mapped_column(DateTime, nullable=True)


class McpConnector(Base):
    """Layer-1 generic MCP source registration (see amy/connectors/mcp.py).

    Registering a row here only makes the server's tools callable — it does
    NOT write to the vault or event log. Only rows with promoted_to_sensor=True
    are picked up by the Layer-2 polling loop (amy/sensors/mcp_sensor.py).
    """
    __tablename__ = "mcp_connectors"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(120))
    server_url: Mapped[str] = mapped_column(String(500))
    auth_type: Mapped[str] = mapped_column(String(20))  # api_key | oauth | none
    # Fernet-encrypted (security.encrypt_secret) — never stored plaintext.
    auth_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # official | platform_api | scraping_backed | unofficial_risky
    risk_tier: Mapped[str] = mapped_column(String(24), default="unofficial_risky")
    promoted_to_sensor: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=lambda: _dt.datetime.now(_dt.timezone.utc))
    # Plaintext (not a secret — e.g. a Plane workspace slug, visible in the
    # browser URL already). Some MCP servers need a second, non-secret auth
    # parameter beyond the token itself; see amy/connectors/mcp.py _headers().
    auth_extra: Mapped[str | None] = mapped_column(String(200), nullable=True)


def _migrate_users_table() -> None:
    """Idempotent: add columns introduced after initial schema creation."""
    with engine.connect() as conn:
        existing = {row[1] for row in
                    conn.exec_driver_sql("PRAGMA table_info(users)").fetchall()}
        if "aa_enabled" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN aa_enabled INTEGER NOT NULL DEFAULT 1")
            conn.commit()
        if "location" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN location VARCHAR(120)")
            conn.commit()


def _migrate_mcp_connectors_table() -> None:
    """Idempotent: add columns introduced after initial schema creation."""
    with engine.connect() as conn:
        existing = {row[1] for row in
                    conn.exec_driver_sql("PRAGMA table_info(mcp_connectors)").fetchall()}
        if existing and "auth_extra" not in existing:
            conn.exec_driver_sql(
                "ALTER TABLE mcp_connectors ADD COLUMN auth_extra VARCHAR(200)")
            conn.commit()


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_users_table()
    _migrate_mcp_connectors_table()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
