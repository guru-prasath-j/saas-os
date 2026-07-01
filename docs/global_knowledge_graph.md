# Global Knowledge Graph

A cross-source entity graph connecting notes, emails, calendar events, tasks,
goals, and memories with typed relationships. Separate from the note-only
relationship graph in the knowledge layer (that one is left untouched).

## Overview
`build_graph()` auto-generates nodes + typed edges from all sources into a per-user
`graph.db`. `GraphStore` provides querying, neighbor lookup, and BFS traversal.

## Architecture
```
build_graph(notes, collab_db, connector_dir)
   nodes:  note / goal / task / memory / email / calendar
   edges (auto):
     belongs_to : task -> goal
     depends_on : goal -> goal           (goal_deps)
     blocks     : goal -> goal           (unmet dependency blocks dependent)
     related_to : note <-> note          (wiki-links + shared-term overlap)
     supports   : note -> goal           (note text mentions the goal)
        -> GraphStore (graph.db)  -> nodes() / neighbors() / traverse()
```

## Node types
`note`, `email`, `calendar`, `task`, `goal`, `memory`.

## Relationship types
`depends_on`, `related_to`, `supports`, `blocks`, `belongs_to`.

## Features
- **Automatic relationship generation** — `build_graph()` derives all edges.
- **Relationship updates** — rebuild (idempotent reset) or `add_node` / `add_edge`.
- **Querying** — `nodes(type=...)`, `get_node(id)`, `neighbors(id, rel=...)`, `stats()`.
- **Traversal** — `traverse(id, depth)` BFS with per-node distance.

## APIs
```
POST /api/kg/build                       # (re)build the graph
GET  /api/kg/nodes?type=goal&limit=      # nodes (+ stats)
GET  /api/kg/neighbors?id=goal:g1&rel=   # direct neighbors (optional rel filter)
GET  /api/kg/traverse?id=goal:g1&depth=2 # reachable nodes within N hops
```
Node ids are `"{type}:{ref}"`, e.g. `goal:abc123`, `note:Finance/budget.md`.

## Technical debt
- `related_to` uses shared-term overlap (O(n²) over notes; fine for personal-scale
  vaults, cap/threshold applied). `supports`/`blocks` are heuristic.
- Email/calendar nodes come from the connectors (local provider unless Google token
  set) and aren't yet entity-resolved (no person/org extraction).
- Overlaps conceptually with the knowledge-layer note graph + `/api/graph/viz`;
  unifying the two visualizations is future work.
