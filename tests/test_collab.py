"""Collaboration layer tests — multi-agent + planner, memory, planner goals,
agent cards, reflection, learning trends. Offline (llm=None).

Run:  pytest tests/test_collab.py -v
"""
import datetime as dt
import os
import tempfile

from amy.vault import Note
from amy.collab import CollabMaster


def _note(path, title, body, tags=None):
    return Note(path=path, title=title, meta={"tags": tags or []}, body=body)


VAULT = [
    _note("Finance/budget.md", "Budget", "# Budget\n\nmonthly budget, savings, money; can I afford a trip"),
    _note("Career/switch.md", "Career Switch", "# Career Switch\n\nswitching careers, a new job, work skills"),
]


def _cm():
    d = tempfile.mkdtemp(prefix="amy_collab_")
    return CollabMaster(VAULT, os.path.join(d, "collab.db"), llm=None)


def test_multi_agent_with_planner():
    cm = _cm()
    res = cm.handle("Can I afford a Europe trip while switching careers?")
    assert "finance" in res["domains"]
    assert "career" in res["domains"]
    assert "planner" in res["domains"]              # planner joined the collaboration
    assert any(s["domain"] == "planner" for s in res["sections"])
    cm.close()


def test_memory_manager():
    cm = _cm()
    m = cm.memory
    m.set_pref("tone", "concise")
    assert m.get_pref("tone") == "concise"
    m.add_summary("did stuff")
    assert m.recent_summaries(1)[0]["text"] == "did stuff"
    cm.handle("budget question")
    snap = m.snapshot()
    assert snap["preferences"]["tone"] == "concise"
    assert snap["recent_activities"]
    cm.close()


def test_frequent_notes_tracked():
    cm = _cm()
    cm.handle("how is my budget and savings")
    assert any("Finance/" in f["path"] for f in cm.memory.frequent_notes())
    cm.close()


def test_planner_goals_milestones_progress():
    cm = _cm()
    p = cm.planner
    g = p.create_goal("Save for Europe", "finance")
    m1 = p.add_milestone(g, "Save $2000")
    p.add_milestone(g, "Book flights")
    p.complete_milestone(m1)
    assert p.get_plan(g)["progress"] == 50.0
    for ms in p.get_plan(g)["milestones"]:
        p.complete_milestone(ms["id"])
    assert p.get_plan(g)["progress"] == 100.0
    assert p.get_plan(g)["status"] == "done"
    cm.close()


def test_agent_cards():
    cm = _cm()
    cm.handle("budget savings money")
    card = cm.cards.get("finance_agent")
    assert card is not None
    assert card["topics"]                # known topics
    assert card["faqs"]                  # frequently asked questions recorded
    cm.close()


def test_reflection_summary():
    cm = _cm()
    cm.handle("budget question")
    cm.planner.create_goal("Learn Flutter", "learning")
    r = cm.reflection.weekly_summary()
    assert set(("progress", "gaps", "suggestions")) <= set(r)
    assert isinstance(r["progress"], list)
    cm.close()


class _RecLLM:
    def __init__(self):
        self.contexts = []

    def generate(self, system, prompt, context="", sensitive=False):
        self.contexts.append(context)
        return ("ok", "stub")


def test_conversation_memory_used_as_context():
    llm = _RecLLM()
    cm = CollabMaster(VAULT, os.path.join(tempfile.mkdtemp(prefix="amy_collab_"), "collab.db"), llm=llm)
    cm.handle("my budget is tight")          # turn 1
    cm.handle("what about my savings")       # turn 2 must include turn 1 as context
    joined = " ".join(llm.contexts)
    assert "Conversation so far" in joined
    assert "my budget is tight" in joined
    cm.close()


def test_preferences_flow_into_context():
    llm = _RecLLM()
    cm = CollabMaster(VAULT, os.path.join(tempfile.mkdtemp(prefix="amy_collab_"), "collab.db"), llm=llm)
    cm.memory.set_pref("tone", "concise")
    cm.handle("how is my budget")
    assert any("tone=concise" in c for c in llm.contexts)
    cm.close()


def test_learning_trends():
    cm = _cm()
    for _ in range(5):
        cm.memory.log_activity("topic", "finance", domain="finance")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
    for _ in range(5):
        cm.db.conn.execute("INSERT INTO activities (ts,kind,detail,domain) VALUES (?,?,?,?)",
                           (old, "topic", "career", "career"))
    cm.db.conn.commit()
    trends = cm.learning.trends(window_days=7)
    assert trends["finance"]["trend"] == "increasing"
    assert trends["career"]["trend"] == "decreasing"
    assert cm.learning.recommendations()
    cm.close()
