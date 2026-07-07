"""Phase 8 write-back tools. Every write is a *proposal* that needs explicit
human confirmation before it touches a note, and every applied write is logged
to the relevant Audit Log. Money is never moved — writes only record/notes.

Renamed from amy/tools.py (was a bare top-level module) because amy/tools/
now also exists as a package (the agent tool registry — RISK_READ/WRITE,
register_tool, invoke). Both named "tools" would collide: Python resolves
`from . import tools` to whichever wins the package-vs-module race, silently
breaking the other. This module keeps the OLD single-user write-proposal
behavior (WriteProposal/propose/apply/is_write_request) under a name that
can't collide; amy/agents/{base,career,master}.py import it explicitly."""
from __future__ import annotations
import re, uuid, datetime
from dataclasses import dataclass, field
from pathlib import Path
from . import config

# verbs that indicate the user wants to RECORD something (a write), not just ask
WRITE_MARKERS = ("log ", "record", "add ", "note that", "mark ", "append", "save ")


@dataclass
class WriteProposal:
    id: str
    tool: str
    target: str          # vault-relative note path
    preview: str         # human-readable description of the change
    payload: str         # the exact text to append
    sensitive: bool = False


def is_write_request(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in WRITE_MARKERS)


def _audit_path_for(intent: str, notes) -> str | None:
    """Pick the most relevant Audit Log note for the touched scope."""
    for n in notes:
        if n.path.endswith("Audit Log.md"):
            return n.path
    # fallback: a general log under Home
    return "00_Home/Quick Links.md"


def propose(intent: str, query: str, notes) -> WriteProposal:
    target = _audit_path_for(intent, notes)
    sensitive = any(getattr(n, "sensitive", False) for n in notes)
    stamp = datetime.date.today().isoformat()
    line = f"- {stamp} · logged via Amy: {query.strip()}"
    return WriteProposal(
        id=uuid.uuid4().hex[:8], tool="append_log", target=target,
        preview=f"Append to **{target}**:\n{line}", payload=line, sensitive=sensitive,
    )


def apply(proposal: WriteProposal, vault: Path | None = None) -> str:
    vault = Path(vault or config.VAULT)
    p = vault / proposal.target
    if proposal.tool == "create_file":
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(proposal.payload, encoding="utf-8")
        return f"applied -> {proposal.target}"
        
    if not p.exists():
        return f"target not found: {proposal.target}"
    text = p.read_text(encoding="utf-8")
    if "## Notes" in text:
        text = text.replace("## Notes", "## Notes\n" + proposal.payload, 1)
    else:
        text = text.rstrip() + "\n\n## Notes\n" + proposal.payload + "\n"
    p.write_text(text, encoding="utf-8")
    return f"applied -> {proposal.target}"
