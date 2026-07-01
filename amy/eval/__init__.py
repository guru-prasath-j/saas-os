"""Evaluation harness — measure retrieval quality so reliability is provable.

    from amy.eval import run_eval
    run_eval(notes, [{"query": "...", "expect": "Finance/"}])

Returns a hit-rate (did the expected source appear in top-k) + per-case detail.
Use it to catch regressions when changing embeddings/retrieval.
"""
from .harness import run_eval
