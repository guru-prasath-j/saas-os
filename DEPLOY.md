# Deploying the PersonalOS Public Demo

The **public** mode serves only `demo_vault/` (sample data) and hard-blocks finance,
family, payouts, write-back, and the scheduler — enforced server-side. Safe to host.

## Golden rule
Deploy **only the `_Jarvis` folder**. Your real Obsidian vault is the PARENT folder and
must NEVER be copied into the deploy. `.gitignore` already prevents committing secrets,
the index, and the parent vault.

## What the host needs
- `PERSONALOS_MODE=public`  (forces demo vault + restrictions)
- `OPENAI_API_KEY=sk-...`   (use a NEW key with a low usage cap)
- `AMY_PROVIDER_ORDER=openai`  (no Ollama in the cloud)
Retrieval uses lightweight keyword search (no torch/Chroma needed for the demo).

---

## Option A — Render.com (easiest, free tier)
1. Push `_Jarvis` to a NEW GitHub repo (its own repo, not inside the vault):
   ```
   cd _Jarvis
   git init && git add . && git commit -m "PersonalOS demo"
   git remote add origin https://github.com/<you>/personalos-demo.git
   git push -u origin main
   ```
   (Confirm `git status` does NOT list any personal vault files or `.env`.)
2. Render → New → Blueprint → pick the repo (uses `render.yaml`).
3. Add the secret `OPENAI_API_KEY` in the Render dashboard.
4. Deploy → you get `https://personalos-demo.onrender.com`.

## Option B — Railway
1. Push the repo (as above). Railway auto-detects the `Procfile`.
2. Add variables: `PERSONALOS_MODE=public`, `OPENAI_API_KEY`, `AMY_PROVIDER_ORDER=openai`.
3. Deploy → public URL.

## Option C — Hugging Face Spaces (Docker, free)
1. Create a Space → SDK: Docker.
2. Upload the `_Jarvis` contents; rename `Dockerfile.public` to `Dockerfile`.
3. Space Settings → Secrets → add `OPENAI_API_KEY`.
4. It builds and serves the demo at your Space URL.

## Option D — Any VPS / Docker
```
docker build -f Dockerfile.public -t personalos-demo .
docker run -p 8848:8848 -e OPENAI_API_KEY=sk-... personalos-demo
```

---

## Before you publish — checklist
- [ ] Repo contains NO real vault notes, NO `.env` with keys, NO `.personalos_prefs.json`.
- [ ] `OPENAI_API_KEY` is a fresh key with a **spend limit** set in the OpenAI dashboard.
- [ ] Visit `/api/meta` on the live URL → it must say `"mode":"public"`.
- [ ] Try `who do I pay this month` → must return the "public demo … disabled" message.
- [ ] Try `list my projects` → answers from demo data.

## Portfolio blurb you can use
> **PersonalOS — Multi-Agent AI Operating System.** A second-brain assistant with a master
> orchestrator routing to domain sub-agents over a knowledge vault, RAG retrieval, hybrid
> cloud/local LLM routing, a neural voice dashboard, and a Flutter client. (Live demo runs
> on sanitized sample data.)

Link it from your resume / LinkedIn / GitHub. The personal version stays on your machine.
