"""Tool registry — the enumerated, risk-classified catalog of every action
an AI may take. Importing this package registers all built-in tools.

    from amy.tools import invoke, list_tools
    result = invoke(ctx, "list_budgets", {}, actor="agent")
"""
from .registry import (
    RISK_DESTRUCTIVE, RISK_READ, RISK_WRITE,
    Tool, ToolError, get_tool, invoke, list_tools, register_tool,
    validate_args,
)
from . import builtin  # noqa: F401  — registers all built-in tools on import
from . import mcp_bridge  # noqa: F401  — registers the MCP source bridge tools
