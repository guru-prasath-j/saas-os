"""Connector registry — resolves a kind to a provider and enforces mode gating.

Private mode: email / calendar / tasks readable.
Public portfolio mode: all private connectors are hard-blocked.
"""
from __future__ import annotations

from .local import LocalEmailProvider, LocalCalendarProvider, LocalTaskProvider


class ConnectorRegistry:
    def __init__(self, data_dir):
        # local file providers are the default
        self._providers = {
            "email": LocalEmailProvider(data_dir),
            "calendar": LocalCalendarProvider(data_dir),
            "tasks": LocalTaskProvider(data_dir),
        }
        # if a Google token exists for this user, prefer the real Google providers
        try:
            from .google import build_google_providers
            for kind, prov in build_google_providers(data_dir).items():
                self._providers[kind] = prov
        except Exception:
            pass

    def source(self, kind: str) -> str:
        """'google' or 'local' — which backend serves this kind."""
        p = self._providers.get(kind)
        return "google" if (p and type(p).__module__.endswith("google")) else "local"

    def kinds(self) -> list[str]:
        return list(self._providers)

    def register(self, kind: str, provider):
        """Swap in a real API-backed provider (e.g. Gmail) at runtime."""
        self._providers[kind] = provider

    def list(self, kind: str, mode: str = "private", limit: int = 50) -> list[dict]:
        if kind not in self._providers:
            raise KeyError(kind)
        prov = self._providers[kind]
        if prov.private_only and mode != "private":
            raise PermissionError(f"'{kind}' is private-only and blocked in {mode} mode")
        return [i.__dict__ for i in prov.list(limit)]
