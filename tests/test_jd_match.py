"""JD Match Advisor (amy/jd_match.py) — paste a JD, get a grounded match
report against career_profile.resume_text. All JD text and resume text
below is SYNTHETIC test fixture data, not real postings/resumes.

Scope note: the original brief assumed a resume-versioning system (Phase
D3/D2 — resume_entries, per-version scoring, reorder proposals) that does
not exist in this codebase; this module scores against the one real
resume_text field instead (see amy/jd_match.py's module docstring). These
tests cover the adapted scope only.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.jd_match import analyze_jd, explain_jd_match


@pytest.fixture()
def ctx(tmp_path):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-jd", "t@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


# Synthetic fixture text — not a real posting or resume.
_STRONG_RESUME = (
    "Built RAG pipelines with LangChain over vector databases, deployed "
    "services on Kubernetes, and developed Flutter mobile apps with "
    "PostgreSQL backends. Strong Python skills.")
_STRONG_JD = (
    "We are hiring a GenAI Engineer. You will build RAG pipelines using "
    "LangChain, work with vector databases, and deploy services on "
    "Kubernetes. Experience with Flutter mobile development and "
    "PostgreSQL is a plus. Strong Python skills required for this role.")

_WEAK_JD = "Looking for someone great. Apply now."   # deliberately vague/short

_LITERAL_GAP_RESUME = "Experienced with K8s deployments and Python services."
_LITERAL_GAP_JD = (
    "The ideal candidate has production Kubernetes experience, strong "
    "Python skills, and has shipped several backend services at scale "
    "using modern container orchestration tooling in cloud environments.")


# --- strong match ----------------------------------------------------------

def test_strong_match_high_score_few_gaps(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    out = analyze_jd(ctx, _STRONG_JD)
    assert out["overall_match_score"] is not None
    assert out["overall_match_score"] >= 40   # meaningfully covered
    assert "RAG" in out["matched_requirements"]
    assert "Flutter" in out["matched_requirements"]
    assert "PostgreSQL" in out["matched_requirements"]
    assert out["analysis_id"]


# --- weak match / honest low confidence -------------------------------------

def test_weak_short_jd_flags_low_confidence_not_padded(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    out = analyze_jd(ctx, _WEAK_JD)
    assert out["confidence"] == "low"
    # a 5-word JD must not produce a padded, confident-looking report
    assert len(out["missing_requirements"]) <= 3
    assert len(out["matched_requirements"]) <= 3


def test_no_resume_on_file_is_honest_not_fabricated(ctx):
    out = analyze_jd(ctx, _STRONG_JD)   # no set_career_profile call at all
    assert out["overall_match_score"] is None
    assert "no resume" in out["note"].lower()
    assert len(out["missing_requirements"]) > 0   # everything honestly missing


# --- literal ATS keyword gap, kept distinct from missing_requirements -----------

def test_literal_term_gap_distinct_from_missing_bucket(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_LITERAL_GAP_RESUME)
    out = analyze_jd(ctx, _LITERAL_GAP_JD)
    gap_terms = {g["jd_term"] for g in out["literal_term_gaps"]}
    assert "Kubernetes" in gap_terms
    gap = next(g for g in out["literal_term_gaps"] if g["jd_term"] == "Kubernetes")
    assert gap["resume_has_synonym"] == "k8s"
    # the same term must NOT also appear in missing_requirements — the two
    # buckets are mutually exclusive, not double-counted
    missing_terms = {m["term"] for m in out["missing_requirements"]}
    assert "Kubernetes" not in missing_terms


def test_missing_requirement_has_no_synonym_evidence(ctx):
    """A term absent in every form (no literal match, no known synonym in
    the resume) belongs in missing_requirements, never literal_term_gaps —
    literal_term_gaps only fires when the underlying skill plausibly IS
    present under another name."""
    ctx.store.set_career_profile(ctx.user_id, resume_text="Experienced with Python only.")
    out = analyze_jd(ctx, _LITERAL_GAP_JD)
    gap_terms = {g["jd_term"] for g in out["literal_term_gaps"]}
    missing_terms = {m["term"] for m in out["missing_requirements"]}
    assert "Kubernetes" not in gap_terms   # no "k8s" synonym present -> not a gap
    assert "Kubernetes" in missing_terms   # genuinely absent -> missing


# --- stated_in_jd_as traces to real JD text ----------------------------------------

def test_missing_requirement_traces_to_real_jd_text(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text="Nothing relevant here.")
    out = analyze_jd(ctx, _STRONG_JD)
    for m in out["missing_requirements"]:
        assert m["term"].lower() in _STRONG_JD.lower()
        assert m["stated_in_jd_as"]   # non-empty real snippet


# --- job_posting_id linkage + opt-in keyword backfill --------------------------------

def test_standalone_jd_never_touches_job_postings(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://x/1",
        "location": "Remote", "description": "d"})
    out = analyze_jd(ctx, _STRONG_JD)   # no job_posting_id passed
    assert out["job_posting_id"] is None
    assert out["backfilled_posting_keywords"] is False
    assert ctx.store.get_posting(ctx.user_id, pid)["keywords"] == []


def test_linked_posting_with_thin_keywords_gets_backfilled(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://x/2",
        "location": "Remote", "description": "d"})
    out = analyze_jd(ctx, _STRONG_JD, job_posting_id=pid)
    assert out["job_posting_id"] == pid
    assert out["backfilled_posting_keywords"] is True
    assert len(ctx.store.get_posting(ctx.user_id, pid)["keywords"]) > 0


def test_linked_posting_with_existing_keywords_not_overwritten(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    pid, _ = ctx.store.add_posting_if_new(ctx.user_id, {
        "title": "GenAI Engineer", "company": "Acme", "url": "https://x/3",
        "location": "Remote", "description": "d"})
    ctx.store.set_posting_keywords(ctx.user_id, pid, ["existing", "curated", "list"])
    analyze_jd(ctx, _STRONG_JD, job_posting_id=pid)
    assert ctx.store.get_posting(ctx.user_id, pid)["keywords"] == \
        ["existing", "curated", "list"]


# --- persistence + explain_jd_match ------------------------------------------------

def test_analysis_persisted_and_listed(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    out = analyze_jd(ctx, _STRONG_JD)
    stored = ctx.store.get_jd_analysis(ctx.user_id, out["analysis_id"])
    assert stored is not None
    assert stored["raw_jd_text"] == _STRONG_JD
    history = ctx.store.list_jd_analyses(ctx.user_id)
    assert any(a["id"] == out["analysis_id"] for a in history)


def test_explain_jd_match_summarizes_without_rescoring(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    out = analyze_jd(ctx, _STRONG_JD)
    explained = explain_jd_match(ctx, out["analysis_id"])
    assert explained["analysis_id"] == out["analysis_id"]
    assert "Match score" in explained["summary"]


def test_explain_jd_match_unknown_id_is_honest_error(ctx):
    out = explain_jd_match(ctx, "does-not-exist")
    assert "error" in out


def test_empty_jd_text_errors(ctx):
    out = analyze_jd(ctx, "   ")
    assert "error" in out


# --- event emission -------------------------------------------------------------------

def test_emits_jd_analyzed_event(ctx):
    ctx.store.set_career_profile(ctx.user_id, resume_text=_STRONG_RESUME)
    analyze_jd(ctx, _STRONG_JD)
    rows = ctx.collab.conn.execute(
        "SELECT type FROM events WHERE type='career.jd_analyzed'").fetchall()
    assert rows


# --- tool registration --------------------------------------------------------------

def test_analyze_jd_and_explain_registered_as_read_tools():
    from amy import tools
    names = {t["name"]: t for t in tools.list_tools()}
    assert names["analyze_jd"]["risk"] == "read"
    assert names["explain_jd_match"]["risk"] == "read"


# --- shared scorer: no duplication between posting-ATS and JD-match ---------------------

def test_ats_estimate_and_jd_match_share_one_scorer():
    from amy.automation.orchestrator import score_keyword_coverage
    from amy.career_apply import _ats_estimate
    posting = {"title": "GenAI Engineer", "company": "Acme", "url": "https://x/4",
              "description": "LangChain RAG vector database experience wanted."}
    out = _ats_estimate("Experienced with LangChain and RAG pipelines.", posting)
    # _ats_estimate must delegate to the shared function, not a private copy
    direct = score_keyword_coverage(
        "Experienced with LangChain and RAG pipelines.",
        __import__("amy.automation.orchestrator", fromlist=["_extract_keywords"])
        ._extract_keywords([posting], top_n=15))
    assert out["matched"] == direct["matched"]
    assert out["coverage_pct"] == direct["coverage_pct"]


# ---------------------------------------------------------------------------
# Routes (TestClient, mirrors tests/test_career_routes.py's fixture)
# ---------------------------------------------------------------------------

@pytest.fixture()
def app_client():
    data_dir = tempfile.mkdtemp(prefix="amy_jd_match_routes_test_")
    os.environ["AMY_SAAS_DATA"] = data_dir
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    c = TestClient(app)
    email = f"jdmatch-{uuid.uuid4().hex[:8]}@test.com"
    r = c.post("/auth/signup", json={"email": email, "password": "test1234"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    uid = r.json()["user"]["id"]
    tenancy.ensure_dirs(uid)
    return c, {"Authorization": f"Bearer {token}"}, uid


def _set_resume(c, headers, resume_text):
    r = c.put("/api/career/profile", headers=headers, json={"resume_text": resume_text})
    assert r.status_code == 200, r.text


def test_route_analyze_then_list_then_get(app_client):
    c, headers, _uid = app_client
    _set_resume(c, headers, _STRONG_RESUME)

    r = c.post("/api/career/jd/analyze", headers=headers, json={"jd_text": _STRONG_JD})
    assert r.status_code == 200, r.text
    aid = r.json()["analysis_id"]
    assert r.json()["overall_match_score"] is not None

    r2 = c.get("/api/career/jd/analyses", headers=headers)
    assert r2.status_code == 200
    assert any(a["id"] == aid for a in r2.json()["analyses"])

    r3 = c.get(f"/api/career/jd/analyses/{aid}", headers=headers)
    assert r3.status_code == 200
    assert r3.json()["id"] == aid


def test_route_empty_jd_text_400s(app_client):
    c, headers, _uid = app_client
    r = c.post("/api/career/jd/analyze", headers=headers, json={"jd_text": "  "})
    assert r.status_code == 400


def test_route_unknown_analysis_id_404s(app_client):
    c, headers, _uid = app_client
    r = c.get("/api/career/jd/analyses/nope", headers=headers)
    assert r.status_code == 404
