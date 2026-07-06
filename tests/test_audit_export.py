"""Phase R7A-6 — audit export: joined report with reasoning + routing docs."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy import tools
from amy.automation import build_ctx, executors
from amy.automation.audit import build_audit_report
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-audit", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def test_audit_report_sections_and_provenance(ctx):
    # generate activity: an agent proposal, an approval, a rejection, a run
    ctx._extras["agent_name"] = "test_agent"
    ctx._extras["agent_reasoning"] = "audit trail check"
    a1 = tools.invoke(ctx, "set_budget", {"category": "A", "monthly_limit": 1},
                      actor="agent")
    a2 = tools.invoke(ctx, "set_budget", {"category": "B", "monthly_limit": 2},
                      actor="agent")
    executors.approve(ctx, a1["approval_id"])
    executors.reject(ctx, a2["approval_id"], reason="testing rejection")
    rid = ctx.store.start_run("test_job")
    ctx.store.finish_run(rid, "ok", {"n": 1})
    ctx.events().emit("agent.insight", {"agent": "budget", "summary": "s",
                                        "reasoning": "r"}, source="budget_agent")

    rep = build_audit_report(ctx)

    md = rep["metadata"]
    assert md["counts"]["approvals"] == 2
    assert md["counts"]["approvals_rejected"] == 1
    assert md["counts"]["automation_runs"] >= 1
    assert md["counts"]["decisions"] >= 2          # approve + reject recorded
    assert "llm_routing" in md and md["llm_routing"]["general_provider_order"]
    assert "sensitive" in md["llm_routing"]["sensitive_data_rule"]

    # approvals carry reasoning + risk + outcome
    by_status = {a["status"]: a for a in rep["approvals"]}
    assert by_status["executed"]["reasoning"] == "audit trail check"
    assert by_status["executed"]["risk"] == "write"
    assert by_status["rejected"]["result"] == {"reason": "testing rejection"}

    # agent activity section filters agent.* events and keeps reasoning
    assert any(e["reasoning"] == "r" for e in rep["agent_activity"])

    # screening flags section exists (empty until R7A-1 ships the table)
    assert rep["screening_flags"] == []


def test_audit_period_filter(ctx):
    ctx.events().emit("agent.insight", {"agent": "x", "summary": "now",
                                        "reasoning": "y"}, source="x")
    rep = build_audit_report(ctx, since="2099-01-01")
    assert rep["metadata"]["counts"]["events"] == 0
    rep2 = build_audit_report(ctx, since="2000-01-01")
    assert rep2["metadata"]["counts"]["events"] >= 1


def test_local_only_flag_documented(ctx):
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES('llm_local_only','1')")
    ctx.collab.conn.commit()
    rep = build_audit_report(ctx)
    assert rep["metadata"]["llm_routing"]["user_local_only_flag"] is True
    assert "local-only" in rep["metadata"]["llm_routing"]["effective_routing"]
