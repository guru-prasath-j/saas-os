# Universal Search

One ranked search across every source. A thin aggregator over existing services —
no new index or retrieval implementation.

## Overview
`UniversalSearch.search(query)` queries all sources, merges + ranks the hits, and
returns a paginated, source-attributed, confidence-scored result.

## Architecture
```
User -> UniversalSearch -> source adapters -> aggregator -> response
            vault     (hybrid_search: semantic + keyword)
            email     (connector)
            calendar  (connector)
            tasks     (connector)
            memories  (conversation summaries)
            goals     (collab.db)
```

## Completion: 100% of scope
| Feature | Status |
|---|---|
| Semantic + keyword (hybrid) | ✅ reuses `knowledge/retrieval.hybrid_search` |
| Confidence scoring | ✅ from top score |
| Source attribution | ✅ every hit tagged with `source` + `ref` |
| Filters | ✅ `sources=[...]` |
| Pagination | ✅ `limit` / `offset` |

## Data sources
Obsidian vault, Gmail, Calendar, Tasks, Memories (conversations), Goals.
Connector sources are private-mode only and use the local provider unless a Google
token is configured.

## API
```
POST /api/search   {"query": "...", "sources": ["vault","email"], "limit": 10, "offset": 0}
-> {query, total, limit, offset, confidence, sources_searched, results:[{source,title,ref,score}]}
```

## Technical debt
- Cross-source scores aren't globally normalized (vault uses hybrid scores; other
  sources use fixed weights) — ranking is approximate across source types.
- Connector matching is keyword-only (vault is hybrid). Add embeddings for connectors later.
- Reuses the local hashing embedder for the vault adapter (consistent with chat).
