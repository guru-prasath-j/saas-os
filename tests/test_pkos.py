"""PKOS tests (analyzer, domain detection, multi-intent router, master merge).

All offline (no LLM / no API key): agents use the heuristic/template path.

Run:  pytest tests/test_pkos.py -v
"""
from amy.vault import Note
from amy import pkos
from amy.pkos import analyzer, domains, router as routermod


def _note(path, title, body, tags=None):
    return Note(path=path, title=title, meta={"tags": tags or []}, body=body)


VAULT = [
    _note("Money/budget.md", "Budget",
          "# Budget\n\nMy monthly budget and savings plan. I invest in index funds.\n## Expenses\nrent, food"),
    _note("People/parents.md", "Parents",
          "# Parents\n\nNotes about my mom and dad and family visits home."),
    _note("Work/project.md", "Project X",
          "# Project X\n\nWork project with a client deadline and a team meeting."),
    _note("Fitness/run.md", "Running",
          "# Running\n\nGym and running plan to lose weight; track calories and sleep."),
    _note("Misc/random.md", "Random", "just some unrelated jottings"),
]


# --- analyzer ---------------------------------------------------------------
def test_extract_headings():
    h = analyzer.extract_headings(VAULT[0].body)
    assert "Budget" in h and "Expenses" in h


def test_summary_is_first_paragraph():
    s = analyzer.summarize(VAULT[1])
    assert "mom and dad" in s.lower()
    assert not s.startswith("#")


def test_analyze_vault_shape():
    out = analyzer.analyze_vault(VAULT)
    assert len(out) == len(VAULT)
    assert set(out[0]) == {"path", "title", "tags", "headings", "summary"}


# --- domain detection -------------------------------------------------------
def test_content_based_domain_detection():
    dm = domains.detect(VAULT)
    assert "Money/budget.md" in dm["finance"]      # money -> finance
    assert "People/parents.md" in dm["family"]     # parents -> family
    assert "Work/project.md" in dm["career"]       # work -> career
    assert "Fitness/run.md" in dm["health"]        # gym -> health


def test_unmatched_note_falls_back_to_folder_domain():
    dm = domains.detect(VAULT)
    # 'Misc/random.md' matches no keyword -> folder domain 'misc'
    assert any("Misc/random.md" in paths for d, paths in dm.items() if d == "misc")


# --- router (multi-intent) --------------------------------------------------
def test_router_multi_intent():
    dm = domains.detect(VAULT)
    r = routermod.IntentRouter(list(dm.keys()))
    hits = r.route("how is my money and how are my parents")
    assert "finance" in hits and "family" in hits


def test_router_single_intent():
    dm = domains.detect(VAULT)
    r = routermod.IntentRouter(list(dm.keys()))
    assert r.route("show my gym progress") == ["health"]


# --- master (merge + sources) ----------------------------------------------
def test_master_merges_and_attributes_sources():
    master, registry, dm = pkos.build_pkos(VAULT, llm=None)
    res = master.handle("summarize my money and my parents")
    assert set(res["domains"]) >= {"finance", "family"}
    # combined, de-duplicated source attribution from BOTH agents
    assert "Money/budget.md" in res["sources"]
    assert "People/parents.md" in res["sources"]
    assert len(res["sources"]) == len(set(res["sources"]))
    assert len(res["sections"]) == len(res["domains"])


def test_router_fallback_is_single_general():
    dm = domains.detect(VAULT)
    r = routermod.IntentRouter(list(dm.keys()) + ["general"])
    # a query that matches no domain keyword -> exactly one 'general', never all
    assert r.route("hi say my name please") == ["general"]


def test_master_unmatched_uses_general_only():
    master, registry, dm = pkos.build_pkos(VAULT, llm=None)
    res = master.handle("hi say my name please")
    assert res["domains"] == ["general"]
