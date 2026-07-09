"""Factory for EventStore instances with reactive agents already registered.

Fixes CLAUDE.md quirk 20: every emit site used to have to remember to call
register_reactive_agents(events, ctx) itself; forgetting it doesn't error —
the agent just silently never fires (found and fixed three times now: the
learning-feed router/sensor during that feature's build, and
amy/saas/routers/business.py's _emit_biz found while building this factory —
finance.ledger_entry_posted went through a bare EventStore there, so posting
a ledger entry via the business router never woke the compliance agent).

get_events() is now THE way to build an EventStore that should react — call
it once per logical unit of work (a request, a job run) and emit on the
result. Bare EventStore(cdb) construction stays valid for event types no
agent subscribes to (read-only stats/dead-letter queries, or a different
event consumer entirely, e.g. amy/operational/'s legacy trigger system) —
those sites are commented "intentionally bare" at the call site. Any bare
site emitting an AGENT_RELEVANT_EVENTS type now gets a loud dev-time warning
(EventStore._warn_zero_subscribers) instead of failing silently.

RISK A (circular imports): this module must NOT import amy.agents.reactive
or amy.automation at module level. The likely cycle is
events -> agents.reactive -> tools -> automation -> events (reactive agents
call tools.invoke; tools/AGENT_GATE wiring lives in amy.automation; automation
emits events). Both are imported lazily, inside get_events()'s body, exactly
matching the idiom already used in amy/saas/routers/finance.py's _emit_fin,
geo.py's _events_with_agents, and the learning-feed router/sensor. Verified
safe by: (1) `python -c "import amy.events.factory"` in isolation, and
(2) running the app cold. amy/events/store.py itself stays dependency-free.

RISK B (double registration): guarded at the EventStore level, not here —
see EventStore._registered_agent_keys / register_reactive_agents in
amy/agents/reactive.py. Calling get_events() twice for logically-the-same
collab_db connection is safe (each agent registers onto a given EventStore
instance at most once); it still creates a NEW EventStore object each call,
so prefer building one per unit of work rather than per emit.
"""
from __future__ import annotations


def get_events(user_id: str, collab_db, index_dir=None, user_email: str = "",
               ctx=None):
    """Build an EventStore for `collab_db` with reactive agents registered.

    ctx: reuse an existing JobCtx (e.g. from automation.jobs.build_ctx) when
    the caller already has one — cheaper, and keeps the same _extras (like
    jurisdictions) the caller set up. When omitted, a minimal ctx is built
    from user_id/collab_db/index_dir (index_dir defaults to
    amy.saas.paths.index_dir(user_id)).

    Agent-wiring failures degrade to a bare-but-warned store (never raises)
    — the event itself must still emit even if agents can't be reached.
    """
    from .store import EventStore
    es = EventStore(collab_db)
    try:
        from ..agents.reactive import register_reactive_agents
        if ctx is None:
            from ..automation.jobs import build_ctx
            from ..saas import paths as _saas_paths
            idx = index_dir or _saas_paths.index_dir(user_id)
            ctx = build_ctx(user_id, user_email, collab_db, idx, llm_router=None)
        register_reactive_agents(es, ctx)
    except Exception:
        pass   # agents are optional; the caller's emit() must still work
    return es
