"""Graph builder — automatic relationship generation across all sources.

Creates nodes for notes / goals / tasks / memories / emails / calendar events,
and edges:
  belongs_to  : task   -> goal
  depends_on  : goal   -> goal      (from goal_deps)
  blocks      : goal   -> goal      (an unmet dependency blocks the dependent)
  related_to  : note  <-> note      (wiki-links + shared terms)
  supports    : note   -> goal      (note text mentions the goal)
"""
from __future__ import annotations

import re

from .store import GraphStore

_W = re.compile(r"[a-z0-9]+")
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
_STOP = set("the a an and or to of in on for with my your our is are be this that it".split())


def _tok(s):
    return {t for t in _W.findall((s or "").lower()) if t not in _STOP and len(t) > 2}


def build_graph(graph_path, notes, collab_db, connector_dir=None) -> dict:
    g = GraphStore(graph_path)
    g.reset()
    db = collab_db.conn

    # --- nodes -------------------------------------------------------------
    note_sig = {}
    title_to_note = {}
    for n in notes:
        nid = "note:" + n.path
        g.add_node(nid, "note", n.title, n.path)
        note_sig[nid] = (_tok(n.title + " " + (n.body or "")), n)
        title_to_note[n.title.lower()] = nid

    goals = db.execute("SELECT id,title,domain,status FROM goals").fetchall()
    for go in goals:
        g.add_node("goal:" + go["id"], "goal", go["title"], go["id"])
    for t in db.execute("SELECT id,goal_id,title FROM tasks").fetchall():
        g.add_node("task:" + t["id"], "task", t["title"], t["id"])
        g.add_edge("task:" + t["id"], "goal:" + t["goal_id"], "belongs_to")
    for s in db.execute("SELECT id,text FROM summaries ORDER BY id DESC LIMIT 50").fetchall():
        g.add_node("memory:" + str(s["id"]), "memory", s["text"][:60], str(s["id"]))

    if connector_dir:
        try:
            from ..connectors import ConnectorRegistry
            reg = ConnectorRegistry(connector_dir)
            for kind in ("email", "calendar", "tasks"):
                for it in reg.list(kind, mode="private", limit=80):
                    nodetype = {"tasks": "task"}.get(kind, kind)
                    g.add_node(f"{kind}:{it.get('id','')}", nodetype, it.get("title", ""), it.get("id", ""))
        except Exception:
            pass

    # --- edges -------------------------------------------------------------
    # depends_on + blocks (from goal_deps)
    status = {go["id"]: go["status"] for go in goals}
    for r in db.execute("SELECT goal_id, depends_on FROM goal_deps").fetchall():
        g.add_edge("goal:" + r["goal_id"], "goal:" + r["depends_on"], "depends_on")
        if status.get(r["depends_on"]) != "done":
            g.add_edge("goal:" + r["depends_on"], "goal:" + r["goal_id"], "blocks")

    # related_to: wiki-links + shared-term overlap between notes
    ids = list(note_sig)
    for nid, (sig, note) in note_sig.items():
        for target in _WIKILINK.findall(note.body or ""):
            tid = title_to_note.get(target.strip().lower())
            if tid and tid != nid:
                g.add_edge(nid, tid, "related_to")
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            overlap = len(note_sig[ids[i]][0] & note_sig[ids[j]][0])
            if overlap >= 8:
                g.add_edge(ids[i], ids[j], "related_to", round(min(1.0, overlap / 50), 3))

    # supports: a note that mentions a goal supports it
    for go in goals:
        gterms = _tok(go["title"])
        if not gterms:
            continue
        for nid, (sig, note) in note_sig.items():
            if len(gterms & sig) >= max(1, len(gterms) - 1):
                g.add_edge(nid, "goal:" + go["id"], "supports")

    g.commit()
    stats = g.stats()
    g.close()
    return stats
