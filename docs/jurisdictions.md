# Jurisdiction packs

A jurisdiction pack is **one JSON file** in `amy/jurisdictions/` that teaches
Amy everything country-specific: currency + number formatting, fiscal year,
calendar systems, obligation presets (rates, thresholds, deadlines),
compliance deadline calendar, enabled financing models, and default values
screening profiles. **No jurisdiction logic exists in Python** — the loader
(`amy/jurisdictions/__init__.py`) reads whatever packs are present.

Shipped packs: `uae.json`, `us.json`, `india.json`.

## Adding jurisdiction #4 — JSON only, zero code changes

1. Copy the template below to `amy/jurisdictions/<id>.json`.
2. Fill it in. Run the validator:
   ```python
   from amy.jurisdictions import load_pack
   load_pack("<id>")     # raises PackError listing every problem
   ```
3. Restart the server. The pack now appears in `GET /api/jurisdictions`,
   can be selected as home/active in `POST /api/settings/locale`, feeds the
   deadline calendar, obligations engine, FX display, and briefings.

That's the entire process. The only time code is ever needed is a **new
calendar system** (e.g. a lunisolar calendar) — one adapter class in
`amy/calendars/` — because a calendar is an algorithm, not configuration.
Jurisdictions using `gregorian`, `hijri`, or `fiscal` (any start month)
need JSON only.

## Template

```json
{
  "id": "example",
  "name": "Example Country",
  "currency": {
    "code": "EXC",
    "symbol": "E",
    "grouping": "western",
    "decimals": 2
  },
  "fiscal_year": { "calendar": "fiscal", "start_month": 1 },
  "calendar_systems": ["gregorian"],
  "default_language": "en",
  "financing_models": ["amortized_interest"],
  "default_screening_profiles": ["budget_discipline"],
  "obligation_presets": [
    {
      "id": "example_estimated_tax",
      "name": "Estimated tax",
      "description": "What this obligation is, in plain language.",
      "kind": "scheduled_estimate",
      "calendar_system": "fiscal",
      "calendar_config": { "start_month": 1 },
      "eligible_account_types": ["savings", "current"],
      "versions": [
        {
          "effective_from": "2025-01-01",
          "effective_to": null,
          "rate": null,
          "rate_basis": "user_estimated_annual_tax",
          "wealth_threshold": { "amount": 1000, "basis": "annual_tax_liability", "currency": "EXC" },
          "schedule": [
            { "month": 4, "day": 15, "cumulative_portion": 0.5, "label": "H1 payment" },
            { "month": 10, "day": 15, "cumulative_portion": 1.0, "label": "H2 payment" }
          ]
        }
      ]
    }
  ],
  "compliance_deadlines": [
    {
      "id": "annual_filing",
      "name": "Annual filing deadline",
      "calendar_system": "gregorian",
      "applies_to": "individual",
      "versions": [
        { "effective_from": "2025-01-01", "effective_to": null, "month": 4, "day": 30 }
      ]
    }
  ],
  "tax_facts": [],
  "seasonal_notes": [],
  "disclaimer": "Figures produced from this pack are ESTIMATES, not professional tax advice. Verify the rates and dates shown against official sources."
}
```

## Concepts

- **Effective-date versioning** — anything that changes yearly (rates,
  thresholds, dates) lives in a `versions` array; each version carries
  `effective_from`/`effective_to`. `resolve_version()` picks the entry
  active on the computation date, so packs can hold history and future
  rules simultaneously. This extends the `rate_table` pattern already used
  for GST slabs.
- **Obligation preset `kind`s** — `scheduled_estimate` (installment
  schedule toward a user-estimated annual figure), `wealth_rate` (rate ×
  qualifying wealth held above a threshold for `holding_period_years`, e.g.
  a 2.5% lunar-year wealth obligation), `recurring_commitment` (fixed share
  of income on a recurrence), `annual_cap_tracking` (progress toward a
  fixed annual cap).
- **Calendars** — `calendar_system` + optional `calendar_config` are passed
  straight to `amy.calendars.get_calendar()`. Schedules use the pack's
  calendar: `{"month": 9, "day": 1}` under `hijri` resolves to a Gregorian
  date automatically.
- **eligible_account_types** — obligation math only ever sees these account
  types. Custodial accounts are excluded by the engine regardless of what a
  pack says.
- **Disclaimers** — every pack MUST carry one; obligation and deadline API
  responses attach it, and the UI shows amounts as estimates with the
  pack's rules visible for verification.

## Multi-currency

Accounts and transactions carry an optional native `currency` (NULL = the
user's home-pack currency). `amy/fx.py` converts via daily-cached rates —
default source is the static seed `amy/jurisdictions/fx_seed.json` (edit it
or plug a live source into `FxConverter(source=...)`).
`GET /api/finance/overview/fx` returns per-currency and per-jurisdiction
totals converted to the user's base currency.
