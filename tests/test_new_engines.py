"""Offline tests for Decision/Predictive/Simulation engines, Digital Twin suite,
and the GitHub Sensor. No network, no LLM."""
import datetime as _dt
import os
import tempfile

import pytest

from amy.collab.db import CollabDB
from amy.events.store import EventStore
from amy.engines.decision_engine import DecisionEngine
from amy.engines.predictive_engine import PredictiveEngine
from amy.engines.simulation_engine import SimulationEngine
from amy.digital_twin import DigitalTwinEngine, PersonalityEngine, FutureSelfAgent
from amy.sensors import GitHubSensor, NEW_COMMIT, NEW_ISSUE, CI_FAILURE
from amy.sensors.github_service import GitHubService


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(days_ago=0):
    return (_now() - _dt.timedelta(days=days_ago)).isoformat()


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    d = CollabDB(path)
    yield d
    d.close()
    os.unlink(path)


class _Note:
    def __init__(self, body):
        self.body = body
        self.title = "n"
        self.category = "learning"
        self.path = "n.md"
        self.owner = "me"
        self.tags = []


# --- Decision Engine ----------------------------------------------------
def test_decision_categories_and_history(db):
    e = DecisionEngine(db)
    did = e.record("Take the new job", category="career", reason="more pay", confidence=0.8)
    e.record("Buy index funds", category="finance", confidence=0.6)
    assert e.get(did)["category"] == "career"
    hist = e.history(category="career")
    assert len(hist) == 1 and hist[0]["title"] == "Take the new job"
    assert len(e.history()) == 2


def test_decision_invalid_category_falls_back(db):
    e = DecisionEngine(db)
    did = e.record("x", category="nonsense")
    assert e.get(did)["category"] == "personal"


def test_decision_analysis_and_outcomes(db):
    e = DecisionEngine(db)
    a = e.record("good call", category="career", confidence=0.8)
    b = e.record("bad call", category="career", confidence=0.9)
    e.set_outcome(a, "this worked out great, success")
    e.set_outcome(b, "regret, it was a mistake")
    rep = e.analyze()
    assert rep["total"] == 2
    assert rep["resolved"] == 2
    assert rep["by_category"]["career"]["success_rate"] == 0.5
    assert rep["by_category"]["career"]["avg_confidence"] is not None


def test_decision_recommendations(db):
    e = DecisionEngine(db)
    for i in range(4):
        did = e.record(f"finance {i}", category="finance", confidence=0.8)
        e.set_outcome(did, "bad mistake")  # all bad
    recs = e.recommend("finance")
    assert any("finance" in r for r in recs)


def test_decision_interop_with_legacy_table(db):
    """Legacy intelligence.DecisionEngine and new engine share the table."""
    from amy.intelligence.decisions import DecisionEngine as Legacy
    legacy = Legacy(db)
    lid = legacy.record("legacy decision", domain="health", confidence=0.5)
    new = DecisionEngine(db)
    got = new.get(lid)
    assert got is not None and got["category"] == "health"


# --- Predictive Engine --------------------------------------------------
def test_forecast_goal_eta(db):
    db.conn.execute(
        "INSERT INTO goals (id,title,domain,status,progress,created_at,target_date) VALUES (?,?,?,?,?,?,?)",
        ("g1", "Ship app", "projects", "active", 0.5, _iso(10), _iso(-30)))
    db.conn.commit()
    p = PredictiveEngine(db)
    f = p.forecast_goal("g1")
    assert f["eta_days"] is not None
    # 50% in 10 days -> ~10 more days
    assert 8 <= f["eta_days"] <= 12
    assert f["on_track"] is True


def test_forecast_productivity_trend(db):
    p = PredictiveEngine(db)
    # 3 activities this week, 1 last week -> up
    for d in (1, 2, 3):
        db.conn.execute("INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
                        (_iso(d), "note", "x", "learning"))
    db.conn.execute("INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
                    (_iso(10), "note", "x", "learning"))
    db.conn.commit()
    f = p.forecast_productivity()
    assert f["this_week"] == 3 and f["prev_week"] == 1
    assert f["trend"] == "up"
    lf = p.forecast_learning()
    assert lf["this_week"] == 3


# --- Simulation Engine --------------------------------------------------
def test_sim_job_change():
    s = SimulationEngine()
    r = s.simulate("job_change", current_salary=100000, new_salary=125000)
    assert r["salary_change_pct"] == 25.0
    assert "Strong raise" in r["recommendation"]


def test_sim_financial_deficit():
    s = SimulationEngine()
    r = s.simulate("financial_change", monthly_income=3000, monthly_expenses=3500)
    assert r["new_monthly_net"] == -500
    assert "deficit" in r["recommendation"]


def test_sim_learning_and_project():
    s = SimulationEngine()
    r = s.simulate("learning_path", total_hours=100, hours_per_week=5, skill="Rust")
    assert r["estimated_weeks"] == 20.0
    pr = s.simulate("project_timeline", total_tasks=20, completed_tasks=5,
                    tasks_per_week=3, deadline_weeks=3)
    assert pr["remaining_tasks"] == 15
    assert pr["on_track"] is False


def test_sim_unknown():
    assert "error" in SimulationEngine().simulate("nope")


# --- Personality + Digital Twin + Future Self ---------------------------
def test_personality_profile(db):
    notes = [_Note("I build things. I ship fast. Short sentences win.")]
    pe = PersonalityEngine(notes, db)
    prof = pe.profile()
    assert prof["writing_style"]["sample_words"] > 0
    assert "verbosity" in prof["writing_style"]
    assert isinstance(prof["priorities"], list)


def test_digital_twin_engine_snapshot(db):
    notes = [_Note("Learning python and shipping projects.")]
    # seed a decision + activity + goal
    DecisionEngine(db).record("learn rust", category="learning", confidence=0.7)
    db.conn.execute("INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
                    (_iso(1), "study", "x", "learning"))
    db.conn.execute(
        "INSERT INTO goals (id,title,domain,status,progress,created_at,target_date) VALUES (?,?,?,?,?,?,?)",
        ("g1", "Learn Rust", "learning", "active", 0.2, _iso(5), _iso(-20)))
    db.conn.commit()
    twin = DigitalTwinEngine(notes, db)
    snap = twin.snapshot()
    assert "habits" in snap and "decisions" in snap and "personality" in snap
    ans = twin.ask("what am I focused on?")
    assert "answer" in ans


def test_future_self_alignment(db):
    db.conn.execute(
        "INSERT INTO goals (id,title,domain,status,progress,created_at,target_date) VALUES (?,?,?,?,?,?,?)",
        ("g1", "Grow career", "career", "active", 0.3, _iso(5), _iso(-30)))
    db.conn.commit()
    fsa = FutureSelfAgent(db, priorities=["career"])
    r = fsa.validate("Accept senior engineer role", category="career", reason="career growth")
    assert r["verdict"] == "aligned"
    assert r["supports"]
    c = fsa.validate("Quit my career plans", category="career", reason="quit")
    assert c["verdict"] == "conflict"


# --- GitHub Sensor ------------------------------------------------------
def test_github_service_offline_no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    svc = GitHubService()
    assert svc.authenticated is False
    assert svc.fetch_events("owner/repo") == []


def test_github_sensor_webhook_push(db):
    es = EventStore(db)
    sensor = GitHubSensor(es, service=GitHubService())
    payload = {
        "repository": {"full_name": "me/proj"},
        "sender": {"login": "me"},
        "head_commit": {"message": "fix bug", "url": "http://x", "author": {"name": "me"}},
        "commits": [{"message": "fix bug"}],
    }
    ev = sensor.ingest_webhook("push", payload)
    assert ev.type == NEW_COMMIT and ev.repo == "me/proj"
    recent = es.recent(NEW_COMMIT)
    assert len(recent) == 1 and recent[0]["payload"]["title"] == "fix bug"


def test_github_sensor_ci_failure(db):
    es = EventStore(db)
    sensor = GitHubSensor(es)
    payload = {
        "repository": {"full_name": "me/proj"}, "sender": {"login": "me"},
        "workflow_run": {"conclusion": "failure", "name": "CI", "html_url": "http://x"},
    }
    ev = sensor.ingest_webhook("workflow_run", payload)
    assert ev.type == CI_FAILURE
    # success should not emit
    payload["workflow_run"]["conclusion"] = "success"
    assert sensor.ingest_webhook("workflow_run", payload) is None


def test_github_sensor_api_feed(db):
    es = EventStore(db)
    sensor = GitHubSensor(es)
    raw = [
        {"type": "IssuesEvent", "repo": {"name": "me/proj"}, "actor": {"login": "me"},
         "payload": {"action": "opened", "issue": {"title": "Bug report"}}},
        {"type": "WatchEvent", "repo": {"name": "me/proj"}, "actor": {"login": "x"},
         "payload": {}},  # ignored
    ]
    out = sensor.ingest_raw(raw)
    assert len(out) == 1 and out[0].type == NEW_ISSUE
    assert len(es.recent(NEW_ISSUE)) == 1


def test_github_sensor_publishes_to_subscribers(db):
    es = EventStore(db)
    sensor = GitHubSensor(es)
    got = []
    es.subscribe(NEW_ISSUE, lambda ev: got.append(ev))
    sensor.ingest_webhook("issues", {
        "repository": {"full_name": "me/proj"}, "sender": {"login": "me"},
        "action": "opened", "issue": {"title": "t", "html_url": "http://x", "number": 1},
    })
    assert len(got) == 1 and got[0]["payload"]["title"] == "t"
