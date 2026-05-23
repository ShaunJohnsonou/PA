"""Unit tests for FR-5.6 — Validation review workflow."""
from __future__ import annotations

import os
import uuid
import pytest

from mcp_servers.document_catalog.finance.ledger_db import FinanceLedger
from mcp_servers.document_catalog.catalog_db import CatalogDB, DocumentRow
from mcp_servers.document_catalog.audit import AuditLogger
from mcp_servers.document_catalog.tools.review import (
    handle_get_validation_issues,
    handle_override_validation,
    handle_set_document_status,
)


@pytest.fixture
def ledger(tmp_path):
    db_path = str(tmp_path / "test_finance.duckdb")
    led = FinanceLedger(db_path)
    led.connect()
    yield led
    led.close()


@pytest.fixture
def catalog(tmp_path):
    db_path = str(tmp_path / "test_catalog.sqlite")
    cat = CatalogDB(db_path)
    yield cat
    cat.close()


@pytest.fixture
def audit(tmp_path):
    db_path = str(tmp_path / "test_audit.sqlite")
    al = AuditLogger(db_path)
    yield al
    al.close()


def _seed_validation_data(ledger):
    """Insert test accounts, statements, and validation results."""
    doc_id = "doc-test-001"
    stmt_id = "stmt-test-001"
    acct_id = "acct-test-001"

    ledger.upsert_account(
        bank_name="FNB",
        account_number_masked="****1234",
        account_type="cheque",
        seen_date="2025-03-01",
    )

    ledger.insert_statement(
        statement_id=stmt_id,
        document_id=doc_id,
        account_id=acct_id,
        period_start="2025-03-01",
        period_end="2025-03-31",
        extraction_status="extracted",
        validation_status="needs_review",
    )

    # Insert some validation results
    ledger.insert_validation_result(
        validation_id="val-001",
        document_id=doc_id,
        statement_id=stmt_id,
        rule_name="balance_equation",
        passed=False,
        expected_value="12345",
        actual_value="12300",
        severity="error",
        notes="Closing balance mismatch",
    )
    ledger.insert_validation_result(
        validation_id="val-002",
        document_id=doc_id,
        statement_id=stmt_id,
        rule_name="evidence_completeness",
        passed=False,
        expected_value="100%",
        actual_value="95%",
        severity="warning",
        notes="5 transactions missing source_page",
    )
    ledger.insert_validation_result(
        validation_id="val-003",
        document_id=doc_id,
        statement_id=stmt_id,
        rule_name="currency_consistency",
        passed=True,
        severity="error",
    )
    return doc_id, stmt_id


class TestGetValidationIssues:
    async def test_get_issues_by_statement(self, ledger):
        doc_id, stmt_id = _seed_validation_data(ledger)
        result = await handle_get_validation_issues(
            statement_id=stmt_id, ledger=ledger
        )
        assert "error" not in result
        assert result["total_count"] == 2  # Only failed rules
        assert result["error_count"] == 1
        assert result["warning_count"] == 1

    async def test_get_issues_by_severity(self, ledger):
        _seed_validation_data(ledger)
        result = await handle_get_validation_issues(
            severity="error", ledger=ledger
        )
        assert result["total_count"] == 1
        assert result["issues"][0]["rule_name"] == "balance_equation"

    async def test_issues_include_explanation(self, ledger):
        doc_id, stmt_id = _seed_validation_data(ledger)
        result = await handle_get_validation_issues(
            statement_id=stmt_id, ledger=ledger
        )
        for issue in result["issues"]:
            assert "explanation" in issue
            assert len(issue["explanation"]) > 0


class TestOverrideValidation:
    async def test_override_success(self, ledger, audit):
        _seed_validation_data(ledger)
        result = await handle_override_validation(
            validation_id="val-001",
            reason="Bank confirmed closing balance is correct",
            overridden_by="shaun",
            ledger=ledger,
            audit=audit,
        )
        assert result["success"] is True

    async def test_override_nonexistent(self, ledger, audit):
        result = await handle_override_validation(
            validation_id="nonexistent",
            reason="test",
            ledger=ledger,
            audit=audit,
        )
        assert "error" in result


class TestSetDocumentStatus:
    async def test_exclude_document(self, catalog, audit):
        doc = DocumentRow(
            document_id="doc-001",
            sha256_hash="a" * 64,
            original_filename="test.pdf",
            canonical_path="originals/aa/aa/aaa.pdf",
            status="ingested",
        )
        catalog.insert_document(doc)
        result = await handle_set_document_status(
            document_id="doc-001",
            status="excluded",
            reason="Bad data",
            catalog=catalog,
            audit=audit,
        )
        assert result["success"] is True
        assert result["new_status"] == "excluded"

    async def test_invalid_status(self, catalog, audit):
        result = await handle_set_document_status(
            document_id="doc-001",
            status="invalid_status",
            catalog=catalog,
            audit=audit,
        )
        assert "error" in result
