"""Knowledge layer: structured metadata + embeddings + semantic search +
relationship graph + confidence scoring, over an Obsidian vault.

    from amy.knowledge import KnowledgeBase
    kb = KnowledgeBase("/data/knowledge")     # creates metadata.db, vector.db, agent_registry.db
    kb.build(notes)                            # markdown -> metadata -> embeddings -> graph
    kb.ask("how are my finances?")             # -> {answer, sources, confidence, chunks}
"""
from .base import KnowledgeBase
from .db import KnowledgeDBs
from .embeddings import (HashingEmbedder, OpenAIEmbedder, NvidiaEmbedder, STEmbedder,
                         EmbeddingEngine, make_embedder, cosine)
from .retrieval import hybrid_search, is_relevant
from .metadata import MetadataEngine, note_id
from .relationships import RelationshipEngine
from .search import SemanticSearch
from . import confidence
