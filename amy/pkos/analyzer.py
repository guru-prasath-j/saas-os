"""Per-note analysis: title, headings, tags, summary.

Summary is a fast, free heuristic (first real paragraph) by default so analyzing a
whole vault is cheap and deterministic. Pass an LLM to `summarize` for a richer
summary when you want to spend tokens.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$", re.MULTILINE)
_MD_NOISE = re.compile(r"[#*_>`~]|\[\[|\]\]|\[|\]\(.*?\)|!\[.*?\]")


def extract_headings(body: str) -> list[str]:
    return [m.group(2).strip() for m in _HEADING.finditer(body or "")]


def _clean(text: str) -> str:
    text = _MD_NOISE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def summarize(note, max_len: int = 240, llm=None) -> str:
    """First meaningful paragraph (heuristic), or an LLM summary if `llm` given."""
    body = note.body or ""
    if llm is not None and body.strip():
        try:
            text, _ = llm.generate(
                "Summarize this note in one sentence. Be factual; no preamble.",
                "Summarize:", body[:4000])
            s = _clean(text)
            if s:
                return s[:max_len]
        except Exception:
            pass
    # heuristic: first non-heading, non-empty paragraph
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if not p or p.lstrip().startswith("#"):
            continue
        clean = _clean(p)
        if clean:
            return (clean[: max_len - 1] + "…") if len(clean) > max_len else clean
    return note.title or ""


def analyze(note, llm=None) -> dict:
    return {
        "path": note.path,
        "title": note.title,
        "tags": list(note.tags or []),
        "headings": extract_headings(note.body),
        "summary": summarize(note, llm=llm),
    }


def analyze_vault(notes, llm=None) -> list[dict]:
    return [analyze(n, llm=llm) for n in notes]
