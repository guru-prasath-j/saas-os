"""Reliability tests — embedder factory, hybrid retrieval + abstention, eval harness.
Offline (hashing embedder; Nvidia/OpenAI constructed without network calls).

Run:  pytest tests/test_reliability.py -v
"""
from amy.vault import Note
from amy import pkos
from amy.knowledge.embeddings import (HashingEmbedder, NvidiaEmbedder, make_embedder, cosine)
from amy.knowledge.retrieval import hybrid_search, is_relevant
from amy.eval import run_eval


def _n(path, title, body):
    return Note(path=path, title=title, meta={"tags": []}, body=body)


VAULT = [
    _n("Finance/budget.md", "Budget", "# Budget\n\nmonthly budget and savings; money plan"),
    _n("Family/parents.md", "Parents", "# Parents\n\nnotes about my mom and dad and home"),
    _n("Projects/app.md", "Flutter App", "# App\n\nbuilt a flutter app with python backend"),
]


# --- embedder factory -------------------------------------------------------
def test_factory_hashing_and_auto_fallback():
    assert make_embedder("hashing").name == "hashing"
    e = make_embedder("auto")          # no keys/ST in CI -> hashing
    assert e.embed("hello world")      # produces a vector
    assert cosine(e.embed("a b c"), e.embed("a b c")) > 0.99


def test_nvidia_embedder_constructs_offline():
    e = NvidiaEmbedder("nvapi-fake-key")     # no network on construct
    assert e.dim == 1024
    assert hasattr(e, "embed_query")
    assert "nvidia" in str(e._c.base_url)


# --- hybrid retrieval + abstention gate -------------------------------------
def test_hybrid_relevant_vs_irrelevant():
    fin = [VAULT[0]]
    assert is_relevant(hybrid_search("budget savings", fin, HashingEmbedder()))
    # totally unrelated query -> not relevant -> agent would abstain
    assert not is_relevant(hybrid_search("quantum chromodynamics lattice", fin, HashingEmbedder()))


def test_specialist_abstains_general_answers():
    master, registry, dm = pkos.build_pkos(VAULT, llm=None)
    res = master.handle("quantum chromodynamics lattice gauge theory")
    # no specialist matches -> general handles it, not a fan-out of guesses
    assert res["domains"] == ["general"]


def test_only_relevant_domain_answers():
    master, registry, dm = pkos.build_pkos(VAULT, llm=None)
    res = master.handle("how is my budget")
    assert "finance" in res["domains"]
    assert "family" not in res["domains"]      # family abstains (irrelevant)


# --- eval harness -----------------------------------------------------------
def test_eval_hit_rate():
    cases = [
        {"query": "monthly budget and savings", "expect": "Finance/"},
        {"query": "my mom and dad", "expect": "Family/"},
        {"query": "flutter app python", "expect": "Projects/"},
    ]
    report = run_eval(VAULT, cases, embedder=HashingEmbedder())
    assert report["n"] == 3
    assert report["hit_rate"] >= 0.66          # retrieval finds the right notes
