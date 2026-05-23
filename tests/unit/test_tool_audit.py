"""Unit tests for TR-5.1 — Tool registration audit."""
from __future__ import annotations

import pytest
from mcp_servers.document_catalog.tool_audit import (
    DISALLOWED_TOOL_PATTERNS,
    audit_registered_tools,
)


class TestToolAudit:
    """Tests for audit_registered_tools()."""

    def test_allowed_tools_pass(self):
        """Standard PA tools should pass audit with no violations."""
        tools = [
            "ingest_document", "list_documents", "extract_document",
            "get_document_page", "index_document", "search_documents",
            "delete_document", "query_transactions", "get_financial_coverage",
            "get_transaction_evidence", "run_spending_analysis",
            "find_anomalies", "run_financial_report",
            "get_validation_issues", "override_validation",
            "set_document_status",
        ]
        violations = audit_registered_tools(tools)
        assert violations == []

    def test_denied_tool_blocked(self):
        """A tool matching a disallowed pattern should be flagged."""
        tools = ["ingest_document", "execute_shell", "list_documents"]
        violations = audit_registered_tools(tools)
        assert len(violations) == 1
        assert "execute_shell" in violations[0]

    def test_pattern_matching_case_insensitive(self):
        """Pattern matching should be case-insensitive."""
        tools = ["My_File_System_Tool"]
        violations = audit_registered_tools(tools)
        assert len(violations) == 1
        assert "file_system" in violations[0]

    def test_multiple_violations(self):
        """Multiple denied tools should each produce a violation."""
        tools = ["execute_shell", "arbitrary_sql", "list_directory"]
        violations = audit_registered_tools(tools)
        assert len(violations) == 3

    def test_empty_tool_list(self):
        """Empty tool list should produce no violations."""
        assert audit_registered_tools([]) == []
