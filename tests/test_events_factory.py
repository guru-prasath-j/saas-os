"""Part 0 (CONNECTOR COMPLETION) — amy.events.factory.get_events() and the
idempotent-registration / zero-subscriber-warning guardrails on EventStore.
"""
import logging
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.agents.reactive import register_reactive_agents
from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.factory import get_events
from amy.events.store import EventStore


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    ctx = build_ctx("u-evfac", "t@example.com", cdb, tmp_path, llm_router=None)
    yield ctx, cdb, tmp_path
    cdb.close()


def test_factory_built_store_fires_reactive_agent(env):
    """get_events() wires agents automatically — no manual
    register_reactive_agents() call needed at the emit site."""
    ctx, cdb, tmp = env
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 1000)
        fe.add_transaction(-1500, "Food", "BIG RESTAURANT")
    finally:
        fe.close()

    es = get_events("u-evfac", cdb, index_dir=tmp, user_email="t@example.com")
    es.emit("finance.csv_imported", {"bank_name": "HDFC", "imported": 1}, source="test")

    insights = es.recent("agent.insight")
    assert insights, "factory-built store produced no agent reaction"
    assert insights[0]["payload"]["agent"] == "budget"


def test_bare_store_warns_zero_subscribers(env, caplog):
    """A bare EventStore(cdb) emitting an agent-relevant event type must log
    a loud warning instead of silently dropping the reaction."""
    ctx, cdb, tmp = env
    es = EventStore(cdb)   # intentionally NOT wired via get_events()

    def _emit_from_one_site():
        # both calls made from this exact line — same call-site by design
        es.emit("finance.transaction_added", {"category": "Food"}, source="test")

    with caplog.at_level(logging.WARNING, logger="amy.events"):
        _emit_from_one_site()
    assert any("ZERO subscribers" in r.message for r in caplog.records)

    # a second emit from the SAME call site must not warn again (once per
    # process per call-site)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="amy.events"):
        _emit_from_one_site()
    assert not any("ZERO subscribers" in r.message for r in caplog.records)


def test_double_registration_fires_agent_exactly_once(env, monkeypatch):
    """Calling register_reactive_agents twice on the same EventStore must not
    double-subscribe an agent — one emit runs the handler exactly once.
    Uses the subscription agent (non-deduped tool call: it never sets
    agent_dedup_key) specifically because dedup-key collapsing would mask a
    double-fire bug for deduped agents while non-deduped ones keep double-
    proposing — this test must NOT rely on dedup to pass."""
    ctx, cdb, tmp = env

    import amy.finance.subscription_detect as sub_detect
    calls = {"n": 0}

    def fake_detect(fe, llm):
        calls["n"] += 1
        return [{"name": "Netflix", "amount": 649.0, "confidence": 0.9,
                 "billing_cycle": "monthly", "occurrences": 3,
                 "last_date": "2024-01-01", "next_due": "2024-02-01"}]
    monkeypatch.setattr(sub_detect, "detect_subscriptions", fake_detect)

    es = get_events("u-evfac", cdb, index_dir=tmp, user_email="t@example.com", ctx=ctx)
    before = len(es._handlers.get("finance.csv_imported", []))
    # second registration on the SAME instance — must be a no-op
    registered_again = register_reactive_agents(es, ctx)
    after = len(es._handlers.get("finance.csv_imported", []))
    assert before == after, "register_reactive_agents subscribed a duplicate handler"
    assert "subscription" in registered_again

    es.emit("finance.csv_imported", {"bank_name": "HDFC", "imported": 1}, source="test")

    assert calls["n"] == 1, f"subscription detector ran {calls['n']} times, expected 1"
    approvals = ctx.store.list_approvals(status=None, limit=50)
    tool_calls = [a for a in approvals if a["action_type"] == "tool_call"
                 and a["payload"].get("tool") == "add_subscription"]
    assert len(tool_calls) == 1, (
        f"expected exactly 1 add_subscription approval row, got {len(tool_calls)}")


def test_factory_import_is_isolated_no_circular_import():
    """amy.events.factory must not import amy.agents.reactive or
    amy.automation at module level (RISK A) — verified by importing it in a
    FRESH interpreter with nothing else pre-imported."""
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", "import amy.events.factory"],
        cwd=str(repo_root), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"bare import of amy.events.factory failed:\n{result.stderr}")
