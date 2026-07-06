"""Phase R7A-1 — values screening: presets are data, rules flag with
reasoning, agent proposes remediation via the queue, audit picks up flags."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.audit import build_audit_report
from amy.collab import CollabDB
from amy.events.store import EventStore
from amy.agents.reactive import register_reactive_agents
from amy.values import (enable_profile, get_preset, list_flags, list_presets,
                        list_profiles, mark_screened, persist_flags,
                        screen_transactions, set_flag_status, update_profile,
                        unscreened_transactions)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-val", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def test_presets_are_pure_data():
    ids = {p["id"] for p in list_presets()}
    assert ids == {"interest_free_finance", "esg_basic", "budget_discipline"}
    # code contains no religion/company/country branch: rules are declarative
    for p in list_presets():
        for r in p["rules"]:
            assert r["kind"] in ("description_pattern", "category",
                                 "amount_share_of_income", "financing_type")
            assert r.get("reason")


def test_interest_free_profile_flags_interest_charge(ctx):
    fe = ctx.open_finance()
    try:
        pid = enable_profile(fe, preset_id="interest_free_finance")
        tid = fe.add_transaction(-350, "EMI/Loan",
                                 "FINANCE CHARGE INTEREST ON CARD",
                                 date="2026-07-01")
        fe.add_transaction(-500, "Food", "SWIGGY", date="2026-07-02")
        profiles = list_profiles(fe, enabled_only=True)
        flags = screen_transactions(fe, fe.list_transactions(limit=10), profiles)
    finally:
        fe.close()
    assert pid
    assert len(flags) == 1
    f = flags[0]
    assert f["transaction_id"] == tid
    assert f["severity"] == "high"
    assert "interest" in f["reasoning"].lower()
    assert f["profile_name"] == "Interest-free finance"


def test_budget_discipline_share_of_income(ctx):
    fe = ctx.open_finance()
    try:
        fe.add_income_source("Job", amount=50000, recurrence="monthly")
        enable_profile(fe, preset_id="budget_discipline")
        fe.add_transaction(-15000, "Shopping", "BIG TV STORE", date="2026-07-01")
        fe.add_transaction(-15000, "Rent", "LANDLORD", date="2026-07-01")   # excluded cat
        fe.add_transaction(-2000, "Shopping", "SMALL BUY", date="2026-07-02")
        profiles = list_profiles(fe, enabled_only=True)
        flags = screen_transactions(fe, fe.list_transactions(limit=10), profiles)
    finally:
        fe.close()
    assert len(flags) == 1
    assert "30% of monthly income" in flags[0]["reasoning"]


def test_esg_profile(ctx):
    fe = ctx.open_finance()
    try:
        enable_profile(fe, preset_id="esg_basic")
        fe.add_transaction(-1000, "Entertainment", "ROYAL CASINO ONLINE",
                           date="2026-07-01")
        profiles = list_profiles(fe, enabled_only=True)
        flags = screen_transactions(fe, fe.list_transactions(limit=10), profiles)
    finally:
        fe.close()
    assert flags and "gambling" in flags[0]["reasoning"]


def test_screening_agent_flow_and_audit(ctx):
    """import event → flags persisted + insight + notification + queued
    remediation task; flags appear in the audit export; dedup on re-run."""
    fe = ctx.open_finance()
    try:
        enable_profile(fe, preset_id="interest_free_finance")
        fe.add_transaction(-350, "EMI/Loan", "INTEREST CHARGED ON OVERDRAFT",
                           date="2026-07-01")
    finally:
        fe.close()
    es = EventStore(ctx.collab)
    register_reactive_agents(es, ctx)
    es.emit("finance.csv_imported", {"bank_name": "X", "imported": 1},
            source="test")

    flags = list_flags(ctx.collab.conn, status="open")
    assert len(flags) == 1 and flags[0]["status"] == "open"

    notifs = ctx.notify_store().list()
    assert any(n["type"] == "values_flag" for n in notifs)

    pend = ctx.store.list_approvals("pending")
    assert pend and pend[0]["payload"]["tool"] == "add_goal_task"
    assert pend[0]["source"] == "screening_agent"

    insights = es.recent("agent.insight")
    assert any(i["payload"]["agent"] == "screening" for i in insights)

    rep = build_audit_report(ctx)
    assert rep["metadata"]["counts"]["screening_flags"] == 1
    assert rep["screening_flags"][0]["reasoning"]

    # re-emit: transactions already screened → no duplicate flags/proposals
    es.emit("finance.csv_imported", {"bank_name": "X", "imported": 1},
            source="test")
    assert len(list_flags(ctx.collab.conn, status="open")) == 1


def test_profile_editing_and_flag_dismissal(ctx):
    fe = ctx.open_finance()
    try:
        pid = enable_profile(fe, preset_id="esg_basic")
        assert update_profile(fe, pid, enabled=False)
        assert list_profiles(fe, enabled_only=True) == []
        custom = get_preset("budget_discipline")["rules"]
        custom[0]["max_share"] = 0.5
        pid2 = enable_profile(fe, name="My rule", rules=custom)
        profs = {p["id"]: p for p in list_profiles(fe)}
        assert profs[pid2]["rules"][0]["max_share"] == 0.5
    finally:
        fe.close()
    persist_flags(ctx.collab.conn, [{
        "transaction_id": "t1", "profile_id": "p1", "profile_name": "P",
        "rule_kind": "description_pattern", "severity": "normal",
        "reasoning": "r"}])
    fid = list_flags(ctx.collab.conn)[0]["id"]
    assert set_flag_status(ctx.collab.conn, fid, "dismissed")
    assert list_flags(ctx.collab.conn, status="open") == []
