"""Metadata-filtered retrieval. Public mode never returns sensitive notes."""
from __future__ import annotations
from . import config
from .vault import Note


class Retriever:
    def __init__(self, index_getter):
        self._get = index_getter

    def search(self, query: str, scope_prefixes=None, k=5, allow_sensitive=True):
        if config.PUBLIC:
            allow_sensitive = False
        def allow(n: Note) -> bool:
            if scope_prefixes and not any(n.path.startswith(p) for p in scope_prefixes):
                return False
            if not allow_sensitive and n.sensitive:
                return False
            return True
        return self._get().search(query, k=k, allow=allow)
