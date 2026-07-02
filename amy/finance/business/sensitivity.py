"""GSTIN/PAN detection — extends the existing sensitive-data routing rule
(LLMRouter.pick(sensitive=True) forces Ollama-only, see amy/llm.py) to
business-compliance data. Pure function, no I/O — every existing LLM call
site elsewhere in the codebase is unaffected; only amy/finance/business/
compliance.py calls this before generate().
"""
from __future__ import annotations

import re

GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]{1}[A-Z\d]{1}Z[A-Z\d]{1}\b")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]{1}\b")


def is_sensitive(*fields: str | None) -> bool:
    """True if any field contains a GSTIN or PAN token."""
    for f in fields:
        if not f:
            continue
        text = str(f).upper()
        if GSTIN_RE.search(text) or PAN_RE.search(text):
            return True
    return False
