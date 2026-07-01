# Amy PersonalOS — API Endpoints Reference

All endpoints require `Authorization: Bearer <token>` header (obtained from `/auth/login`).

Base URL: `http://localhost:8849`

---

## Auth

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | Sign in — returns `{"token": "..."}` |
| POST | `/auth/signup` | Create account |
| GET | `/api/me` | Current user info (email, has_openai_key) |
| POST | `/api/me/key` | Save OpenAI API key |
| GET | `/api/me/privacy` | Get private folder prefixes |
| POST | `/api/me/privacy` | Save private folder prefixes |
| GET | `/api/vault` | Vault stats (note count, last import) |
| POST | `/api/vault/settings` | Save vault path (cloud/local toggle) |
| GET | `/api/vault/settings` | Load vault path settings |

---

## Finance CFO

### Transactions
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/transactions` | List transactions (filters: search, category, account_type, since, until) |
| POST | `/api/finance/transactions` | Add a single transaction |
| DELETE | `/api/finance/transactions` | **Reset** — delete ALL transactions (keep accounts) |
| DELETE | `/api/finance/transactions/{tid}` | Delete one transaction |
| POST | `/api/finance/transactions/auto-categorize` | Run rule-based categorizer on all Uncategorized |
| PATCH | `/api/finance/transactions/{tid}/category` | Update category of one transaction |

### Overview & Forecast
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/overview` | Summary cards: income, expenses, balance, top categories |
| GET | `/api/finance/forecast/cashflow` | Next-week cashflow prediction |
| POST | `/api/finance/afford` | "Can I afford this?" — body: `{amount, description}` |
| GET | `/api/finance/goals` | Financial goals derived from budgets + income |

### Accounts
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/accounts` | List all accounts |
| POST | `/api/finance/accounts` | Add account — body: `{nickname, bank_name, account_type}` |
| DELETE | `/api/finance/accounts` | Delete all accounts |
| DELETE | `/api/finance/accounts/{aid}` | Delete account + all its transactions |
| GET | `/api/finance/accounts/{aid}/transactions` | Transactions for one account |

### Import — CSV / XLS / XLSX
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/accounts/{aid}/preview/csv` | Parse CSV/XLS — returns rows, **no DB write** |
| POST | `/api/finance/accounts/{aid}/upload/csv` | Parse + save CSV/XLS to DB |
| POST | `/api/finance/accounts/{aid}/column-map` | Save manual column mapping for this bank |
| GET | `/api/finance/bank-presets` | Named bank presets (HDFC, ICICI, SBI…) |
| GET | `/api/finance/column-maps` | Saved column maps per bank |

### Import — PDF
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/accounts/{aid}/preview/pdf` | Parse PDF — returns rows, **no DB write** |
| POST | `/api/finance/accounts/{aid}/upload/pdf` | Parse + save PDF to DB |

### Gmail Sync
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/sync/gmail` | Global sync — all savings/current accounts. Params: `since`, `until`, `max_messages` |
| POST | `/api/finance/accounts/{aid}/sync/gmail` | Per-account Gmail sync (legacy) |
| GET | `/api/finance/gmail/scope-status` | Check if Gmail OAuth scope is active |

### Deduplication
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/duplicates` | Scan and return duplicate groups (exact / near / fuzzy) |
| POST | `/api/finance/duplicates/resolve` | Delete selected duplicates — body: `{delete_ids: [...]}` |
| DELETE | `/api/finance/duplicates/auto` | Auto-remove all exact duplicates (keeps oldest) |

### Budgets
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/budgets` | List budgets with spending vs limit |
| POST | `/api/finance/budgets` | Set budget — body: `{category, monthly_limit}` |
| DELETE | `/api/finance/budgets` | Delete all budgets |

### Subscriptions
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/subscriptions` | List subscriptions |
| POST | `/api/finance/subscriptions` | Add subscription — body: `{name, amount, billing_cycle, next_due, category}` |
| PATCH | `/api/finance/subscriptions/{sid}` | Update subscription |
| DELETE | `/api/finance/subscriptions/{sid}` | Delete subscription |

### Investments
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/investments` | List investments with total value + P&L |
| POST | `/api/finance/investments` | Add investment — body: `{type, name, current_value, cost_basis}` |
| PATCH | `/api/finance/investments/{iid}` | Update investment |
| DELETE | `/api/finance/investments/{iid}` | Delete investment |

### Income Sources
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/finance/income` | List income sources |
| POST | `/api/finance/income` | Add income source — body: `{name, amount, frequency}` |
| DELETE | `/api/finance/income/{id}` | Delete income source |

### Calendar
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/finance/calendar/sync` | Push bill due-dates & subscription renewals to Google Calendar |

---

## Knowledge (Vault / Memory Lake)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/search` | Full-text + semantic search — param: `q` |
| POST | `/api/vault/import` | Import Obsidian vault ZIP |
| GET | `/api/vault` | Vault stats |
| POST | `/api/knowledge/build` | Rebuild knowledge graph from vault notes |
| GET | `/api/knowledge/graph` | Graph data (nodes + edges) for visualization |
| GET | `/api/knowledge/tags` | All tags in vault with counts |
| GET | `/api/knowledge/folders` | Folder tree |
| GET | `/api/knowledge/files` | All files with metadata |
| GET | `/api/knowledge/file` | Read one file — param: `path` |

---

## Memory & Journal

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/memory/summary` | AI-generated memory summary across all notes |
| GET | `/api/memory/entities` | Extracted people, places, orgs, events |
| POST | `/api/memory/sync` | Sync memory from latest notes |
| GET | `/api/memory/journal` | Journal entries |
| POST | `/api/memory/journal` | Add journal entry |
| GET | `/api/memory/reindex` | Rebuild memory index |

---

## Goals

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/goals` | List goals with milestones |
| POST | `/api/goals` | Create goal — body: `{title, domain, target_date}` |
| POST | `/api/goals/{gid}/milestone` | Add milestone to a goal |
| PATCH | `/api/goals/{gid}/milestone/{mid}` | Toggle milestone done |
| DELETE | `/api/goals/{gid}` | Delete goal |

---

## Habits

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/habits` | List habits + streak data |
| POST | `/api/habits` | Add habit — body: `{name, frequency}` |
| POST | `/api/habits/{hid}/log` | Log today's completion |
| DELETE | `/api/habits/{hid}` | Delete habit |

---

## Decisions

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/decisions` | Decision history |
| POST | `/api/decisions` | Log decision — body: `{title, reason, category}` |
| GET | `/api/decisions/analysis` | Pattern analysis of past decisions |
| GET | `/api/decisions/recommendations` | AI recommendations based on decision history |

---

## Timeline

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/timeline` | Life events timeline — param: `range` (day/week/month) |

---

## Intelligence (Digital Twin & AI)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/intelligence/twin` | Digital twin summary |
| GET | `/api/intelligence/personality` | Personality profile derived from notes |
| GET | `/api/intelligence/predict` | Predictions for next week/month |
| POST | `/api/intelligence/future-self` | Validate decision against long-term goals — body: `{title, category}` |
| POST | `/api/intelligence/autopilot` | Run autonomous planning cycle |

---

## Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List available domain agents + enabled status |
| POST | `/api/agents/{name}/toggle` | Enable or disable an agent |
| POST | `/api/master` | Send message — multi-agent routing, returns AI response with sources |

---

## Learning & Review

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/intelligence/learn` | Learning trend analysis from notes |
| GET | `/api/intelligence/reflect` | Weekly reflection summary |
| GET | `/api/srs/cards` | Due flashcards (spaced repetition) |
| POST | `/api/srs/review` | Submit card review — body: `{card_id, rating}` |

---

## People & Entities

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/entities` | All extracted people, places, organisations |
| GET | `/api/entities/{name}` | Entity detail with all mentions |

---

## Portfolio (Public Profile)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/product/portfolio` | Public portfolio data (projects, skills — no private data) |
| GET | `/api/product/suggestions` | AI suggestions for improving profile |

---

## Connectors (Google OAuth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/connectors/google/status` | Check if Google account is connected |
| GET | `/api/connectors/google/auth-url` | Get OAuth redirect URL |
| GET | `/api/connectors/google/callback` | OAuth callback (redirect target) |
| POST | `/api/connectors/google/sync` | Sync Google Calendar + Tasks |
| DELETE | `/api/connectors/google/disconnect` | Revoke Google access |

---

## Events

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events` | Calendar events |
| POST | `/api/events` | Add event |
| DELETE | `/api/events/{eid}` | Delete event |

---

## Notifications

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/notifications` | Recent notifications |
| POST | `/api/notifications/mark-read` | Mark all read |

---

## Account Aggregator (AA)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/connectors/aa/status` | AA connection status |
| POST | `/api/connectors/aa/toggle` | Enable/disable AA data access |

---

## LLM Routing

Amy routes LLM calls automatically:

| Provider | Model | Used For |
|----------|-------|----------|
| NVIDIA NIM | `nvidia/nemotron-3-ultra-550b-a55b` | Gmail enrichment, PDF parsing, primary chat |
| OpenAI | `gpt-4o-mini` | Per-user key fallback |
| Groq | `llama-3.3-70b-versatile` | Secondary fallback |
| Ollama | `llama3.2` (local) | Sensitive/private data — never leaves device |
| Template | deterministic | Last-resort fallback (always works) |

**Rule:** Notes marked `sensitive` (private folders) → Ollama only, never cloud.

---

## Running the Server

```bash
# Start
python -m uvicorn amy.saas.app:app --host 0.0.0.0 --port 8849

# Kill existing (Windows)
Get-NetTCPConnection -LocalPort 8849 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

Environment: copy `.env.example` to `.env` and fill in API keys.
