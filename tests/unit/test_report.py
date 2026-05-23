"""Unit tests for FR-5.7 — Financial report generator."""
from __future__ import annotations

import uuid
import pytest

from mcp_servers.document_catalog.finance.ledger_db import FinanceLedger
from mcp_servers.document_catalog.tools.report import handle_run_financial_report


@pytest.fixture
def ledger(tmp_path):
    db_path = str(tmp_path / "test_finance.duckdb")
    led = FinanceLedger(db_path)
    led.connect()
    yield led
    led.close()


def _seed_transactions(ledger):
    """Insert test data for report generation."""
    acct_id = ledger.upsert_account(
        bank_name="FNB",
        account_number_masked="****1234",
        account_type="cheque",
        seen_date="2025-01-01",
    )
    stmt_id = ledger.insert_statement(
        document_id="doc-001",
        account_id=acct_id,
        period_start="2025-01-01",
        period_end="2025-03-31",
        extraction_status="extracted",
        validation_status="passed",
    )
    txns = []
    # January transactions
    for i in range(10):
        txns.append({
            "transaction_id": str(uuid.uuid4()),
            "statement_id": stmt_id,
            "account_id": acct_id,
            "transaction_date": f"2025-01-{(i+1):02d}",
            "description_raw": f"Transaction {i}",
            "amount_cents": 5000 if i == 0 else -100 * (i + 1),
            "category": "salary" if i == 0 else "groceries",
            "merchant": "Employer" if i == 0 else "Pick n Pay",
            "source_document_id": "doc-001",
            "currency": "ZAR",
        })
    # February transactions
    for i in range(5):
        txns.append({
            "transaction_id": str(uuid.uuid4()),
            "statement_id": stmt_id,
            "account_id": acct_id,
            "transaction_date": f"2025-02-{(i+1):02d}",
            "description_raw": f"Feb Transaction {i}",
            "amount_cents": 10000 if i == 0 else -500 * (i + 1),
            "category": "salary" if i == 0 else "utilities",
            "merchant": "Employer" if i == 0 else "Eskom",
            "source_document_id": "doc-001",
            "currency": "ZAR",
        })
    ledger.insert_transactions(txns)
    return acct_id, stmt_id


class TestFinancialReport:
    async def test_monthly_summary_structure(self, ledger):
        _seed_transactions(ledger)
        result = await handle_run_financial_report(
            report_type="monthly_summary",
            start_date="2025-01-01",
            end_date="2025-03-31",
            ledger=ledger,
        )
        assert "error" not in result
        assert result["report_type"] == "monthly_summary"
        assert "period" in result
        assert "data_quality" in result
        assert "sections" in result

    async def test_annual_overview(self, ledger):
        _seed_transactions(ledger)
        result = await handle_run_financial_report(
            report_type="annual_overview",
            start_date="2025-01-01",
            end_date="2025-12-31",
            ledger=ledger,
        )
        assert "error" not in result
        assert result["report_type"] == "annual_overview"

    async def test_category_breakdown(self, ledger):
        _seed_transactions(ledger)
        result = await handle_run_financial_report(
            report_type="category_breakdown",
            start_date="2025-01-01",
            end_date="2025-03-31",
            ledger=ledger,
        )
        assert "error" not in result
        assert "sections" in result

    async def test_data_quality_included(self, ledger):
        _seed_transactions(ledger)
        result = await handle_run_financial_report(
            report_type="monthly_summary",
            start_date="2025-01-01",
            end_date="2025-03-31",
            ledger=ledger,
        )
        dq = result["data_quality"]
        assert "statements_included" in dq
        assert "statements_excluded" in dq
        assert "coverage_gaps" in dq

    async def test_markdown_format(self, ledger):
        _seed_transactions(ledger)
        result = await handle_run_financial_report(
            report_type="monthly_summary",
            start_date="2025-01-01",
            end_date="2025-03-31",
            format="markdown",
            ledger=ledger,
        )
        assert "markdown" in result
        assert "#" in result["markdown"]  # Should contain markdown headers

    async def test_invalid_report_type(self, ledger):
        result = await handle_run_financial_report(
            report_type="nonexistent",
            start_date="2025-01-01",
            end_date="2025-12-31",
            ledger=ledger,
        )
        assert "error" in result
