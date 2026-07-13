"""Subscription duplicate-detection bug fix — user-reported live: 'YouTube
Premium' appeared 3x in the Subscriptions tab at the same cost/renewal
date. Root cause was two-layered: (1) the reactive subscription_agent set
no agent_dedup_key, so every Gmail sync that still saw the same recurring
charge (it always does — the transaction never leaves history) re-proposed
a fresh approval; (2) FinanceEngine.add_subscription had no insert-time
guard, so each approval inserted another row. Both fixed; tested here.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.finance.engine import FinanceEngine


# --- engine-level insert dedup -----------------------------------------------

@pytest.fixture()
def fdb(tmp_path):
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


def test_add_subscription_same_name_updates_not_duplicates(fdb):
    sid1 = fdb.add_subscription("YouTube Premium", monthly_cost=149.0,
                                renewal_date="2026-06-29")
    sid2 = fdb.add_subscription("YouTube Premium", monthly_cost=149.0,
                                renewal_date="2026-06-29")
    sid3 = fdb.add_subscription("YouTube Premium", monthly_cost=149.0,
                                renewal_date="2026-06-29")
    assert sid1 == sid2 == sid3
    assert len(fdb.list_subscriptions(status="active")) == 1


def test_add_subscription_dedup_is_whitespace_case_insensitive(fdb):
    sid1 = fdb.add_subscription("YouTube Premium", monthly_cost=149.0)
    sid2 = fdb.add_subscription("  youtube   premium  ", monthly_cost=149.0)
    assert sid1 == sid2
    assert len(fdb.list_subscriptions(status="active")) == 1


def test_add_subscription_updates_cost_on_repeat_call(fdb):
    sid = fdb.add_subscription("Netflix", monthly_cost=649.0)
    fdb.add_subscription("Netflix", monthly_cost=699.0)   # price hike, same sub
    subs = fdb.list_subscriptions(status="active")
    assert len(subs) == 1
    assert subs[0]["id"] == sid
    assert subs[0]["monthly_cost"] == 699.0


def test_add_subscription_missing_renewal_date_does_not_clear_existing(fdb):
    sid = fdb.add_subscription("Spotify", monthly_cost=119.0, renewal_date="2026-08-01")
    fdb.add_subscription("Spotify", monthly_cost=119.0, renewal_date=None)
    subs = fdb.list_subscriptions(status="active")
    assert subs[0]["renewal_date"] == "2026-08-01"


def test_cancelled_then_resubscribed_creates_a_new_row(fdb):
    """A genuinely new subscription after cancellation must NOT be silently
    merged into the old (paused/cancelled) row — only ACTIVE rows dedup."""
    sid1 = fdb.add_subscription("Hotstar", monthly_cost=299.0, status="active")
    fdb.update_subscription(sid1, status="cancelled")
    sid2 = fdb.add_subscription("Hotstar", monthly_cost=299.0, status="active")
    assert sid1 != sid2
    assert len(fdb.list_subscriptions(status="active")) == 1


def test_different_names_never_merged(fdb):
    fdb.add_subscription("Netflix Basic", monthly_cost=199.0)
    fdb.add_subscription("Netflix Premium", monthly_cost=649.0)
    assert len(fdb.list_subscriptions(status="active")) == 2


# --- reactive-agent-level dedup key -------------------------------------------

@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-subdedup", "t@example.com", cdb, tmp_path, llm_router=None)
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)
    yield c
    cdb.close()


def _candidate(name="YouTube Premium", amount=149.0):
    return [{"name": name, "amount": amount, "billing_cycle": "monthly",
             "confidence": 0.9, "occurrences": 3, "last_date": "2026-06-01",
             "next_due": "2026-06-29"}]


def test_repeat_import_events_propose_exactly_once(ctx, monkeypatch):
    from amy.agents.reactive import _subscription_agent

    monkeypatch.setattr("amy.finance.subscription_detect.detect_subscriptions",
                        lambda fe, llm: _candidate())
    es = EventStore(ctx.collab)
    _subscription_agent(es, ctx)

    for _ in range(3):   # simulate 3 separate Gmail syncs re-detecting the same charge
        es.emit("finance.gmail_synced", {"imported": 1}, source="test")

    rows = ctx.collab.conn.execute(
        "SELECT * FROM approvals WHERE dedup_key LIKE 'subscription_%'").fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["tool"] == "add_subscription"
    assert payload["args"]["name"] == "YouTube Premium"


def test_dedup_key_normalizes_name(ctx, monkeypatch):
    from amy.agents.reactive import _subscription_agent

    calls = {"n": 0}

    def fake_detect(fe, llm):
        calls["n"] += 1
        name = "YouTube Premium" if calls["n"] == 1 else "  youtube   premium  "
        return _candidate(name=name)

    monkeypatch.setattr("amy.finance.subscription_detect.detect_subscriptions",
                        fake_detect)
    es = EventStore(ctx.collab)
    _subscription_agent(es, ctx)
    es.emit("finance.gmail_synced", {"imported": 1}, source="test")
    es.emit("finance.csv_imported", {"imported": 1}, source="test")

    rows = ctx.collab.conn.execute(
        "SELECT * FROM approvals WHERE dedup_key LIKE 'subscription_%'").fetchall()
    assert len(rows) == 1   # second detection's differently-cased/spaced
                            # name still maps to the same dedup key


def test_different_subscriptions_each_get_their_own_proposal(ctx, monkeypatch):
    from amy.agents.reactive import _subscription_agent

    monkeypatch.setattr(
        "amy.finance.subscription_detect.detect_subscriptions",
        lambda fe, llm: _candidate("YouTube Premium") + _candidate("Spotify", 119.0))
    es = EventStore(ctx.collab)
    _subscription_agent(es, ctx)
    es.emit("finance.gmail_synced", {"imported": 1}, source="test")

    rows = ctx.collab.conn.execute(
        "SELECT * FROM approvals WHERE dedup_key LIKE 'subscription_%'").fetchall()
    assert len(rows) == 2
