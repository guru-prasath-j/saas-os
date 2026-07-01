# PKOS — Personal Knowledge Operating System

Turns an uploaded Obsidian vault into an AI Personal OS. Built as a clean,
self-contained, independently-testable layer (`amy/pkos/`) that reuses the existing
vault loader and (optionally) the LLM router. No FastAPI/DB coupling inside the core.

## Architecture (matches the spec)

```
User query
  ↓
MasterAgent        amy/pkos/master.py   — route, invoke agents, MERGE responses
  ↓
IntentRouter       amy/pkos/router.py   — single + MULTI intent
  ↓
Agent Registry     amy/pkos/registry.py — one runtime DomainAgent per domain (no files)
  ↓
Domain Agent       amy/pkos/registry.py — retrieves within its domain's notes
  ↓
Vault Knowledge    amy/vault.py + amy/pkos/domains.py, analyzer.py
```

## Spec coverage

| Requirement | Implementation |
|---|---|
| 1. Vault upload (scan .md, hierarchy) | `amy/saas/app.py` `/api/vault/import` + `amy/vault.py` |
| 2. Vault analyzer (title, headings, tags, summary) | `amy/pkos/analyzer.py` → `analyze_vault()` |
| 3. Domain detection (money→finance, gym→health, …) | `amy/pkos/domains.py` `detect()` (content keywords + folder fallback) |
| 4. Dynamic agent registry (runtime, no files) | `amy/pkos/registry.py` `build_registry()` / `DomainAgent` |
| 5. Intent router (single + multi-intent) | `amy/pkos/router.py` `IntentRouter.route()` |
| 6. Master agent (route, invoke, merge) | `amy/pkos/master.py` `MasterAgent.handle()` |
| 7. Source attribution | every response returns combined, de-duplicated `sources` |

## API (SaaS, per logged-in user)

```
POST /api/ask            -> multi-intent answer: {answer, domains, sections[], sources[]}
GET  /api/domains        -> detected domains + note counts (the agent registry)
GET  /api/vault/analyze  -> per-note {title, headings, tags, summary}
```

`/api/ask` example response:

```json
{
  "query": "how's my money and my parents",
  "domains": ["finance", "family"],
  "answer": "**finance**\n…\n\n**family**\n…",
  "sections": [
    {"domain": "finance", "answer": "…", "sources": ["Money/budget.md"]},
    {"domain": "family",  "answer": "…", "sources": ["People/parents.md"]}
  ],
  "sources": ["Money/budget.md", "People/parents.md"]
}
```

## Use it directly (no web layer)

```python
from amy.vault import load_notes
from amy.pkos import build_pkos
from amy.llm import LLMRouter

notes = load_notes("/path/to/vault")
master, registry, domain_map = build_pkos(notes, llm=LLMRouter())   # llm optional
print(master.handle("summarize my money and my parents"))
```

## Domains

Default content keywords map to: `finance, family, career, health, learning`
(extensible — pass your own `keywords` dict to `build_pkos`/`detect`). A note that
matches no keyword falls back to its folder-derived domain, so nothing is lost.

## Tests (offline, no API key)

```bash
pytest tests/test_pkos.py -v
```

Covers: heading extraction, heuristic summary, content-based domain detection,
folder fallback, single + multi-intent routing, and master merge with combined
source attribution.

## Design notes

- **Not overengineered:** plain functions + small classes, no dynamic file
  generation — agents are runtime configs.
- **Independently testable:** the core takes plain `Note` objects + an optional LLM;
  no FastAPI/DB needed to test it.
- **Summary is free by default** (first real paragraph); pass an LLM to `summarize`
  for richer summaries when you want to spend tokens.
