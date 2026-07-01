# Memory Lake — the infinite-memory operational layer

The Memory Lake turns the Obsidian vault into PIOS's long-term memory: everything
the system learns is written into the vault as dated, linked markdown, synced by
Obsidian, and read back to ground future replies. The vault is the **source of
truth**; SQLite is a disposable index that can be rebuilt from it.

```
event → Journaler → 00_Daily + 09_Memory (markdown) → Obsidian Sync
                                  │
                          watcher re-reads + embeds
                                  │
        chat recall ◄── memory lake ──► weekly consolidation
                                  │
                       reindex rebuilds SQLite
```

All of it is offline (stdlib + hashing embedder), deterministic, and idempotent
via in-note `<!-- eid:... -->` markers, so cloud-sync re-reads never duplicate.

## Layers (built in 6 phases)

**Phase 1 — Journaler (`memory/writer.py`).** `MemoryWriter` appends every event
to a daily note `00_Daily/YYYY-MM-DD.md` and creates atomic notes
`09_Memory/<Type> - <slug>.md` for significant items (decisions, captures, new
goals, repos/releases). Frontmatter + idempotency markers; vault-as-truth.

**Phase 2 — Journaling bridge (`memory/journal.py`).** `attach_journal()` (push)
subscribes the writer to a live event bus; `JournalSync.sync()` (pull) reads the
persisted `events` table and writes anything not yet journaled — the reliable
path for the per-request SaaS layer. A cursor is cached in `prefs`.

**Phase 3 — Auto-linking (`memory/entities.py`).** `EntityIndex` is built from
vault note titles + goals and injects `[[wikilinks]]`/`#tags` into entries that
mention known entities, so Obsidian's graph view fills in with real edges. Links
only point at entities that exist (no dangling links).

**Phase 4 — Recall into chat (`memory/recall.py`).** `MemoryRecall` searches only
the journaled folders (+ recent summaries), relevance-gated, and the master
prepends the result to the responding agent's context. This is what makes replies
*depend on* memory. Wired additively via an optional `extra_context` param on
`SubAgent.answer()`.

**Phase 5 — Consolidation (`memory/consolidate.py`).** `Consolidator` reads the
week's daily notes and writes a rollup `01_Weekly/YYYY-Www.md` (activity counts,
decisions, new goals, top tags/links). `patterns()` exposes the same data so
other engines can consume the learning signal. Keeps memory navigable as it
grows.

**Phase 6 — Vault-as-truth reindex (`memory/reindex.py`).** `VaultReindex.scan()`
inventories the vault, `verify(db)` reconciles vault eids against the events table
(drift detection), and `rebuild_decisions(db)` reconstructs structured decision
rows from the markdown — so if SQLite is wiped, state is recoverable from
Obsidian alone.

## API

| Method & path                        | Purpose                              |
|--------------------------------------|--------------------------------------|
| `POST /api/memory/sync`              | journal pending events into the vault|
| `GET  /api/memory/daily?date=`       | read a daily note                    |
| `GET  /api/memory/recall?q=`         | recall relevant memory (chat uses this) |
| `POST /api/memory/consolidate`       | build/refresh this week's rollup     |
| `GET  /api/memory/patterns`          | weekly learning signal as data       |
| `GET  /api/memory/verify`            | vault ↔ SQLite drift report          |
| `POST /api/memory/reindex`           | rebuild structured rows from the vault |

## Linking your Obsidian vault

Set `AMY_VAULT` to your Obsidian vault root (the folder containing `.obsidian/`).
The Journaler creates `00_Daily/`, `09_Memory/`, and `01_Weekly/` there. Turn on
Obsidian Sync (or iCloud/Dropbox/Git) on that folder — because PIOS writes plain
markdown, sync provides the "infinite" storage for free, and the 2s vault watcher
picks up synced changes automatically.

## Design guarantees

* **Vault-as-truth** — markdown is canonical; SQLite is rebuildable (Phase 6).
* **Idempotent** — eid markers make every write safe to repeat.
* **Offline & deterministic** — no network, no LLM required; hashing embedder.
* **Additive** — no existing module was rewritten; recall is opt-in via a
  defaulted parameter, so all prior behavior is preserved.
