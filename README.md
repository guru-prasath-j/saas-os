# Amy — Multi-Agent Voice Assistant over the Obsidian Second Brain

A master agent (orchestrator) routes your questions to specialized sub-agents, each
scoped to part of the `personal` Obsidian vault, retrieves with a vector index, and
answers via a hybrid LLM — **Groq / OpenAI** for general queries, **local Ollama** for
sensitive finance data (cloud never sees it). Voice + a live dashboard sit on top.

> The vault is the brain (data). This app is the engine (agents + voice + dashboard).

## Agents
- **Master** (`agents/master.py`) — intent routing + guardrails. The only agent the user talks to.
- **Finance / Payout** — `02_Family/Sathish Appa/SBI Account`, `03_Finances` 🔒
- **Family / Business** — `02_Family` (Farm House, MJVR Investo, KMD Production)
- **Career / Job-Search** — `01_Profile`, `04_Career`, `06_Job_Search`
- **Knowledge** — `00_Home`, `07_Knowledge`

## Guardrails (enforced by the master)
- Never moves money / performs irreversible actions — refuses and defers to you.
- Sensitive (SBI / Sathish Appa) data → **local Ollama only**, never the cloud.
- Voice channel redacts account numbers & UPI ids — shown on screen, never spoken.

## Run
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # optional — runs offline without them too
cp .env.example .env                      # add ANTHROPIC_API_KEY, start Ollama (optional)

./run_cli.sh                              # terminal chat
./run_api.sh                              # http://127.0.0.1:8848  (dashboard + REST + /ws)
python3 -m tests.test_smoke               # offline smoke test
```

## Graceful degradation
- No `chromadb` → keyword (TF-IDF) retrieval.
- General: Groq → OpenAI → Ollama → template (first available wins).
- Sensitive: local Ollama only; never a cloud API.
So it boots and answers even with zero optional dependencies installed.

See `ROADMAP.md` for the phased plan.

## Deploy (Phase 9)
```bash
cp .env.example .env            # set ANTHROPIC_API_KEY / AMY_AUTH_TOKEN if desired
docker compose up --build       # starts Amy + Ollama; vault mounted at /vault
./scripts/setup_models.sh       # pull local model + prebuild index
```
- `GET /api/health` shows live providers (groq/openai/ollama), index backend, auth on/off.
- Set `AMY_AUTH_TOKEN` to require `Authorization: Bearer <token>` on `/api/*`; the
  websocket then expects `{ "token": "<token>" }` as its first message.
