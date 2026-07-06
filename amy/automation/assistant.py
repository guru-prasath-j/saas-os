"""AI assistant console — one chat endpoint that can operate the app.

A small deterministic tool loop over the formal tool registry (amy/tools):
the LLM is shown the registry catalog and must answer with strict JSON —
either {"tool": "...", "args": {...}} to act, or {"final": "..."} to reply.
Tool results are fed back until it produces a final answer (max 6 steps).

Actor semantics (trust boundary):
- Chat is a live human instruction, so read/write tools run with
  actor="human" (direct execution — same as clicking the UI).
- destructive-risk tools are still invoked with actor="agent", so once the
  approval gate is installed they park in the Approval Inbox for an explicit
  second confirmation instead of executing mid-conversation.
"""
from __future__ import annotations

import json
import re

from .executors import JobCtx

_MAX_STEPS = 6


# ---------------------------------------------------------------------------
# Catalog + invocation via the registry
# ---------------------------------------------------------------------------

def _catalog() -> str:
    from .. import tools
    lines = []
    for t in tools.list_tools():
        props = t["params"].get("properties", {})
        required = set(t["params"].get("required", []))
        args = ", ".join(
            f"{name}{'' if name in required else '?'}: {spec.get('type', 'any')}"
            for name, spec in props.items())
        risk = "" if t["risk"] == "read" else f" [{t['risk']}]"
        lines.append(f"- {t['name']}({args}){risk} — {t['description']}")
    return "\n".join(lines)


def _call_tool(ctx: JobCtx, name: str, args: dict):
    from .. import tools
    risk = tools.get_tool(name).risk
    actor = "agent" if risk == tools.RISK_DESTRUCTIVE else "human"
    return tools.invoke(ctx, name, args, actor=actor)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    return (
        "You are Amy, the user's personal-finance operating assistant. You can "
        "call tools to read data and take safe actions on the user's explicit "
        "instruction. Currency amounts are shown per the user's locale.\n\n"
        f"Tools:\n{_catalog()}\n\n"
        "Respond with EXACTLY ONE JSON object, no prose around it and never "
        "more than one tool call per response:\n"
        '  {"tool": "<name>", "args": {...}}   to call a tool\n'
        '  {"final": "<answer for the user>"}  when done\n'
        "Call tools to get real numbers before answering; never invent data. "
        "Tools marked [destructive] are queued for the user's approval rather "
        "than executed immediately — say so when you use one. Keep the final "
        "answer short and concrete."
    )


def _parse_step(raw: str) -> dict:
    """Take the FIRST complete JSON object in the response — models sometimes
    emit several tool calls at once; we execute one per turn."""
    raw = re.sub(r"```(?:json)?", "", raw or "").strip()
    decoder = json.JSONDecoder()
    idx = raw.find("{")
    while idx != -1:
        try:
            obj, _end = decoder.raw_decode(raw, idx)
        except Exception:
            idx = raw.find("{", idx + 1)
            continue
        if isinstance(obj, dict) and ("tool" in obj or "final" in obj):
            return obj
        idx = raw.find("{", idx + 1)
    return {"final": raw.strip() or "I couldn't produce an answer."}


def chat(ctx: JobCtx, message: str, history: list[dict] | None = None) -> dict:
    """Run the tool loop. Returns {reply, steps: [{tool, args, result}...]}"""
    if ctx.llm is None:
        return {"reply": "No LLM provider is available right now.", "steps": []}

    transcript: list[str] = []
    for h in (history or [])[-6:]:
        role = h.get("role", "user")
        transcript.append(f"{role}: {h.get('content', '')}")
    transcript.append(f"user: {message}")

    steps: list[dict] = []
    for _ in range(_MAX_STEPS):
        prompt = "\n".join(transcript) + "\nassistant:"
        raw = None
        for _attempt in range(2):   # one retry on provider timeout/flake
            try:
                raw, _provider = ctx.llm.generate(
                    _system_prompt(), prompt, sensitive=False)
                break
            except Exception:
                continue
        if raw is None:
            return {"reply": "The LLM provider timed out — please try again "
                             "in a moment.", "steps": steps}
        step = _parse_step(raw)

        if "final" in step:
            return {"reply": str(step["final"]), "steps": steps}

        tool = str(step.get("tool") or "")
        args = step.get("args") or {}
        try:
            result = _call_tool(ctx, tool, args)
            ok = True
        except Exception as exc:
            result = {"error": str(exc)}
            ok = False
        steps.append({"tool": tool, "args": args, "ok": ok, "result": result})
        transcript.append(f"assistant: {json.dumps({'tool': tool, 'args': args})}")
        transcript.append(
            "tool_result: " + json.dumps(result, default=str)[:3000])

    return {"reply": "I hit the tool-step limit before finishing — "
                     "try a more specific question.", "steps": steps}
