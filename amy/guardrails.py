"""Guardrails enforced by the master before any answer leaves the system."""
from __future__ import annotations
import re
from . import config
from .vault import Note

_ACCOUNT_RE = re.compile(r"\b(\d{9,18})\b")            # long digit runs = account numbers
_UPI_RE = re.compile(r"\b[\w.\-]+@[a-z]{2,}\b")        # upi ids like 8056...@sbi

# Words that mark a query as informational (a question), not a command to act.
_QUESTION_MARKERS = ("who", "what", "how much", "how many", "when", "which",
                     "list", "show", "tell me", "need to", "do i", "should i",
                     "remind", "summary", "?")
_AMOUNT_RE = re.compile(r"\b\d{2,}\b")                 # an explicit amount signals intent to act
_IMPERATIVE_START = ("transfer", "send", "pay", "withdraw", "delete", "remove")


def blocked_action(text: str) -> str | None:
    """Refuse genuine money-moving / irreversible *commands*, not questions.

    Blocks only when a money-moving verb appears AND the phrasing is an imperative
    (starts with an action verb, or carries an explicit amount / 'now') and is not a
    question.
    """
    low = text.lower().strip()
    if not low:
        return None
    first = low.split()[0]
    is_question = low.endswith("?") or any(m in low for m in _QUESTION_MARKERS)
    looks_imperative = (first in _IMPERATIVE_START) or bool(_AMOUNT_RE.search(low)) or (" now" in low)
    if is_question or not looks_imperative:
        return None
    for verb in config.BLOCKED_ACTION_VERBS:
        if verb in low:
            return verb.strip()
    return None


def touches_sensitive(notes: list[Note]) -> bool:
    return any(n.sensitive for n in notes)


def redact_for_voice(text: str) -> str:
    """Spoken channel: never read account numbers / UPI ids aloud."""
    text = _ACCOUNT_RE.sub("[account number - shown on screen]", text)
    text = _UPI_RE.sub("[UPI id - shown on screen]", text)
    return text
