"""Tool registry — the single catalog of everything an AI is allowed to do.

Each tool declares: name, description, a JSON-schema for its params, a
handler, and a risk level:

  read         — inspects data, never changes it
  write        — changes data reversibly (add/update rows, notes, events)
  destructive  — money-affecting, deleting, or external sends

The registry is the one choke point for machine-initiated actions:
- invoke(ctx, name, args, actor=...) validates args against the schema.
- actor="agent" + risk write/destructive routes through AGENT_GATE (set by
  the approval-queue layer) instead of executing — the trust boundary is
  architectural, not per-caller good manners. actor="human" (an explicit
  user click/API call) executes directly.

Handlers receive (ctx, args) where ctx is the automation JobCtx.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

RISK_READ = "read"
RISK_WRITE = "write"
RISK_DESTRUCTIVE = "destructive"
_RISKS = (RISK_READ, RISK_WRITE, RISK_DESTRUCTIVE)


class ToolError(ValueError):
    """Bad tool name or arguments — safe to show to the model/user."""


@dataclass
class Tool:
    name: str
    description: str
    params: dict                       # JSON-schema-style object schema
    risk: str
    handler: Callable                  # handler(ctx, args) -> result
    extras: dict = field(default_factory=dict)

    def catalog_entry(self) -> dict:
        return {"name": self.name, "description": self.description,
                "params": self.params, "risk": self.risk}


_REGISTRY: dict[str, Tool] = {}

# Set by the approval layer (amy/automation): callable(ctx, tool, args) -> dict.
# When None, agent-invoked write tools execute directly (pre-R3 behavior).
AGENT_GATE: Callable | None = None


def register_tool(name: str, description: str, params: dict | None = None,
                  risk: str = RISK_READ, extras: dict | None = None):
    """extras: opt-in flags the tier router (amy/automation/executors.py)
    reads to hard-pin tiers beyond the risk level alone — e.g.
    extras={"external": True} for a tool that sends something to a
    third-party system (a GitHub comment, a Plane task) and so must stay at
    tier 2 even if AMY_AGENT_WRITE_TIER softens ordinary internal writes."""
    if risk not in _RISKS:
        raise ValueError(f"unknown risk {risk!r}")

    def deco(fn):
        _REGISTRY[name] = Tool(
            name=name, description=description,
            params=params or {"type": "object", "properties": {}},
            risk=risk, handler=fn, extras=extras or {})
        return fn
    return deco


def get_tool(name: str) -> Tool:
    t = _REGISTRY.get(name)
    if t is None:
        raise ToolError(f"unknown tool {name!r}")
    return t


def list_tools(risk: str | None = None) -> list[dict]:
    out = [t.catalog_entry() for t in _REGISTRY.values()]
    if risk:
        out = [t for t in out if t["risk"] == risk]
    return sorted(out, key=lambda t: t["name"])


# ---------------------------------------------------------------------------
# Lightweight JSON-schema validation (object schemas: required + type checks)
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_args(tool: Tool, args: dict | None) -> dict:
    args = dict(args or {})
    props = tool.params.get("properties", {})
    required = tool.params.get("required", [])
    for r in required:
        if r not in args or args[r] is None:
            raise ToolError(f"{tool.name}: missing required param {r!r}")
    for key, value in list(args.items()):
        if key not in props:
            raise ToolError(f"{tool.name}: unexpected param {key!r}")
        if value is None:
            continue
        expected = props[key].get("type")
        py = _TYPE_MAP.get(expected)
        if py is None:
            continue
        # ints satisfy "number"; also coerce numeric strings the way LLMs emit them
        if expected in ("number", "integer") and isinstance(value, str):
            try:
                value = float(value) if expected == "number" else int(value)
                args[key] = value
            except ValueError:
                raise ToolError(f"{tool.name}: param {key!r} must be {expected}")
        if expected == "integer" and isinstance(value, bool):
            raise ToolError(f"{tool.name}: param {key!r} must be integer")
        if not isinstance(args[key], py):
            raise ToolError(f"{tool.name}: param {key!r} must be {expected}")
    return args


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

def invoke(ctx, name: str, args: dict | None = None, actor: str = "human"):
    """Validate and run a tool.

    actor="agent" and risk!=read → routed through AGENT_GATE (approval queue)
    when the gate is installed. The handler can read the actor from
    ctx._extras["tool_actor"] (e.g. approve_action refuses non-human actors).
    """
    tool = get_tool(name)
    clean = validate_args(tool, args)
    try:
        ctx._extras["tool_actor"] = actor
    except Exception:
        pass
    if actor == "agent" and tool.risk != RISK_READ and AGENT_GATE is not None:
        return AGENT_GATE(ctx, tool, clean)
    return tool.handler(ctx, clean)
