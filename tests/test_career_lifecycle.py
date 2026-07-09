"""CAREER AUTOPILOT Part 5E — pipeline safety + lifecycle: duplicate guard,
cross-source fuzzy dedup, wind-down bundle, debrief once-guard, resume
evolution, referral check, retention. All MCP/LLM/calendar calls mocked.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.automation.executors import approve, execute
from amy.automation.jobs import HANDLERS
from amy.career_apply import duplicate_application_block, prepare_application
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-life", "me@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)


def _seed_posting(ctx, company="Acme Corp", title="ML Engineer",
                  location="Bangalore", url=None, source="jobspy"):
    pid, is_new = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": title, "company": company, "location": location,
        "url": url or f"https://a.example/{company}-{title}".replace(" ", "-"),
        "description": "desc with hr@acme.example contact", "source": source})
    return pid, is_new


def _seed_application(ctx, pid, status="sent", days_old=0):
    import datetime as _dt
    app_id = ctx.store.create_application(
        ctx.user_id, pid, channel="email",
        draft=json.dumps({"to_email": "hr@acme.example"}))
    ctx.store.update_application_status(ctx.user_id, app_id, status, "seeded")
    if days_old:
        old = (_dt.datetime.now(_dt.timezone.utc)
               - _dt.timedelta(days=days_old)).isoformat()
        ctx.collab.conn.execute(
            "UPDATE applications SET timeline=?, updated_at=? WHERE id=?",
            (json.dumps([{"ts": old, "status": status, "note": "aged"}]),
             old, app_id))
        ctx.collab.conn.commit()
    return app_id


def _pending(ctx, action_type=None):
    q = "SELECT * FROM approvals WHERE status='pending'"
    rows = [dict(r) for r in ctx.collab.conn.execute(q).fetchall()]
    if action_type:
        rows = [r for r in rows if r["action_type"] == action_type]
    return rows


# --- duplicate-application guard ----------------------------------------------

def test_duplicate_guard_blocks_active_application(ctx):
    pid, _ = _seed_posting(ctx)
    _seed_application(ctx, pid, status="sent")
    pid2, _ = _seed_posting(ctx, title="Senior ML Engineer",
                            url="https://b.example/other")
    block = duplicate_application_block(
        ctx, ctx.store.get_posting(ctx.user_id, pid2))
    assert block is not None and "already active" in block["reason"]


def test_duplicate_guard_blocks_recent_rejection_allows_old(ctx):
    pid, _ = _seed_posting(ctx)
    _seed_application(ctx, pid, status="rejected", days_old=10)
    pid2, _ = _seed_posting(ctx, title="Other Role", url="https://b.example/x")
    posting2 = ctx.store.get_posting(ctx.user_id, pid2)
    assert duplicate_application_block(ctx, posting2) is not None

    # age the rejection past the window — clear to re-apply
    ctx.collab.conn.execute("DELETE FROM applications")
    ctx.collab.conn.commit()
    _seed_application(ctx, pid, status="rejected", days_old=90)
    assert duplicate_application_block(ctx, posting2) is None


def test_agent_path_blocked_absolutely_manual_override_works(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, source: None)
    pid, _ = _seed_posting(ctx)
    _seed_application(ctx, pid, status="interview")
    pid2, _ = _seed_posting(ctx, title="Another Role", url="https://b.example/y")

    # agent path (no force): blocked, no approval parked
    out = prepare_application(ctx, pid2)
    assert "blocked" in out
    assert _pending(ctx) == []

    # manual override: proceeds and parks exactly one approval
    out2 = prepare_application(ctx, pid2, force=True)
    assert "application_id" in out2
    assert len(_pending(ctx)) == 1


def test_different_company_not_blocked(ctx):
    pid, _ = _seed_posting(ctx)
    _seed_application(ctx, pid, status="sent")
    pid2, _ = _seed_posting(ctx, company="Globex", url="https://b.example/g")
    assert duplicate_application_block(
        ctx, ctx.store.get_posting(ctx.user_id, pid2)) is None


# --- cross-source fuzzy dedup ---------------------------------------------------

def test_fuzzy_dedup_merges_two_sources_one_row(ctx):
    pid1, new1 = _seed_posting(ctx, url="https://indeed.example/123",
                               source="jobspy")
    pid2, new2 = _seed_posting(ctx, title="ML  Engineer!",   # punctuation noise
                               url="https://linkedin.example/456",
                               source="linkedin")
    assert new1 is True and new2 is False and pid1 == pid2
    posting = ctx.store.get_posting(ctx.user_id, pid1)
    assert posting["sources_count"] == 2
    assert posting["alt_sources"][0]["url"] == "https://linkedin.example/456"
    assert ctx.collab.conn.execute(
        "SELECT COUNT(*) n FROM job_postings").fetchone()["n"] == 1


def test_fuzzy_dedup_distinct_locations_stay_separate(ctx):
    pid1, _ = _seed_posting(ctx, url="https://a.example/1")
    pid2, new2 = _seed_posting(ctx, location="Chennai",
                               url="https://a.example/2")
    assert new2 is True and pid1 != pid2


# --- wind-down bundle -----------------------------------------------------------

def _accept_offer(ctx, app_id):
    from amy.events.factory import get_events
    es = get_events(ctx.user_id, ctx.collab, ctx=ctx)
    ctx.store.update_application_status(ctx.user_id, app_id, "accepted", "human")
    es.emit("career.application_status_changed",
            {"application_id": app_id, "status": "accepted"}, source="career_ui")


def test_accepted_offer_proposes_one_winddown_bundle(ctx):
    ctx.collab.conn.execute(
        "INSERT INTO goals(id,title,domain,status,created_at)"
        " VALUES('g1','Become an MLE','career','active',datetime('now'))")
    ctx.collab.conn.commit()
    pid, _ = _seed_posting(ctx)
    app_id = _seed_application(ctx, pid, status="offer")
    _accept_offer(ctx, app_id)
    bundles = _pending(ctx, "career_wind_down")
    assert len(bundles) == 1
    # re-emitting doesn't double-propose (dedup key)
    _accept_offer(ctx, app_id)
    assert len(_pending(ctx, "career_wind_down")) == 1


def test_winddown_execution_closes_goal_archives_postings(ctx):
    ctx.collab.conn.execute(
        "INSERT INTO goals(id,title,domain,status,created_at)"
        " VALUES('g2','Become an MLE','career','active',datetime('now'))")
    ctx.collab.conn.commit()
    pid, _ = _seed_posting(ctx)
    pid_open, _ = _seed_posting(ctx, company="Globex", url="https://g.example/1")
    app_id = _seed_application(ctx, pid, status="offer")

    result = execute(ctx, "career_wind_down",
                     {"goal_id": "g2", "accepted_application_id": app_id,
                      "withdraw_others": False})
    assert result["closed_goal"] is True
    assert result["archived_postings"] >= 1
    goal = ctx.collab.conn.execute(
        "SELECT status FROM goals WHERE id='g2'").fetchone()
    assert goal["status"] == "completed"

    # goal closed -> the scout's next poll is a no-op
    from amy.career_scout import JobScoutSensor
    from amy.events.store import EventStore
    assert JobScoutSensor(EventStore(ctx.collab), ctx).poll() == []


def test_winddown_withdrawals_park_as_individual_tier2_sends(ctx):
    pid, _ = _seed_posting(ctx)
    app_id = _seed_application(ctx, pid, status="offer")
    pid2, _ = _seed_posting(ctx, company="Globex", url="https://g.example/2")
    other_app = _seed_application(ctx, pid2, status="sent")

    result = execute(ctx, "career_wind_down",
                     {"goal_id": None, "accepted_application_id": app_id,
                      "withdraw_others": True})
    assert result["withdrawal_sends_proposed"] == 1
    sends = [r for r in _pending(ctx, "tool_call")
             if json.loads(r["payload"]).get("tool") == "send_hr_email"]
    assert len(sends) == 1 and sends[0]["tier"] == 2


# --- interview debrief -----------------------------------------------------------

def _fake_calendar(monkeypatch, events_items):
    class _Events:
        def list(self, **kw):
            return self
        def execute(self):
            return {"items": events_items}

    class _Svc:
        def events(self):
            return _Events()

    import googleapiclient.discovery as d
    monkeypatch.setattr(d, "build", lambda *a, **k: _Svc())


def test_debrief_prompts_exactly_once(ctx, monkeypatch):
    import datetime as _dt

    from amy.agents.reactive import interview_debrief_check
    from amy.events.store import EventStore

    pid, _ = _seed_posting(ctx)
    _seed_application(ctx, pid, status="interview")
    monkeypatch.setattr(type(ctx), "google_creds", lambda self: object())
    ended = (_dt.datetime.now(_dt.timezone.utc)
             - _dt.timedelta(hours=1)).isoformat()
    _fake_calendar(monkeypatch, [{
        "id": "ev1", "summary": "Acme final interview",
        "end": {"dateTime": ended}}])
    monkeypatch.setattr("amy.saas.tenancy.resolve_vault_dir",
                        lambda uid: Path(ctx.finance_path).parent / "vault")

    es = EventStore(ctx.collab)
    assert interview_debrief_check(es, ctx) == 1
    assert interview_debrief_check(es, ctx) == 0   # never re-prompts
    types = [r["type"] for r in ctx.notify_store().list(limit=20)]
    assert types.count("career_interview_debrief") == 1


def test_debrief_ignores_unrelated_meetings(ctx, monkeypatch):
    import datetime as _dt

    from amy.agents.reactive import interview_debrief_check
    from amy.events.store import EventStore

    pid, _ = _seed_posting(ctx)
    _seed_application(ctx, pid, status="interview")
    monkeypatch.setattr(type(ctx), "google_creds", lambda self: object())
    ended = (_dt.datetime.now(_dt.timezone.utc)
             - _dt.timedelta(hours=1)).isoformat()
    _fake_calendar(monkeypatch, [{
        "id": "ev2", "summary": "Dentist appointment",
        "end": {"dateTime": ended}}])
    assert interview_debrief_check(EventStore(ctx.collab), ctx) == 0


# --- resume evolution --------------------------------------------------------------

def test_resume_evolution_proposes_tier2_with_diff(ctx):
    from amy.agents.reactive import _propose_resume_evolution

    ctx.store.set_career_profile(ctx.user_id, resume_text="I build ML systems.")
    status = _propose_resume_evolution(ctx, [
        {"repo": "cool-repo", "why": "", "bullets": ["Built cool-repo, a "
         "streaming feature store used in production."]}])
    assert status == "pending"
    rows = _pending(ctx, "resume_update")
    assert len(rows) == 1
    assert "resume (proposed)" in rows[0]["body"]   # the diff is in the approval

    # approving applies it
    out = approve(ctx, rows[0]["id"])
    assert out["status"] == "executed"
    profile = ctx.store.get_career_profile(ctx.user_id)
    assert "cool-repo" in profile["resume_text"]


def test_resume_evolution_skips_without_master_resume(ctx):
    from amy.agents.reactive import _propose_resume_evolution
    assert _propose_resume_evolution(ctx, [
        {"repo": "r", "bullets": ["something"]}]) is None
    assert _pending(ctx, "resume_update") == []


# --- referral check ------------------------------------------------------------------

def test_referral_check_finds_own_graph_mentions(ctx, monkeypatch):
    """The graph vocabulary has no person type (note/email/calendar/task/
    goal/memory) — an email node naming the company is the warm-path
    signal, surfaced with its linked nodes for context."""
    from amy.career_apply import _referral_paths
    from amy.knowledge_graph.store import GraphStore

    g = GraphStore(str(Path(ctx.finance_path).parent / "graph.db"))
    g.add_node("e1", "email", "Intro call with Ravi (Acme)")
    g.add_node("n1", "note", "Ravi Kumar - contacts")
    g.add_edge("e1", "n1", "related_to")
    g.commit()
    g.close()
    monkeypatch.setattr("amy.saas.tenancy.resolve_vault_dir",
                        lambda uid: Path(ctx.finance_path).parent / "novault")
    paths_found = _referral_paths(ctx, "Acme Corp")
    assert any("Acme" in p for p in paths_found)
    assert any("Ravi Kumar" in p for p in paths_found)   # linked context


def test_referral_check_empty_is_honest(ctx, monkeypatch):
    from amy.career_apply import _referral_paths
    monkeypatch.setattr("amy.saas.tenancy.resolve_vault_dir",
                        lambda uid: Path(ctx.finance_path).parent / "novault")
    assert _referral_paths(ctx, "NoSuchCo") == []


# --- retention ---------------------------------------------------------------------

def test_retention_archives_old_unapplied_keeps_applications(ctx):
    import datetime as _dt

    old = (_dt.datetime.now(_dt.timezone.utc)
           - _dt.timedelta(days=120)).isoformat()
    pid_old, _ = _seed_posting(ctx, company="OldCo", url="https://o.example/1")
    pid_applied, _ = _seed_posting(ctx, company="AppliedCo",
                                   url="https://o.example/2")
    ctx.collab.conn.execute(
        "UPDATE job_postings SET discovered_at=?", (old,))
    ctx.collab.conn.commit()
    app_id = _seed_application(ctx, pid_applied, status="rejected")
    ctx.collab.conn.execute(
        "INSERT INTO events(id,type,payload,source,ts) VALUES"
        " ('e1','career.job_discovered',?, 'job_scout',?)",
        (json.dumps({"posting_id": pid_old}), old))
    ctx.collab.conn.commit()

    out = HANDLERS["career_retention"](ctx)
    assert out["archived"] == 1
    assert out["events_compacted"] == 1
    assert ctx.store.get_posting(ctx.user_id, pid_old)["status"] == "archived"
    # the applied-to posting is untouched; the application row still exists
    assert ctx.store.get_posting(ctx.user_id, pid_applied)["status"] == "discovered"
    assert ctx.store.get_application(ctx.user_id, app_id) is not None
