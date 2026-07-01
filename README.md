# PersonalOS / Amy

A self-hosted personal AI operating system — a FastAPI SaaS app + single-page frontend.
Multi-tenant codebase (JWT auth, SQLite-per-user data), but currently run for one user.

> See `CLAUDE.md` for the full technical map of this repo (architecture, DB schema,
> API routes, known quirks) and `SAAS.md` for the multi-tenant design.

## Primary active feature: Finance CFO mode

Import bank statements (CSV/XLS/PDF), sync transaction emails from Gmail, get
automatic categorization, and track budgets, subscriptions, and investments —
all from one dashboard.

- **Import**: CSV/XLS/XLSX and PDF bank statements, with a preview step before
  anything is saved. Falls back to an LLM (NVIDIA Nemotron) for PDFs that don't
  parse via table extraction.
- **Gmail sync**: reads bank-alert emails, extracts transactions, and can
  auto-poll in the background — resumable per account, so closing the app
  doesn't create a gap in your history.
- **Categorization**: rule-based first (instant, free), with a batched LLM
  fallback for merchants no rule recognizes. Account-type aware (a credit-card
  payment isn't income).
- **Budgets & subscriptions**: set limits manually, or let Amy suggest them —
  subscriptions detected from recurring charges in your transaction history,
  budgets suggested from your income, location, and current spend.
- **"Can I afford this?"**: checks a proposed purchase against your cashflow,
  budget headroom, and active savings goals before answering.

## Run

```bash
pip install -r requirements.txt -r requirements-saas.txt
cp .env.example .env               # fill in NVIDIA_API_KEY / GOOGLE_CLIENT_ID etc.
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849
```

Open `http://localhost:8849`, sign up, and connect Google (for Gmail sync) from
the Account tab. See `.env.example` for all supported keys — everything
degrades gracefully if a given provider's key is missing (LLM calls fall back
down the provider chain: NVIDIA → OpenAI → Groq → local Ollama → a
deterministic template).

## Tests

```bash
pytest tests/ -v
```

Includes tenant-isolation tests (one user can never see another's data) — these
must pass before touching the multi-tenant `saas/` layer.

## More docs

| Doc | Covers |
|---|---|
| `CLAUDE.md` | Full technical reference — architecture, schema, routes, known quirks |
| `SAAS.md` | Multi-tenant design, phases, what to harden before a real launch |
| `API_ENDPOINTS.md` | Full endpoint list across all routers |
| `OPERATIONS.md` | Deployment / ops runbook |
| `PRIVACY.md` | Private-folder / sensitive-data handling |
