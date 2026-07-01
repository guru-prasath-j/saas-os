"""Confidence scoring for a retrieval result.

Combines the best similarity, the average of the top similarities, and how many
distinct sources backed the answer, into a 0-100 percentage. Heuristic and
transparent — not a calibrated probability.
"""
from __future__ import annotations


def score(similarities: list[float], n_sources: int) -> float:
    if not similarities:
        return 0.0
    top = max(similarities)
    avg = sum(similarities) / len(similarities)
    # base on similarity, nudged up by corroborating sources
    base = (0.7 * top + 0.3 * avg)
    corroboration = min(0.15, 0.05 * max(0, n_sources - 1))
    pct = (base + corroboration) * 100
    return round(max(0.0, min(100.0, pct)), 1)
