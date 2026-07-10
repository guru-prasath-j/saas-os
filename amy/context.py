"""Rolling event window for LLM injection — NOT a general-purpose "context
engine". This is a small, single-purpose helper: a bounded deque of recent
events plus a markdown-bullet formatter, fed manually (not via pub/sub) by
its one real call site, amy/automation/orchestrator.py::_context_block(),
for goal-planning LLM prompts.

Chat context assembly (the thing CollabMaster.handle() injects into
/api/collab/ask) is a completely separate, federated code path that does
NOT use this class: it stitches together MemoryRecall.context_block(),
FinanceEngine.context_block(), captures.context_block(), and a live Plane
MCP call directly in amy/collab/orchestrator.py. GeoStore (amy/geo/) and
patterns.py (cadence detection) don't feed either path — they only drive
reactive agents and job proposals via events.

Usage:
    from amy.context import ContextModule
    cm = ContextModule(event_store)
    for ev in reversed(event_store.recent(n=30)):
        cm._on_event(ev)              # manual feed, not pub/sub — the real
                                       # bus subscription this once offered
                                       # (.attach()) had zero callers and
                                       # was removed
    print(cm.get_context(15))         # recent events as a bullet list
"""
from __future__ import annotations

from collections import deque

_MAX_EVENTS = 100
_CONTEXT_WINDOW = 20   # events shown in get_context()

# Locale layer (R7B): the display symbol is configuration, not code. Default
# comes from AMY_CURRENCY_SYMBOL (falls back to ₹ for existing installs);
# per-user formatting flows through amy/locale_fmt.py + jurisdiction packs.
import os as _os
CURRENCY_SYMBOL = _os.getenv("AMY_CURRENCY_SYMBOL", "₹")


def _money(amount: float) -> str:
    return f"{CURRENCY_SYMBOL}{abs(amount):,.0f}"


class ContextModule:
    """Bounded rolling window of recent events, formatted for LLM prompt
    injection. See the module docstring — manually fed, single call site."""

    def __init__(self, event_store, max_events: int = _MAX_EVENTS):
        self.events = event_store
        self._recent: deque[dict] = deque(maxlen=max_events)

    def _on_event(self, ev: dict) -> None:
        self._recent.appendleft(ev)

    # --- public API --------------------------------------------------------

    def get_context(self, n: int = _CONTEXT_WINDOW) -> str:
        """Return last ``n`` events as a markdown bullet list for LLM injection."""
        events = list(self._recent)[:n]
        if not events:
            return "No recent activity."
        lines = []
        for ev in events:
            ts = ev.get("ts", "")[:16].replace("T", " ")
            etype = ev.get("type", "")
            payload = ev.get("payload") or {}
            snippet = _format_payload(etype, payload)
            lines.append(f"- [{ts}] **{etype}**: {snippet}")
        return "\n".join(lines)


# --- formatting helpers ----------------------------------------------------

def _format_payload(etype: str, p: dict) -> str:
    if etype == "finance.transaction_added":
        amt = p.get("amount", 0)
        sign = "+" if amt > 0 else ""
        return f"{p.get('merchant', '?')} {sign}{_money(amt)} [{p.get('category', '?')}]"
    if etype == "finance.csv_imported":
        return f"{p.get('imported', 0)} tx from {p.get('bank_name', '?')} CSV"
    if etype == "finance.pdf_imported":
        return f"{p.get('imported', 0)} tx from {p.get('bank_name', '?')} PDF"
    if etype == "finance.gmail_synced":
        return f"{p.get('imported', 0)} tx imported, {p.get('accounts_synced', 0)} accounts"
    if etype == "finance.budget_set":
        return f"{p.get('category', '?')} → {_money(p.get('monthly_limit', 0))}/month"
    if etype == "finance.subscription_added":
        return f"{p.get('name', '?')} {_money(p.get('monthly_cost', 0))}/month"
    if etype == "finance.investment_added":
        return f"{p.get('name', '?')} ({p.get('type', '?')}) {_money(p.get('current_value', 0))}"
    if etype == "vault.note_edited":
        return p.get("path", "?")
    if etype == "goal.created":
        return p.get("title", "?")
    if etype == "goal.completed":
        return f"{p.get('title', '?')} ✓"
    summary = ", ".join(f"{k}={v}" for k, v in list(p.items())[:3])
    return summary or "(no payload)"
