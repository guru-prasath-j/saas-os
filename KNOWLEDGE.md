# Knowledge Layer — Structured AI Knowledge from Markdown

Turns raw notes into structured, searchable knowledge. Clean, modular,
independently testable (`amy/knowledge/`), and offline-capable (deterministic local
embedder by default; OpenAI optional). **Original markdown is never modified.**

```
Markdown -> Metadata -> Embeddings -> Relationship graph -> Agents -> LLM
```

## Components

| File | Role |
|---|---|
| `db.py` | The three internal DBs: `metadata.db`, `vector.db`, `agent_registry.db` |
| `metadata.py` | Per-note metadata: id, title, summary, domain, subdomains, entities, keywords, tags, importance, created_at, updated_at, embedding_id |
| `chunking.py` | Split notes into retrieval-sized chunks |
| `embeddings.py` | `HashingEmbedder` (offline), `OpenAIEmbedder` (optional), `cosine`, `EmbeddingEngine` |
| `search.py` | Semantic search: metadata filter → similarity → chunks → context |
| `confidence.py` | Confidence % from similarity + corroborating sources |
| `relationships.py` | Graph edges: `references` (wikilinks), `related_to` (shared terms), manual `depends_on` |
| `base.py` | `KnowledgeBase` facade |

## Internal databases (separate files)

- **metadata.db** — `notes` (all metadata fields) + `relationships` (the graph)
- **vector.db** — `chunks` (text + embedding per chunk)
- **agent_registry.db** — `agents` (one runtime config per detected domain)

## Search flow (spec)

```
query
  ↓  metadata filter (domain / tags)
  ↓  embedding similarity (cosine over candidate chunks)
  ↓  retrieve top-k chunks
  ↓  return context + sources + confidence%
```

## Confidence + sources

Every `ask()` / `search()` returns a `confidence` percentage (from the top/average
similarity, nudged up when multiple sources corroborate) and the `sources` used.

## Use directly

```python
from amy.vault import load_notes
from amy.knowledge import KnowledgeBase, OpenAIEmbedder
from amy.llm import LLMRouter

notes = load_notes("/path/to/vault")
kb = KnowledgeBase("/data/knowledge",
                   embedder=OpenAIEmbedder("sk-..."),   # or default HashingEmbedder()
                   llm=LLMRouter())                      # optional, for answering
kb.build(notes, vault_root="/path/to/vault")
print(kb.ask("how are my finances and what depends on them?"))
# -> {answer, sources, confidence, chunks, model}
```

## API (SaaS, per user)

```
POST /api/knowledge/build         -> {notes, chunks, relationships, domains, embedder}
POST /api/knowledge/ask           -> {answer, sources, confidence, chunks}
POST /api/knowledge/search        -> {context, chunks, sources, confidence}
GET  /api/knowledge/metadata      -> all note metadata
GET  /api/knowledge/graph         -> relationship edges + agent registry
POST /api/knowledge/relationship  -> add a manual edge (e.g. depends_on)
```

The embedder is chosen per user: OpenAI if they set a key, else the local hashing
embedder. Building uses the **free heuristic** for summaries (no per-note LLM cost);
the LLM is only used to answer in `ask()`.

## Tests (offline)

```bash
pytest tests/test_knowledge.py -v
```

Covers: three DBs populated, all metadata fields, semantic search + confidence,
domain-filtered search, wikilink + manual `depends_on` relationships, and
deterministic embeddings.

## Notes

- **Not overengineered:** stdlib `sqlite3`, plain functions/classes, no extra services.
- **Scale later:** swap `HashingEmbedder` → `OpenAIEmbedder`, and `vector.db` →
  pgvector/Chroma; move `build` to a background job for large vaults.
