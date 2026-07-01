# SaaS — Phase 3 (Bring-Your-Own-Key + Per-User Captures)

Each user's own OpenAI key now powers their AI, and the photo-capture feature is
tenant-scoped (photos land in that user's vault). No shared cloud key is ever used
for a user's content.

## BYO-key wiring

- `amy/llm.py` — `LLMRouter(openai_api_key=..., use_global_keys=...)`.
  - Personal app: `use_global_keys=True` → behaves as before (keys from `.env`).
  - SaaS: `use_global_keys=False` + the user's key → uses **only** that key for
    OpenAI; **Groq is disabled** (no per-user Groq key); local **Ollama** is still
    allowed (server's local model, used for sensitive queries); template fallback
    always works.
- `amy/engine.py` — `Engine(..., openai_api_key=..., use_global_keys=...)` passes it through.
- `amy/saas/tenancy.py` — `get_engine(user_id, openai_key)` builds the engine with
  `use_global_keys=False` and the user's key.
- `amy/saas/app.py`:
  - `POST /api/settings/openai-key` stores the key encrypted **and invalidates** the
    cached engine so the next call uses it. `DELETE` removes it.
  - `/api/query` and `/api/stats` decrypt the user's key per request (`_user_key`).
  - **No key set** → no cloud calls; the user gets local/template answers and is
    prompted (via `/api/me` → `has_openai_key`) to add a key.

**Privacy guarantee preserved:** sensitive queries still route to the local model
only (`pick(sensitive=True)` → Ollama/template), never to any cloud key — proven by
`tests/test_byok.py::test_sensitive_never_uses_cloud_openai`.

## Per-user captures

- `amy/captures.py` — `ingest(..., vault=..., openai_api_key=...)` and
  `analyze_image(..., api_key=...)`. `api_key=""` skips captioning entirely so a
  shared key is never used when a user has none.
- `amy/saas/app.py` — tenant-scoped `POST /api/captures`, `GET /api/captures`,
  `GET /api/captures/image` writing into / reading from the user's own
  `08_Captures/` folder, then indexing into that user's engine.

## Tests

```bash
pytest tests/test_byok.py tests/test_captures_saas.py -v
```

- `test_byok.py` — no-key → no cloud; user key → OpenAI used; sensitive → local only; shared Groq never used.
- `test_captures_saas.py` — a capture lands in the right user's vault, not another's; dedup by hash works.

## Mobile/web

The mobile app's existing capture + chat now work against the SaaS backend once the
user logs in and sets their OpenAI key (Phase 5 adds the login + key-entry screens).
