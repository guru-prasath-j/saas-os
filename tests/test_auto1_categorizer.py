"""Tests for Automation 1: FinanceCategorizer."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def finance_engine(tmp_path):
    from amy.finance.engine import FinanceEngine
    fe = FinanceEngine(str(tmp_path / "finance.db"))
    yield fe
    fe.close()


def _mock_llm(assignments: dict) -> MagicMock:
    """Build a mock LLM that returns the given {description_fragment: category} map."""
    import json
    llm = MagicMock()

    def _generate(system, prompt, **kwargs):
        # Parse ids from the prompt, assign from keyword match
        import re
        ids = re.findall(r'"id":\s*"([^"]+)"', prompt)
        descs = re.findall(r'"description":\s*"([^"]+)"', prompt)
        result = []
        for tid, desc in zip(ids, descs):
            cat = "Other"
            for kw, c in assignments.items():
                if kw.lower() in desc.lower():
                    cat = c
                    break
            result.append({"id": tid, "category": cat})
        return json.dumps(result), "mock-model"

    llm.generate.side_effect = _generate
    return llm


class TestFinanceCategorizer:
    def test_auto_categorize_empty_db(self, finance_engine):
        from amy.finance.categorizer import FinanceCategorizer
        llm = MagicMock()
        result = FinanceCategorizer().auto_categorize(finance_engine, llm)
        assert result["categorized"] == 0
        assert result["skipped"] == 0
        llm.generate.assert_not_called()

    def test_auto_categorize_assigns_categories(self, finance_engine):
        from amy.finance.categorizer import FinanceCategorizer
        import datetime as _dt
        today = _dt.date.today().isoformat()
        # Add uncategorized transactions
        finance_engine.add_transaction(-200, "Uncategorized", merchant="Swiggy food order", date=today)
        finance_engine.add_transaction(-50, "Uncategorized", merchant="Metro card", date=today)
        finance_engine.add_transaction(-500, "Uncategorized", merchant="Amazon shopping", date=today)

        llm = _mock_llm({"swiggy": "Food & Dining", "metro": "Transport", "amazon": "Shopping"})
        result = FinanceCategorizer().auto_categorize(finance_engine, llm)

        assert result["categorized"] == 3
        assert result["skipped"] == 0
        # Verify DB was updated
        txns = finance_engine.list_transactions(limit=10)
        categories = {t["category"] for t in txns}
        assert "Food & Dining" in categories
        assert "Transport" in categories
        assert "Shopping" in categories
        assert "Uncategorized" not in categories

    def test_skips_already_categorized(self, finance_engine):
        from amy.finance.categorizer import FinanceCategorizer
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.add_transaction(-200, "Food & Dining", merchant="Restaurant", date=today)
        finance_engine.add_transaction(-50, "Uncategorized", merchant="Cab", date=today)

        llm = _mock_llm({"cab": "Transport"})
        result = FinanceCategorizer().auto_categorize(finance_engine, llm)

        # Only the uncategorized one should be processed
        assert result["categorized"] == 1
        # Already-categorized one stays unchanged
        categorized_txns = [t for t in finance_engine.list_transactions()
                            if t["category"] == "Food & Dining"]
        assert len(categorized_txns) == 1

    def test_llm_failure_degrades_gracefully(self, finance_engine):
        from amy.finance.categorizer import FinanceCategorizer
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.add_transaction(-100, "Uncategorized", merchant="Something", date=today)

        llm = MagicMock()
        llm.generate.side_effect = RuntimeError("LLM offline")
        result = FinanceCategorizer().auto_categorize(finance_engine, llm)

        assert result["categorized"] == 0
        assert result["skipped"] == 1
        # Transaction stays uncategorized
        txns = finance_engine.list_transactions()
        assert txns[0]["category"] == "Uncategorized"

    def test_invalid_category_response_skipped(self, finance_engine):
        from amy.finance.categorizer import FinanceCategorizer
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.add_transaction(-100, "Uncategorized", merchant="Test", date=today)

        llm = MagicMock()
        llm.generate.return_value = ('[{"id": "fake_id", "category": "NotACategory"}]', "mock")
        result = FinanceCategorizer().auto_categorize(finance_engine, llm)

        # The real ID won't match fake_id, so it'll be skipped
        assert result["categorized"] == 0

    def test_by_category_counts(self, finance_engine):
        from amy.finance.categorizer import FinanceCategorizer
        import datetime as _dt
        today = _dt.date.today().isoformat()
        for i in range(3):
            finance_engine.add_transaction(-100, "Uncategorized",
                                           merchant=f"Swiggy order {i}", date=today)

        llm = _mock_llm({"swiggy": "Food & Dining"})
        result = FinanceCategorizer().auto_categorize(finance_engine, llm)

        assert result["categorized"] == 3
        assert result["by_category"]["Food & Dining"] == 3

    def test_batch_prompt_parsing(self):
        """Unit test the _build_prompt and _parse_response functions."""
        from amy.finance.categorizer import _build_prompt, _parse_response
        import json
        txns = [
            {"id": "abc123", "merchant": "Zomato", "notes": "", "amount": -250},
            {"id": "def456", "merchant": "Uber", "notes": "", "amount": -150},
        ]
        prompt = _build_prompt(txns)
        assert "abc123" in prompt
        assert "Zomato" in prompt

        response = json.dumps([
            {"id": "abc123", "category": "Food & Dining"},
            {"id": "def456", "category": "Transport"},
        ])
        parsed = _parse_response(response)
        assert len(parsed) == 2
        assert parsed[0]["category"] == "Food & Dining"

    def test_parse_response_handles_markdown_fences(self):
        from amy.finance.categorizer import _parse_response
        raw = '```json\n[{"id": "x1", "category": "Shopping"}]\n```'
        parsed = _parse_response(raw)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "x1"

    def test_parse_response_handles_bad_json(self):
        from amy.finance.categorizer import _parse_response
        assert _parse_response("not json at all") == []
        assert _parse_response("{}") == []

    def test_scheduler_accepts_llm_param(self, finance_engine, tmp_path):
        """generate_and_store passes llm through to categorizer."""
        from amy.events.scheduler import generate_and_store
        from amy.collab.db import CollabDB
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.add_transaction(-300, "Uncategorized", merchant="Netflix", date=today)

        cdb = CollabDB(str(tmp_path / "collab.db"))
        llm = _mock_llm({"netflix": "Entertainment"})
        generate_and_store(cdb,
                           finance_db_path=str(finance_engine.path),
                           llm=llm)
        cdb.close()

        # Transaction should now be categorized
        txns = finance_engine.list_transactions()
        assert txns[0]["category"] == "Entertainment"

    def test_scheduler_no_llm_skips_categorization(self, finance_engine, tmp_path):
        """Digest runs fine when no llm is provided; transactions stay uncategorized."""
        from amy.events.scheduler import generate_and_store
        from amy.collab.db import CollabDB
        import datetime as _dt
        today = _dt.date.today().isoformat()
        finance_engine.add_transaction(-300, "Uncategorized", merchant="Netflix", date=today)

        cdb = CollabDB(str(tmp_path / "collab.db"))
        generate_and_store(cdb, finance_db_path=str(finance_engine.path))
        cdb.close()

        txns = finance_engine.list_transactions()
        assert txns[0]["category"] == "Uncategorized"
