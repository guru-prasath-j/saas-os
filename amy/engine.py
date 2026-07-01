"""Engine. Loads notes immediately (fast); builds the vector index lazily on
first query so the dashboard/stats respond instantly.

A default singleton serves the single-user personal app (get_engine()). In SaaS
mode the tenancy layer creates one Engine per user by passing that user's
vault_path / index_dir / collection."""
from __future__ import annotations
from pathlib import Path
from . import vault as vaultmod, config
from .index import build_index
from .retrieval import Retriever
from .llm import LLMRouter
from .agents.master import MasterAgent


class Engine:
    def __init__(self, vault_path=None, index_dir=None, collection="vault",
                 openai_api_key=None, use_global_keys=True):
        import threading
        self._lock = threading.RLock()
        self.vault_path = Path(vault_path or config.VAULT)
        self.index_dir = index_dir or config.INDEX_DIR
        self.collection = collection
        self.openai_api_key = openai_api_key
        self.use_global_keys = use_global_keys
        self.notes = vaultmod.load_notes(self.vault_path)
        self._index = None
        self.backend = "pending (builds on first query)"
        self.retriever = Retriever(self._get_index)
        self.master = MasterAgent(
            self.retriever,
            LLMRouter(openai_api_key=openai_api_key, use_global_keys=use_global_keys),
            notes=self.notes,
        )
        
        # Start background vault file watcher
        self._watcher_thread = threading.Thread(target=self._watch_vault, daemon=True)
        self._watcher_thread.start()

    def _get_index(self):
        with self._lock:
            if self._index is None:
                self._index, self.backend = build_index(self.notes, self.index_dir, self.collection)
            return self._index

    def add_capture_note(self, rel_path: str):
        """Load one freshly-written capture note from disk into the live engine
        and refresh the index so Amy can answer about it immediately."""
        with self._lock:
            note = vaultmod.load_one(rel_path, self.vault_path)
            if note is None:
                return False
            self.notes[:] = [n for n in self.notes if n.path != note.path] + [note]
            # rebuild lazily on next query (Chroma upserts incrementally; keyword rebuilds)
            self._index = None
            self.backend = "pending (rebuilds on next query)"
            return True

    def _watch_vault(self):
        """Background thread that polls the vault directory for note updates/deletes every 2s."""
        import time
        
        def scan_vault():
            current = {}
            try:
                if not self.vault_path.exists():
                    return current
                for p in self.vault_path.rglob("*.md"):
                    rel = str(p.relative_to(self.vault_path)).replace("\\", "/")
                    if rel.startswith("_Amy/") or rel.startswith("_Jarvis/") or rel.startswith(".git/") or rel.startswith(".obsidian/"):
                        continue
                    try:
                        current[rel] = p.stat().st_mtime
                    except Exception:
                        pass
            except Exception:
                pass
            return current

        # Initialize base state
        last_mtimes = scan_vault()

        while True:
            time.sleep(2)
            current = scan_vault()
            
            deleted = set(last_mtimes.keys()) - set(current.keys())
            modified = []
            for path, mtime in current.items():
                if path not in last_mtimes or mtime > last_mtimes[path]:
                    modified.append(path)
                    
            if deleted or modified:
                with self._lock:
                    new_notes = []
                    to_remove = deleted.union(modified)
                    for n in self.notes:
                        if n.path not in to_remove:
                            new_notes.append(n)
                            
                    for path in modified:
                        note = vaultmod.load_one(path, self.vault_path)
                        if note:
                            new_notes.append(note)
                            
                    self.notes[:] = new_notes
                    self._index = None
                    self.backend = "pending (rebuilt due to watcher change)"
                    
            last_mtimes = current

    def ask(self, query: str, channel: str = "text"):
        return self.master.handle(query, channel=channel)

    def confirm(self, proposal_id: str):
        return self.master.confirm(proposal_id)

    def stats(self):
        cats = {}
        for n in self.notes:
            cats[n.category or "?"] = cats.get(n.category or "?", 0) + 1
        return {"notes": len(self.notes), "index_backend": self.backend, "by_category": cats}

    def health(self):
        return {
            "ok": True, "notes": len(self.notes), "index_backend": self.backend,
            "index_built": self._index is not None,
            "providers": self.master.classifier.llm.status(),
            "auth": bool(config.AUTH_TOKEN), "vault": str(config.VAULT),
        }


def _vault_settings_path():
    return Path(config.INDEX_DIR).parent / "vault_settings.json"


def resolve_vault_path():
    """Active vault folder: cloud folder if cloud-sync is on, else local folder,
    falling back to config.VAULT."""
    from .vault_settings import VaultSettings
    return VaultSettings(_vault_settings_path()).active_path(default=config.VAULT)


_engine = None
def get_engine() -> "Engine":
    global _engine
    if _engine is None:
        _engine = Engine(vault_path=resolve_vault_path())
    return _engine


def reset_engine() -> "Engine":
    """Rebuild the engine against the currently-resolved vault folder. Call after
    changing vault settings so the new folder takes effect."""
    global _engine
    _engine = Engine(vault_path=resolve_vault_path())
    return _engine
