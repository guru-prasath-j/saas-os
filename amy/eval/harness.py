"""Retrieval evaluation harness.

Each case: {"query": str, "expect": <substring of the correct note path>}.
A case "hits" if a note whose path contains `expect` is in the top-k retrieved.
"""
from __future__ import annotations

from ..knowledge.retrieval import hybrid_search
from ..knowledge.embeddings import HashingEmbedder


def run_eval(notes, cases, embedder=None, k: int = 5) -> dict:
    emb = embedder or HashingEmbedder()
    results = []
    hits = 0
    for c in cases:
        ranked = hybrid_search(c["query"], notes, embedder=emb, k=k)
        paths = [r["note"].path for r in ranked]
        hit = any(c["expect"] in p for p in paths)
        hits += 1 if hit else 0
        results.append({"query": c["query"], "expect": c["expect"], "hit": hit, "top": paths})
    return {
        "hit_rate": round(hits / len(cases), 3) if cases else 0.0,
        "hits": hits, "n": len(cases),
        "embedder": getattr(emb, "name", "?"),
        "results": results,
    }


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    from ..vault import load_notes
    from ..knowledge.embeddings import make_embedder

    vault = sys.argv[1] if len(sys.argv) > 1 else "."
    cases_file = sys.argv[2] if len(sys.argv) > 2 else "eval_cases.json"
    notes = load_notes(vault)
    cases = json.load(open(cases_file, encoding="utf-8"))
    report = run_eval(notes, cases, embedder=make_embedder())
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    for r in report["results"]:
        print(("PASS" if r["hit"] else "FAIL"), "-", r["query"])
