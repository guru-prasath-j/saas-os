"""CAREER AUTOPILOT Part 6 — /api/career/* routes, via TestClient (mirrors
tests/test_connectors_status.py's fixture pattern). No live MCP calls: the
portfolio/apply routes degrade to their documented no-connector paths.
"""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_client():
    data_dir = tempfile.mkdtemp(prefix="amy_career_routes_test_")
    os.environ["AMY_SAAS_DATA"] = data_dir
    from amy.saas.app import app
    from amy.saas.db import init_db
    from amy.saas import tenancy
    init_db()
    c = TestClient(app)
    email = f"career-{uuid.uuid4().hex[:8]}@test.com"
    r = c.post("/auth/signup", json={"email": email, "password": "test1234"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    uid = r.json()["user"]["id"]
    tenancy.ensure_dirs(uid)
    return c, {"Authorization": f"Bearer {token}"}, uid, data_dir


def _store_for(uid: str):
    from amy.collab import CollabDB
    from amy.automation.store import AutomationStore
    from amy.saas import paths
    cdb = CollabDB(str(paths.index_dir(uid) / "collab.db"))
    return cdb, AutomationStore(cdb)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

def test_profile_get_empty_then_put_roundtrips(app_client):
    c, headers, uid, _ = app_client
    r = c.get("/api/career/profile", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["profile"] == {} or r.json()["goal"] is None

    r = c.put("/api/career/profile", headers=headers, json={
        "target_role": "GenAI Engineer", "target_location": "Bangalore",
        "remote_ok": True, "skills": ["python", "langchain"],
        "resume_text": "Secret resume contents"})
    assert r.status_code == 200, r.text

    r = c.get("/api/career/profile", headers=headers)
    body = r.json()
    assert body["profile"]["target_role"] == "GenAI Engineer"
    assert body["profile"]["skills"] == ["python", "langchain"]
    assert "resume_text" not in body["profile"]   # never returned over the wire


# ---------------------------------------------------------------------------
# Postings / applications
# ---------------------------------------------------------------------------

def test_postings_and_applications_empty(app_client):
    c, headers, uid, _ = app_client
    r = c.get("/api/career/postings", headers=headers)
    assert r.status_code == 200 and r.json()["postings"] == []

    r = c.get("/api/career/applications", headers=headers)
    assert r.status_code == 200
    assert r.json()["applications"] == []
    assert r.json()["funnel"]["discovered"] == 0


def test_application_patch_updates_status(app_client):
    c, headers, uid, _ = app_client
    cdb, store = _store_for(uid)
    try:
        pid, _isnew = store.add_posting_if_new(uid, {
            "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1"})
        aid = store.create_application(uid, pid, channel="email")
    finally:
        cdb.close()

    r = c.patch(f"/api/career/applications/{aid}", headers=headers,
               json={"status": "interview", "note": "phone screen went well"})
    assert r.status_code == 200, r.text

    r = c.get("/api/career/applications", headers=headers)
    apps = r.json()["applications"]
    assert apps[0]["status"] == "interview"
    assert apps[0]["timeline"][-1]["note"] == "phone screen went well"


def test_application_patch_unknown_id_404(app_client):
    c, headers, uid, _ = app_client
    r = c.patch("/api/career/applications/nope", headers=headers, json={"status": "sent"})
    assert r.status_code == 404


def test_application_patch_invalid_status_400(app_client):
    c, headers, uid, _ = app_client
    cdb, store = _store_for(uid)
    try:
        pid, _ = store.add_posting_if_new(uid, {
            "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1"})
        aid = store.create_application(uid, pid)
    finally:
        cdb.close()
    r = c.patch(f"/api/career/applications/{aid}", headers=headers,
               json={"status": "not-a-real-status"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def test_apply_unknown_posting_404(app_client):
    c, headers, uid, _ = app_client
    r = c.post("/api/career/postings/nope/apply", headers=headers)
    assert r.status_code == 404


def test_apply_happy_path_parks_one_approval(app_client, monkeypatch):
    # Force the fast/deterministic no-LLM fallback path — an unset ctx.llm
    # would make _get_llm build a REAL LLMRouter and attempt real provider
    # calls (slow, network-dependent; same fix applied in Parts 3-5's tests).
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)
    c, headers, uid, _ = app_client
    cdb, store = _store_for(uid)
    try:
        pid, _ = store.add_posting_if_new(uid, {
            "title": "GenAI Engineer", "company": "Acme", "url": "https://example.invalid/1",
            "description": "Reach us at jobs@acme.example for this role."})
    finally:
        cdb.close()

    r = c.post(f"/api/career/postings/{pid}/apply", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channel"] == "email"
    assert body["proposal"]["status"] == "pending"


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def test_portfolio_route_no_target_role_skips(app_client):
    c, headers, uid, _ = app_client
    r = c.get("/api/career/portfolio", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json().get("skipped")


# ---------------------------------------------------------------------------
# Career ladder (Part 5F)
# ---------------------------------------------------------------------------

def test_patch_goal_ladder_updates_meta_and_profile(app_client):
    import json as _json
    c, headers, uid, _ = app_client
    r = c.patch("/api/career/goal", headers=headers,
                json={"target_role": "AI Mobile Engineer"})
    assert r.status_code == 404   # no active career goal yet

    cdb, store = _store_for(uid)
    try:
        cdb.conn.execute(
            "INSERT INTO goals(id,title,domain,status,created_at,career_meta)"
            " VALUES('gl','ladder','career','active',datetime('now'),'{}')")
        cdb.conn.commit()

        r = c.patch("/api/career/goal", headers=headers,
                    json={"target_role": "AI Mobile Engineer",
                          "north_star_role": "GenAI Engineer"})
        assert r.status_code == 200, r.text
        assert r.json()["north_star_role"] == "GenAI Engineer"

        meta = _json.loads(cdb.conn.execute(
            "SELECT career_meta FROM goals WHERE id='gl'").fetchone()["career_meta"])
        assert meta["target_role"] == "AI Mobile Engineer"
        assert meta["north_star_role"] == "GenAI Engineer"
        # profile follows the scouted role (ATS/drafts re-aim too)
        assert store.get_career_profile(uid)["target_role"] == "AI Mobile Engineer"

        # "" clears the north star; ladder fields surface on the goal payload
        r = c.patch("/api/career/goal", headers=headers,
                    json={"north_star_role": ""})
        assert r.status_code == 200 and r.json()["north_star_role"] is None
        g = c.get("/api/career/profile", headers=headers).json()["goal"]
        assert g["target_role"] == "AI Mobile Engineer"
        assert g["north_star_role"] is None
    finally:
        cdb.close()


# ---------------------------------------------------------------------------
# Resume version content / PDF export
# ---------------------------------------------------------------------------

def test_resume_version_content_and_pdf_routes(app_client):
    c, headers, uid, _ = app_client
    cdb, store = _store_for(uid)
    try:
        vid = store.create_resume_version(uid, "My Label", "## Highlights\n- Did a thing",
                                          target_track="GenAI Engineer")
    finally:
        cdb.close()

    r = c.get(f"/api/career/resume/versions/{vid}/content", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "My Label"
    assert "Did a thing" in body["content"]

    r2 = c.get(f"/api/career/resume/versions/{vid}/pdf", headers=headers)
    assert r2.status_code == 200
    assert r2.headers["content-type"] == "application/pdf"
    assert "My-Label.pdf" in r2.headers["content-disposition"]
    assert r2.content[:5] == b"%PDF-"


def test_resume_version_content_unknown_id_404(app_client):
    c, headers, uid, _ = app_client
    assert c.get("/api/career/resume/versions/nope/content",
                 headers=headers).status_code == 404
    assert c.get("/api/career/resume/versions/nope/pdf",
                 headers=headers).status_code == 404


def test_resume_version_content_is_owner_scoped(app_client):
    """A second user's resume version must 404 for the first user — the
    lookup is uid-scoped, never leaking cross-account."""
    c, headers, uid, _ = app_client
    cdb, store = _store_for(uid)
    try:
        vid = store.create_resume_version(uid, "Owner Only", "secret content",
                                          target_track="X")
    finally:
        cdb.close()

    email2 = f"other-{uuid.uuid4().hex[:8]}@test.com"
    r2 = c.post("/auth/signup", json={"email": email2, "password": "test1234"})
    headers2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    from amy.saas import tenancy
    tenancy.ensure_dirs(r2.json()["user"]["id"])

    assert c.get(f"/api/career/resume/versions/{vid}/content",
                headers=headers2).status_code == 404
