"""Resume Version Manager (CAREER AUTOPILOT Phase D, part 2) — track-
specific resume drafts layered on top of the ONE master `career_profile.
resume_text`, plus a course-completion trigger that folds newly-learned,
gap-closing skills into that master resume.

Distinct from the existing Part 5E master-resume evolution
(`amy/agents/reactive.py::_propose_resume_evolution`, reused unchanged
below for the course-completion path): that mechanism edits the SINGLE
master resume from portfolio bullets. `resume_versions` here is a
genuinely different concept — multiple labeled, TRACK-specific drafts
derived FROM the master resume + persisted portfolio classification
(`amy/career_portfolio.py`) + Phase A's skill-demand data, never a
second way to edit the master.

Every version-creation call goes through submit_action(ctx, tier=2, ...)
DIRECTLY (never tools.invoke(actor="agent")) — same reasoning as `amy/
career_portfolio.py`'s module docstring: RISK_WRITE + actor="human"
would otherwise execute immediately (quirk 15), and this phase's own
framing requires no exceptions for public-facing content. Nothing is
ever claimed that isn't grounded in real stored data: skills come only
from `career_profile.skills`, project bullets only from portfolio_items
rows already classified 'showcase', and the emphasis ORDER (not content)
is informed by Phase A's `skill_demand_report()` — a skill never on the
candidate's own profile is never inserted into a draft.
"""
from __future__ import annotations

import datetime as _dt

_HIGHLIGHTS_HEADER = "Project highlights"
_MIN_APPLICATIONS_FOR_CONFIDENCE = 3


# ---------------------------------------------------------------------------
# Resume version generation
# ---------------------------------------------------------------------------

def _emphasized_skills(ctx, target_track: str, owned: list[str]) -> list[str]:
    """Owned skills ordered by real market-demand frequency for this track
    (amy/career_scout.py::skill_demand_report, Phase A — never
    recomputed), most in-demand first. Owned skills that don't appear in
    the demand report at all are appended afterward, order preserved —
    still real skills, just no demand signal for them in this window."""
    from .career_scout import skill_demand_report

    report = skill_demand_report(ctx, target_track, propose=False)
    demand_pct = {e["skill"].strip().lower(): e["frequency_pct"]
                 for e in report["top_missing_skills"] if e["in_profile"]}
    owned_sorted = sorted(owned, key=lambda s: demand_pct.get(s.strip().lower(), -1),
                          reverse=True)
    return owned_sorted


def _showcase_bullets_for_track(ctx, target_track: str) -> list[str]:
    """Real bullets only, from portfolio_items rows already classified
    'showcase' — falls back to every showcase item on file (still real,
    just not track-narrowed) when none were classified specifically
    under this exact track string, rather than returning nothing."""
    items = ctx.store.list_portfolio_items(ctx.user_id, classification="showcase")
    matching = [i for i in items if i.get("target_role", "").strip().lower()
               == target_track.strip().lower()]
    pool = matching or items
    bullets = []
    for item in pool:
        for b in (item.get("bullets") or []):
            bullets.append(f"{item['repo_name']}: {b}")
    return bullets[:8]


def _deterministic_draft(profile: dict, target_track: str, emphasized: list[str],
                         bullets: list[str]) -> str:
    lines = [f"Resume — {target_track}", ""]
    skills_line = ", ".join(emphasized[:10]) or "none on file"
    lines.append(f"Summary: targeting {target_track} roles. Core skills: {skills_line}.")
    lines.append("")
    lines.append(f"## {_HIGHLIGHTS_HEADER}")
    if bullets:
        lines.extend(f"- {b}" for b in bullets)
    else:
        lines.append("- (no showcase repos classified for this track yet)")
    base = (profile.get("resume_text") or "").strip()
    if base:
        lines.append("")
        lines.append("## Base resume (reference)")
        lines.append(base)
    return "\n".join(lines)


_RESUME_VERSION_SYSTEM = (
    "You tailor a developer's resume draft for a specific target track. "
    "Reorganize and rephrase for clarity and impact — NEVER invent a "
    "skill, employer, metric, or project not present in the input. Every "
    "bullet must trace back to something given. Respond with EXACTLY ONE "
    'JSON object: {"draft": "<full resume text>"}'
)


def generate_resume_version(ctx, target_track: str, label: str | None = None) -> dict:
    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    if not (profile.get("resume_text") or "").strip():
        return {"skipped": "no master resume on file — set one via set_career_profile first"}

    owned = [s for s in (profile.get("skills") or []) if s.strip()]
    emphasized = _emphasized_skills(ctx, target_track, owned)
    bullets = _showcase_bullets_for_track(ctx, target_track)
    draft = _deterministic_draft(profile, target_track, emphasized, bullets)

    from .agents.reactive import _get_llm
    llm = _get_llm(ctx)
    if llm is not None:
        prompt = (f"Target track: {target_track}\n"
                 f"Owned skills (do not add others): {', '.join(emphasized) or 'none'}\n"
                 f"Real project bullets (do not add others):\n"
                 + "\n".join(f"- {b}" for b in bullets) +
                 f"\n\nCurrent master resume:\n{profile.get('resume_text', '')[:3000]}")
        try:
            import json as _json
            import re as _re
            text, provider = llm.generate(_RESUME_VERSION_SYSTEM, prompt, sensitive=True)
            if provider != "template":
                m = _re.search(r"\{.*\}", text, _re.DOTALL)
                if m:
                    parsed = _json.loads(m.group(0))
                    if str(parsed.get("draft") or "").strip():
                        draft = str(parsed["draft"])[:6000]
        except Exception:
            pass   # degrade to the deterministic draft — never block the proposal

    label = (label or f"{target_track} — {_dt.date.today().isoformat()}")[:120]
    today = _dt.date.today().isoformat()

    from .automation.executors import submit_action
    return submit_action(
        ctx, tier=2, action_type="resume_version_create",
        title=f"New resume version: {label}",
        body=f"Proposed track-specific resume draft for '{target_track}':\n\n{draft[:3500]}",
        payload={"label": label, "content": draft, "target_track": target_track,
                "source": "resume_manager"},
        source="resume_manager",
        dedup_key=f"resume_version_{target_track}_{today}",
        reasoning=f"Track-optimized resume draft for '{target_track}', built from "
                 "career_profile skills + persisted showcase portfolio bullets + "
                 "real skill-demand ordering — never inserts an unproven skill.",
        risk="write", affected_entity=f"target_track={target_track}")


# ---------------------------------------------------------------------------
# Course-completion trigger — reuses the EXISTING resume_update executor
# (Part 5E), not a new one; this is a small addition to the master resume,
# not a new version.
# ---------------------------------------------------------------------------

def propose_course_completion_bullet(ctx, item_title: str, focus_topic: str) -> dict | None:
    from .automation.executors import submit_action

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    current = (profile.get("resume_text") or "").strip()
    if not current:
        return None   # no master resume on file — nothing to evolve

    line = f"- Completed '{item_title}' ({focus_topic})"
    if line.lower() in current.lower():
        return None
    section_header = "## Continuous learning"
    if section_header in current:
        proposed = current.replace(section_header, f"{section_header}\n{line}", 1)
    else:
        proposed = f"{current}\n\n{section_header}\n{line}\n"

    month = _dt.date.today().strftime("%Y-%m")
    return submit_action(
        ctx, tier=2, action_type="resume_update",
        title=f"Resume update: add '{item_title}' to Continuous learning",
        body=f"Completed a course/item relevant to your '{focus_topic}' skill gap "
            f"(from Phase B's skill-gap roadmap) — proposing one line under "
            f"'Continuous learning'.\n\nAdded: {line}",
        payload={"resume_text": proposed},
        source="career_resume_course_completion",
        dedup_key=f"resume_course_bullet_{item_title[:60]}",
        reasoning=f"'{item_title}' closes a real, currently-tracked skill gap "
                 f"('{focus_topic}') — same resume_update executor Part 5E's "
                 "portfolio-driven evolution already uses.",
        risk="write")


def scan_course_completions(ctx) -> dict:
    """Job-driven trigger: only fires for a completed learning_feed_item
    whose focus topic case-insensitively matches a CURRENT top-skill-gap
    entry for an active track (amy/career_graph.py::top_skill_gap, Phase
    B — never recomputed) — not every completion, only gap-closing ones.
    Cursor stored in prefs, same idiom life/inference.py's _should_
    renotify/_mark_notified use."""
    from .career_graph import top_skill_gap
    from .career_scout import _active_tracks

    profile = ctx.store.get_career_profile(ctx.user_id) or {}
    tracks = _active_tracks(profile)
    if not tracks:
        return {"skipped": "no target_role on file"}

    gap_topics: set[str] = set()
    for track in tracks:
        for e in top_skill_gap(ctx, track)["missing_skills"]:
            gap_topics.add(e["skill"].strip().lower())
    if not gap_topics:
        return {"skipped": "no current skill gaps identified"}

    cursor_key = "career_resume_course_scan_last_ts"
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (cursor_key,)).fetchone()
    since = row["value"] if row else "1970-01-01T00:00:00"

    rows = ctx.collab.conn.execute(
        "SELECT title, focus_tag, completed_at FROM learning_feed_items"
        " WHERE uid=? AND completed_at IS NOT NULL AND completed_at>?"
        " ORDER BY completed_at", (ctx.user_id, since)).fetchall()

    proposed = 0
    latest_ts = since
    for r in rows:
        topic = (r["focus_tag"] or "").strip().lower()
        if topic in gap_topics:
            result = propose_course_completion_bullet(ctx, r["title"] or "an item",
                                                       r["focus_tag"] or topic)
            if result and result.get("status") == "pending":
                proposed += 1
        if r["completed_at"] and r["completed_at"] > latest_ts:
            latest_ts = r["completed_at"]

    if latest_ts != since:
        ctx.collab.conn.execute(
            "INSERT INTO prefs(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cursor_key, latest_ts))
        ctx.collab.conn.commit()

    return {"scanned": len(rows), "proposed": proposed}


# ---------------------------------------------------------------------------
# Performance tracking
# ---------------------------------------------------------------------------

def resume_performance(ctx) -> dict:
    """Groups applications by resume_version_id. A version used by fewer
    than _MIN_APPLICATIONS_FOR_CONFIDENCE applications gets an explicit
    'insufficient_data' confidence marker instead of a raw rate — never
    implies statistical confidence that isn't there. Versions with zero
    linked applications are listed with zero counts, not omitted."""
    versions = ctx.store.list_resume_versions(ctx.user_id)
    applications = ctx.store.list_applications(ctx.user_id)
    by_version: dict[str, list[dict]] = {}
    for a in applications:
        vid = a.get("resume_version_id")
        if vid:
            by_version.setdefault(vid, []).append(a)

    out = []
    for v in versions:
        apps = by_version.get(v["id"], [])
        n = len(apps)
        interviews = 0
        offers = 0
        for a in apps:
            statuses = {e.get("status") for e in (a.get("timeline") or [])}
            if "interview" in statuses:
                interviews += 1
            if "offer" in statuses or "accepted" in statuses:
                offers += 1
        entry = {"id": v["id"], "label": v["label"], "target_track": v["target_track"],
                 "created_at": v["created_at"], "applications_count": n,
                 "interviews_count": interviews, "offers_count": offers}
        if n < _MIN_APPLICATIONS_FOR_CONFIDENCE:
            entry["confidence"] = "insufficient_data"
            entry["note"] = (f"only {n} application(s) used this version — too few "
                            "to draw a conclusion")
        else:
            entry["confidence"] = "observed"
            entry["interview_rate_pct"] = round(100.0 * interviews / n, 1)
        out.append(entry)
    return {"versions": out}


# ---------------------------------------------------------------------------
# PDF export — the whole point of "generate a resume version" is to get a
# file you can actually send; a resume is not GSTIN/PAN-class data (it
# EXISTS to be shared), so unlike list_resume_versions' metadata-only rule
# this deliberately returns the real content, to its own owner only.
# ---------------------------------------------------------------------------

# fpdf2's core (non-embedded) fonts are latin-1 only and raise on anything
# outside it — found live: an em dash from an LLM-drafted version crashed
# rendering. Transliterate the common offenders instead of shipping a bundled
# Unicode font just for a plain-text resume; a final latin-1 replace-encode
# is the never-crash safety net for anything still unmapped.
_PDF_CHAR_MAP = {
    "—": "-", "–": "-",   # em dash, en dash
    "‘": "'", "’": "'",   # curly single quotes
    "“": '"', "”": '"',   # curly double quotes
    "…": "...",                     # ellipsis
    "•": "-",                       # bullet
}


def _pdf_safe(text: str) -> str:
    for src, dst in _PDF_CHAR_MAP.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def render_resume_pdf(content: str, title: str = "Resume") -> bytes:
    """Plain, legible PDF of a resume version's exact text — same content
    the /content route returns, just paginated. Minimal markdown-ish
    parsing matching _deterministic_draft's own output shape ('## ' and
    '# ' headers, '- ' bullets); anything else renders as a paragraph."""
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    # fpdf2's multi_cell defaults to new_x=XPos.RIGHT — after a short line
    # (rendered in one pass, not wrapped) the cursor is left at the cell's
    # right edge, not the left margin. With w=0 (full remaining width) that
    # edge sits AT the right margin, so the next multi_cell call finds ~0mm
    # left and raises "not enough horizontal space" (found live). Every
    # call below must explicitly reset to the left margin.
    def _line(text, h, **kw):
        pdf.multi_cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT, **kw)

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    _line(_pdf_safe(title), 9)
    pdf.ln(2)
    pdf.set_font("Helvetica", size=11)

    for raw_line in content.split("\n"):
        line = _pdf_safe(raw_line.rstrip())
        stripped = line.strip()
        if not stripped:
            pdf.ln(3)
            continue
        if stripped.startswith("## "):
            pdf.set_font("Helvetica", "B", 13)
            pdf.ln(2)
            _line(stripped[3:], 7)
            pdf.set_font("Helvetica", size=11)
        elif stripped.startswith("# "):
            pdf.set_font("Helvetica", "B", 14)
            _line(stripped[2:], 7)
            pdf.set_font("Helvetica", size=11)
        elif stripped.startswith("- "):
            _line("  -  " + stripped[2:], 6)
        else:
            _line(stripped, 6)

    out = pdf.output()
    return bytes(out)
