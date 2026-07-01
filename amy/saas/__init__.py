"""Multi-tenant SaaS layer for PersonalOS / Amy (Phase 1).

Adds user accounts, JWT auth, and per-user vaults on top of the existing core
(vault loader, index, engine, agents). The single-user personal app is untouched;
this is a separate FastAPI app (amy.saas.app:app) you run when you want SaaS mode.
"""
