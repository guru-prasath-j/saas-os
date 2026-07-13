"""AML Monitoring Module (Phase 2) — typology detection + case lifecycle.

All transactions/accounts constructed in this file are SYNTHETIC test
fixtures, not real financial data. See amy/finance/aml_engine.py's module
docstring for the "illustrative, not sourced from regulation" framing this
module follows, and for why circular-transfer detection is scoped to the
user's own accounts/beneficiaries rather than general AML layering.
"""
import datetime as _dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.executors import approve
from amy.collab import CollabDB
from amy.finance import aml_engine
from amy.tools.registry import invoke as tool_invoke

_MONDAY = _dt.date(2026, 1, 5)


def _d(offset_days: int) -> str:
    return (_MONDAY + _dt.timedelta(days=offset_days)).isoformat()


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-aml", "aml@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


# ---------------------------------------------------------------------------
# Unavailable signals
# ---------------------------------------------------------------------------

def test_unavailable_signals_are_named_not_faked():
    for key in ("high_risk_country_screening", "pep_screening",
               "sanctions_screening", "money_mule_detection"):
        assert key in aml_engine.UNAVAILABLE_SIGNALS
        assert aml_engine.UNAVAILABLE_SIGNALS[key]


# ---------------------------------------------------------------------------
# Clean case
# ---------------------------------------------------------------------------

def test_clean_account_triggers_nothing(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-437, "Food", "Everyday Store", date=_d(0), account_id=aid)
        fe.add_transaction(-289, "Food", "Everyday Store", date=_d(5), account_id=aid)
        candidates = aml_engine.scan_account_for_aml(fe, aid)
    finally:
        fe.close()
    assert candidates == []


# ---------------------------------------------------------------------------
# Typology 1 — structuring
# ---------------------------------------------------------------------------

def test_structuring_detects_subthreshold_cluster(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        fe.add_transaction(-38000, "Transfer", "Payee C", date=_d(2), account_id=aid)
        candidates = aml_engine.detect_structuring(fe, aid)
    finally:
        fe.close()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.typology == "structuring"
    assert c.risk_level in ("HIGH", "CRITICAL")
    assert len(c.evidence) == 3


def test_structuring_does_not_trigger_below_min_count(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        candidates = aml_engine.detect_structuring(fe, aid)
    finally:
        fe.close()
    assert candidates == []


# ---------------------------------------------------------------------------
# Typology 2 — layering
# ---------------------------------------------------------------------------

def test_layering_detects_rapid_in_and_out(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(20000, "Income", "Big Credit", date=_d(0), account_id=aid)
        fe.add_transaction(-18000, "Transfer", "Fast Out", date=_d(1), account_id=aid)
        candidates = aml_engine.detect_layering(fe, aid)
    finally:
        fe.close()
    assert len(candidates) == 1
    assert candidates[0].typology == "layering"
    assert len(candidates[0].evidence) == 2


def test_layering_does_not_trigger_when_funds_stay_put(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(20000, "Income", "Big Credit", date=_d(0), account_id=aid)
        fe.add_transaction(-500, "Food", "Small Spend", date=_d(1), account_id=aid)
        candidates = aml_engine.detect_layering(fe, aid)
    finally:
        fe.close()
    assert candidates == []


# ---------------------------------------------------------------------------
# Typology 3 — cash spike
# ---------------------------------------------------------------------------

def test_cash_spike_detects_atm_spike_vs_own_average(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        for i in range(3):
            fe.add_transaction(-1000, "Cash", "ATM WDL", date=_d(-10 + i), account_id=aid)
        tid = fe.add_transaction(-5000, "Cash", "ATM WDL", date=_d(0), account_id=aid)
        candidates = aml_engine.detect_cash_spike(fe, aid)
    finally:
        fe.close()
    assert any(tid in c.evidence for c in candidates)


def test_cash_spike_ignores_non_cash_merchants(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        for i in range(3):
            fe.add_transaction(-1000, "Shopping", "Online Store", date=_d(-10 + i), account_id=aid)
        fe.add_transaction(-5000, "Shopping", "Online Store", date=_d(0), account_id=aid)
        candidates = aml_engine.detect_cash_spike(fe, aid)
    finally:
        fe.close()
    assert candidates == []


# ---------------------------------------------------------------------------
# Typology 4 — circular transfer
# ---------------------------------------------------------------------------

def test_circular_transfer_detects_cycle_across_own_accounts(ctx):
    fe = ctx.open_finance()
    try:
        a = fe.add_account("Alpha", "Test Bank", account_type="savings")
        b = fe.add_account("Beta", "Test Bank", account_type="savings")
        g = fe.add_account("Gamma", "Test Bank", account_type="savings")
        t1 = fe.add_transaction(-10000, "Transfer", "Transfer to Beta", date=_d(0), account_id=a)
        t2 = fe.add_transaction(-8000, "Transfer", "Transfer to Gamma", date=_d(1), account_id=b)
        t3 = fe.add_transaction(-7000, "Transfer", "Transfer to Alpha", date=_d(2), account_id=g)
        candidates = aml_engine.detect_circular_transfers(fe, account_id=a)
    finally:
        fe.close()
    assert len(candidates) == 1
    c = candidates[0]
    assert c.typology == "circular_transfer"
    assert set(c.evidence) == {t1, t2, t3}


def test_circular_transfer_empty_when_no_cycle(ctx):
    fe = ctx.open_finance()
    try:
        a = fe.add_account("Alpha", "Test Bank", account_type="savings")
        b = fe.add_account("Beta", "Test Bank", account_type="savings")
        fe.add_transaction(-10000, "Transfer", "Transfer to Beta", date=_d(0), account_id=a)
        candidates = aml_engine.detect_circular_transfers(fe, account_id=a)
    finally:
        fe.close()
    assert candidates == []


# ---------------------------------------------------------------------------
# Case lifecycle
# ---------------------------------------------------------------------------

def test_investigate_account_opens_case_and_dedups_on_rescan(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        fe.add_transaction(-38000, "Transfer", "Payee C", date=_d(2), account_id=aid)
    finally:
        fe.close()

    first = aml_engine.investigate_account(ctx, aid)
    assert len(first) == 1
    case_id = first[0]["case_id"]

    second = aml_engine.investigate_account(ctx, aid)
    assert len(second) == 1
    assert second[0]["case_id"] == case_id

    fe = ctx.open_finance()
    try:
        cases = fe.list_aml_cases(account_id=aid)
    finally:
        fe.close()
    assert len(cases) == 1


def test_escalate_case_parks_pending_approval_then_updates_status(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        fe.add_transaction(-38000, "Transfer", "Payee C", date=_d(2), account_id=aid)
    finally:
        fe.close()
    cases = aml_engine.investigate_account(ctx, aid)
    case_id = cases[0]["case_id"]

    result = aml_engine.escalate_case(ctx, case_id)
    assert result["status"] == "pending"
    approval_id = result["approval_id"]

    pending = ctx.store.list_approvals("pending")
    assert len(pending) == 1
    assert pending[0]["payload"]["case_id"] == case_id
    assert pending[0]["dedup_key"] == f"aml_escalate_{case_id}"
    assert pending[0]["tier"] == 2

    # not escalated until a human approves
    fe = ctx.open_finance()
    try:
        assert fe.get_aml_case(case_id)["status"] == "open"
    finally:
        fe.close()

    out = approve(ctx, approval_id)
    assert out["status"] == "executed"

    fe = ctx.open_finance()
    try:
        case = fe.get_aml_case(case_id)
    finally:
        fe.close()
    assert case["status"] == "escalated"
    assert any(e.get("event") == "escalated by human approval" for e in case["timeline"])

    with pytest.raises(ValueError):
        aml_engine.escalate_case(ctx, case_id)


def test_sar_draft_is_never_automatic_and_carries_mandatory_header(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        fe.add_transaction(-38000, "Transfer", "Payee C", date=_d(2), account_id=aid)
    finally:
        fe.close()
    cases = aml_engine.investigate_account(ctx, aid)
    case_id = cases[0]["case_id"]

    fe = ctx.open_finance()
    try:
        assert fe.get_aml_case(case_id)["sar_draft"] is None
    finally:
        fe.close()

    result = aml_engine.generate_sar_draft(ctx, case_id)
    assert result["status"] == "pending"
    approval_id = result["approval_id"]
    approve(ctx, approval_id)

    fe = ctx.open_finance()
    try:
        draft = fe.get_aml_case(case_id)["sar_draft"]
    finally:
        fe.close()
    assert draft.startswith("DRAFT — NOT A REAL REGULATORY FILING")
    assert draft.rstrip().endswith("standing and must never be submitted to any authority.")
    assert case_id in draft


# ---------------------------------------------------------------------------
# Registry tools
# ---------------------------------------------------------------------------

def test_score_aml_typologies_tool_matches_direct_call(ctx):
    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        fe.add_transaction(-38000, "Transfer", "Payee C", date=_d(2), account_id=aid)
        direct = aml_engine.scan_account_for_aml(fe, aid)
    finally:
        fe.close()
    via_tool = tool_invoke(ctx, "score_aml_typologies", {"account_id": aid}, actor="human")
    assert len(via_tool["candidates"]) == len(direct)
    assert via_tool["candidates"][0]["typology"] == direct[0].typology


def test_explain_aml_alert_is_honest_before_and_after(ctx):
    before = tool_invoke(ctx, "explain_aml_alert", {"case_id": "not-a-real-case"}, actor="human")
    assert before["available"] is False

    fe = ctx.open_finance()
    try:
        aid = fe.add_account("Checking", "Test Bank", account_type="savings")
        fe.add_transaction(-40000, "Transfer", "Payee A", date=_d(0), account_id=aid)
        fe.add_transaction(-42000, "Transfer", "Payee B", date=_d(1), account_id=aid)
        fe.add_transaction(-38000, "Transfer", "Payee C", date=_d(2), account_id=aid)
    finally:
        fe.close()
    cases = aml_engine.investigate_account(ctx, aid)
    case_id = cases[0]["case_id"]

    after = tool_invoke(ctx, "explain_aml_alert", {"case_id": case_id}, actor="human")
    assert after["available"] is True
    assert after["typology"] == "structuring"
