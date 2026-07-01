"""Filesystem layout for SaaS data (per-user vaults + indexes + uploads)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .. import config

# Root for all tenant data. Override with AMY_SAAS_DATA.
SAAS_DATA = Path(os.getenv("AMY_SAAS_DATA", str(config.HERE / "saas_data")))


def _safe_uid(user_id: str) -> str:
    # user ids are hex uuids, but guard against traversal regardless
    return re.sub(r"[^a-zA-Z0-9_]", "", user_id)


def vault_dir(user_id: str) -> Path:
    return SAAS_DATA / "vaults" / _safe_uid(user_id)


def index_dir(user_id: str) -> Path:
    return SAAS_DATA / "index" / _safe_uid(user_id)


def uploads_dir(user_id: str) -> Path:
    return SAAS_DATA / "uploads" / _safe_uid(user_id)


def collection_name(user_id: str) -> str:
    return f"vault_{_safe_uid(user_id)}"
