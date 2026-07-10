"""LIFE AUTOPILOT L1 — health profile bootstrap + target proposals.

There is no pre-existing "vault-bootstrap" pattern in this codebase to
clone (career_profile is manual-only — verified by grep before writing
this). This module is built from the two closest reusable idioms instead:
amy/finance/custodial_ai.py's fuzzy token-matching (match_beneficiary) and
its sensitive=True LLM-rescue pattern (llm_parse_transfer), plus the plain
vault.rglob("*.md") walk amy/agents/reactive.py already uses for keyword
search.

Flow: find_health_folder() fuzzy-matches a vault top-level folder against
health/fitness/wellness/personal/profile candidates -> parse_health_notes()
does ONE sensitive=True LLM extraction pass over its notes -> if all
essentials are present, propose_health_targets() parks tier-2 proposals
(each with its formula/inputs) via submit_action; if the folder is missing
or essentials are incomplete, target features stay dormant and a
durably-deduped notification says exactly what's needed (re-notifies at
most every AMY_LIFE_RESUGGEST_DAYS, never daily).
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import re as _re
from pathlib import Path

from . import targets as life_targets

_WORD_RE = _re.compile(r"[a-z0-9]+")
_CANDIDATE_FOLDER_NAMES = ("health", "fitness", "wellness", "personal", "profile")
_MATCH_THRESHOLD = 0.55
_ESSENTIAL_LABELS = {
    "dob_or_age": "date of birth or age",
    "sex": "sex (for the BMR formula)",
    "height_cm": "height (cm)",
    "weight_kg": "weight (kg)",
    "activity_level": "activity level (sedentary/light/moderate/active/very_active)",
}


def _tokens(s: str) -> list[str]:
    return _WORD_RE.findall((s or "").lower())


def _fuzzy_score(name: str) -> float:
    name_toks = _tokens(name)
    if not name_toks:
        return 0.0
    best = 0.0
    for cand in _CANDIDATE_FOLDER_NAMES:
        contained = sum(1 for t in name_toks if cand in t or t in cand)
        score = contained / len(name_toks)
        if score < 1.0:
            ratio = difflib.SequenceMatcher(None, cand, name.lower()).ratio()
            score = max(score, ratio)
        best = max(best, score)
    return best


def find_health_folder(vault_dir: Path) -> Path | None:
    """Best-scoring top-level vault folder matching a health-ish name, or
    None if nothing clears the threshold (never guesses a wrong folder)."""
    if not vault_dir.exists():
        return None
    best_dir, best_score = None, 0.0
    for child in sorted(vault_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        score = _fuzzy_score(child.name)
        if score > best_score:
            best_dir, best_score = child, score
    return best_dir if best_score >= _MATCH_THRESHOLD else None


_PARSE_SYSTEM = (
    "You extract a personal health profile from free-form notes for a "
    "life-tracking assistant. Return STRICT JSON only, using null for "
    "anything not explicitly stated — never guess or infer a number that "
    "isn't written down: "
    '{"dob_or_age": "<YYYY-MM-DD date of birth, or a plain age number as a '
    'string, or null>", "sex": "<male|female or null>", '
    '"height_cm": <number or null>, "weight_kg": <number or null>, '
    '"activity_level": "<sedentary|light|moderate|active|very_active or null>", '
    '"constraints": "<any stated dietary/medical constraints, verbatim, or '
    'empty string>"}'
)


def parse_health_notes(folder: Path, llm) -> dict:
    """ONE sensitive=True LLM extraction pass over the folder's notes.
    Returns only fields the LLM actually reported (never fabricates)."""
    if llm is None:
        return {}
    texts = []
    for p in sorted(folder.rglob("*.md"))[:20]:
        try:
            texts.append(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
    combined = "\n\n---\n\n".join(texts)[:6000]
    if not combined.strip():
        return {}
    try:
        raw, model = llm.generate(_PARSE_SYSTEM, combined, sensitive=True)
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception:
        return {}
    out: dict = {}
    for key in ("dob_or_age", "sex", "activity_level"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    for key in ("height_cm", "weight_kg"):
        v = data.get(key)
        if isinstance(v, (int, float)) and v > 0:
            out[key] = float(v)
    c = data.get("constraints")
    if isinstance(c, str) and c.strip():
        out["constraints"] = c.strip()[:2000]
    return out


def missing_essentials(profile: dict) -> list[str]:
    """profile: the dict shape returned by AutomationStore.get_health_profile
    (or a plain parsed dict with the same keys). Returns human labels for
    whatever's missing — the exact copy used in the dormancy notification
    and the Habits-tab empty state."""
    out = []
    if life_targets.resolve_age(profile.get("dob_or_age") or "") is None:
        out.append(_ESSENTIAL_LABELS["dob_or_age"])
    for key in ("sex", "height_cm", "weight_kg", "activity_level"):
        if not profile.get(key):
            out.append(_ESSENTIAL_LABELS[key])
    return out


# ---------------------------------------------------------------------------
# Durable re-notify guard (never a nag — prefs-table dedup, same idiom as
# agents/reactive.py's debrief_prompted_{id})
# ---------------------------------------------------------------------------

def _should_renotify(ctx, key: str, days: int) -> bool:
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (key,)).fetchone()
    if row is None:
        return True
    try:
        last = _dt.datetime.fromisoformat(row["value"])
    except Exception:
        return True
    return (_dt.datetime.now(_dt.timezone.utc) - last).days >= days


def _mark_notified(ctx, key: str) -> None:
    ctx.collab.conn.execute(
        "INSERT INTO prefs(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, _dt.datetime.now(_dt.timezone.utc).isoformat()))
    ctx.collab.conn.commit()


def _resuggest_days(ctx) -> int:
    from .. import config
    try:
        return int(config._env("AMY_LIFE_RESUGGEST_DAYS", "21"))
    except ValueError:
        return 21


def _notify_dormant(ctx, reason: str, missing: list[str] | None = None) -> None:
    key = f"health_bootstrap_needed_{ctx.user_id}"
    if not _should_renotify(ctx, key, _resuggest_days(ctx)):
        return
    if missing:
        body = ("Health targets are on hold until this is filled in: "
                + "; ".join(missing) + ". Add a note under a health/personal "
                "vault folder, or fill in Habits -> Health targets manually.")
    else:
        body = reason
    try:
        ns = ctx.notify_store()
        ns.create(type="health_bootstrap_needed",
                  title="Health targets need a bit more info",
                  body=body, priority="normal",
                  related_entity={"entity_type": "health_profile", "id": ctx.user_id})
    except Exception:
        pass
    _mark_notified(ctx, key)


# ---------------------------------------------------------------------------
# Target proposals (tier 2, evidence = the formula shown in the body)
# ---------------------------------------------------------------------------

def propose_health_targets(ctx, profile: dict, dedup_suffix: str = "") -> list[dict]:
    """One tier-2 proposal per actionable target group (calorie budget from
    BMR+TDEE, sleep window, protein, water) — never auto-applied. Dedup is
    per-uid-per-kind (+ dedup_suffix): re-running bootstrap with the default
    empty suffix doesn't re-propose an already pending/executed target.
    check_weight_shift() calls this again with a month-stamped suffix so a
    >5% weight shift can still get a fresh proposal even though the
    original one was already approved (create_approval's dedup blocks
    pending/executed/auto_executed rows, so the *same* dedup_key would
    otherwise permanently block any re-proposal)."""
    from ..automation.executors import submit_action

    suffix = f"_{dedup_suffix}" if dedup_suffix else ""
    age = life_targets.resolve_age(profile.get("dob_or_age") or "")
    computed = life_targets.all_targets(
        profile.get("sex") or "", float(profile["weight_kg"]),
        float(profile["height_cm"]), age, profile.get("activity_level") or "")
    disclaimer = computed["disclaimer"]
    results = []

    bmr, tdee = computed["bmr"], computed["tdee"]
    results.append(submit_action(
        ctx, 2, "health_target_propose",
        title="Proposed daily calorie budget",
        body=(f"BMR {bmr['value']} kcal/day ({bmr['formula']}); "
             f"TDEE {tdee['value']} kcal/day ({tdee['formula']}). {disclaimer}"),
        payload={"kind": "calorie_budget", "target": tdee["value"], "unit": tdee["unit"],
                "formula": {"bmr": bmr, "tdee": tdee}},
        source="health_bootstrap", dedup_key=f"health_target_calorie_{ctx.user_id}{suffix}",
        reasoning="Mifflin-St Jeor BMR x activity multiplier, from the health profile.",
        risk="write", affected_entity="health_profile"))

    sleep = computed["sleep"]
    lo, hi = sleep["value"]["min_hours"], sleep["value"]["max_hours"]
    results.append(submit_action(
        ctx, 2, "health_target_propose",
        title=f"Proposed sleep window: {lo}-{hi}h/night",
        body=f"{sleep['formula']}. {disclaimer}",
        payload={"kind": "sleep_target", "target": sleep["value"], "unit": sleep["unit"],
                "formula": sleep},
        source="health_bootstrap", dedup_key=f"health_target_sleep_{ctx.user_id}{suffix}",
        reasoning="Published age-band sleep guideline, from the health profile's age.",
        risk="write", affected_entity="health_profile"))

    protein = computed["protein"]
    results.append(submit_action(
        ctx, 2, "health_target_propose",
        title=f"Proposed protein target: {protein['value']}g/day",
        body=f"{protein['formula']}. {disclaimer}",
        payload={"kind": "protein_target", "target": protein["value"], "unit": protein["unit"],
                "formula": protein},
        source="health_bootstrap", dedup_key=f"health_target_protein_{ctx.user_id}{suffix}",
        reasoning="Weight x activity-scaled g/kg, from the health profile.",
        risk="write", affected_entity="health_profile"))

    water = computed["water"]
    results.append(submit_action(
        ctx, 2, "health_target_propose",
        title=f"Proposed water target: {water['value']}ml/day",
        body=f"{water['formula']}. {disclaimer}",
        payload={"kind": "water_target", "target": water["value"], "unit": water["unit"],
                "formula": water},
        source="health_bootstrap", dedup_key=f"health_target_water_{ctx.user_id}{suffix}",
        reasoning="Weight-scaled water estimate, from the health profile.",
        risk="write", affected_entity="health_profile"))

    return results


def check_weight_shift(ctx, shift: dict) -> dict | None:
    """Called after AutomationStore.append_weight_log(). >5% shift ->
    tier-2 re-proposal with the delta shown; smaller shifts adjust nothing
    (never a silent target change)."""
    pct = shift.get("pct_change")
    if pct is None or abs(pct) < 5.0:
        return None
    from ..automation.executors import submit_action
    month = _dt.date.today().strftime("%Y-%m")
    direction = "up" if pct > 0 else "down"
    return submit_action(
        ctx, 2, "health_target_propose",
        title=f"Weight shifted {direction} {abs(pct):.1f}% — re-check targets?",
        body=(f"Weight went from {shift['previous_weight_kg']}kg to "
             f"{shift['weight_kg']}kg ({pct:+.1f}%). Your calorie/protein/"
             f"water targets were computed from the old weight — approving "
             f"this recomputes them. {life_targets.ESTIMATE_DISCLAIMER}"),
        payload={"kind": "weight_shift_recompute", "previous_weight_kg": shift["previous_weight_kg"],
                "weight_kg": shift["weight_kg"], "pct_change": pct},
        source="health_bootstrap", dedup_key=f"health_weight_shift_{ctx.user_id}_{month}",
        reasoning=f"Weight moved {pct:+.1f}% since the last computed targets (>5% threshold).",
        risk="write", affected_entity="health_profile")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _is_bootstrapped(profile: dict | None) -> bool:
    if not profile:
        return False
    return not missing_essentials(profile)


def bootstrap_health_profile(ctx) -> dict:
    """Idempotent: no-ops (status=already_bootstrapped) once every essential
    is on file. Otherwise fuzzy-matches a health folder, parses it
    sensitive=True, stores whatever was found with provenance='vault', and
    either proposes targets (all essentials present) or stays dormant with
    an exact list of what's missing."""
    from ..saas import tenancy

    existing = ctx.store.get_health_profile(ctx.user_id)
    if _is_bootstrapped(existing):
        return {"status": "already_bootstrapped"}

    vault = tenancy.resolve_vault_dir(ctx.user_id)
    folder = find_health_folder(vault)
    if folder is None:
        _notify_dormant(ctx, "No health/fitness/personal folder found in your "
                        "vault yet — target features stay dormant until one "
                        "exists (or you fill in Habits -> Health targets manually).")
        return {"status": "dormant", "reason": "no_folder"}

    llm = _get_llm(ctx)
    parsed = parse_health_notes(folder, llm)
    mtime = _folder_max_mtime(folder)
    if mtime > 0:
        marker_key = f"health_folder_synced_{ctx.user_id}"
        ctx.collab.conn.execute(
            "INSERT INTO prefs(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (marker_key, str(mtime)))
        ctx.collab.conn.commit()
    missing = missing_essentials(parsed)
    if parsed:
        ctx.store.upsert_health_profile(
            ctx.user_id,
            dob_or_age=parsed.get("dob_or_age"), sex=parsed.get("sex"),
            height_cm=parsed.get("height_cm"), weight_kg=parsed.get("weight_kg"),
            activity_level=parsed.get("activity_level"),
            constraints=parsed.get("constraints"),
            provenance={k: "vault" for k in parsed})
    if missing:
        _notify_dormant(ctx, "", missing=missing)
        return {"status": "dormant", "reason": "missing_essentials", "missing": missing,
               "folder": str(folder)}

    profile = ctx.store.get_health_profile(ctx.user_id)
    proposed = propose_health_targets(ctx, profile)
    return {"status": "bootstrapped", "folder": str(folder),
           "proposals": [p.get("status") for p in proposed]}


def _folder_max_mtime(folder: Path) -> float:
    best = 0.0
    for p in folder.rglob("*.md"):
        try:
            best = max(best, p.stat().st_mtime)
        except OSError:
            continue
    return best


def check_vault_reparse(ctx) -> dict | None:
    """Poll-driven re-parse (tier 1, diff shown): compares the health
    folder's newest .md mtime against the last-synced marker in prefs
    (key health_folder_synced_{uid}). Same 'no natural push event, use the
    job-scan idiom' choice as meeting_prep_scan/portfolio_review — a live
    vault.note_edited subscription would require rewiring app.py's
    VaultWatcher off its bare EventStore (not in AGENT_RELEVANT_EVENTS
    today), while this is self-contained to amy/life and gives identical
    tier-1-diff-shown behavior. First run after bootstrap just seeds the
    marker (the initial parse already happened in bootstrap_health_profile,
    so there's nothing to diff against yet)."""
    from ..saas import tenancy

    vault = tenancy.resolve_vault_dir(ctx.user_id)
    folder = find_health_folder(vault)
    if folder is None:
        return None
    current_mtime = _folder_max_mtime(folder)
    if current_mtime <= 0:
        return None
    marker_key = f"health_folder_synced_{ctx.user_id}"
    row = ctx.collab.conn.execute(
        "SELECT value FROM prefs WHERE key=?", (marker_key,)).fetchone()
    last_mtime = float(row["value"]) if row and row["value"] else 0.0
    if last_mtime <= 0:
        ctx.collab.conn.execute(
            "INSERT INTO prefs(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (marker_key, str(current_mtime)))
        ctx.collab.conn.commit()
        return None
    if current_mtime <= last_mtime:
        return None

    try:
        llm = _get_llm(ctx)
        parsed = parse_health_notes(folder, llm)
        if not parsed:
            return None
        before = ctx.store.get_health_profile(ctx.user_id) or {}

        # Weight changes route through append_weight_log + check_weight_shift
        # (the dedicated >5% tier-2 re-proposal path) instead of the generic
        # tier-1 vault_reparse executor, so a >5% shift still gets the
        # explicit delta-shown re-proposal the spec requires — never silent.
        new_weight = parsed.pop("weight_kg", None)
        weight_result = None
        if new_weight is not None and str(before.get("weight_kg") or "") != str(new_weight):
            shift = ctx.store.append_weight_log(ctx.user_id, new_weight, source="vault")
            weight_result = check_weight_shift(ctx, shift)

        diff_lines = []
        for key in ("dob_or_age", "sex", "height_cm", "activity_level", "constraints"):
            new_v = parsed.get(key)
            if new_v is None:
                continue
            old_v = before.get(key)
            if str(old_v or "") != str(new_v):
                diff_lines.append(f"{key}: {old_v!r} -> {new_v!r}")
        if not diff_lines:
            return weight_result

        from ..automation.executors import submit_action
        result = submit_action(
            ctx, 1, "health_target_propose",
            title="Health profile updated from vault edit",
            body="Vault note change re-parsed:\n" + "\n".join(diff_lines),
            payload={"kind": "vault_reparse", "fields": parsed},
            source="health_bootstrap", dedup_key=None,
            reasoning="vault.note_edited under the health folder triggered a re-parse.",
            risk="write", affected_entity="health_profile")
        return weight_result or result
    finally:
        # Update the marker regardless of outcome — an unchanged-looking
        # parse (or a parse failure) shouldn't re-trigger on every poll.
        ctx.collab.conn.execute(
            "INSERT INTO prefs(key,value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (marker_key, str(current_mtime)))
        ctx.collab.conn.commit()


def _get_llm(ctx):
    if ctx.llm is not None:
        return ctx.llm
    cached = ctx._extras.get("lazy_llm")
    if cached is not None:
        return cached
    try:
        from ..llm import LLMRouter
        from ..automation.store import TrackedLLM
        llm = TrackedLLM(LLMRouter(use_global_keys=True), ctx.store,
                         purpose="life_health_bootstrap")
    except Exception:
        llm = None
    ctx._extras["lazy_llm"] = llm
    return llm
