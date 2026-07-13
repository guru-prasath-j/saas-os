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
from . import connector_tools  # noqa: F401  — GitHub/Plane/Meet tools (CONNECTOR COMPLETION)
from . import career_tools  # noqa: F401  — job search/portfolio/application tools (CAREER AUTOPILOT)
from . import life_tools  # noqa: F401  — health targets tool (LIFE AUTOPILOT)
from . import fraud_tools  # noqa: F401  — fraud detection tools (Phase 1)
from . import aml_tools  # noqa: F401  — AML monitoring tools (Phase 2)
from . import credit_tools  # noqa: F401  — Amy Credit Score tools (Phase 3)
from . import loan_tools  # noqa: F401  — Loan Underwriting tools (Phase 5)
