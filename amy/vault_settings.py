"""Vault settings — cloud-sync vs local folder selection.

Lets the user choose which folder Amy uses as the active Obsidian vault:

  * cloud_sync ON  → use ``cloud_path`` (a folder kept in sync by Obsidian Sync /
    iCloud / Dropbox / Git — i.e. your "cloud" vault)
  * cloud_sync OFF → use ``local_path`` (a purely local folder)

Important honesty note: Amy cannot start or stop Obsidian's own Sync *service*
(that's internal to the Obsidian app). What this toggle controls is which folder
Amy treats as the vault — so turning it on points Amy at your cloud-synced vault,
and off points it at a local one. That is what makes "use cloud sync if enabled,
else local" work in practice.

Backed by a small JSON file so it works for both the single-user local app and
per-user in SaaS (one file per user under their index dir).
"""
from __future__ import annotations

import json
from pathlib import Path

_DEFAULT = {"cloud_sync": False, "cloud_path": "", "local_path": ""}


class VaultSettings:
    def __init__(self, store_path):
        self.path = Path(store_path)

    # --- persistence ----------------------------------------------------
    def get(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return {**_DEFAULT, **{k: data.get(k, _DEFAULT[k]) for k in _DEFAULT}}
            except Exception:
                pass
        return dict(_DEFAULT)

    def set(self, cloud_sync: bool | None = None, cloud_path: str | None = None,
            local_path: str | None = None) -> dict:
        cur = self.get()
        if cloud_sync is not None:
            cur["cloud_sync"] = bool(cloud_sync)
        if cloud_path is not None:
            cur["cloud_path"] = cloud_path.strip()
        if local_path is not None:
            cur["local_path"] = local_path.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
        return cur

    # --- resolution -----------------------------------------------------
    def active_path(self, default) -> str:
        """Resolve the folder Amy should use as the vault.

        Falls back to ``default`` if the chosen path is unset or does not exist,
        so a mis-typed path never leaves Amy with no vault."""
        s = self.get()
        chosen = s["cloud_path"] if s["cloud_sync"] else s["local_path"]
        if chosen and Path(chosen).expanduser().exists():
            return str(Path(chosen).expanduser())
        return str(default)

    def status(self, default) -> dict:
        """Settings + the resolved active path + whether it fell back."""
        s = self.get()
        active = self.active_path(default)
        chosen = s["cloud_path"] if s["cloud_sync"] else s["local_path"]
        return {
            **s,
            "active_path": active,
            "mode": "cloud" if s["cloud_sync"] else "local",
            "using_fallback": bool(chosen) and active == str(default) and
                              str(Path(chosen).expanduser()) != str(default),
        }
