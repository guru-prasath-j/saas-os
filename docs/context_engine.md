# Context Engine

Understands the user's current **mode** and reprioritizes agents, goals, and
recommendations accordingly. Builds on the Executive Agent (no rewrite).

## Overview
`ContextEngine` detects (or is told) the active mode, then returns a context
profile: which domains/agents to emphasize, which goals to surface, and mode-aware
recommendations.

## Architecture
```
ContextEngine
  detect()/set_mode()  -> current mode (pref override > time-of-day heuristic)
  profile(mode)        -> Executive.reprioritize_domains + prioritize_goals,
                          re-ordered to put the mode's domains first
```

## Context types
| Mode | Emphasized domains | Focus |
|---|---|---|
| work | career, projects, finance | ship work + career goals |
| learning | learning, knowledge | study & skill growth |
| weekend | family, health, personal | rest, family, health |
| vacation | family, health | disconnect, minimal work |
| meeting | career, projects | meeting prep + follow-ups |
| focus | (none) | single deep-work goal, no noise |

Detection: an explicit `set_mode` override wins; otherwise heuristic — weekend →
`weekend`, weekday 09:00–18:00 → `work`, else `focus`. (meeting / vacation are set
explicitly; calendar-driven meeting detection is future work.)

## Completion: 100% of scope
| Capability | Status |
|---|---|
| Mode detection + override | ✅ |
| Reprioritize agents | ✅ suggested_agents per mode |
| Reprioritize goals | ✅ mode-domain goals first |
| Reprioritize recommendations | ✅ mode-aware recommendations |

## APIs
```
GET  /api/context              # profile for the auto-detected (or set) mode
POST /api/context/mode {"mode":"focus"}   # override the mode
```

## Example flows
```
POST /api/context/mode {"mode":"weekend"}
GET  /api/context
-> { mode:"weekend", priority_domains:["family","health",…],
     suggested_agents:["family_agent","health_agent","personal_agent"],
     top_goals:[…family/health goals first…],
     recommendations:["Deprioritize work/finance; surface family & health.", "Next: …"] }
```

## Technical debt
- Detection is heuristic (clock/weekend) + explicit override; no calendar-driven
  meeting detection or learned routines yet.
- Profile reprioritizes/advises but doesn't enforce (e.g. doesn't auto-disable
  agents) — pairs naturally with Autopilot if you want enforcement.
