# Future Self Agent

The Future Self Agent validates a decision against your **long-term goals** and
priorities, then speaks as advice from the version of you that has to live with
the choice. It's a guardrail: before you commit, it checks whether the decision
moves you toward what you said you wanted.

## How it works

Given a proposed decision (`title`, `category`, optional `reason`) it:

1. Loads your active (non-done) goals.
2. Matches the decision against each goal by **domain** (career, finance, …) and
   by **keyword overlap** between the decision text and the goal title.
3. Detects light **conflict signals** — words like *quit, stop, drop, abandon,
   delay, pause* aimed at a goal's domain.
4. Checks alignment with your stated **priorities** (from the Personality
   Engine).

## Verdicts

| Verdict    | Meaning                                                   |
|------------|----------------------------------------------------------|
| `aligned`  | the decision supports one or more active goals           |
| `conflict` | the decision may abandon or delay an active goal         |
| `neutral`  | no clear link to current goals                           |

## Output

```json
{
  "decision": "Accept senior engineer role", "category": "career",
  "verdict": "aligned",
  "supports": [{"goal": "Grow career", "why": "shares domain 'career'"}],
  "conflicts": [],
  "aligns_with_priorities": true,
  "active_goals_considered": 3,
  "future_self_says": "Go for it — this moves you toward Grow career. It also matches your stated priorities. Future you will likely thank you."
}
```

## API

```
POST /api/future-self/validate
{ "title": "Accept senior engineer role", "category": "career", "reason": "career growth" }
```

The endpoint pulls your priorities from the Personality Engine automatically.

## Design notes

This is a deterministic, transparent check — it explains every match and never
hides its reasoning. It's meant to prompt reflection, not to veto: you always
make the final call. It pairs naturally with the Decision Engine (log the
decision) and the Simulation Engine (model its consequences).
