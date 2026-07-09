"""Job discovery agent: searches and crawls job portals for relevant postings."""
from __future__ import annotations
from ...llm import LLMRouter

def discover_jobs(llm: LLMRouter, query: str) -> list[dict]:
    """Disabled (CAREER AUTOPILOT phase, docs/AGENT_PLAN.md): this used to
    ask the LLM to "simulate" job postings — fabricated titles/companies/
    URLs, zero real data. Real job discovery now lives behind the jobspy
    MCP connector (amy/tools/career_tools.py's job_search tool /
    amy.automation.orchestrator's career plan template) — this legacy path
    returns nothing rather than inventing results. CareerAgent's other
    intents (matching/resume tailoring/pipeline analytics) are unaffected.
    """
    return []
