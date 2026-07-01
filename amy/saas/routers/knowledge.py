"""Knowledge base (metadata, embeddings, graph) and global knowledge-graph routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import User
from .. import paths
from ..deps import current_user, _engine_for, _knowledge_for, _collab_db_path, _connector_dir

router = APIRouter()


class KSearch(BaseModel):
    query: str
    domain: str | None = None
    tags: list[str] | None = None
    k: int = 5


class Relationship(BaseModel):
    src_id: str
    dst_id: str
    rel_type: str = "depends_on"
    weight: float = 1.0


def _graph_path(user: "User") -> str:
    return str(paths.index_dir(user.id) / "graph.db")


# --- knowledge layer ---------------------------------------------------------

@router.post("/api/knowledge/build")
def knowledge_build(user: User = Depends(current_user)):
    eng = _engine_for(user)
    kb = _knowledge_for(user)
    try:
        return kb.build(eng.notes, vault_root=str(paths.vault_dir(user.id)))
    finally:
        kb.close()


@router.post("/api/knowledge/ask")
def knowledge_ask(body: KSearch, user: User = Depends(current_user)):
    kb = _knowledge_for(user)
    try:
        return kb.ask(body.query, domain=body.domain, tags=body.tags, k=body.k)
    finally:
        kb.close()


@router.post("/api/knowledge/search")
def knowledge_search(body: KSearch, user: User = Depends(current_user)):
    kb = _knowledge_for(user)
    try:
        return kb.search_engine.search(body.query, domain=body.domain, tags=body.tags, k=body.k)
    finally:
        kb.close()


@router.get("/api/knowledge/metadata")
def knowledge_metadata(limit: int = 500, user: User = Depends(current_user)):
    kb = _knowledge_for(user)
    try:
        return {"notes": kb.metadata.all()[:limit]}
    finally:
        kb.close()


@router.get("/api/knowledge/graph")
def knowledge_graph(user: User = Depends(current_user)):
    kb = _knowledge_for(user)
    try:
        return {"edges": kb.relationships.graph(), "agents": kb.agents()}
    finally:
        kb.close()


@router.post("/api/knowledge/relationship")
def knowledge_add_relationship(body: Relationship, user: User = Depends(current_user)):
    kb = _knowledge_for(user)
    try:
        kb.relationships.add(body.src_id, body.dst_id, body.rel_type, body.weight)
        return {"ok": True}
    finally:
        kb.close()


# --- global knowledge graph --------------------------------------------------

@router.post("/api/kg/build")
def kg_build(user: User = Depends(current_user)):
    from ...knowledge_graph import build_graph
    from ...collab import CollabDB
    eng = _engine_for(user)
    db = CollabDB(_collab_db_path(user))
    try:
        return build_graph(_graph_path(user), eng.notes, db,
                           connector_dir=_connector_dir(user))
    finally:
        db.close()


@router.get("/api/kg/nodes")
def kg_nodes(type: str | None = None, limit: int = 500,
             user: User = Depends(current_user)):
    from ...knowledge_graph import GraphStore
    g = GraphStore(_graph_path(user))
    try:
        return {"nodes": g.nodes(type=type, limit=limit), "stats": g.stats()}
    finally:
        g.close()


@router.get("/api/kg/neighbors")
def kg_neighbors(id: str, rel: str | None = None, user: User = Depends(current_user)):
    from ...knowledge_graph import GraphStore
    g = GraphStore(_graph_path(user))
    try:
        return {"node": g.get_node(id), "neighbors": g.neighbors(id, rel=rel)}
    finally:
        g.close()


@router.get("/api/kg/traverse")
def kg_traverse(id: str, depth: int = 2, user: User = Depends(current_user)):
    from ...knowledge_graph import GraphStore
    g = GraphStore(_graph_path(user))
    try:
        return {"root": g.get_node(id), "reached": g.traverse(id, depth=depth)}
    finally:
        g.close()


# --- graph viz ---------------------------------------------------------------

@router.get("/api/graph/viz")
def graph_viz(user: User = Depends(current_user)):
    from ...product import to_graph
    kb = _knowledge_for(user)
    try:
        return to_graph(kb.metadata.all(), kb.relationships.graph())
    finally:
        kb.close()
