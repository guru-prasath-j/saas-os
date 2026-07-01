# Personality Engine

The Personality Engine learns *how* you think and write, so the Digital Twin and
drafting features can imitate you. It produces a personality profile from
observable data only — no ML, no external calls — and every value is
explainable.

## What it learns

**Writing style** — quantified from your own note prose:

| Signal               | How it's measured                          |
|----------------------|--------------------------------------------|
| avg_sentence_length  | mean words per sentence                    |
| vocabulary_richness  | unique words ÷ total words                 |
| verbosity            | concise / balanced / elaborate (by length) |
| tone                 | formal / casual (share of long words)      |
| uses_bullets         | frequency of list markers                  |
| exclamation_rate / question_rate | punctuation per sentence       |

**Preferences** — your stored prefs (key/value), JSON-decoded where possible.

**Habits** — your most frequent activity kinds.

**Priorities** — top domains, scored by active goals (weighted heavily) plus
recent activity (weighted lightly).

**Decision pattern** — from the decisions table: average confidence mapped to a
decisiveness label (decisive / measured / cautious) and a follow-through rate
(share of decisions you actually resolved).

## Profile shape

```json
{
  "writing_style": {"avg_sentence_length": 12.4, "verbosity": "balanced",
                    "tone": "casual", "uses_bullets": true},
  "preferences": {"tone": "concise"},
  "habits": ["note", "study", "query"],
  "priorities": ["learning", "career", "projects"],
  "decision_pattern": {"avg_confidence": 0.72, "decisiveness": "decisive",
                       "follow_through_rate": 0.8}
}
```

## API

```
GET /api/personality
```

## Privacy

The engine reads only the user's own notes and collab DB. It runs fully locally
and never sends your writing anywhere — important because writing style is
personal data. It composes naturally with the project's existing private-folder
rules.
