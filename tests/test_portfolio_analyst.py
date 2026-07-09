"""CAREER AUTOPILOT Part 3 — portfolio analyst: GitHub repo pull, real-
posting keyword classification, gap-project batch proposal, vault note.
All external MCP calls are mocked — no live network calls in tests.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.jobs import HANDLERS
from amy.agents.reactive import (_classify_repos, portfolio_analyze,
                                 register_reactive_agents)
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-portfolio", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _fake_row(name, default_target=""):
    return SimpleNamespace(name=name, server_url="https://example.invalid/mcp",
                           auth_type="none", auth_ref=None, auth_extra=None,
                           default_target=default_target)


class _FakeClient:
    def __init__(self, tool_names, result):
        self._tool_names = tool_names
        self._result = result

    async def list_tools(self):
        return [{"name": n, "description": "", "input_schema": {"properties": {}}}
                for n in self._tool_names]

    async def call_tool(self, name, arguments=None):
        return self._result


def _mock_connectors(monkeypatch, repos, jobs):
    """github -> repos, jobspy -> jobs. Routed by source name."""
    rows = {"github": _fake_row("github"), "jobspy": _fake_row("jobspy")}
    clients = {
        "github": _FakeClient(["list_repositories"],
                              {"is_error": False, "text": "", "structured": repos}),
        "jobspy": _FakeClient(["search_jobs"],
                              {"is_error": False, "text": "", "structured": jobs}),
    }
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: rows.get(source))
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for",
                        lambda row: clients[row.name])


_SHOWCASE_REPO = {"name": "rag-pipeline", "description": "A RAG pipeline with LangChain",
                  "language": "Python", "topics": ["langchain", "rag"],
                  "homepage": "https://rag-pipeline.example.invalid",
                  "stargazers_count": 5}
_NEEDS_WORK_REPO = {"name": "vector-search-experiment",
                    "description": "Trying out vector databases",
                    "language": "Python", "topics": [], "homepage": "",
                    "stargazers_count": 0}
_IRRELEVANT_REPO = {"name": "dotfiles", "description": "My shell config",
                    "language": "Shell", "topics": [], "homepage": ""}
_POSTINGS = [{"title": "GenAI Engineer", "description": "LangChain RAG vector database prompt engineering"}]


# ---------------------------------------------------------------------------
# _classify_repos (deterministic, no LLM)
# ---------------------------------------------------------------------------

def test_classify_repos_three_way_split():
    keywords = {"langchain", "rag", "vector", "database"}
    showcase, needs_work, not_relevant = _classify_repos(
        [dict(_SHOWCASE_REPO), dict(_NEEDS_WORK_REPO), dict(_IRRELEVANT_REPO)], keywords)
    assert [r["name"] for r in showcase] == ["rag-pipeline"]
    assert [r["name"] for r in needs_work] == ["vector-search-experiment"]
    assert [r["name"] for r in not_relevant] == ["dotfiles"]
    assert "demo/deployment link" in needs_work[0]["_missing"] or \
           "topics for discoverability" in needs_work[0]["_missing"]


def test_classify_repos_archived_and_fork_are_not_relevant():
    repo = dict(_SHOWCASE_REPO)
    repo["archived"] = True
    showcase, needs_work, not_relevant = _classify_repos([repo], {"langchain"})
    assert not showcase and not needs_work
    assert not_relevant


# ---------------------------------------------------------------------------
# portfolio_analyze — full flow
# ---------------------------------------------------------------------------

def test_portfolio_analyze_no_target_role_skips(ctx):
    out = portfolio_analyze(ctx.events(), ctx)
    assert out.get("skipped")


def test_portfolio_analyze_full_flow(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    _mock_connectors(monkeypatch,
                     repos=[_SHOWCASE_REPO, _NEEDS_WORK_REPO, _IRRELEVANT_REPO],
                     jobs=_POSTINGS)
    # Force the fast/deterministic no-LLM (fallback-entry) path rather than
    # letting _get_llm build a real router (slow, network-dependent).
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    out = portfolio_analyze(ctx.events(), ctx)

    assert out["target_role"] == "GenAI Engineer"
    assert len(out["showcase"]) == 1
    assert out["showcase"][0]["repo"] == "rag-pipeline"
    assert len(out["needs_work"]) == 1
    assert out["not_relevant_count"] == 1
    assert out["note"]   # vault note was written

    evs = ctx.collab.conn.execute(
        "SELECT type FROM events WHERE type='career.portfolio_analyzed'").fetchall()
    assert evs


def test_portfolio_analyze_gaps_batched_into_one_approval(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    # No repo evidences "kubernetes" or "terraform" -> gap keywords
    _mock_connectors(monkeypatch, repos=[_SHOWCASE_REPO],
                     jobs=[{"title": "GenAI Engineer",
                           "description": "LangChain RAG kubernetes terraform deploy"}])
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    out = portfolio_analyze(ctx.events(), ctx)
    assert out["gaps"], "expected at least one gap project suggestion"
    assert out["queued_approvals"] == 1

    pending = ctx.store.list_approvals("pending")
    batch = [a for a in pending if a["payload"].get("tool") == "plane_batch_create_tasks"]
    assert len(batch) == 1
    assert batch[0]["tier"] == 2


def test_portfolio_analyze_github_failure_degrades_to_error(ctx, monkeypatch):
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: None)
    out = portfolio_analyze(ctx.events(), ctx)
    assert "error" in out


# ---------------------------------------------------------------------------
# Registration + job wiring
# ---------------------------------------------------------------------------

def test_portfolio_agent_registered():
    from amy.automation import build_ctx as _build_ctx
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "connectors").mkdir(parents=True)
        cdb = CollabDB(str(Path(d) / "collab.db"))
        c = _build_ctx("u-reg", "t@example.com", cdb, Path(d), llm_router=None)
        registered = register_reactive_agents(c.events(), c)
        assert "portfolio" in registered
        cdb.close()


def test_portfolio_review_job_skips_without_active_career_goal(ctx):
    out = HANDLERS["portfolio_review"](ctx)
    assert out.get("skipped") == "no active career goal"


def test_portfolio_review_job_runs_for_active_career_goal(ctx, monkeypatch):
    from amy.autonomous import GoalEngine
    GoalEngine(ctx.collab).create_goal("Become a GenAI Engineer", domain="career")
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    _mock_connectors(monkeypatch, repos=[_SHOWCASE_REPO], jobs=_POSTINGS)
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)

    out = HANDLERS["portfolio_review"](ctx)
    assert out["target_role"] == "GenAI Engineer"
