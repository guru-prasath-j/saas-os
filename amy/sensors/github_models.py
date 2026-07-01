"""GitHub event models — canonical, normalized shapes.

The sensor maps raw GitHub webhook/API payloads into these neutral events so the
rest of PIOS never depends on GitHub's payload format.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

# canonical GitHub event types (published on the bus as "github.<TYPE>")
NEW_REPOSITORY = "github.NEW_REPOSITORY"
NEW_COMMIT = "github.NEW_COMMIT"
NEW_PULL_REQUEST = "github.NEW_PULL_REQUEST"
NEW_ISSUE = "github.NEW_ISSUE"
NEW_RELEASE = "github.NEW_RELEASE"
CI_FAILURE = "github.CI_FAILURE"

GITHUB_EVENT_TYPES = [
    NEW_REPOSITORY, NEW_COMMIT, NEW_PULL_REQUEST,
    NEW_ISSUE, NEW_RELEASE, CI_FAILURE,
]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class GitHubEvent:
    type: str                      # one of GITHUB_EVENT_TYPES
    repo: str = ""                 # "owner/name"
    title: str = ""                # human summary (commit msg / PR title / …)
    actor: str = ""                # github login
    url: str = ""                  # link to the resource
    ts: str = field(default_factory=_now)
    extra: dict = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            "repo": self.repo, "title": self.title, "actor": self.actor,
            "url": self.url, "ts": self.ts, **self.extra,
        }
