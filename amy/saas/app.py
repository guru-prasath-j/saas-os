"""PersonalOS SaaS — thin FastAPI entrypoint.

Run:  uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849

Creates the FastAPI app, registers all routers, and manages background tasks
(digest scheduler and weekly memory consolidation).
"""
from __future__ import annotations

import os

# SaaS always uses per-user folder agents; set before any amy.* import.
os.environ.setdefault("AMY_DYNAMIC_AGENTS", "1")

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .db import SessionLocal, User, init_db
from . import paths
from .routers import (
    auth, vault, knowledge, collab, intelligence,
    twin, events, memory, connectors, product, captures, habits, finance,
    notifications, mcp_connectors, business, automation, agent,
)

app = FastAPI(title="PersonalOS SaaS", version="0.1.0")

# Register all routers (intelligence before collab: /api/goals/overview is static)
app.include_router(auth.router)
app.include_router(vault.router)
app.include_router(knowledge.router)
app.include_router(intelligence.router)
app.include_router(collab.router)
app.include_router(twin.router)
app.include_router(events.router)
app.include_router(memory.router)
app.include_router(connectors.router)
app.include_router(product.router)
app.include_router(captures.router)
app.include_router(habits.router)
app.include_router(finance.router)
app.include_router(notifications.router)
app.include_router(mcp_connectors.router)
app.include_router(business.router)
app.include_router(automation.router)
app.include_router(agent.router)

_INDEX_HTML = Path(__file__).parent / "static" / "index.html"


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/memory", response_class=HTMLResponse)
def memory_page():
    return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/health")
def health():
    return {"ok": True, "app": "PersonalOS SaaS", "mode": "saas"}


# ---------------------------------------------------------------------------
# Background tasks (startup)
# ---------------------------------------------------------------------------

def _run_all_digests():
    from ..events.scheduler import generate_and_store
    from ..collab import CollabDB
    s = SessionLocal()
    try:
        users = s.query(User).all()
        user_map = {u.id: u.email for u in users}
        user_ids = list(user_map)
    finally:
        s.close()
    for uid in user_ids:
        path = str(paths.index_dir(uid) / "collab.db")
        if not os.path.exists(path):
            continue
        cdb = CollabDB(path)
        finance_db_path = str(paths.index_dir(uid) / "finance.db")
        try:
            from ..llm import LLMRouter
            try:
                _llm = LLMRouter(use_global_keys=True)
            except Exception:
                _llm = None
            generate_and_store(cdb, finance_db_path=finance_db_path,
                               user_email=user_map.get(uid), llm=_llm)
            try:
                from ..operational.scheduler import run_ops_maintenance
                run_ops_maintenance(cdb,
                                    connector_dir=paths.index_dir(uid) / "connectors")
            except Exception:
                pass
            try:
                if os.path.exists(finance_db_path):
                    from ..finance.engine import FinanceEngine
                    from ..finance.custodial import check_custodial_nudges
                    from ..notifications import NotificationStore
                    fe = FinanceEngine(finance_db_path)
                    try:
                        store = NotificationStore(cdb)
                        for acc in fe.list_accounts():
                            if acc.get("account_type") == "custodial":
                                check_custodial_nudges(fe, store, acc)
                    finally:
                        fe.close()
            except Exception:
                pass
        except Exception:
            pass
        finally:
            cdb.close()


async def _digest_loop():
    import asyncio
    hours = float(os.getenv("AMY_DIGEST_INTERVAL_HOURS", "24"))
    while True:
        try:
            await asyncio.to_thread(_run_all_digests)
        except Exception:
            pass
        await asyncio.sleep(max(0.1, hours) * 3600)


def _run_all_consolidations():
    from ..memory.consolidate import Consolidator
    s = SessionLocal()
    try:
        user_ids = [u.id for u in s.query(User).all()]
    finally:
        s.close()
    for uid in user_ids:
        vault_path = paths.vault_dir(uid)
        if not vault_path.exists():
            continue
        try:
            Consolidator(vault_path).weekly()
        except Exception:
            pass


async def _consolidation_loop():
    import asyncio
    while True:
        await asyncio.sleep(7 * 24 * 3600)
        try:
            await asyncio.to_thread(_run_all_consolidations)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Gmail auto-poll (Option A) — runs every 30 min for all linked accounts
# ---------------------------------------------------------------------------

def _run_gmail_poll():
    """Poll Gmail for new bank emails for every user who has a Google token."""
    from ..finance.engine import FinanceEngine
    from ..connectors.google import load_credentials, TOKEN_FILENAME
    from ..llm import LLMRouter
    from ..finance.sync.gmail_import import sync_gmail as _sync_gmail
    import datetime, uuid as _uuid

    s = SessionLocal()
    try:
        users = s.query(User).all()
    finally:
        s.close()

    # Only sync emails from the last 24 h to keep each poll fast
    since = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    for user in users:
        uid = user.id
        token_path = paths.index_dir(uid) / "connectors" / TOKEN_FILENAME
        if not token_path.exists():
            continue
        try:
            creds = load_credentials(str(token_path))
        except Exception:
            continue
        if creds is None:
            continue

        finance_path = paths.index_dir(uid) / "finance.db"
        if not finance_path.exists():
            continue

        fe = FinanceEngine(str(finance_path))
        cdb = None
        try:
            accounts = fe.list_accounts()
            try:
                llm = LLMRouter(use_global_keys=True)
            except Exception:
                llm = None

            savings_accounts = [a for a in accounts
                                if a.get("account_type") in ("savings", "current", None, "")]
            if savings_accounts:
                primary = savings_accounts[0]
                aid = primary["id"]
                bank = primary.get("bank_name", "Bank")

                # Find or create CC account
                row = fe.conn.execute(
                    "SELECT id FROM accounts WHERE bank_name=? AND account_type='credit_card' LIMIT 1",
                    (bank,)
                ).fetchone()
                cc_aid = row[0] if row else fe.add_account(
                    nickname=f"{bank} Credit Card",
                    bank_name=bank,
                    account_type="credit_card",
                )

                _sync_gmail(creds, fe, aid, llm,
                            since=since,
                            max_messages=100,
                            cc_account_id=cc_aid)

            # Custodial accounts get bank alerts too — refills just aren't
            # the user's own income (see amy/finance/custodial.py).
            custodial_accounts = [a for a in accounts if a.get("account_type") == "custodial"]
            if custodial_accounts:
                from ..finance.custodial import emit_refill_events
                from ..events.store import EventStore
                from ..collab import CollabDB
                collab_path = paths.index_dir(uid) / "collab.db"
                if collab_path.exists():
                    cdb = CollabDB(str(collab_path))
                    events = EventStore(cdb)
                    for acc in custodial_accounts:
                        result = _sync_gmail(creds, fe, acc["id"], llm,
                                             since=since, max_messages=100)
                        emit_refill_events(fe, events, result.transactions)
        except Exception:
            pass
        finally:
            fe.close()
            if cdb is not None:
                cdb.close()


async def _gmail_poll_loop():
    import asyncio
    interval = float(os.getenv("AMY_GMAIL_POLL_MINUTES", "30")) * 60
    # First poll 60 s after startup so logs are clean
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(_run_gmail_poll)
        except Exception:
            pass
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Automation scheduler — ticks every minute, runs due jobs per user, with a
# run ledger + Approval Inbox (see amy/automation/). Replaces nothing: the
# legacy digest/consolidation/gmail loops keep running unchanged.
# ---------------------------------------------------------------------------

def _run_automation_tick():
    from ..automation import build_ctx, run_due
    from ..collab import CollabDB
    from ..llm import LLMRouter

    s = SessionLocal()
    try:
        users = [(u.id, u.email) for u in s.query(User).all()]
    finally:
        s.close()

    for uid, email in users:
        index_dir = paths.index_dir(uid)
        collab_path = index_dir / "collab.db"
        if not collab_path.exists():
            continue
        cdb = CollabDB(str(collab_path))
        try:
            try:
                llm = LLMRouter(use_global_keys=True)
            except Exception:
                llm = None
            ctx = build_ctx(uid, email, cdb, index_dir, llm_router=llm)
            run_due(ctx)
        except Exception:
            pass   # per-user failure must never kill the scheduler
        finally:
            cdb.close()


async def _automation_loop():
    import asyncio
    interval = float(os.getenv("AMY_AUTOMATION_TICK_SECONDS", "60"))
    await asyncio.sleep(30)   # let startup finish first
    while True:
        try:
            await asyncio.to_thread(_run_automation_tick)
        except Exception:
            pass
        await asyncio.sleep(max(10.0, interval))


@app.on_event("startup")
async def _startup():
    import asyncio
    init_db()
    asyncio.create_task(_digest_loop())
    asyncio.create_task(_consolidation_loop())
    asyncio.create_task(_gmail_poll_loop())
    asyncio.create_task(_automation_loop())
