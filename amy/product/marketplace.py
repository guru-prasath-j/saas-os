"""Agent Marketplace — enable/disable agents per user.

State lives in collab.db `agent_state(agent, enabled)`. Agents default to enabled;
the orchestrator consults `is_enabled` and skips disabled domains.
"""
from __future__ import annotations


class Marketplace:
    def __init__(self, collab_db):
        self.db = collab_db.conn

    def set_enabled(self, agent: str, enabled: bool):
        self.db.execute(
            "INSERT INTO agent_state (agent, enabled) VALUES (?,?) "
            "ON CONFLICT(agent) DO UPDATE SET enabled=excluded.enabled",
            (agent, 1 if enabled else 0))
        self.db.commit()

    def enable(self, agent: str):
        self.set_enabled(agent, True)

    def disable(self, agent: str):
        self.set_enabled(agent, False)

    def is_enabled(self, agent: str) -> bool:
        r = self.db.execute("SELECT enabled FROM agent_state WHERE agent=?", (agent,)).fetchone()
        return True if r is None else bool(r["enabled"])

    def disabled_set(self) -> set:
        return {r["agent"] for r in self.db.execute(
            "SELECT agent FROM agent_state WHERE enabled=0")}

    def listing(self, available_agents: list[str]) -> list[dict]:
        disabled = self.disabled_set()
        return [{"agent": a, "enabled": a not in disabled} for a in available_agents]
