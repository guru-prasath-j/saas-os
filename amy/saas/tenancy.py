"""Per-user engines. Each user gets their own vault folder, vector collection,
and Engine instance — so retrieval is physically scoped to one tenant.

Engines are cached (one per user) and rebuilt on demand after a vault import.
"""
from __future__ import annotations

import os
import threading

from ..engine import Engine
from . import paths

_lock = threading.Lock()
_engines: dict[str, Engine] = {}
_MAX = int(os.getenv("AMY_MAX_CACHED_ENGINES", "200"))


def ensure_dirs(user_id: str) -> None:
    paths.vault_dir(user_id).mkdir(parents=True, exist_ok=True)
    paths.index_dir(user_id).mkdir(parents=True, exist_ok=True)


def _mark_private(eng: Engine, prefixes: list[str]) -> None:
    """Tag notes under the user's private folders as 'sensitive' so the LLM router
    keeps them on the local model (never a cloud key)."""
    if not prefixes:
        return
    for n in eng.notes:
        if any(n.path.startswith(p) for p in prefixes):
            tags = n.meta.get("tags") or []
            if "sensitive" not in tags:
                n.meta["tags"] = list(tags) + ["sensitive"]


def vault_settings_path(user_id: str):
    return paths.index_dir(user_id) / "vault_settings.json"


def _resolve_vault(user_id: str):
    """Active vault folder = cloud folder if cloud-sync is on, else local folder,
    falling back to the user's default managed vault."""
    from ..vault_settings import VaultSettings
    return VaultSettings(vault_settings_path(user_id)).active_path(
        default=paths.vault_dir(user_id))


def get_engine(user_id: str, openai_key: str | None = None,
               sensitive_prefixes: list[str] | None = None) -> Engine:
    """Return the user's engine. In SaaS we never use a shared cloud key:
    use_global_keys=False, and the user's own OpenAI key (if any) is used.
    `sensitive_prefixes` marks the user's private folders as sensitive.
    Engines are cached; call invalidate(user_id) after key/privacy changes."""
    with _lock:
        eng = _engines.get(user_id)
        if eng is None:
            ensure_dirs(user_id)
            eng = Engine(
                vault_path=_resolve_vault(user_id),
                index_dir=paths.index_dir(user_id),
                collection=paths.collection_name(user_id),
                openai_api_key=openai_key,
                use_global_keys=False,
            )
            _mark_private(eng, sensitive_prefixes or [])
            if len(_engines) >= _MAX:
                _engines.pop(next(iter(_engines)))  # simple FIFO eviction
            _engines[user_id] = eng
        return eng


def invalidate(user_id: str) -> None:
    """Drop the cached engine so the next request reloads the user's vault."""
    with _lock:
        _engines.pop(user_id, None)


def warm(user_id: str) -> int:
    """Build the user's index now (instead of lazily on first query). Returns
    the number of notes loaded."""
    eng = get_engine(user_id)
    try:
        eng._get_index()
    except Exception:
        pass
    return len(eng.notes)


def delete_user_data(user_id: str) -> None:
    """Full per-user wipe: cached engine, vector collection, vault + index dirs.
    Used for re-import and account deletion."""
    import shutil
    from ..index import drop_index

    invalidate(user_id)
    drop_index(paths.index_dir(user_id), paths.collection_name(user_id))
    shutil.rmtree(paths.vault_dir(user_id), ignore_errors=True)
    shutil.rmtree(paths.index_dir(user_id), ignore_errors=True)
