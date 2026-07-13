"""AML Monitoring Module (Phase 2) — rule-based typology detection + case
lifecycle. Phase 2 of the same illustrative "Banking Risk Intelligence"
series as amy/finance/fraud_engine.py (Phase 1) — read that module first;
this one reuses its patterns instead of inventing new ones.

ILLUSTRATIVE / SIMULATED ONLY. Not a real AML/compliance system: there is
no sanctions list, no PEP database, and no regulator feed anywhere in this
codebase. Every threshold below is a placeholder for a plausible demo, not
sourced from any regulation or real AML dataset — each is commented
`# illustrative threshold, not sourced from regulation`. Any SAR-style text
this module produces (see build_sar_draft_text) opens with a
"DRAFT — NOT A REAL REGULATORY FILING" header baked into the text itself,
not just a code comment, because that text could be screenshotted out of
context.

Signals this system has no data source for (UNAVAILABLE_SIGNALS below) are
named and surfaced honestly rather than faked, same convention as
fraud_engine.py's UNAVAILABLE_SIGNALS.

Four design decisions worth knowing before touching this file:

1. Circular-transfer detection uses its OWN dedicated aml_graph.db
   (via amy/knowledge_graph/store.py's GraphStore CLASS, reused exactly as
   the project's typed node/edge store — but NOT the shared graph.db).
   graph.db already backs /api/graph/viz and career_apply.py's referral
   search, which substring-scans every node label with no type filter —
   writing account/beneficiary nodes into that shared file would risk
   financial account nicknames surfacing in an unrelated career-referral
   chat answer or the general knowledge-graph visualization. A dedicated
   file avoids that while still reusing the class (see detect_circular_
   transfers()).

2. Cycle detection is a small directed DFS over GraphStore.edges(), NOT
   GraphStore.traverse()/neighbors() — those two are direction-agnostic
   (they union src=? and dst=?, so A->B and B->A look identical to them).
   Reusing them for "is this a cycle" would silently produce false
   positives for any merely-connected set of accounts. edges() preserves
   src/dst, so the store is still doing the storage/retrieval work; only
   the traversal itself is custom (see _find_directed_cycles).

3. Circular-transfer detection is scoped to the user's OWN linked accounts
   and custodial beneficiaries — not general multi-party AML layering.
   Bank statements in this schema never carry a counterparty account
   identifier; the only way to build a directed, identifiable edge is (a)
   a transaction with beneficiary_id set (custodial disbursement), or (b)
   a transaction whose merchant text contains another of the user's own
   account nicknames (the common self-transfer pattern). This is real and
   honestly computable, but it cannot see money moving through accounts
   this system doesn't hold.

4. Cash-spike detection is a merchant-keyword heuristic (ATM/cash-
   withdrawal narration patterns), not a verified cash-transaction flag —
   this schema has no such column. Same idiom already used in this
   codebase for Life Autopilot's `late_night_orders` (documented in
   CLAUDE.md's Known Constraints as "a merchant-identity proxy... not an
   hour-verified signal").
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
from collections import defaultdict
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Illustrative thresholds — every one is a placeholder, none sourced from
# regulation or a real AML dataset.
# ---------------------------------------------------------------------------

STRUCTURING_THRESHOLD = 50_000.0     # illustrative threshold, not sourced from regulation
STRUCTURING_BAND_LOW_FRACTION = 0.70 # illustrative threshold, not sourced from regulation
STRUCTURING_WINDOW_DAYS = 3          # illustrative threshold, not sourced from regulation
STRUCTURING_MIN_COUNT = 3            # illustrative threshold, not sourced from regulation

LAYERING_WINDOW_DAYS = 2             # illustrative threshold, not sourced from regulation
LAYERING_OUTFLOW_FRACTION = 0.80     # illustrative threshold, not sourced from regulation
LAYERING_MIN_CREDIT = 10_000.0       # illustrative threshold, not sourced from regulation

CASH_KEYWORDS = ("atm", "cash wdl", "cash withdrawal", "csh wdl", "cash w/d", "pos cash")
CASH_SPIKE_MULTIPLIER = 3.0          # illustrative threshold, not sourced from regulation
CASH_SPIKE_FLOOR = 5_000.0           # illustrative threshold, not sourced from regulation

CIRCULAR_MAX_CYCLE_LEN = 5           # search bound, not a regulatory figure
LOOKBACK_DAYS = 180                  # history window the comparison signals draw from

RISK_THRESHOLDS = (   # illustrative threshold, not sourced from regulation
    ("LOW", 0, 24),
    ("MEDIUM", 25, 49),
    ("HIGH", 50, 74),
    ("CRITICAL", 75, 100),
)

# Signals this rule-based module does NOT compute, and why. No invented
# values, ever — mirrors fraud_engine.py's UNAVAILABLE_SIGNALS.
UNAVAILABLE_SIGNALS = {
    "high_risk_country_screening": "no country-risk list exists in this codebase",
    "pep_screening": "no politically-exposed-persons database exists in this codebase",
    "sanctions_screening": "no sanctions list exists in this codebase",
    "money_mule_detection": "requires network-level data across multiple people's "
                            "accounts — this is a single-user personal finance system "
                            "with no visibility into other accounts/holders",
}

SAR_DRAFT_HEADER = (
    "DRAFT — NOT A REAL REGULATORY FILING\n"
    "This is an illustrative/demo document generated by a personal-finance\n"
    "portfolio project (Amy PersonalOS). It has no legal or regulatory\n"
    "standing and must never be submitted to any authority.\n"
)


@dataclass
class AlertCandidate:
    typology: str                          # structuring|layering|circular_transfer|cash_spike
    score: int                             # 0-100, illustrative
    risk_level: str                        # LOW|MEDIUM|HIGH|CRITICAL
    account_id: str | None                 # None for portfolio-level (circular_transfer)
    evidence: list[str] = field(default_factory=list)   # transaction ids
    timeline: list[dict] = field(default_factory=list)  # [{date, event, transaction_id}]
    explanation: str = ""


def _parse_date(date_str) -> _dt.date | None:
    if not date_str:
        return None
    try:
        return _dt.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None


def _risk_level_for_score(score: int) -> str:
    for level, lo, hi in RISK_THRESHOLDS:
        if lo <= score <= hi:
            return level
    return "CRITICAL"


def _timeline_from_ids(fe, ids: list[str]) -> list[dict]:
    out = []
    for tid in ids:
        t = fe.get_transaction(tid)
        if t:
            out.append({"date": t["date"],
                       "event": f"{t.get('merchant', '')}: {t['amount']:,.2f}",
                       "transaction_id": tid})
    out.sort(key=lambda e: e["date"])
    return out


# ---------------------------------------------------------------------------
# Typology 1 — structuring: a cluster of sub-threshold transactions bunched
# near a reporting threshold within a short window.
# ---------------------------------------------------------------------------

def _in_structuring_band(amount: float) -> bool:
    return STRUCTURING_THRESHOLD * STRUCTURING_BAND_LOW_FRACTION <= amount < STRUCTURING_THRESHOLD


def detect_structuring(fe, account_id: str) -> list[AlertCandidate]:
    txns = [t for t in fe.list_transactions(limit=5000, account_id=account_id)
           if _in_structuring_band(abs(t["amount"] or 0))]
    txns.sort(key=lambda t: t["date"])
    candidates = []
    i, n = 0, len(txns)
    while i < n:
        window_start = _parse_date(txns[i]["date"])
        if window_start is None:
            i += 1
            continue
        cluster = [txns[i]]
        j = i + 1
        while j < n:
            d = _parse_date(txns[j]["date"])
            if d is None or (d - window_start).days > STRUCTURING_WINDOW_DAYS:
                break
            cluster.append(txns[j])
            j += 1
        if len(cluster) >= STRUCTURING_MIN_COUNT:
            evidence = [t["id"] for t in cluster]
            total = sum(abs(t["amount"] or 0) for t in cluster)
            score = min(100, 20 * len(cluster))
            explanation = (f"{len(cluster)} transactions of {STRUCTURING_BAND_LOW_FRACTION:.0%}-99% "
                          f"of the {STRUCTURING_THRESHOLD:,.0f} illustrative reporting threshold "
                          f"within {STRUCTURING_WINDOW_DAYS} days (total {total:,.0f}).")
            candidates.append(AlertCandidate(
                typology="structuring", score=score, risk_level=_risk_level_for_score(score),
                account_id=account_id, evidence=evidence,
                timeline=_timeline_from_ids(fe, evidence), explanation=explanation))
            i = j   # don't re-trigger on overlapping sub-windows of the same cluster
        else:
            i += 1
    return candidates


# ---------------------------------------------------------------------------
# Typology 2 — layering: a credit followed by rapid, disproportionate
# outflow within a short window (day-granularity — see module docstring's
# Phase 1 cross-reference on transactions.date having no time component).
# ---------------------------------------------------------------------------

def detect_layering(fe, account_id: str) -> list[AlertCandidate]:
    txns = fe.list_transactions(limit=5000, account_id=account_id)
    candidates = []
    for credit in txns:
        amt = credit["amount"] or 0
        if amt < LAYERING_MIN_CREDIT:
            continue
        cdate = _parse_date(credit["date"])
        if cdate is None:
            continue
        window_end = cdate + _dt.timedelta(days=LAYERING_WINDOW_DAYS)
        outflow = [t for t in txns
                  if t["id"] != credit["id"] and (t["amount"] or 0) < 0
                  and (d := _parse_date(t["date"])) and cdate <= d <= window_end]
        if not outflow:
            continue
        outflow_total = sum(abs(t["amount"] or 0) for t in outflow)
        fraction = outflow_total / amt
        if fraction < LAYERING_OUTFLOW_FRACTION:
            continue
        evidence = [credit["id"]] + [t["id"] for t in outflow]
        score = min(100, int(30 + fraction * 50))
        explanation = (f"{amt:,.0f} credited on {credit['date']}, {outflow_total:,.0f} "
                      f"({fraction:.0%}) moved back out within {LAYERING_WINDOW_DAYS} days "
                      f"across {len(outflow)} transaction(s).")
        candidates.append(AlertCandidate(
            typology="layering", score=score, risk_level=_risk_level_for_score(score),
            account_id=account_id, evidence=evidence,
            timeline=_timeline_from_ids(fe, evidence), explanation=explanation))
    return candidates


# ---------------------------------------------------------------------------
# Typology 3 — cash spike: an ATM/cash-withdrawal-shaped debit far above
# this account's own trailing cash-withdrawal average.
# ---------------------------------------------------------------------------

def _looks_like_cash_withdrawal(merchant: str | None) -> bool:
    m = (merchant or "").lower()
    return any(k in m for k in CASH_KEYWORDS)


def detect_cash_spike(fe, account_id: str) -> list[AlertCandidate]:
    debits = [t for t in fe.list_transactions(limit=5000, account_id=account_id)
             if (t["amount"] or 0) < 0]
    cash_txns = [t for t in debits if _looks_like_cash_withdrawal(t.get("merchant"))]
    candidates = []
    for t in cash_txns:
        amt = abs(t["amount"] or 0)
        d = _parse_date(t["date"])
        since = (d - _dt.timedelta(days=LOOKBACK_DAYS)).isoformat() if d else None
        until = d.isoformat() if d else None
        history = [h for h in cash_txns if h["id"] != t["id"]
                  and (since is None or h["date"] >= since)
                  and (until is None or h["date"] <= until)]
        if len(history) < 3:
            continue
        avg = sum(abs(h["amount"] or 0) for h in history) / len(history)
        if avg <= 0 or amt < max(CASH_SPIKE_FLOOR, CASH_SPIKE_MULTIPLIER * avg):
            continue
        ratio = amt / avg
        score = min(100, int(40 + (ratio - CASH_SPIKE_MULTIPLIER) * 10))
        explanation = (f"cash-withdrawal-shaped debit of {amt:,.0f} vs this account's "
                      f"trailing cash-withdrawal average of {avg:,.0f} "
                      f"({ratio:.1f}x, matched on merchant text — not a verified cash flag).")
        candidates.append(AlertCandidate(
            typology="cash_spike", score=score, risk_level=_risk_level_for_score(score),
            account_id=account_id, evidence=[t["id"]],
            timeline=_timeline_from_ids(fe, [t["id"]]), explanation=explanation))
    return candidates


# ---------------------------------------------------------------------------
# Typology 4 — circular transfers across the user's own accounts and
# custodial beneficiaries. Portfolio-wide by nature (a cycle can't be found
# looking at one account alone); account_id filters the result afterward.
# ---------------------------------------------------------------------------

def _find_directed_cycles(edges: list[dict], max_len: int = CIRCULAR_MAX_CYCLE_LEN) -> list[list[str]]:
    """Small DFS over directed edges — see module docstring point 2 for why
    GraphStore.traverse()/neighbors() can't be reused here (they're
    direction-agnostic)."""
    adj: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        adj[e["src"]].append(e["dst"])
    cycles: list[list[str]] = []
    for start in list(adj.keys()):
        def dfs(node: str, path: list[str], visited: set[str]):
            if len(path) > max_len:
                return
            for nxt in adj.get(node, []):
                if nxt == start and len(path) >= 2:
                    cycles.append(list(path))
                elif nxt not in visited:
                    visited.add(nxt)
                    path.append(nxt)
                    dfs(nxt, path, visited)
                    path.pop()
                    visited.discard(nxt)
        dfs(start, [start], {start})
    return cycles


def detect_circular_transfers(fe, account_id: str | None = None) -> list[AlertCandidate]:
    from ..knowledge_graph.store import GraphStore

    accounts = fe.list_accounts()
    aml_graph_path = fe.path.parent / "aml_graph.db"
    g = GraphStore(str(aml_graph_path))
    evidence_map: dict[tuple, list[str]] = defaultdict(list)
    label_by_id: dict[str, str] = {}
    try:
        g.reset()   # fresh graph each scan — stale edges from since-changed
                    # transactions must not linger and produce phantom cycles
        for a in accounts:
            node_id = f"acct:{a['id']}"
            g.add_node(node_id, "account", a["nickname"])
            label_by_id[node_id] = a["nickname"]
        for a in accounts:
            for t in fe.list_transactions(limit=5000, account_id=a["id"]):
                amt = t["amount"] or 0
                if amt >= 0:
                    continue   # outbound leg only — direction comes from the debit
                dst = None
                if t.get("beneficiary_id"):
                    dst = f"ben:{t['beneficiary_id']}"
                    if dst not in label_by_id:
                        row = fe.conn.execute(
                            "SELECT name FROM beneficiaries WHERE id=?",
                            (t["beneficiary_id"],)).fetchone()
                        label = row["name"] if row else t["beneficiary_id"]
                        g.add_node(dst, "beneficiary", label)
                        label_by_id[dst] = label
                else:
                    merchant = (t.get("merchant") or "").lower()
                    for other in accounts:
                        if other["id"] == a["id"]:
                            continue
                        nickname = (other["nickname"] or "").lower()
                        if nickname and nickname in merchant:
                            dst = f"acct:{other['id']}"
                            break
                if dst is None:
                    continue
                src = f"acct:{a['id']}"
                g.add_edge(src, dst, "transferred_to", weight=abs(amt))
                evidence_map[(src, dst)].append(t["id"])
        g.commit()
        edges = g.edges()
    finally:
        g.close()

    cycles = _find_directed_cycles(edges)
    candidates: list[AlertCandidate] = []
    seen: set[frozenset] = set()
    target_node = f"acct:{account_id}" if account_id else None
    for cycle in cycles:
        if target_node and target_node not in cycle:
            continue
        key = frozenset(cycle)
        if key in seen:
            continue
        seen.add(key)
        evidence: list[str] = []
        for i in range(len(cycle)):
            src, dst = cycle[i], cycle[(i + 1) % len(cycle)]
            evidence.extend(evidence_map.get((src, dst), []))
        if not evidence:
            continue
        evidence = list(dict.fromkeys(evidence))   # de-dupe, keep order
        score = min(100, 30 + 15 * len(cycle))
        path_labels = " -> ".join(label_by_id.get(n, n) for n in cycle + [cycle[0]])
        explanation = (f"circular movement across {len(cycle)} of your own "
                      f"accounts/beneficiaries: {path_labels}. Scoped to accounts/"
                      f"beneficiaries this system holds — not general cross-"
                      f"institution AML layering (see module docstring).")
        candidates.append(AlertCandidate(
            typology="circular_transfer", score=score, risk_level=_risk_level_for_score(score),
            account_id=account_id, evidence=evidence,
            timeline=_timeline_from_ids(fe, evidence), explanation=explanation))
    return candidates


# ---------------------------------------------------------------------------
# Aggregate scan (pure) + case lifecycle (side-effecting)
# ---------------------------------------------------------------------------

def scan_account_for_aml(fe, account_id: str) -> list[AlertCandidate]:
    """Pure/read-only — runs all four detectors, never persists anything.
    Mirrors fraud_engine.score_transaction's purity."""
    out: list[AlertCandidate] = []
    out += detect_structuring(fe, account_id)
    out += detect_layering(fe, account_id)
    out += detect_cash_spike(fe, account_id)
    out += detect_circular_transfers(fe, account_id=account_id)
    return out


def open_case(ctx, candidate: AlertCandidate) -> str:
    """Persists an aml_cases row (deduped against any existing open/
    investigating case with overlapping evidence for the same account+
    typology) and emits aml.alert (always) / aml.case_opened (only when a
    NEW row was created). Called directly — no submit_action/approval; the
    case table holds the investigation, the approvals table only enters
    the picture on escalate_case()/generate_sar_draft() below."""
    fe = ctx.open_finance()
    try:
        existing = fe.find_open_aml_case(candidate.account_id, candidate.typology, candidate.evidence)
        is_new = existing is None
        case_id = (fe.create_aml_case(
            candidate.account_id, candidate.typology, candidate.risk_level, candidate.score,
            candidate.evidence, candidate.timeline, candidate.explanation)
            if is_new else existing["id"])
    finally:
        fe.close()
    try:
        events = ctx.events()
        events.emit("aml.alert", {
            "case_id": case_id, "typology": candidate.typology,
            "risk_level": candidate.risk_level, "evidence_count": len(candidate.evidence),
        }, source="aml_engine")
        if is_new:
            events.emit("aml.case_opened", {
                "case_id": case_id, "typology": candidate.typology,
                "risk_level": candidate.risk_level,
            }, source="aml_engine")
    except Exception:
        pass
    return case_id


def investigate_account(ctx, account_id: str) -> list[dict]:
    """The side-effecting counterpart to scan_account_for_aml(): scores,
    then opens (or reconfirms) a case per triggered typology. This is what
    the scan_account_for_aml REGISTRY TOOL calls (amy/tools/aml_tools.py) —
    named differently here to keep this pure-vs-persisting distinction
    unambiguous in code, even though the tool-facing name matches the
    prompt's spec."""
    fe = ctx.open_finance()
    try:
        candidates = scan_account_for_aml(fe, account_id)
    finally:
        fe.close()
    return [{"case_id": open_case(ctx, c), "typology": c.typology,
            "risk_level": c.risk_level, "score": c.score} for c in candidates]


def escalate_case(ctx, case_id: str) -> dict:
    """Always tier 2, fixed — not severity-computed like Phase 1's
    review_transaction(). Escalation is an explicit human-requested step,
    not an automatic detection output, so there's no "let it auto-apply"
    case the way LOW/MEDIUM fraud scores have one."""
    from ..automation.executors import submit_action

    fe = ctx.open_finance()
    try:
        case = fe.get_aml_case(case_id)
    finally:
        fe.close()
    if case is None:
        raise ValueError(f"aml case {case_id!r} not found")
    if case["status"] in ("escalated", "closed"):
        raise ValueError(f"case is already {case['status']}")
    return submit_action(
        ctx, 2, "aml_case_escalate",
        title=f"AML case escalation — {case['typology']} ({case['risk_level']})",
        body=case["explanation"],
        payload={"case_id": case_id},
        source="aml_engine",
        dedup_key=f"aml_escalate_{case_id}",
        reasoning=f"Human-requested escalation of AML case {case_id} "
                  f"({case['typology']}, {case['risk_level']}).",
        risk="destructive",
        affected_entity=f"aml_case_id={case_id}")


def generate_sar_draft(ctx, case_id: str) -> dict:
    """Always tier 2, fixed. Only ever called on explicit request — never
    automatically from a scan. The header lives in build_sar_draft_text(),
    applied by the aml_case_sar_draft executor once approved."""
    from ..automation.executors import submit_action

    fe = ctx.open_finance()
    try:
        case = fe.get_aml_case(case_id)
    finally:
        fe.close()
    if case is None:
        raise ValueError(f"aml case {case_id!r} not found")
    return submit_action(
        ctx, 2, "aml_case_sar_draft",
        title=f"SAR draft request — case {case_id} ({case['typology']})",
        body="Generates a DRAFT/DEMO document only — not a real regulatory filing.",
        payload={"case_id": case_id},
        source="aml_engine",
        dedup_key=None,   # regenerating after new evidence is legitimate — no dedup block
        reasoning=f"Human-requested SAR draft for AML case {case_id}.",
        risk="destructive",
        affected_entity=f"aml_case_id={case_id}")


def build_sar_draft_text(case: dict) -> str:
    """Builds the draft text — called by the aml_case_sar_draft executor on
    approval, never automatically. The draft/demo header is baked into the
    text itself (opens AND closes with it) so it survives a screenshot out
    of context, per the prompt's explicit requirement."""
    lines = [SAR_DRAFT_HEADER, ""]
    lines.append(f"Case ID: {case['id']}")
    lines.append(f"Typology: {case['typology']}")
    lines.append(f"Risk level (illustrative, not a regulatory rating): "
                f"{case['risk_level']} (score {case['score']}/100)")
    lines.append(f"Account: {case.get('account_id') or 'multiple / portfolio-level'}")
    lines.append(f"Status: {case['status']}")
    lines.append("")
    lines.append("Narrative:")
    lines.append(case.get("explanation") or "(no explanation on file)")
    lines.append("")
    lines.append(f"Evidence — {len(case['evidence'])} transaction(s):")
    for tid in case["evidence"]:
        lines.append(f"  - {tid}")
    lines.append("")
    lines.append("Timeline:")
    for entry in case.get("timeline") or []:
        lines.append(f"  - {entry.get('date', '?')}: {entry.get('event', '')}")
    lines.append("")
    lines.append(SAR_DRAFT_HEADER)
    return "\n".join(lines)
