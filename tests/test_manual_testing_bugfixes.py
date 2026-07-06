"""Fixes for two bugs found during manual UI testing of the orchestrator:

1. The orchestrator proposed cutting a 'Custodial Disbursement' budget by
   10% as if it were personal spending, because tools had no way to signal
   that category is pass-through money forwarded to beneficiaries.
   Fix: list_budgets/set_budget flag custodial-linked categories, and the
   approval gate injects a visible warning regardless of whether the LLM
   heeded the tool description.

2. Running an equivalent goal twice ("cut spending 10%" then "reduce
   spending by 10 percent") queued two separate approvals for the
   IDENTICAL action (same tool, same args) because orchestrator tool
   calls carried no dedup key, unlike the reactive agents.
   Fix: dedup key = tool name + sorted args hash.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy import tools
from amy.automation import build_ctx, executors
from amy.automation.orchestrator import run_goal
from amy.collab import CollabDB
from amy.tools.builtin import is_custodial_category


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-fix", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed(fe):
    custodial = fe.add_account("Trust", "SBI", account_type="custodial")
    fe.add_transaction(-28000, "Custodial Disbursement", "BENEFICIARY X",
                       account_id=custodial)
    personal = fe.add_account("Main", "HDFC", account_type="savings")
    fe.add_transaction(-500, "Food", "SWIGGY", account_id=personal)


# ===========================================================================
# Fix 1 — custodial-category awareness
# ===========================================================================

def test_is_custodial_category_flags_correctly(ctx):
    fe = ctx.open_finance()
    try:
        _seed(fe)
        assert is_custodial_category(fe, "Custodial Disbursement") is True
        assert is_custodial_category(fe, "Food") is False
        assert is_custodial_category(fe, "Nonexistent Category") is False
    finally:
        fe.close()


def test_list_budgets_tool_exposes_flag(ctx):
    fe = ctx.open_finance()
    try:
        _seed(fe)
        fe.set_budget("Custodial Disbursement", 25200)
        fe.set_budget("Food", 1800)
    finally:
        fe.close()
    rows = {b["category"]: b for b in tools.invoke(ctx, "list_budgets", {})}
    assert rows["Custodial Disbursement"]["custodial_category"] is True
    assert rows["Food"]["custodial_category"] is False


def test_agent_set_budget_on_custodial_category_gets_warning(ctx):
    fe = ctx.open_finance()
    try:
        _seed(fe)
    finally:
        fe.close()
    ctx._extras["agent_name"] = "test_agent"
    ctx._extras["agent_reasoning"] = "cut spending 10%"
    out = tools.invoke(ctx, "set_budget",
                       {"category": "Custodial Disbursement", "monthly_limit": 25200},
                       actor="agent")
    ap = ctx.store.get_approval(out["approval_id"])
    assert "⚠️" in ap["reasoning"]
    assert "not the user's own discretionary spending" in ap["reasoning"]
    assert "cut spending 10%" in ap["reasoning"]   # original reasoning kept


def test_agent_set_budget_on_normal_category_no_warning(ctx):
    fe = ctx.open_finance()
    try:
        _seed(fe)
    finally:
        fe.close()
    ctx._extras["agent_name"] = "test_agent"
    ctx._extras["agent_reasoning"] = "cut spending 10%"
    out = tools.invoke(ctx, "set_budget",
                       {"category": "Food", "monthly_limit": 450}, actor="agent")
    ap = ctx.store.get_approval(out["approval_id"])
    assert "⚠️" not in ap["reasoning"]


def test_human_direct_set_budget_unaffected_by_warning_logic(ctx):
    """Human-invoked (actor=human) writes bypass the gate entirely — the
    warning logic only ever runs inside agent_gate, so direct human actions
    are untouched."""
    fe = ctx.open_finance()
    try:
        _seed(fe)
    finally:
        fe.close()
    out = tools.invoke(ctx, "set_budget",
                       {"category": "Custodial Disbursement", "monthly_limit": 25200},
                       actor="human")
    assert out["monthly_limit"] == 25200   # executed directly, no approval row


# ===========================================================================
# Fix 2 — dedup on orchestrator-proposed actions
# ===========================================================================

class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, system, prompt, context="", sensitive=False):
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return (json.dumps(r), "scripted")


def _cut_food_budget_script():
    return [
        {"plan": ["Cut the Food budget"], "reasoning": "test plan"},
        {"tool": "set_budget", "args": {"category": "Food", "monthly_limit": 1800},
         "reasoning": "10% cut"},
        {"step_done": "done"},
        {"summary": "cut it"},
    ]


def test_orchestrator_dedupes_identical_proposal(ctx):
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 2000)
    finally:
        fe.close()

    ctx.llm = ScriptedLLM(_cut_food_budget_script())
    out1 = run_goal(ctx, "cut my spending 10%")
    assert out1["queued_approvals"] == 1
    assert len(ctx.store.list_approvals("pending")) == 1

    # different phrasing, IDENTICAL underlying tool+args -> must dedupe
    ctx.llm = ScriptedLLM(_cut_food_budget_script())
    out2 = run_goal(ctx, "please reduce my spending by 10 percent")
    write_step = out2["steps"][0]
    assert write_step["result"]["status"] == "duplicate"
    assert out2["queued_approvals"] == 0
    assert len(ctx.store.list_approvals("pending")) == 1   # still just one


def test_orchestrator_allows_new_proposal_after_rejection(ctx):
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 2000)
    finally:
        fe.close()

    ctx.llm = ScriptedLLM(_cut_food_budget_script())
    run_goal(ctx, "cut my spending 10%")
    aid = ctx.store.list_approvals("pending")[0]["id"]
    executors.reject(ctx, aid, reason="not now")
    assert ctx.store.list_approvals("pending") == []

    ctx.llm = ScriptedLLM(_cut_food_budget_script())
    out2 = run_goal(ctx, "cut my spending 10%")
    write_step = out2["steps"][0]
    assert write_step["result"]["status"] == "pending"   # allowed to re-propose
    assert len(ctx.store.list_approvals("pending")) == 1


def test_orchestrator_dedup_is_per_tool_and_args(ctx):
    """A different monthly_limit is a DIFFERENT action and must not dedupe
    against an existing pending proposal for the same category."""
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 2000)
    finally:
        fe.close()
    ctx.llm = ScriptedLLM(_cut_food_budget_script())
    run_goal(ctx, "cut my spending 10%")

    script2 = [
        {"plan": ["Cut Food budget more"], "reasoning": "test"},
        {"tool": "set_budget", "args": {"category": "Food", "monthly_limit": 1500},
         "reasoning": "different amount"},
        {"step_done": "done"},
        {"summary": "cut it more"},
    ]
    ctx.llm = ScriptedLLM(script2)
    out2 = run_goal(ctx, "cut spending more aggressively")
    assert out2["steps"][0]["result"]["status"] == "pending"
    assert len(ctx.store.list_approvals("pending")) == 2
