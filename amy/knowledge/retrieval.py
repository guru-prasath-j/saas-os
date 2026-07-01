"""Unified hybrid retrieval: keyword (token overlap) + embedding (cosine), fused.

Used by the conversational agents (over in-memory notes) so every agent retrieves
the same way and can ABSTAIN when nothing is relevant — which stops irrelevant
domains from hallucinating answers.

The embedder here is cheap/local (hashing) by default so per-query scoring over a
domain's notes costs nothing; the precomputed knowledge vector store uses the real
(NVIDIA/OpenAI) embeddings.
"""
from __future__ import annotations

import re
from collections import Counter

from .embeddings import cosine

_W = re.compile(r"[a-z0-9]+")
_STOP = set("the a an and or but if then of to in on for with at by from is are was "
            "were be this that it my your our notes note about how what who when where "
            "i you we they do does did have has had will can could would should a an".split())


def _tok(s: str) -> list[str]:
    return [t for t in _W.findall((s or "").lower()) if t not in _STOP and len(t) > 1]


def hybrid_search(query: str, notes, embedder=None, k: int = 5) -> list[dict]:
    """Return ranked [{note, kw_overlap, emb, score}] (best first)."""
    qtok = Counter(_tok(query))
    qvec = embedder.embed(query) if embedder is not None else None
    out = []
    for n in notes:
        text = (n.title or "") + " " + (n.body or "")
        ntok = Counter(_tok(text))
        kw_overlap = sum(1 for w in qtok if ntok.get(w, 0) > 0)
        kw_raw = sum(qtok[w] * ntok.get(w, 0) for w in qtok)
        emb = cosine(qvec, embedder.embed(text[:1500])) if qvec is not None else 0.0
        score = emb + 0.15 * kw_overlap + 0.01 * kw_raw
        out.append({"note": n, "kw_overlap": kw_overlap, "emb": round(emb, 4),
                    "score": round(score, 4)})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:k]


def is_relevant(ranked: list[dict], emb_threshold: float | None = None) -> bool:
    """A result is relevant if the top hit shares a keyword OR clears the embedding
    threshold. Used as the abstention gate."""
    if not ranked:
        return False
    from .. import config
    thr = config.ABSTAIN_EMB_THRESHOLD if emb_threshold is None else emb_threshold
    top = ranked[0]
    return top["kw_overlap"] > 0 or top["emb"] >= thr
