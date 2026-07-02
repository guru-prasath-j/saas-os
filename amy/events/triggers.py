"""Default triggers + digest builder.

Triggers are subscribers wired at startup so the system reacts to events
(e.g. completing a goal writes a memory note). The digest composes
reflection + learning + suggestions and is what a scheduler would run daily.
"""
from __future__ import annotations

from . import store


def register_default_triggers(events, memory):
    """Wire reactive behavior onto an EventStore."""
    def on_goal_completed(ev):
        title = ev["payload"].get("title", "a goal")
        memory.add_summary(f"🎉 Completed goal: {title}")

    def on_vault_imported(ev):
        n = ev["payload"].get("notes_loaded", "?")
        memory.add_summary(f"Imported vault ({n} notes).")

    def on_gmail_synced(ev):
        p = ev["payload"]
        imported = p.get("imported", 0)
        if imported > 0:
            memory.add_summary(
                f"Gmail sync imported {imported} transaction(s) "
                f"(skipped {p.get('skipped', 0)})."
            )

    def on_csv_imported(ev):
        p = ev["payload"]
        imported = p.get("imported", 0)
        bank = p.get("bank_name", "bank")
        if imported > 0:
            memory.add_summary(f"Imported {imported} transactions from {bank} CSV.")

    def on_pdf_imported(ev):
        p = ev["payload"]
        imported = p.get("imported", 0)
        bank = p.get("bank_name", "bank")
        if imported > 0:
            memory.add_summary(f"Imported {imported} transactions from {bank} PDF.")

    def on_budget_set(ev):
        p = ev["payload"]
        memory.add_summary(
            f"Budget set: {p.get('category', '?')} → ₹{p.get('monthly_limit', 0):,.0f}/month."
        )

    def on_subscription_added(ev):
        p = ev["payload"]
        memory.add_summary(f"Subscription added: {p.get('name', '?')}.")

    def on_investment_added(ev):
        p = ev["payload"]
        memory.add_summary(
            f"Investment added: {p.get('name', '?')} "
            f"({p.get('type', '?')}, ₹{p.get('current_value', 0):,.0f})."
        )

    def on_ledger_entry_posted(ev):
        p = ev["payload"]
        memory.add_summary(
            f"Ledger entry posted for {p.get('entity_name', '?')}: "
            f"₹{p.get('amount', 0):,.0f}."
        )

    events.subscribe(store.GOAL_COMPLETED, on_goal_completed)
    events.subscribe(store.VAULT_IMPORTED, on_vault_imported)
    events.subscribe(store.FINANCE_GMAIL_SYNCED, on_gmail_synced)
    events.subscribe(store.FINANCE_CSV_IMPORTED, on_csv_imported)
    events.subscribe(store.FINANCE_PDF_IMPORTED, on_pdf_imported)
    events.subscribe(store.FINANCE_BUDGET_SET, on_budget_set)
    events.subscribe(store.FINANCE_SUBSCRIPTION_ADDED, on_subscription_added)
    events.subscribe(store.FINANCE_INVESTMENT_ADDED, on_investment_added)
    events.subscribe(store.FINANCE_LEDGER_ENTRY_POSTED, on_ledger_entry_posted)


def build_digest(reflection, learning, planner, suggestions_fn, days: int = 7) -> dict:
    """Compose the proactive digest (what the scheduler emits)."""
    return {
        "reflection": reflection.weekly_summary(days),
        "trends": learning.trends(days),
        "recommendations": learning.recommendations(days),
        "suggestions": suggestions_fn(learning, reflection, planner, days)["suggestions"],
        "open_goals": [g for g in planner.list_goals() if g["status"] == "active"],
    }
