"""Calendar Agent — finance due dates + Google Calendar events.

Unlike vault-RAG SubAgents, this agent is data-driven:
  1. Reads upcoming bills/renewals from finance.db (next 30 days)
  2. Optionally queries Google Calendar for upcoming events (if token exists)
  3. Answers queries like "what financial events are coming up?"
  4. Can push finance due-dates to Google Calendar (via push_to_calendar())

Wired into the PKOS registry by CollabMaster (not built from vault notes).
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


PERSONA = """You are the Calendar Agent for a personal finance OS.
You have access to the user's upcoming financial events (bill due dates,
subscription renewals, loan payments) and their Google Calendar (if linked).
Answer questions about upcoming financial events with specific dates and amounts.
Be concise and actionable. Never instruct money transfers.
"""


class CalendarAgent:
    """Domain agent for calendar/schedule queries."""
    name = "calendar"

    def __init__(self, notes: list | None = None,
                 finance_db_path: str | None = None,
                 connector_dir: str | None = None):
        self.notes = notes or []
        self._finance_db_path = finance_db_path
        self._connector_dir = connector_dir

    # -------------------------------------------------------------------------
    # Context builders
    # -------------------------------------------------------------------------

    def _finance_calendar_context(self, days: int = 30) -> str:
        if not self._finance_db_path:
            return ""
        if not os.path.exists(self._finance_db_path):
            return ""
        try:
            from ..finance import FinanceEngine
            fe = FinanceEngine(self._finance_db_path)
            try:
                bills = fe.upcoming_bills(days)
                if not bills:
                    return ""
                today = _dt.date.today().isoformat()
                lines = [f"[Finance Calendar — as of {today}]",
                         f"  Upcoming bills/renewals (next {days} days):"]
                for b in bills:
                    days_left = (
                        _dt.date.fromisoformat(b["renewal_date"]) -
                        _dt.date.today()
                    ).days
                    urgency = " ⚠ DUE SOON" if days_left <= 3 else ""
                    lines.append(
                        f"    • {b['name']} — due {b['renewal_date']}"
                        f" (in {days_left} days) — ₹{b['monthly_cost']:,.0f}/mo"
                        f"{urgency}"
                    )
                return "\n".join(lines)
            finally:
                fe.close()
        except Exception:
            return ""

    def _google_calendar_context(self, max_events: int = 10) -> str:
        if not self._connector_dir:
            return ""
        try:
            from ..connectors.google import load_credentials, TOKEN_FILENAME
            token_path = os.path.join(self._connector_dir, TOKEN_FILENAME)
            creds = load_credentials(token_path)
            if not creds:
                return ""
            from googleapiclient.discovery import build
            svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
            now = _dt.datetime.now(_dt.timezone.utc).isoformat()
            cutoff = (_dt.datetime.now(_dt.timezone.utc) +
                      _dt.timedelta(days=30)).isoformat()
            res = svc.events().list(
                calendarId="primary", timeMin=now, timeMax=cutoff,
                maxResults=max_events, singleEvents=True, orderBy="startTime"
            ).execute()
            items = res.get("items", [])
            if not items:
                return ""
            lines = ["[Google Calendar — next 30 days]"]
            for e in items:
                start = e.get("start", {})
                dt = start.get("dateTime", start.get("date", ""))
                lines.append(f"    • {e.get('summary', '(no title)')} — {dt}")
            return "\n".join(lines)
        except Exception:
            return ""

    def _build_context(self) -> str:
        parts = [
            self._finance_calendar_context(),
            self._google_calendar_context(),
        ]
        return "\n\n".join(p for p in parts if p)

    # -------------------------------------------------------------------------
    # PKOS DomainAgent interface
    # -------------------------------------------------------------------------

    def answer(self, query: str, llm=None, extra_context: str = "") -> dict:
        context = self._build_context()
        if extra_context:
            context = f"{extra_context}\n\n{context}" if context else extra_context
        if not context:
            return {
                "domain": "calendar",
                "answer": (
                    "No calendar data is available yet. "
                    "Add subscriptions/bills in Finance, or link Google Calendar."
                ),
                "sources": [],
                "model": "none",
                "abstained": False,
            }
        if llm is not None:
            text, model = llm.generate(PERSONA, query, context)
        else:
            text = context[:800]
            model = "none"
        return {"domain": "calendar", "answer": text,
                "sources": [], "model": model, "abstained": False}

    # -------------------------------------------------------------------------
    # Google Calendar push
    # -------------------------------------------------------------------------

    def push_finance_events_to_calendar(self, days: int = 30) -> dict:
        """Push upcoming finance due-dates as Google Calendar events.

        Returns {"created": N, "skipped": N, "errors": [...]}
        """
        if not self._connector_dir or not self._finance_db_path:
            return {"created": 0, "skipped": 0, "errors": ["connector or finance DB not configured"]}

        created, skipped, errors = 0, 0, []

        try:
            from ..connectors.google import load_credentials, TOKEN_FILENAME
            token_path = os.path.join(self._connector_dir, TOKEN_FILENAME)
            creds = load_credentials(token_path)
            if not creds:
                return {"created": 0, "skipped": 0,
                        "errors": ["Google not linked — authorize via connectors first"]}

            from googleapiclient.discovery import build
            svc = build("calendar", "v3", credentials=creds, cache_discovery=False)

            from ..finance import FinanceEngine
            fe = FinanceEngine(self._finance_db_path)
            try:
                bills = fe.upcoming_bills(days)
            finally:
                fe.close()

            for bill in bills:
                try:
                    date_str = bill["renewal_date"]
                    summary = f"[Amy] {bill['name']} renewal — ₹{bill['monthly_cost']:,.0f}"
                    # Check if event already exists (title match on that date)
                    existing = svc.events().list(
                        calendarId="primary",
                        timeMin=f"{date_str}T00:00:00Z",
                        timeMax=f"{date_str}T23:59:59Z",
                        q=f"[Amy] {bill['name']}",
                        maxResults=1,
                    ).execute().get("items", [])
                    if existing:
                        skipped += 1
                        continue
                    svc.events().insert(calendarId="primary", body={
                        "summary": summary,
                        "start": {"date": date_str},
                        "end": {"date": date_str},
                        "description": (
                            f"Subscription/bill renewal for {bill['name']}.\n"
                            f"Monthly cost: ₹{bill['monthly_cost']:,.0f}\n"
                            f"Auto-renew: {'Yes' if bill['auto_renew'] else 'No'}\n"
                            "Created by Amy Finance Agent."
                        ),
                    }).execute()
                    created += 1
                except Exception as exc:
                    errors.append(f"{bill['name']}: {exc}")

        except Exception as exc:
            errors.append(str(exc))

        return {"created": created, "skipped": skipped, "errors": errors}
