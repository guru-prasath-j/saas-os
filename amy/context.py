"""Rolling context module — subscribes to all events and provides a concise
context snapshot for agent/LLM injection.

Usage:
    from amy.context import ContextModule
    ctx = ContextModule(event_store, finance_engine=fe)
    ctx.attach()                     # subscribe to event bus
    print(ctx.get_context())         # recent events as bullet list
    print(ctx.finance_summary())     # finance-specific summary
"""
from __future__ import annotations

import datetime as _dt
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

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
    """Maintains a rolling window of recent events for agent context injection."""

    def __init__(self, event_store, finance_engine=None, max_events: int = _MAX_EVENTS):
        self.events = event_store
        self.fe = finance_engine
        self._recent: deque[dict] = deque(maxlen=max_events)
        self._attached = False

    def attach(self) -> None:
        """Subscribe to all events on the bus. Call once at startup."""
        if not self._attached:
            self.events.subscribe("*", self._on_event)
            self._attached = True

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

    def finance_summary(self) -> str:
        """Return a concise finance snapshot for LLM context.

        Uses live FinanceEngine data if available, otherwise falls back to
        recent finance events from the rolling window.
        """
        if self.fe is not None:
            try:
                return _finance_from_engine(self.fe)
            except Exception:
                pass
        return _finance_from_events(list(self._recent))

    def since(self, minutes: int = 60) -> list[dict]:
        """Return events from the last ``minutes`` minutes."""
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes)
        result = []
        for ev in self._recent:
            ts_str = ev.get("ts", "")
            try:
                ts = _dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts >= cutoff:
                    result.append(ev)
            except Exception:
                pass
        return result

    def finance_events(self, n: int = 10) -> list[dict]:
        """Return last ``n`` finance-category events."""
        return [e for e in self._recent if e.get("type", "").startswith("finance.")][:n]


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


def _finance_from_engine(fe) -> str:
    lines = []
    try:
        overview = fe.overview()
        lines.append(f"Balance (30d): income {_money(overview.get('income_30d', 0))},"
                     f" spend {_money(overview.get('spend_30d', 0))}")
    except Exception:
        pass
    try:
        budgets = fe.budget_status()
        for b in (budgets or [])[:5]:
            pct = b.get("pct_used", 0)
            lines.append(f"Budget {b['category']}: {pct:.0f}% of {_money(b['monthly_limit'])}")
    except Exception:
        pass
    try:
        subs = fe.list_subscriptions()
        total = fe.subscription_total_monthly()
        lines.append(f"Subscriptions: {len(subs)} active, {_money(total)}/month")
    except Exception:
        pass
    return "\n".join(lines) if lines else "Finance data unavailable."


def _finance_from_events(events: list[dict]) -> str:
    finance_evs = [e for e in events if e.get("type", "").startswith("finance.")][:10]
    if not finance_evs:
        return "No recent finance activity."
    lines = [_format_payload(e["type"], e.get("payload") or {}) for e in finance_evs]
    return "\n".join(f"- {l}" for l in lines)
