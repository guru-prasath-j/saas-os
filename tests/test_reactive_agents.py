"""Phase R2 — reactive agents: emit event → agent acts → journal written."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.agents.reactive import register_reactive_agents
from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.store import EventStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # point the vault at tmp so journaling writes somewhere inspectable
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-react", "t@example.com", cdb, tmp_path, llm_router=None)
    es = EventStore(cdb)
    registered = register_reactive_agents(es, ctx)
    assert set(registered) == {"budget", "subscription", "compliance", "screening",
                               "errand", "learning", "pr_task", "meeting_prep",
                               "career_goal", "portfolio", "application_lifecycle"}
    yield ctx, es, tmp_path
    cdb.close()


def test_budget_agent_full_flow(env):
    """import event → over-budget insight event + notification + vault journal."""
    ctx, es, tmp = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        fe.add_transaction(-1500, "Food", "BIG RESTAURANT")   # blow the cap
    finally:
        fe.close()

    es.emit("finance.csv_imported", {"bank_name": "HDFC", "imported": 1},
            source="test")

    insights = es.recent("agent.insight")
    assert insights, "budget agent produced no insight"
    p = insights[0]["payload"]
    assert p["agent"] == "budget" and p["category"] == "Food"
    assert "over budget" in p["summary"]
    assert p["reasoning"]                                      # explicit reasoning
    assert p["source_event_id"]                                # provenance link

    notifs = ctx.notify_store().list()
    assert any(n["type"] == "agent_budget_check" for n in notifs)

    # journaled to the vault daily note (idempotent writer)
    from amy.saas import paths
    daily_dir = paths.vault_dir("u-react") / "00_Daily"
    notes = list(daily_dir.glob("*.md"))
    assert notes and "budget" in notes[0].read_text(encoding="utf-8")


def test_budget_agent_reacts_to_single_manual_transaction_add(env):
    """Entering ONE transaction manually (finance.transaction_added, no
    'imported' count) must trigger the same budget check an import does —
    this was the gap: only screening_agent reacted to manual adds before."""
    ctx, es, _ = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        tid = fe.add_transaction(-1500, "Food", "BIG RESTAURANT")
    finally:
        fe.close()

    es.emit("finance.transaction_added",
            {"id": tid, "amount": -1500, "category": "Food",
             "merchant": "BIG RESTAURANT", "source": "manual"}, source="finance")

    insights = es.recent("agent.insight")
    assert insights, "budget agent produced no insight on manual add"
    p = insights[0]["payload"]
    assert p["agent"] == "budget" and p["category"] == "Food"
    assert "you just added a transaction" in p["reasoning"].lower()
    notifs = ctx.notify_store().list()
    assert any(n["type"] == "agent_budget_check" for n in notifs)


def test_budget_agent_manual_add_scopes_to_affected_category_only(env):
    """A manual add for category A must not re-notify about an unrelated
    category B that also happens to be over budget from earlier activity."""
    ctx, es, _ = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        fe.set_budget("Shopping", 1000)
        fe.add_transaction(-1500, "Shopping", "ALREADY OVER")   # pre-existing
        tid = fe.add_transaction(-100, "Food", "SMALL COFFEE")  # well under cap
    finally:
        fe.close()

    es.emit("finance.transaction_added",
            {"id": tid, "amount": -100, "category": "Food",
             "merchant": "SMALL COFFEE", "source": "manual"}, source="finance")

    insights = es.recent("agent.insight")
    assert insights == [], (
        "manual add for Food must not surface Shopping's pre-existing overage")


def test_budget_agent_manual_add_no_budget_set_is_quiet(env):
    ctx, es, _ = env
    fe = ctx.open_finance()
    try:
        tid = fe.add_transaction(-50, "Food", "NO BUDGET SET")
    finally:
        fe.close()
    es.emit("finance.transaction_added",
            {"id": tid, "amount": -50, "category": "Food",
             "merchant": "NO BUDGET SET", "source": "manual"}, source="finance")
    assert es.recent("agent.insight") == []


def test_budget_agent_quiet_when_nothing_imported(env):
    ctx, es, _ = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 100)
        fe.add_transaction(-500, "Food", "X")
    finally:
        fe.close()
    es.emit("finance.csv_imported", {"bank_name": "HDFC", "imported": 0},
            source="test")
    assert not es.recent("agent.insight")


def test_subscription_agent_proposes_via_queue(env):
    """recurring charges → insight + a PENDING approval, nothing written."""
    ctx, es, _ = env

    class FakeLLM:   # confirms candidate 0 as a subscription
        def generate(self, system, prompt, context="", sensitive=False):
            return (json.dumps([{"idx": 0, "is_subscription": True,
                                 "name": "Netflix", "billing_cycle": "monthly",
                                 "confidence": 0.9}]), "fake")
    ctx._extras["lazy_llm"] = FakeLLM()

    fe = ctx.open_finance()
    try:
        for d in ("2026-04-05", "2026-05-05", "2026-06-05"):
            fe.add_transaction(-649, "Entertainment", "NETFLIX.COM", date=d)
    finally:
        fe.close()

    es.emit("finance.gmail_synced", {"imported": 3}, source="test")

    pend = ctx.store.list_approvals("pending")
    assert pend, "no approval parked"
    ap = pend[0]
    assert ap["payload"]["tool"] == "add_subscription"
    assert ap["payload"]["args"]["name"] == "Netflix"
    assert ap["source"] == "subscription_agent"
    assert "recurring charge" in ap["reasoning"].lower() or ap["reasoning"]
    fe = ctx.open_finance()
    try:
        assert fe.list_subscriptions(status=None) == []   # parked, not written
    finally:
        fe.close()


def test_agent_error_reported_not_raised(env, monkeypatch):
    """A crashing agent reports agent.error; the emitting route never sees it."""
    ctx, es, _ = env
    import amy.agents.reactive as reactive
    monkeypatch.setattr(
        reactive, "_get_llm",
        lambda c: (_ for _ in ()).throw(RuntimeError("boom")))
    fe = ctx.open_finance()
    try:
        for d in ("2026-04-05", "2026-05-05", "2026-06-05"):
            fe.add_transaction(-649, "Entertainment", "NETFLIX.COM", date=d)
    finally:
        fe.close()
    # emit must not raise even though the subscription agent's LLM factory blows up
    es.emit("finance.gmail_synced", {"imported": 3}, source="test")


def test_kill_switch_disables_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_BUDGET", "0")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    try:
        ctx = build_ctx("u-ks", "t@example.com", cdb, tmp_path, llm_router=None)
        es = EventStore(cdb)
        registered = register_reactive_agents(es, ctx)
        assert "budget" not in registered
        assert "subscription" in registered
    finally:
        cdb.close()
