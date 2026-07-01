"""GitHubSensor — normalizes GitHub activity and publishes it to the Event Bus.

Responsibilities:
  1. authenticate (delegated to GitHubService → env-var token only)
  2. normalize raw GitHub payloads into canonical GitHubEvent objects
  3. publish each event to the Event Bus (EventStore.publish)

It is a Sensor, not an agent: it never reasons or replies. Agents subscribe to
the ``github.*`` event types and decide what to do.

Two ingestion paths:
  * ingest_webhook(event_name, payload)  — for GitHub webhook deliveries
  * poll(owner_repo)                      — pulls the repo event feed via the API
  * ingest_raw(raw_events)               — normalize a list of API event dicts
"""
from __future__ import annotations

from .github_models import (
    GitHubEvent, NEW_REPOSITORY, NEW_COMMIT, NEW_PULL_REQUEST,
    NEW_ISSUE, NEW_RELEASE, CI_FAILURE,
)
from .github_service import GitHubService


try:
    from ..operational.sensors import Sensor as _Sensor
except Exception:  # operational layer optional at import time
    _Sensor = object


class GitHubSensor(_Sensor):
    name = "github"

    def __init__(self, event_store, service: GitHubService | None = None):
        self.events = event_store
        self.service = service or GitHubService()

    @property
    def authenticated(self) -> bool:
        return self.service.authenticated

    # --- publish --------------------------------------------------------
    def _publish(self, ev: GitHubEvent) -> str:
        return self.events.publish(ev.type, ev.to_payload(), source="github")

    # --- webhook normalization -----------------------------------------
    def ingest_webhook(self, event_name: str, payload: dict) -> GitHubEvent | None:
        """Normalize one GitHub webhook delivery (X-GitHub-Event header value)."""
        ev = self._from_webhook(event_name, payload or {})
        if ev:
            self._publish(ev)
        return ev

    def _from_webhook(self, name: str, p: dict) -> GitHubEvent | None:
        repo = (p.get("repository") or {}).get("full_name", "")
        sender = (p.get("sender") or {}).get("login", "")
        action = p.get("action", "")
        if name == "repository" and action in ("created", ""):
            r = p.get("repository") or {}
            return GitHubEvent(NEW_REPOSITORY, repo=repo, title=r.get("name", ""),
                               actor=sender, url=r.get("html_url", ""))
        if name == "push":
            commits = p.get("commits") or []
            head = (p.get("head_commit") or (commits[-1] if commits else {})) or {}
            return GitHubEvent(NEW_COMMIT, repo=repo,
                               title=head.get("message", "")[:140],
                               actor=(head.get("author") or {}).get("name", sender),
                               url=head.get("url", ""),
                               extra={"commit_count": len(commits)})
        if name == "pull_request":
            pr = p.get("pull_request") or {}
            return GitHubEvent(NEW_PULL_REQUEST, repo=repo, title=pr.get("title", ""),
                               actor=sender, url=pr.get("html_url", ""),
                               extra={"action": action, "number": pr.get("number")})
        if name == "issues":
            iss = p.get("issue") or {}
            return GitHubEvent(NEW_ISSUE, repo=repo, title=iss.get("title", ""),
                               actor=sender, url=iss.get("html_url", ""),
                               extra={"action": action, "number": iss.get("number")})
        if name == "release":
            rel = p.get("release") or {}
            return GitHubEvent(NEW_RELEASE, repo=repo,
                               title=rel.get("name") or rel.get("tag_name", ""),
                               actor=sender, url=rel.get("html_url", ""),
                               extra={"tag": rel.get("tag_name")})
        if name in ("workflow_run", "check_run", "check_suite"):
            run = p.get("workflow_run") or p.get("check_run") or p.get("check_suite") or {}
            conclusion = run.get("conclusion")
            if conclusion == "failure":
                return GitHubEvent(CI_FAILURE, repo=repo,
                                   title=run.get("name") or "CI failed",
                                   actor=sender, url=run.get("html_url", ""),
                                   extra={"conclusion": conclusion})
        return None

    # --- API feed normalization ----------------------------------------
    def ingest_raw(self, raw_events: list[dict]) -> list[GitHubEvent]:
        """Normalize + publish a list of GitHub API event dicts (the /events feed)."""
        out: list[GitHubEvent] = []
        for raw in raw_events or []:
            ev = self._from_api(raw)
            if ev:
                self._publish(ev)
                out.append(ev)
        return out

    def _from_api(self, raw: dict) -> GitHubEvent | None:
        t = raw.get("type")
        repo = (raw.get("repo") or {}).get("name", "")
        actor = (raw.get("actor") or {}).get("login", "")
        p = raw.get("payload") or {}
        if t == "CreateEvent" and p.get("ref_type") == "repository":
            return GitHubEvent(NEW_REPOSITORY, repo=repo, title=repo, actor=actor)
        if t == "PushEvent":
            commits = p.get("commits") or []
            msg = commits[-1]["message"][:140] if commits else ""
            return GitHubEvent(NEW_COMMIT, repo=repo, title=msg, actor=actor,
                               extra={"commit_count": len(commits)})
        if t == "PullRequestEvent":
            pr = p.get("pull_request") or {}
            return GitHubEvent(NEW_PULL_REQUEST, repo=repo, title=pr.get("title", ""),
                               actor=actor, url=pr.get("html_url", ""),
                               extra={"action": p.get("action")})
        if t == "IssuesEvent":
            iss = p.get("issue") or {}
            return GitHubEvent(NEW_ISSUE, repo=repo, title=iss.get("title", ""),
                               actor=actor, url=iss.get("html_url", ""),
                               extra={"action": p.get("action")})
        if t == "ReleaseEvent":
            rel = p.get("release") or {}
            return GitHubEvent(NEW_RELEASE, repo=repo,
                               title=rel.get("name") or rel.get("tag_name", ""),
                               actor=actor, url=rel.get("html_url", ""))
        return None

    # --- polling --------------------------------------------------------
    def poll(self, owner_repo: str) -> list[GitHubEvent]:
        """Pull the repo's recent event feed (needs a token) and publish them."""
        raw = self.service.fetch_events(owner_repo)
        return self.ingest_raw(raw)
