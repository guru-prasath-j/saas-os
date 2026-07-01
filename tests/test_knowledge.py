"""Knowledge layer tests — metadata, embeddings/search, relationships, confidence.

Offline: uses the deterministic HashingEmbedder (no API key needed).

Run:  pytest tests/test_knowledge.py -v
"""
import os
import tempfile

from amy.vault import Note
from amy.knowledge import KnowledgeBase, HashingEmbedder, cosine


def _note(path, title, body, tags=None):
    return Note(path=path, title=title, meta={"tags": tags or []}, body=body)


VAULT = [
    _note("Finance/budget.md", "Budget",
          "# Budget\n\nMonthly budget and savings. I invest in index funds. Linked to [[Europe Trip]]."),
    _note("Travel/europe.md", "Europe Trip",
          "# Europe Trip\n\nPlanning a trip to Europe. Needs money from my [[Budget]] and savings."),
    _note("Work/career.md", "Career Goal",
          "# Career Goal\n\nGet promoted at work; improve skills and take on a bigger project."),
]


def _kb():
    d = tempfile.mkdtemp(prefix="amy_kb_test_")
    kb = KnowledgeBase(d, embedder=HashingEmbedder())
    kb.build(VAULT)
    return kb


def test_build_populates_three_dbs():
    kb = _kb()
    stats = kb.build(VAULT)  # rebuild
    assert stats["notes"] == 3
    assert stats["chunks"] >= 3
    assert kb.agents()  # agent_registry.db populated
    kb.close()


def test_metadata_fields_present():
    kb = _kb()
    metas = kb.metadata.all()
    m = next(x for x in metas if x["path"] == "Finance/budget.md")
    for field in ("id", "title", "summary", "domain", "subdomains", "entities",
                  "keywords", "tags", "importance", "created_at", "updated_at", "embedding_id"):
        assert field in m
    assert m["domain"] == "finance"
    assert "Europe Trip" in m["entities"]      # wikilink captured as entity
    assert m["importance"] >= 0
    kb.close()


def test_semantic_search_returns_context_and_confidence():
    kb = _kb()
    res = kb.search_engine.search("how much should I budget and save")
    assert res["context"]
    assert res["sources"]
    assert 0 <= res["confidence"] <= 100
    # the budget note should be among the sources
    assert any("budget" in s.lower() for s in res["sources"])
    kb.close()


def test_metadata_filter_by_domain():
    kb = _kb()
    finance_paths = {m["path"] for m in kb.metadata.filter(domain="finance")}
    res = kb.search_engine.search("savings", domain="finance")
    assert res["chunks"]
    # filter must only return notes that belong to the finance domain
    assert all(c["path"] in finance_paths for c in res["chunks"])
    kb.close()


def test_relationships_from_wikilinks():
    kb = _kb()
    edges = kb.relationships.graph()
    types = {e["rel_type"] for e in edges}
    assert "references" in types          # Budget <-> Europe Trip wikilinks
    kb.close()


def test_manual_depends_on_relationship():
    kb = _kb()
    from amy.knowledge import note_id
    a = note_id("Travel/europe.md")
    b = note_id("Finance/budget.md")
    kb.relationships.add(a, b, "depends_on", 1.0)
    nbrs = kb.relationships.neighbors(a)
    assert any(n["rel_type"] == "depends_on" for n in nbrs)
    kb.close()


def test_ask_includes_confidence_and_sources():
    kb = _kb()
    res = kb.ask("what's my budget plan?")
    assert "answer" in res
    assert "confidence" in res and 0 <= res["confidence"] <= 100
    assert isinstance(res["sources"], list)
    kb.close()


def test_hashing_embedder_is_deterministic():
    e = HashingEmbedder()
    assert e.embed("hello world") == e.embed("hello world")
    assert cosine(e.embed("budget savings money"), e.embed("budget savings money")) > 0.99
