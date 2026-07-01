"""OperationalAgent base + a reference agent (OL-7).

Shows the contract every future domain agent (Career, Finance, Health, …) follows
to use the Operational Layer:

  * **subscribe** to the bus events it cares about
  * **read** live entity state for context (never poll connectors directly)
  * **publish** its own operational events + **upsert** its own entities

`CareerOpsAgent` is the template: it watches GitHub + calendar activity and emits
`career.application_updated`. It is intentionally small — it demonstrates wiring,
not career logic.
"""
from __future__ import annotations

from .models import EntityState


class OperationalAgent:
    """Base for event-driven domain agents on the Operational Layer."""
    name = "base"
    subscribes: list[str] = []     # event types this agent reacts to

    def __init__(self, ops):
        self.ops = ops             # an OperationalLayer

    def activate(self):
        """Wire this agent's subscriptions onto the one bus."""
        for etype in self.subscribes:
            self.ops.subscribe(etype, self.on_event)
        return self

    # --- helpers agents reuse ------------------------------------------
    def state(self, **filters):
        return self.ops.entities.list_entities(**filters)

    def publish(self, event_type: str, payload: dict):
        return self.ops.publish(event_type, payload, source=self.name)

    def upsert(self, entity: EntityState):
        return self.ops.entities.upsert_entity(entity)

    # --- override -------------------------------------------------------
    def on_event(self, event: dict):
        raise NotImplementedError


class CareerOpsAgent(OperationalAgent):
    """Reference agent: turns GitHub/calendar signals into career state + events."""
    name = "career"
    subscribes = ["github.NEW_RELEASE", "github.NEW_REPOSITORY", "calendar.NEW_EVENT"]

    def on_event(self, event: dict):
        etype = event.get("type", "")
        p = event.get("payload") or {}
        if etype.startswith("github."):
            # a release/new repo is a portfolio signal → update a career entity
            repo = p.get("repo") or p.get("title") or "portfolio"
            ent = EntityState(entity_id=f"career:portfolio:{repo}", kind="portfolio_item",
                              source="career", title=repo,
                              state={"signal": etype, "url": p.get("url", "")})
            self.upsert(ent)
            self.publish("career.application_updated",
                         {"reason": etype, "repo": repo})
        elif etype == "calendar.NEW_EVENT":
            title = (p.get("title") or "").lower()
            if any(w in title for w in ("interview", "screen", "onsite")):
                self.publish("career.interview_detected",
                             {"title": p.get("title"), "ts": p.get("ts", "")})
