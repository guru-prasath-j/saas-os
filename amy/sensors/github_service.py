"""GitHubService — authentication + raw fetch for the GitHub Sensor.

SECURITY: the GitHub token is NEVER stored in code. It is read from the
environment variable ``GITHUB_TOKEN`` (or a custom var name passed in). If no
token is present the service runs in offline mode and simply returns nothing,
so the rest of PIOS keeps working without GitHub.

Network calls use the stdlib only (urllib) and are best-effort; any failure
returns an empty list rather than raising, keeping the sensor resilient.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error

API = "https://api.github.com"


class GitHubService:
    def __init__(self, token_env: str = "GITHUB_TOKEN", api_base: str = API):
        # Read token from the environment ONLY. Never hardcode or persist it.
        self.token_env = token_env
        self.token = os.environ.get(token_env, "").strip()
        self.api_base = api_base.rstrip("/")

    @property
    def authenticated(self) -> bool:
        return bool(self.token)

    def _get(self, path: str) -> list | dict | None:
        if not self.authenticated:
            return None
        url = f"{self.api_base}{path}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "PIOS-GitHubSensor",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
            return None

    def fetch_events(self, owner_repo: str) -> list[dict]:
        """Fetch the recent raw event feed for a 'owner/repo'. Empty if offline."""
        data = self._get(f"/repos/{owner_repo}/events")
        return data if isinstance(data, list) else []

    def fetch_user_events(self, username: str) -> list[dict]:
        data = self._get(f"/users/{username}/events")
        return data if isinstance(data, list) else []
