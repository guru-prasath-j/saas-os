"""Phase 6 scaffold — proactive reminders (e.g. month-end payouts).

Run as a cron / APScheduler job. It reads the engine and pushes a reminder
(notification, or a message over the websocket) without moving any money.
"""
from __future__ import annotations
from .engine import get_engine

def month_end_payout_reminder() -> str:
    r = get_engine().ask("who do I need to pay this month and how much", channel="text")
    return r.answer

if __name__ == "__main__":
    print(month_end_payout_reminder())
