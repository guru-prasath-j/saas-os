"""Server-side enforcement of public-mode restrictions. Applies even if API
endpoints are called directly (not just hidden in the UI)."""
from __future__ import annotations
from . import config

BLOCKED_MSG = ("This is the public demo of PersonalOS. Personal, financial and "
               "family data (and write/scheduler actions) are disabled here. "
               "Try asking about projects, skills, career, or the architecture.")


def agent_allowed(intent: str) -> bool:
    return intent not in config.BLOCKED_AGENTS


def writes_allowed() -> bool:
    return config.FEATURES["write"]


def sensitive_allowed() -> bool:
    return config.FEATURES["sensitive"]
