"""CAREER AUTOPILOT Part 1 — career data model + registry tools.
All external MCP/SMTP calls are mocked — no live network calls in tests.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy import tools
from amy.automation import build_ctx
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-career", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _fake_row(name="jobspy", default_target=""):
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


def _agent_invoke(ctx, name, args, agent="test_agent", reasoning="because test"):
    ctx._extras["agent_name"] = agent
    ctx._extras["agent_reasoning"] = reasoning
    return tools.invoke(ctx, name, args, actor="agent")


# ---------------------------------------------------------------------------
# Table creation idempotency
# ---------------------------------------------------------------------------

def test_career_tables_created_idempotently(ctx):
    from amy.automation.store import AutomationStore
    AutomationStore(ctx.collab)   # second init on the same connection
    for table in ("career_profile", "job_postings", "applications", "company_intel"):
        ctx.collab.conn.execute(f"SELECT * FROM {table} LIMIT 1")   # no error


# ---------------------------------------------------------------------------
# career_profile
# ---------------------------------------------------------------------------

def test_career_profile_roundtrip_encrypts_resume(ctx):
    tools.invoke(ctx, "set_career_profile",
                {"target_role": "GenAI Engineer", "target_location": "Bangalore",
                 "resume_text": "Python, PyTorch, RAG pipelines",
                 "skills": ["python", "pytorch"]}, actor="human")
    raw = ctx.collab.conn.execute(
        "SELECT resume_text_enc FROM career_profile WHERE uid=?",
        (ctx.user_id,)).fetchone()["resume_text_enc"]
    assert raw and "Python, PyTorch" not in raw   # stored encrypted, not plaintext

    status = tools.invoke(ctx, "career_status", {}, actor="human")
    assert status["profile"]["target_role"] == "GenAI Engineer"
    assert "resume_text" not in status["profile"]   # never surfaced by career_status


# ---------------------------------------------------------------------------
# job_search / job_details
# ---------------------------------------------------------------------------

def test_job_search_calls_jobspy_and_returns_jobs(ctx, monkeypatch):
    row = _fake_row(name="jobspy")
    seen_args = {}

    class _Client(_FakeClient):
        async def call_tool(self, name, arguments=None):
            seen_args.update(arguments or {})
            return self._result

    client = _Client(["search_jobs"],
                     {"is_error": False, "text": "",
                      "structured": [{"title": "GenAI Engineer", "company": "Acme",
                                      "job_url": "https://example.invalid/1"}]})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for", lambda r: client)

    out = tools.invoke(ctx, "job_search",
                       {"search_term": "GenAI Engineer", "location": "Bangalore",
                        "country_indeed": "India"}, actor="human")
    assert out["count"] == 1
    assert out["jobs"][0]["title"] == "GenAI Engineer"
    assert seen_args["search_term"] == "GenAI Engineer"
    assert seen_args["country_indeed"] == "India"


def test_job_details_reads_local_row_not_mcp(ctx):
    pid, is_new = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1",
        "description": "Build RAG pipelines"})
    assert is_new

    out = tools.invoke(ctx, "job_details", {"posting_id": pid}, actor="human")
    assert out["title"] == "GenAI Engineer"
    assert out["description"] == "Build RAG pipelines"


def test_job_details_unknown_posting_raises(ctx):
    with pytest.raises(Exception, match="no job posting"):
        tools.invoke(ctx, "job_details", {"posting_id": "nope"}, actor="human")


def test_posting_dedup_on_url(ctx):
    posting = {"title": "GenAI Engineer", "company": "Acme",
              "url": "https://example.invalid/1"}
    pid1, is_new1 = ctx.store.add_posting_if_new(ctx.user_id, posting)
    pid2, is_new2 = ctx.store.add_posting_if_new(ctx.user_id, posting)
    assert is_new1 is True
    assert is_new2 is False
    assert pid1 == pid2
    assert len(ctx.store.list_postings(ctx.user_id)) == 1


# ---------------------------------------------------------------------------
# application_log
# ---------------------------------------------------------------------------

def test_application_log_creates_then_updates_status(ctx):
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1"})
    created = tools.invoke(ctx, "application_log",
                           {"posting_id": pid, "channel": "email"}, actor="human")
    app = ctx.store.get_application(ctx.user_id, created["id"])
    assert app["status"] == "prepared"

    tools.invoke(ctx, "application_log",
                {"application_id": created["id"], "status": "sent",
                 "note": "sent via SMTP"}, actor="human")
    app = ctx.store.get_application(ctx.user_id, created["id"])
    assert app["status"] == "sent"
    assert len(app["timeline"]) == 2

    funnel = ctx.store.career_funnel_counts(ctx.user_id)
    assert funnel["sent"] == 1
    assert funnel["discovered"] == 1


def test_application_log_unknown_application_raises(ctx):
    with pytest.raises(Exception, match="no application"):
        tools.invoke(ctx, "application_log",
                    {"application_id": "nope", "status": "sent"}, actor="human")


# ---------------------------------------------------------------------------
# send_hr_email — external pin + SMTP-or-draft fallback
# ---------------------------------------------------------------------------

def test_send_hr_email_parks_tier2_even_with_write_tier_0(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_WRITE_TIER", "0")
    out = _agent_invoke(ctx, "send_hr_email",
                        {"application_id": "app-1", "to_email": "hr@acme.example",
                         "subject": "Application", "body": "Hello"})
    assert out["status"] == "pending", (
        f"external tool must park regardless of AMY_AGENT_WRITE_TIER, got {out}")
    ap = ctx.store.get_approval(out["approval_id"])
    assert ap["tier"] == 2


def test_send_hr_email_without_smtp_produces_draft_not_send(ctx, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1"})
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")

    out = tools.invoke(ctx, "send_hr_email",
                       {"application_id": aid, "to_email": "hr@acme.example",
                        "subject": "Application", "body": "Hello"}, actor="human")
    assert out["sent"] is False
    assert "not sent" in out["note"].lower() or "not configured" in out["note"].lower()
    app = ctx.store.get_application(ctx.user_id, aid)
    assert app["status"] == "prepared"   # stays prepared, no false "sent"


def test_send_hr_email_with_smtp_configured_sends(ctx, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setattr("amy.notifications.email.send_email", lambda *a, **k: True)
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1"})
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email")

    out = tools.invoke(ctx, "send_hr_email",
                       {"application_id": aid, "to_email": "hr@acme.example",
                        "subject": "Application", "body": "Hello"}, actor="human")
    assert out["sent"] is True
    app = ctx.store.get_application(ctx.user_id, aid)
    assert app["status"] == "sent"


# ---------------------------------------------------------------------------
# Sensitive routing: legacy fake job discovery must never fabricate results
# ---------------------------------------------------------------------------

def test_legacy_discover_jobs_returns_nothing_not_fabricated():
    from amy.intelligence.career import discovery
    out = discovery.discover_jobs(llm=None, query="ML Engineer")
    assert out == []
