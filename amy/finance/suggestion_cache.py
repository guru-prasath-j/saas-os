"""Recompute-and-cache entry point for the four "suggested from your
transactions" panels (Budget/Subscriptions/Investments/Income).

Each detector call is an LLM round-trip (and Finance CFO's per-user LLM
routing can force a slow local-only model for GSTIN/PAN-bearing data — see
CLAUDE.md's sensitivity-routing quirk). Running all four synchronously
every time a user opens a tab makes every click feel like a fresh scan.

This module is the fix: run all four ONCE, right after an import actually
changes the transaction data (background task, doesn't block the import
response), and cache the result in FinanceEngine.suggestion_cache. Tabs
then read the cache instantly; a manual "Rescan" still calls the original
live-detect endpoints for an on-demand fresh check.
"""
from __future__ import annotations

import logging

log = logging.getLogger("amy.finance.suggestions")

KINDS = ("budget", "subscription", "investment", "income")


def recompute_all_suggestions(engine, llm, location: str | None = None) -> dict:
    """Run every detector and cache its result. One failing detector never
    blocks the others (each wrapped independently) — same stance as
    LearningFeedSensor.poll_all's per-focus isolation."""
    results = {}

    try:
        from .budget_suggest import suggest_budgets
        results["budget"] = suggest_budgets(engine, location, llm)
    except Exception as exc:
        log.warning("suggestion_cache: budget recompute failed: %s", exc)
        results["budget"] = {"error": str(exc)[:200]}

    try:
        from .subscription_detect import detect_subscriptions
        results["subscription"] = {"suggestions": detect_subscriptions(engine, llm)}
    except Exception as exc:
        log.warning("suggestion_cache: subscription recompute failed: %s", exc)
        results["subscription"] = {"error": str(exc)[:200]}

    try:
        from .investment_detect import detect_investments
        results["investment"] = {"suggestions": detect_investments(engine, llm)}
    except Exception as exc:
        log.warning("suggestion_cache: investment recompute failed: %s", exc)
        results["investment"] = {"error": str(exc)[:200]}

    try:
        from .income_detect import detect_income
        results["income"] = {"suggestions": detect_income(engine, llm)}
    except Exception as exc:
        log.warning("suggestion_cache: income recompute failed: %s", exc)
        results["income"] = {"error": str(exc)[:200]}

    for kind, payload in results.items():
        engine.set_cached_suggestions(kind, payload)

    return {k: (len(v.get("suggestions", [])) if "error" not in v else "error")
            for k, v in results.items()}


def recompute_for_user(user_id: str, finance_db_path: str) -> dict:
    """Self-contained recompute for FastAPI BackgroundTasks — opens its own
    FinanceEngine + LLMRouter (mirrors learning_feed.sensor.refresh_for_user's
    stance: background tasks build their own dependencies, never reuse a
    request-scoped connection after the response has gone out)."""
    from .engine import FinanceEngine
    from ..llm import LLMRouter
    from ..saas.db import SessionLocal, User
    from ..saas.deps import _user_key

    fe = FinanceEngine(finance_db_path)
    try:
        s = SessionLocal()
        try:
            user = s.get(User, user_id)
            openai_key = _user_key(user) if user else ""
            location = getattr(user, "location", None) if user else None
        finally:
            s.close()
        llm = LLMRouter(openai_api_key=openai_key, use_global_keys=True)
        return recompute_all_suggestions(fe, llm, location)
    except Exception as exc:
        log.warning("suggestion_cache: recompute_for_user failed for %s: %s", user_id, exc)
        return {"error": str(exc)[:300]}
    finally:
        fe.close()
