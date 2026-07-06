"""Phase R4 — orchestrator: plan → read tools direct → writes parked →
graph nodes/edges → run persisted."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.orchestrator import list_goal_runs, run_goal
from amy.collab import CollabDB
from amy.knowledge_graph.store import GraphStore


class ScriptedLLM:
    """Returns queued responses in order; repeats the last one if exhausted."""
    def __init__(self, responses):
        self.responses = list(responses)

    def generate(self, system, prompt, context="", sensitive=False):
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return (json.dumps(r), "scripted")


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-orch", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def test_goal_run_full_flow(ctx, tmp_path):
    fe = ctx.open_finance()
    try:
        fe.set_budget("Food", 8000)
        fe.add_transaction(-6000, "Food", "RESTAURANTS", date="2026-07-01")
    finally:
        fe.close()

    ctx.llm = ScriptedLLM([
        {"plan": ["Review current budgets", "Propose a lower Food cap"],
         "reasoning": "spending is concentrated in Food"},
        # step 1: read
        {"tool": "list_budgets", "args": {}, "reasoning": "need current caps"},
        {"step_done": "Food budget is 8000, 6000 spent"},
        # step 2: write → should PARK
        {"tool": "set_budget", "args": {"category": "Food", "monthly_limit": 7000},
         "reasoning": "10% cut on the dominant category"},
        {"step_done": "proposed new cap"},
        # summary
        {"summary": "Reviewed budgets; proposed cutting Food to 7000 (awaiting approval)."},
    ])

    out = run_goal(ctx, "cut my spending 10% this quarter")

    assert out["status"] == "completed"
    assert out["plan"] == ["Review current budgets", "Propose a lower Food cap"]
    assert len(out["steps"]) == 2
    read_step, write_step = out["steps"]
    assert read_step["tool"] == "list_budgets" and read_step["ok"]
    assert not read_step["queued"]
    assert write_step["tool"] == "set_budget" and write_step["queued"]
    assert out["queued_approvals"] == 1

    # write really parked, not applied
    fe = ctx.open_finance()
    try:
        assert [b["monthly_limit"] for b in fe.list_budgets()
                if b["category"] == "Food"] == [8000]
    finally:
        fe.close()
    pend = ctx.store.list_approvals("pending")
    assert pend and pend[0]["payload"]["tool"] == "set_budget"
    assert pend[0]["source"] == "orchestrator"
    assert "10% cut" in pend[0]["reasoning"]

    # plan graph: goal node + 2 tasks, belongs_to + depends_on edges
    g = GraphStore(str(tmp_path / "graph.db"))
    try:
        goals = g.nodes(type="goal")
        tasks = g.nodes(type="task")
        assert len(goals) == 1 and len(tasks) == 2
        rels = {(e["rel"]) for e in g.edges()}
        assert {"belongs_to", "depends_on"} <= rels
        # outcomes recorded on task refs
        assert any("Food budget is 8000" in (t["ref"] or "") for t in tasks)
    finally:
        g.conn.close()

    # run persisted + retrievable
    runs = list_goal_runs(ctx)
    assert runs and runs[0]["goal"].startswith("cut my spending")
    assert runs[0]["summary"].startswith("Reviewed budgets")

    # goal event journaled type exists
    evs = ctx.collab.conn.execute(
        "SELECT type FROM events WHERE type='agent.goal_planned'").fetchall()
    assert evs


def test_goal_requires_plan(ctx):
    ctx.llm = ScriptedLLM([{"final": "no plan here"}])
    out = run_goal(ctx, "do something vague")
    assert "error" in out


def test_goal_without_llm(ctx):
    ctx.llm = None
    assert "error" in run_goal(ctx, "anything")
