"""JD Match Advisor — paste a job description, get a grounded match report
against the user's saved resume.

Scope note (adapted from the original brief): the original spec assumed a
resume-VERSIONING system ("Phase D3 Resume Tailoring Engine" — multiple
resume_entries, per-entry relevance scoring, reorder proposals) that does
not exist anywhere in this codebase. This repo's actual resume model is
ONE field, `career_profile.resume_text` (amy/automation/store.py) — no
versions, no entries. This module scores against that single field
honestly, and deliberately DOES NOT include an `entry_scores` list or a
"which version scores best" reorder proposal (item 4 of the original
brief) — there is nothing to choose between or reorder. If per-version
resume tailoring is ever built, this is the natural extension point.

Everything here traces to literal text: extracted keywords come from
`amy.automation.orchestrator._extract_keywords` (the SAME deterministic
extractor career_apply.py's ATS estimate and portfolio classifier already
use — one extraction method, not a second one), and the coverage
arithmetic is `orchestrator.score_keyword_coverage` — the SAME function
`_ats_estimate` now delegates to, so posting-level and JD-level scoring
can never silently diverge. No LLM anywhere in this module: matching is
deterministic, and confidence is a length/keyword-count heuristic, not a
model's self-reported opinion.
"""
from __future__ import annotations

import datetime as _dt
import re

# Small, curated, deterministic synonym pairs — NOT LLM-guessed. Conservative
# on purpose: a false synonym match would hide a real ATS literal-keyword
# gap from the user, which is the one thing this check exists to catch.
#
# SINGLE TOKENS ONLY on both sides: orchestrator._extract_keywords' regex
# (`[A-Za-z][A-Za-z0-9+.#]{2,}`) never emits a space — "Machine Learning"
# in a JD extracts as two separate tokens ("Machine", "Learning"), never
# one "machine learning" phrase — so a multi-word canonical here could
# never match a real extracted `term` and would be silent dead weight.
_SYNONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("kubernetes", "k8s"),
    ("javascript", "js"),
    ("typescript", "ts"),
    ("postgresql", "postgres"),
    ("golang", "go"),
    ("nodejs", "node"),
    ("mongodb", "mongo"),
    ("kotlin", "kt"),
)


def _synonym_of(term: str) -> str | None:
    low = term.lower().strip()
    for a, b in _SYNONYM_PAIRS:
        if low == a:
            return b
        if low == b:
            return a
    return None


def _has_whole_word(text_l: str, word: str) -> bool:
    """Word-boundary check, not a bare substring — short synonyms like
    'go' or 'js' would otherwise false-positive inside 'algorithm' or
    'jsdom'. text_l is expected already-lowercased; word is compared
    case-insensitively via re.IGNORECASE regardless."""
    return re.search(rf"\b{re.escape(word)}\b", text_l, re.IGNORECASE) is not None


_LOW_SIGNAL_WORD_COUNT = 40    # a JD shorter than this is honestly "vague"
_LOW_SIGNAL_KEYWORD_COUNT = 4  # or one that yields too few real terms


def _confidence(jd_text: str, keywords: list[str]) -> str:
    words = len(re.findall(r"\S+", jd_text))
    if words < _LOW_SIGNAL_WORD_COUNT or len(keywords) < _LOW_SIGNAL_KEYWORD_COUNT:
        return "low"
    return "high"


def _stated_in_jd_as(jd_text: str, term: str, window: int = 60) -> str:
    """Real surrounding text from the JD around the term's first mention —
    never fabricated. Falls back to the bare term if, oddly, the extractor
    surfaced a term that no longer substring-matches (shouldn't happen;
    honest fallback rather than a crash if it ever does)."""
    m = re.search(re.escape(term), jd_text, re.IGNORECASE)
    if not m:
        return term
    start = max(0, m.start() - window)
    end = min(len(jd_text), m.end() + window)
    snippet = jd_text[start:end].strip()
    snippet = re.sub(r"\s+", " ", snippet)
    return (("…" if start > 0 else "") + snippet
            + ("…" if end < len(jd_text) else ""))


def _literal_term_gaps(resume_text: str, jd_text: str,
                       missing: list[str]) -> tuple[list[dict], list[str]]:
    """Partition `missing` (keywords with no literal substring match) into
    (literal_term_gaps, still_missing). A term moves to literal_term_gaps
    ONLY when a known synonym of it is literally present in the resume —
    i.e. the underlying skill plausibly IS there, just under a different
    string, which is a real ATS gap (many real ATS systems literal-string
    match) distinct from the skill being genuinely absent. A term with no
    synonym evidence stays in still_missing — it is never guessed into a
    "you probably have this" bucket."""
    resume_l = (resume_text or "").lower()
    gaps: list[dict] = []
    still_missing: list[str] = []
    for term in missing:
        syn = _synonym_of(term)
        if syn and _has_whole_word(resume_l, syn):
            gaps.append({
                "jd_term": term,
                "resume_has_synonym": syn,
                "suggestion": (f'add the literal term "{term}" somewhere if '
                              f'accurate — ATS keyword matching is often '
                              f'literal-string-based, and your resume only '
                              f'has "{syn}"'),
            })
        else:
            still_missing.append(term)
    return gaps, still_missing


def analyze_jd(ctx, jd_text: str, job_posting_id: str | None = None) -> dict:
    """The main entry point (registered as the analyze_jd tool, risk=read).

    job_posting_id is OPTIONAL and only ever read/linked, never required —
    a JD pasted from a forwarded email or a board this system never
    scouted must work standalone. When it IS linked and that posting's
    stored `keywords` is empty/thin, this backfills it from the JD's
    extraction (opt-in, explicit link only — never for a standalone JD,
    so a one-off/unverified paste can't quietly pollute the aggregate
    keyword data other career features read)."""
    from .automation.orchestrator import _extract_keywords, score_keyword_coverage

    jd_text = (jd_text or "").strip()
    if not jd_text:
        return {"error": "jd_text is empty"}

    keywords = _extract_keywords([{"title": "", "description": jd_text}], top_n=25)
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    resume_text = profile.get("resume_text") or ""

    coverage = score_keyword_coverage(resume_text, keywords)
    matched = coverage["matched"]
    raw_missing = coverage["missing"]
    literal_term_gaps, still_missing = _literal_term_gaps(
        resume_text, jd_text, raw_missing)
    missing_requirements = [
        {"term": t, "stated_in_jd_as": _stated_in_jd_as(jd_text, t)}
        for t in still_missing]

    confidence = _confidence(jd_text, keywords)
    overall_match_score = (round(coverage["coverage_pct"])
                           if coverage["coverage_pct"] is not None else None)

    posting = None
    backfilled_posting_keywords = False
    if job_posting_id:
        posting = ctx.store.get_posting(ctx.user_id, job_posting_id)
        if posting is not None and len(posting.get("keywords") or []) < 3 and keywords:
            try:
                ctx.store.set_posting_keywords(ctx.user_id, job_posting_id, keywords)
                backfilled_posting_keywords = True
            except Exception:
                pass   # backfill is a bonus, never blocks the analysis itself

    analysis_id = ctx.store.create_jd_analysis(
        ctx.user_id, raw_jd_text=jd_text, job_posting_id=job_posting_id,
        extracted_keywords=keywords, overall_match_score=overall_match_score,
        matched_requirements=matched, missing_requirements=missing_requirements,
        literal_term_gaps=literal_term_gaps, confidence=confidence)

    try:
        from .events.store import CAREER_JD_ANALYZED
        ctx.events().emit(
            CAREER_JD_ANALYZED,
            {"analysis_id": analysis_id, "job_posting_id": job_posting_id,
             "overall_match_score": overall_match_score, "confidence": confidence,
             "reasoning": (f"Analyzed a pasted JD: {len(matched)}/{len(keywords)} "
                          f"extracted terms matched literally in the saved resume "
                          f"({confidence} confidence).")},
            source="jd_match")
    except Exception:
        pass

    return {"analysis_id": analysis_id, "overall_match_score": overall_match_score,
           "matched_requirements": matched, "missing_requirements": missing_requirements,
           "literal_term_gaps": literal_term_gaps, "confidence": confidence,
           "note": coverage.get("note") or "",
           "job_posting_id": job_posting_id,
           "backfilled_posting_keywords": backfilled_posting_keywords}


def explain_jd_match(ctx, analysis_id: str) -> dict:
    """Read lookup for the `explain_jd_match` assistant tool — a plain-
    language summary of an already-computed analysis, never re-scores."""
    a = ctx.store.get_jd_analysis(ctx.user_id, analysis_id)
    if a is None:
        return {"error": f"no JD analysis {analysis_id!r} on file"}
    score = a.get("overall_match_score")
    lines = [
        f"Match score: {score if score is not None else 'N/A'}/100 "
        f"({a.get('confidence')} confidence).",
        f"Matched ({len(a.get('matched_requirements') or [])}): "
        + ", ".join(a.get("matched_requirements") or []) or "none",
    ]
    missing = a.get("missing_requirements") or []
    if missing:
        lines.append(f"Missing ({len(missing)}): "
                     + ", ".join(m.get("term", "") for m in missing))
    gaps = a.get("literal_term_gaps") or []
    if gaps:
        lines.append(f"Literal ATS gaps ({len(gaps)}): " + ", ".join(
            f'{g["jd_term"]} (resume says "{g["resume_has_synonym"]}")'
            for g in gaps))
    return {"analysis_id": analysis_id, "summary": "\n".join(lines), **a}
