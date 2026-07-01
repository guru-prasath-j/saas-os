"""Product surface tests — profile, portfolio (safe), marketplace, dashboard,
suggestions, graph viz. Offline.

Run:  pytest tests/test_product.py -v
"""
import os
import tempfile

from amy.vault import Note
from amy.product import (build_profile, build_portfolio, build_dashboard,
                         Marketplace, build_suggestions, to_graph)
from amy.collab import (CollabDB, CollabMaster, PlannerAgent, ReflectionAgent,
                        LearningAgent, MemoryManager)


def _n(path, title, body, tags=None):
    return Note(path=path, title=title, meta={"tags": tags or []}, body=body)


VAULT = [
    _n("Projects/app.md", "Cool App", "# Cool App\n\nbuilt a flutter app with a python backend"),
    _n("Learning/python.md", "Python", "# Python\n\nlearning python and ai, course notes"),
    _n("Finance/budget.md", "Budget", "# Budget\n\nmoney savings budget", tags=["sensitive"]),
]


def _db():
    return CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_prod_"), "collab.db"))


def test_profile_builder():
    db = _db()
    prof = build_profile(VAULT, collab_db=db)
    assert prof["skills"]
    assert isinstance(prof["projects"], list)
    assert "note_count" in prof
    db.close()


def test_portfolio_excludes_sensitive_and_finance():
    port = build_portfolio(VAULT)
    assert "Budget" not in [p["title"] for p in port["projects"]]
    assert "finance" not in port["interests"]
    assert "finance" in port["blocked"]
    assert port["mode"] == "public_portfolio"


def test_marketplace_enable_disable():
    db = _db()
    mk = Marketplace(db)
    assert mk.is_enabled("finance_agent") is True
    mk.disable("finance_agent")
    assert mk.is_enabled("finance_agent") is False
    assert "finance_agent" in mk.disabled_set()
    mk.enable("finance_agent")
    assert mk.is_enabled("finance_agent") is True
    db.close()


def test_marketplace_filters_collab_routing():
    cm = CollabMaster(VAULT, os.path.join(tempfile.mkdtemp(prefix="amy_prod_"), "collab.db"), llm=None)
    cm.marketplace.disable("finance_agent")
    res = cm.handle("how is my budget and money")   # finance matches but is disabled
    assert "finance" not in res["domains"]
    cm.close()


def test_dashboard_assembles():
    db = _db()
    dash = build_dashboard(VAULT, db, knowledge=None)
    assert dash["agent_count"] >= 1
    assert dash["managed_notes"] == len(VAULT)
    assert "memory_count" in dash and "relationships" in dash
    db.close()


def test_suggestions_fuses_sources():
    db = _db()
    mem = MemoryManager(db); pl = PlannerAgent(db)
    refl = ReflectionAgent(db, pl, mem); learn = LearningAgent(db, mem)
    pl.create_goal("Ship app", "projects")          # stalled goal -> a suggestion
    s = build_suggestions(learn, refl, pl)
    assert s["count"] >= 1
    assert any("Ship app" in x["text"] for x in s["suggestions"])
    db.close()


def test_portfolio_is_folder_aware():
    V = [
        _n("01_Profile/Projects/app.md", "Cool App", "a flutter app"),
        _n("01_Profile/Skills/Mobile.md", "Skills — Mobile", "flutter"),
        _n("06_Job_Search/Interview Prep/Flutter.md", "Interview Prep — Flutter", "interview project resume"),
        _n("04_Career/Certifications/Python.md", "Python Advanced", "certification course"),
        _n("03_Finances/Budget.md", "Budget", "money budget", tags=["sensitive"]),
    ]
    p = build_portfolio(V)
    titles = [x["title"] for x in p["projects"]]
    assert "Cool App" in titles
    assert "Interview Prep — Flutter" not in titles   # job-search admin excluded
    assert "Budget" not in titles                      # sensitive excluded
    assert "Skills — Mobile" in p["skills"]            # real skill note, not a token bag
    assert "Python Advanced" in p["roadmap"]           # certification -> roadmap


def test_graphviz_shape():
    metas = [{"id": "a", "title": "A", "domain": "finance", "importance": 10},
             {"id": "b", "title": "B", "domain": "career", "importance": 5}]
    edges = [{"src": "a", "dst": "b", "rel_type": "related_to", "weight": 0.5}]
    g = to_graph(metas, edges)
    assert len(g["nodes"]) == 2 and len(g["edges"]) == 1
    assert g["nodes"][0]["label"] == "A"
    assert g["edges"][0]["source"] == "a" and g["edges"][0]["target"] == "b"
