"""VaultWatcher — poll-based file watcher for the Obsidian vault.

Uses mtime comparison instead of OS file-system events (no new processes,
works across platforms). Call ``check()`` on a schedule (e.g. every 30 s) to
detect `.md` files that changed since the last poll and emit ``vault.note_edited``
events.

Usage:
    from amy.vault_watcher import VaultWatcher
    watcher = VaultWatcher(event_store, vault_path="/path/to/vault")
    changed = watcher.check()  # returns list of changed paths
"""
from __future__ import annotations

import os
from pathlib import Path

from .events import store as _evstore


class VaultWatcher:
    """Detects changed .md files in the vault via mtime polling."""

    def __init__(self, event_store, vault_path: str | Path):
        self.events = event_store
        self.vault = Path(vault_path)
        self._mtimes: dict[str, float] = {}
        self._initialized = False

    def _scan(self) -> dict[str, float]:
        result: dict[str, float] = {}
        if not self.vault.exists():
            return result
        for root, _dirs, files in os.walk(self.vault):
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                full = os.path.join(root, fname)
                try:
                    result[full] = os.path.getmtime(full)
                except OSError:
                    pass
        return result

    def check(self) -> list[str]:
        """Scan vault, emit events for changed/new .md files.

        First call seeds the baseline (no events emitted — avoids flooding on
        startup). Subsequent calls emit ``vault.note_edited`` per changed file.
        Returns a list of changed absolute paths.
        """
        current = self._scan()

        if not self._initialized:
            self._mtimes = current
            self._initialized = True
            return []

        changed = []
        for path, mtime in current.items():
            prev = self._mtimes.get(path)
            if prev is None or mtime > prev:
                changed.append(path)

        for path in changed:
            rel = str(Path(path).relative_to(self.vault))
            try:
                self.events.publish(_evstore.VAULT_NOTE_EDITED, {
                    "path": rel,
                    "absolute": path,
                }, source="vault_watcher")
            except Exception:
                pass

        self._mtimes = current
        return changed
