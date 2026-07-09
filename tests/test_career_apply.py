"""CAREER AUTOPILOT Part 5 — application pipeline: prepare -> approve ->
send -> track. All external MCP/LLM calls are mocked.
"""
import datetime as _dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.jobs import HANDLERS
from amy.career_apply import (_ats_estimate, _company_intel, _recommend_channel,
                              followup_check, prepare_application)
from amy.career_scout import JobScoutSensor
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-apply", "t@example.com", cdb, tmp_path, llm_router=None)
    c._extras["_no_llm"] = True
    yield c
    cdb.close()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    # Every test in this file cares about deterministic fallback behavior,
    # not LLM output — force the fast/no-network path everywhere.
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)


def _fake_row(name):
    return SimpleNamespace(name=name, server_url="https://example.invalid/mcp",
                           auth_type="none", auth_ref=None, auth_extra=None,
                           default_target="")


class _FakeClient:
    def __init__(self, result):
        self._result = result

    async def list_tools(self):
        return [{"name": "web_search", "description": "", "input_schema": {"properties": {}}}]

    async def call_tool(self, name, arguments=None):
        return self._result


def _no_connectors(monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: None)


_POSTING_EMAIL = {"title": "GenAI Engineer", "company": "Acme Corp",
                  "url": "https://example.invalid/1",
                  "description": "Apply by emailing jobs@acme.example with your resume. "
                                  "LangChain RAG vector database experience wanted."}
_POSTING_PORTAL = {"title": "GenAI Engineer", "company": "Widget Co",
                   "url": "https://example.invalid/2",
                   "description": "LangChain RAG vector database experience wanted."}
_POSTING_AGENCY = {"title": "GenAI Engineer", "company": "Acme Staffing Solutions",
                   "url": "https://example.invalid/3",
                   "description": "We are a recruiting agency placing GenAI engineers."}


# ---------------------------------------------------------------------------
# 1. Channel recommendation
# ---------------------------------------------------------------------------

def test_recommend_channel_email_when_found():
    out = _recommend_channel(_POSTING_EMAIL)
    assert out["channel"] == "email"
    assert out["to_email"] == "jobs@acme.example"


def test_recommend_channel_agency():
    out = _recommend_channel(_POSTING_AGENCY)
    assert out["channel"] == "third_party"
    assert out["to_email"] is None


def test_recommend_channel_portal_fallback():
    out = _recommend_channel(_POSTING_PORTAL)
    assert out["channel"] == "portal"
    assert out["to_email"] is None


# ---------------------------------------------------------------------------
# 2. ATS estimate
# ---------------------------------------------------------------------------

def test_ats_estimate_no_resume_is_honest_not_fabricated():
    out = _ats_estimate("", _POSTING_PORTAL)
    assert out["coverage_pct"] is None
    assert "no resume" in out["note"].lower()


def test_ats_estimate_with_resume_computes_coverage():
    out = _ats_estimate("Experienced with LangChain and RAG pipelines.", _POSTING_PORTAL)
    assert out["coverage_pct"] is not None
    assert "langchain" in [k.lower() for k in out["matched"]]


# ---------------------------------------------------------------------------
# 3. Company intel — honest stub without a connector
# ---------------------------------------------------------------------------

def test_company_intel_stub_when_no_connector(ctx, monkeypatch):
    _no_connectors(monkeypatch)
    out = _company_intel(ctx, "Acme Corp")
    assert out["available"] is False
    cached = ctx.store.get_company_intel(ctx.user_id, "Acme Corp")
    assert cached is not None   # cached even when empty, so we don't re-query every time


def test_company_intel_uses_registered_web_search_connector(ctx, monkeypatch):
    row = _fake_row("web_search")
    client = _FakeClient({"is_error": False, "text": "", "structured":
                          [{"title": "Acme interview process", "url": "https://example.invalid/a"}]})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row if source == "web_search" else None)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for", lambda r: client)
    out = _company_intel(ctx, "Acme Corp")
    assert out["available"] is True
    assert out["sources"] == ["https://example.invalid/a"]


# ---------------------------------------------------------------------------
# 4/5. prepare_application — PREPARE + ONE approval
# ---------------------------------------------------------------------------

def test_prepare_application_email_channel_parks_one_approval(ctx, monkeypatch):
    _no_connectors(monkeypatch)   # no github/web_search -> showcase=[], intel unavailable
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer",
                                 resume_text="LangChain RAG experience")
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, _POSTING_EMAIL)

    out = prepare_application(ctx, pid)
    assert out["channel"] == "email"
    assert out["proposal"]["status"] == "pending"

    pending = ctx.store.list_approvals("pending")
    batch = [a for a in pending if a["payload"].get("tool") == "send_hr_email"]
    assert len(batch) == 1
    assert batch[0]["tier"] == 2
    assert batch[0]["payload"]["args"]["to_email"] == "jobs@acme.example"

    app = ctx.store.get_application(ctx.user_id, out["application_id"])
    assert app["channel"] == "email"
    assert app["status"] == "prepared"   # not "sent" — nothing executes until approved


def test_prepare_application_portal_channel_parks_one_approval(ctx, monkeypatch):
    _no_connectors(monkeypatch)
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, _POSTING_PORTAL)

    out = prepare_application(ctx, pid)
    assert out["channel"] == "portal"

    pending = ctx.store.list_approvals("pending")
    batch = [a for a in pending if a["payload"].get("tool") == "application_log"]
    assert len(batch) == 1
    assert batch[0]["payload"]["args"]["status"] == "approved"


def test_prepare_application_dedup_no_duplicate_approval(ctx, monkeypatch):
    _no_connectors(monkeypatch)
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, _POSTING_EMAIL)

    prepare_application(ctx, pid)
    out2 = prepare_application(ctx, pid)
    # Part 5E: the duplicate-application guard catches the repeat BEFORE the
    # approval-dedup layer even sees it (same company, active application)
    assert "blocked" in out2

    pending = ctx.store.list_approvals("pending")
    batch = [a for a in pending if a["payload"].get("tool") == "send_hr_email"]
    assert len(batch) == 1   # still just one, not two


def test_prepare_application_unknown_posting_errors(ctx):
    out = prepare_application(ctx, "nope")
    assert "error" in out


# ---------------------------------------------------------------------------
# 6. Followup / ghosting
# ---------------------------------------------------------------------------

def _seed_sent_application(ctx, days_old: int, to_email: str | None = "hr@acme.example"):
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/x"})
    aid = ctx.store.create_application(ctx.user_id, pid, channel="email" if to_email else "portal",
                                       draft=json.dumps({"subject": "s", "body": "b",
                                                        "to_email": to_email}))
    ctx.store.update_application_status(ctx.user_id, aid, "sent", "Sent via SMTP")
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_old)).isoformat()
    timeline = ctx.store.get_application(ctx.user_id, aid)["timeline"]
    timeline[-1]["ts"] = old_ts
    ctx.collab.conn.execute("UPDATE applications SET timeline=? WHERE id=?",
                            (json.dumps(timeline), aid))
    ctx.collab.conn.commit()
    return aid


def test_followup_check_proposes_after_stale_days(ctx):
    aid = _seed_sent_application(ctx, days_old=11)
    out = followup_check(ctx)
    assert out["followed_up"] == 1

    pending = ctx.store.list_approvals("pending")
    batch = [a for a in pending if a["dedup_key"] == f"followup_{aid}"]
    assert len(batch) == 1
    assert batch[0]["tier"] == 2


def test_followup_check_silent_when_recent(ctx):
    _seed_sent_application(ctx, days_old=2)
    out = followup_check(ctx)
    assert out["followed_up"] == 0


def test_followup_check_no_double_followup(ctx):
    aid = _seed_sent_application(ctx, days_old=11)
    followup_check(ctx)
    out2 = followup_check(ctx)
    assert out2["followed_up"] == 0   # already has a followup_{aid} approval
    batch = [a for a in ctx.store.list_approvals("pending")
            if a["dedup_key"] == f"followup_{aid}"]
    assert len(batch) == 1


def test_followup_check_marks_ghosted_after_window(ctx):
    aid = _seed_sent_application(ctx, days_old=25)
    # Manually seed a prior followup approval (simulates one sent 15 days ago)
    ctx.store.create_approval(tier=2, action_type="tool_call", title="followup",
                              payload={}, dedup_key=f"followup_{aid}", status="executed")
    out = followup_check(ctx)
    assert out["ghosted"] == 1
    app = ctx.store.get_application(ctx.user_id, aid)
    assert app["status"] == "ghosted"


def test_followup_check_skips_portal_channel_no_email(ctx):
    _seed_sent_application(ctx, days_old=15, to_email=None)
    out = followup_check(ctx)
    assert out["followed_up"] == 0


def test_followup_check_kill_switch(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_APPLICATION_TRACKER", "0")
    _seed_sent_application(ctx, days_old=15)
    out = followup_check(ctx)
    assert out.get("skipped")


def test_application_followup_job_wired():
    assert "application_followup_check" in HANDLERS


# ---------------------------------------------------------------------------
# job_scout -> auto-apply-proposal wiring
# ---------------------------------------------------------------------------

class _StubLLM:
    def __init__(self, response):
        self._response = response

    def generate(self, system, prompt, context="", sensitive=False, fast=False):
        return (json.dumps(self._response), "scripted")


class _JobspyClient(_FakeClient):
    async def list_tools(self):
        return [{"name": "search_jobs", "description": "", "input_schema": {"properties": {}}}]


def _mock_jobspy_and_none_else(monkeypatch, jobs):
    row = _fake_row("jobspy")
    client = _JobspyClient({"is_error": False, "text": "", "structured": jobs})
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: row if source == "jobspy" else None)
    monkeypatch.setattr("amy.connectors.mcp_call.mcp_client_for", lambda r: client)


def test_job_scout_proposes_application_for_high_score(ctx, monkeypatch):
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("Become a GenAI Engineer", domain="career")
    ctx.collab.conn.execute("UPDATE goals SET career_meta=? WHERE id=?",
                            (json.dumps({"target_role": "GenAI Engineer"}), gid))
    ctx.collab.conn.commit()
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    monkeypatch.setenv("AMY_AGENT_APPLICATION_TRACKER", "1")
    _mock_jobspy_and_none_else(monkeypatch, [_POSTING_EMAIL])
    # The autouse fixture above forces _get_llm -> None unconditionally;
    # override it here so scoring actually consults ctx.llm.
    ctx.llm = _StubLLM({"scores": [{"index": 0, "score": 95, "factors": {}}]})
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: ctx.llm)

    JobScoutSensor(ctx.events(), ctx).poll()

    apps = ctx.store.list_applications(ctx.user_id)
    assert len(apps) == 1
    pending = ctx.store.list_approvals("pending")
    assert [a for a in pending if a["payload"].get("tool") == "send_hr_email"]


def test_job_scout_skips_auto_apply_when_tracker_disabled(ctx, monkeypatch):
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("Become a GenAI Engineer", domain="career")
    ctx.collab.conn.execute("UPDATE goals SET career_meta=? WHERE id=?",
                            (json.dumps({"target_role": "GenAI Engineer"}), gid))
    ctx.collab.conn.commit()
    ctx.store.set_career_profile(ctx.user_id, target_role="GenAI Engineer")
    monkeypatch.setenv("AMY_AGENT_APPLICATION_TRACKER", "0")
    _mock_jobspy_and_none_else(monkeypatch, [_POSTING_EMAIL])
    ctx.llm = _StubLLM({"scores": [{"index": 0, "score": 95, "factors": {}}]})
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: ctx.llm)

    JobScoutSensor(ctx.events(), ctx).poll()

    assert ctx.store.list_applications(ctx.user_id) == []
