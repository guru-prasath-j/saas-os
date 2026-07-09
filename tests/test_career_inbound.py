"""CAREER AUTOPILOT Part 5D — inbound HR-response detection. No Gmail, no
LLM: the hook is transport-free (sync_gmail does the fetching in prod) and
classification degrades to the deterministic keyword ladder when
_get_llm -> None.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.career_inbound import (CareerInboundHook, _classify_keywords,
                                build_inbound_hook)
from amy.collab import CollabDB


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-inbound", "me@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)


def _seed_app(ctx, status="sent", to_email="hr@acme.example",
              company="Acme Corp", sent_refs=None):
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "ML Engineer", "company": company,
        "url": f"https://jobs.example/{company.replace(' ', '-')}",
        "location": "Bangalore", "description": "ml things"})
    app_id = ctx.store.create_application(
        ctx.user_id, pid, channel="email",
        draft=json.dumps({"subject": "s", "body": "b", "to_email": to_email}))
    ctx.store.update_application_status(ctx.user_id, app_id, status, "seeded")
    for ref in (sent_refs or []):
        ctx.store.add_application_thread_ref(ctx.user_id, app_id, "sent", ref)
    return app_id


def _headers(from_addr, subject, in_reply_to=""):
    h = {"from": from_addr, "subject": subject}
    if in_reply_to:
        h["in-reply-to"] = in_reply_to
    return h


def _status(ctx, app_id):
    return ctx.store.get_application(ctx.user_id, app_id)["status"]


def _pending_followups(ctx):
    return ctx.collab.conn.execute(
        "SELECT COUNT(*) n FROM approvals WHERE dedup_key LIKE 'followup_%'"
    ).fetchone()["n"]


# --- matching ---------------------------------------------------------------

def test_thread_match_moves_sent_to_response_exactly_once(ctx):
    app_id = _seed_app(ctx, sent_refs=["<msg1@amy>"])
    hook = build_inbound_hook(ctx)
    hdrs = _headers("someone-else@totally-unrelated.example",
                    "Re: your application",
                    in_reply_to="<msg1@amy>")
    assert hook.handle("g1", hdrs, "Thanks, could you please send your "
                                   "notice period details?") is True
    assert _status(ctx, app_id) == "response"

    # same message re-fetched by a later poll window — a no-op, and the
    # timeline gains no second 'response' entry
    hook2 = build_inbound_hook(ctx)
    assert hook2.handle("g1", hdrs, "same body") is True
    timeline = ctx.store.get_application(ctx.user_id, app_id)["timeline"]
    assert sum(1 for t in timeline if t["status"] == "response") == 1


def test_sender_address_and_domain_match(ctx):
    app_id = _seed_app(ctx)
    hook = build_inbound_hook(ctx)
    # different mailbox, same domain as the recorded contact
    assert hook.handle("g2", _headers("recruiting-team@acme.example",
                                      "Interview availability"),
                       "We would like to schedule a call — please share "
                       "your availability.") is True
    assert _status(ctx, app_id) == "interview"


def test_company_token_matches_sender_domain(ctx):
    app_id = _seed_app(ctx, to_email="", company="Globex Systems")
    hook = build_inbound_hook(ctx)
    assert hook.handle("g3", _headers("talent@globex.com", "Next steps"),
                       "We are pleased to offer you the position. The "
                       "compensation package is attached.") is True
    assert _status(ctx, app_id) == "offer"


def test_unmatched_hr_looking_mail_is_ignored(ctx):
    """Newsletters and job-board digests never classify or touch any
    application, however interview-ish their copy sounds."""
    app_id = _seed_app(ctx)
    hook = build_inbound_hook(ctx)
    assert hook.handle("g4", _headers("digest@linkedin.example",
                                      "5 interview tips + new ML jobs"),
                       "Ace your next interview! Offers await!") is False
    assert _status(ctx, app_id) == "sent"


def test_own_outbound_copy_ignored(ctx):
    _seed_app(ctx)
    hook = build_inbound_hook(ctx)
    assert hook.handle("g5", _headers("me@example.com", "Re: application"),
                       "my own reply in the thread") is False


# --- classification / ladder -------------------------------------------------

def test_rejection_never_triggers_followup(ctx):
    """A classified rejection moves the app to 'rejected'; followup_check
    then has nothing in status='sent' to propose for."""
    from amy.career_apply import followup_check

    app_id = _seed_app(ctx, sent_refs=["<msg2@amy>"])
    hook = build_inbound_hook(ctx)
    hook.handle("g6", _headers("hr@acme.example", "Your application",
                               in_reply_to="<msg2@amy>"),
                "Unfortunately we will not be moving forward with other "
                "candidates being selected.")
    assert _status(ctx, app_id) == "rejected"
    out = followup_check(ctx)
    assert out.get("followed_up", 0) == 0
    assert _pending_followups(ctx) == 0


def test_rejection_beats_interview_keyword(ctx):
    assert _classify_keywords(
        "Thank you for interviewing. Unfortunately we chose other candidates."
    ) == "rejection"


def test_ladder_only_moves_forward(ctx):
    """A second info-request while already in 'response' records the message
    but does not append another status transition."""
    app_id = _seed_app(ctx, status="interview")
    hook = build_inbound_hook(ctx)
    assert hook.handle("g7", _headers("hr@acme.example", "One more thing"),
                       "Could you provide your notice period, please send "
                       "documents.") is True
    assert _status(ctx, app_id) == "interview"   # never demoted to 'response'


# --- extension hooks ----------------------------------------------------------

def test_interview_invite_notifies_extension_point(ctx):
    _seed_app(ctx)
    hook = build_inbound_hook(ctx)
    hook.handle("g8", _headers("hr@acme.example", "Interview"),
                "Please share your availability for a screening call.")
    types = [r["type"] for r in ctx.notify_store().list(limit=20)]
    assert "career_interview_invite" in types


def test_offer_notifies_extension_point(ctx):
    _seed_app(ctx, sent_refs=["<msg3@amy>"])
    hook = build_inbound_hook(ctx)
    hook.handle("g9", _headers("hr@acme.example", "Offer",
                               in_reply_to="<msg3@amy>"),
                "We are pleased to offer you the role; offer letter attached.")
    types = [r["type"] for r in ctx.notify_store().list(limit=20)]
    assert "career_offer_detected" in types


def test_status_changed_event_emitted(ctx):
    _seed_app(ctx, sent_refs=["<msg4@amy>"])
    hook = build_inbound_hook(ctx)
    hook.handle("g10", _headers("hr@acme.example", "Re: application",
                                in_reply_to="<msg4@amy>"),
                "Could you provide a reference?")
    rows = ctx.collab.conn.execute(
        "SELECT type FROM events WHERE type='career.application_status_changed'"
    ).fetchall()
    assert rows, "career.application_status_changed should be emitted"


def test_never_auto_replies(ctx):
    """Nothing in the inbound path ever proposes or performs a send —
    no send_hr_email approval rows appear from handling a reply."""
    _seed_app(ctx, sent_refs=["<msg5@amy>"])
    hook = build_inbound_hook(ctx)
    hook.handle("g11", _headers("hr@acme.example", "Question",
                                in_reply_to="<msg5@amy>"),
                "Could you provide your current CTC?")
    rows = ctx.collab.conn.execute(
        "SELECT payload FROM approvals WHERE action_type='tool_call'").fetchall()
    for r in rows:
        assert json.loads(r["payload"]).get("tool") != "send_hr_email"


# --- hook construction --------------------------------------------------------

def test_no_open_applications_no_hook(ctx):
    assert build_inbound_hook(ctx) is None


def test_kill_switch_disables_hook(ctx, monkeypatch):
    _seed_app(ctx)
    monkeypatch.setenv("AMY_AGENT_APPLICATION_TRACKER", "0")
    assert build_inbound_hook(ctx) is None


def test_gmail_query_covers_contacts_and_domains(ctx):
    _seed_app(ctx)
    hook = build_inbound_hook(ctx)
    q = hook.gmail_query()
    assert "hr@acme.example" in q and "acme.example" in q
    assert q.startswith("from:(")
