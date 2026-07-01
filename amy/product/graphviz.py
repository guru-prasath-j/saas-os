"""Knowledge-graph visualization transform.

Turns metadata + relationship edges into a viz-ready {nodes, edges} payload a
front-end graph renderer can consume directly.
"""
from __future__ import annotations


def to_graph(metadata_rows: list[dict], edges: list[dict]) -> dict:
    nodes = [{
        "id": m["id"],
        "label": m["title"],
        "domain": m.get("domain", "general"),
        "importance": m.get("importance", 0),
        "group": m.get("domain", "general"),
    } for m in metadata_rows]

    known = {n["id"] for n in nodes}
    links = [{
        "source": e["src"],
        "target": e["dst"],
        "type": e.get("rel_type", "related_to"),
        "weight": e.get("weight", 1.0),
    } for e in edges if e["src"] in known and e["dst"] in known]

    return {"nodes": nodes, "edges": links,
            "stats": {"nodes": len(nodes), "edges": len(links)}}
