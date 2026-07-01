# Reliability Upgrades

Four upgrades that make answers trustworthy: real/NVIDIA embeddings, unified hybrid
retrieval, agent abstention (anti-hallucination), and an eval harness. All verified
by tests.

## 1. Embeddings — NVIDIA (free) by default, with fallbacks

`amy/knowledge/embeddings.py` → `make_embedder(provider, openai_key)` picks, in
**auto** order:

1. **NVIDIA NIM** — `nvidia/nv-embedqa-e5-v5` (1024-dim, **free** API) if a key is set
2. **OpenAI** — `text-embedding-3-small` if an OpenAI key is set
3. **sentence-transformers** — local `all-MiniLM-L6-v2` if installed
4. **hashing** — dependency-free fallback (always works; used in tests)

Enable NVIDIA embeddings:

```
AMY_NVIDIA_API_KEY=nvapi-xxxxxxxx          # free key from build.nvidia.com
AMY_EMBED_PROVIDER=auto                     # or 'nvidia' to force
# optional: AMY_NVIDIA_EMBED_MODEL=nvidia/nv-embedqa-e5-v5
```

NVIDIA's QA embeddings need an `input_type` — handled automatically (`passage` for
notes, `query` for searches via `embed_query`). No code change beyond the env var.

## 2. Unified hybrid retrieval

`amy/knowledge/retrieval.py` → `hybrid_search()` fuses **keyword overlap + embedding
cosine** and returns scores. Used by the conversational agents so every agent
retrieves the same way (was previously a weaker keyword-only path). The precomputed
knowledge vector store uses the real NVIDIA/OpenAI embeddings; the per-query
in-memory agent search uses the cheap local hashing embedder (no per-query API cost).

## 3. Abstention (anti-hallucination)

`DomainAgent.answer` now runs `is_relevant()` on its top hit; a **specialist agent
stays silent when nothing is relevant** instead of guessing. The master drops
abstained sections; if everyone abstains, the whole-vault `general` agent answers.

This directly fixes the bug you hit — "hi say my name" no longer triggers six agents
inventing contradictory answers. Verified:
- `test_specialist_abstains_general_answers` — off-topic query → only `general`.
- `test_only_relevant_domain_answers` — "how is my budget" → `finance` only, `family` stays silent.

Threshold: `AMY_ABSTAIN_EMB_THRESHOLD` (default 0.2); the keyword-overlap gate is the
primary signal.

## 4. Eval harness

`amy/eval/` → `run_eval(notes, cases)` measures **retrieval hit-rate** (did the
correct note appear in top-k). Run as a CLI:

```
python -m amy.eval.harness <vault_path> <cases.json>
# cases.json: [{"query": "...", "expect": "Finance/"}]
```

Use it to catch regressions whenever you change embeddings/retrieval. Covered by
`test_eval_hit_rate`.

## Tests

```bash
pytest tests/test_reliability.py -v
```

6 tests: embedder factory + fallback, NVIDIA embedder constructs, hybrid relevance
gate, specialist abstention + general fallback, single-relevant-domain routing,
eval hit-rate. Verified green (full suite 48/48).

## Not yet (next reliability steps)

- Route the conversational path through the **precomputed** NVIDIA-embedded store
  (so chat uses real embeddings, not just the local hashing one).
- Reranking of fused results; citation-faithfulness check (LLM-as-judge).
- Calibrated confidence (currently similarity/importance heuristic).
