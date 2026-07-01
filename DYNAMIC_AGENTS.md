# Dynamic agents (SaaS / multi-vault mode)

By default PersonalOS uses the **tailored** agents hardcoded for your personal
vault (`home`, `profile`, `family`, `finances`, ...). For a SaaS where each user
uploads their *own* folders, those fixed agents don't fit. **Dynamic mode** builds
one agent per top-level folder in whatever vault is loaded — automatically.

## Enable it

Set an environment variable (or add to `.env`):

```
AMY_DYNAMIC_AGENTS=1
```

Then start the backend as usual. Flag **off** = your normal personal agents
(nothing changes). Flag **on** = agents are generated from the folders present.

## What it does

- Scans the vault's top-level folders (skipping `_Amy`, `attachments`, `.obsidian`, etc.).
- Creates a `GenericAgent` per folder, scoped to that folder, with an auto-written persona.
  - `01_Profile` → a `profile` agent · `Work` → a `work` agent · `Recipes` → a `recipes` agent.
- Always adds a `general` agent scoped to the **whole vault** as a safe fallback.
- Routing (`DynamicClassifier`): matches your words against folder names first; if
  unclear, asks the LLM to pick a folder from the discovered list; otherwise falls
  back to `general` (which searches everything, so a misroute still finds the note).

## Privacy in dynamic mode

The hardcoded sensitivity markers (`SENSITIVE_OWNERS = ["Sathish Appa"]`, the SBI
path) are personal to you and simply won't match other users. The **generic** rule
still works: any note with `sensitive` in its frontmatter `tags` is treated as
sensitive and (per the LLM router) kept on the local model. For SaaS, let each user
mark their private folders/notes with the `sensitive` tag, or add a per-user
"private folders" setting later.

## Files

- `amy/dynamic.py` — `discover_domains`, `GenericAgent`, `GeneralAgent`,
  `DynamicClassifier`, `build_agents`.
- `amy/agents/master.py` — chooses dynamic vs. hardcoded agents based on the flag.
- `amy/config.py` — `DYNAMIC_AGENTS` flag.

## Try it on your own vault

```
# Windows
set AMY_DYNAMIC_AGENTS=1


```

Your folders (`00_Home`, `01_Profile`, ... `08_Captures`) become agents
`home, profile, family, finances, career, resources, job-search, knowledge,
captures` + `general`. Ask "what projects have I built?" and watch it route to the
`profile`/`projects`-style folder agent. Unset the variable to return to normal.

## Next for true SaaS

This makes agents per-vault, but multi-tenancy still needs: user accounts, one
vault namespace per user (not one global `config.VAULT`), per-user vector
collections, and the zip-upload import flow. See `PersonalOS_Mobile_Architecture.md`
and the SaaS notes.
