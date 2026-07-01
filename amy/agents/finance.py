"""Finance CFO agent — vault RAG + optional structured finance.db context injection."""
from __future__ import annotations

from .base import SubAgent, AgentResult


class FinanceAgent(SubAgent):
    """Personal CFO domain agent.

    In vault-RAG mode answers from finance-tagged notes.
    When finance_db_path is provided, prepends actual numbers (balance, spend,
    upcoming bills, budget status) to the context so the LLM answers with
    real figures rather than vault prose alone.
    """
    name = "finance"
    can_write = True
    write_kinds = ["log a budget entry", "record an expense", "note a savings target"]
    persona = (
        "You are the personal Finance CFO agent. "
        "Answer from the financial context provided, which includes actual balances, "
        "spending by category, upcoming bills, and budget status where available. "
        "Be specific with numbers. Flag overspend or risk clearly. "
        "Never instruct money transfers — that is always the human's action. "
        "Treat all figures as sensitive."
    )

    def __init__(self, retriever, llm, finance_db_path: str | None = None):
        super().__init__(retriever, llm)
        self._finance_db_path = finance_db_path

    def _finance_context(self) -> str:
        if not self._finance_db_path:
            return ""
        import os
        if not os.path.exists(self._finance_db_path):
            return ""
        try:
            from ..finance import FinanceEngine
            fe = FinanceEngine(self._finance_db_path)
            try:
                return fe.context_block()
            finally:
                fe.close()
        except Exception:
            return ""

    def answer(self, query: str, retrieval_query: str | None = None,
               extra_context: str | None = None) -> AgentResult:
        finance_ctx = self._finance_context()
        combined = "\n\n".join(p for p in (finance_ctx, extra_context) if p)
        return super().answer(
            query,
            retrieval_query=retrieval_query,
            extra_context=combined or None,
        )
