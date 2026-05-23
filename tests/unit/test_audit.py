"""Unit tests for TR-5.3 — Audit logger."""
from __future__ import annotations

import csv
import json
import os
import pytest
from mcp_servers.document_catalog.audit import AuditLogger, redact_sensitive


@pytest.fixture
def audit_logger(tmp_path):
    """Provide a fresh AuditLogger in a temp directory."""
    db_path = str(tmp_path / "audit.sqlite")
    logger = AuditLogger(db_path)
    yield logger
    logger.close()


class TestRedaction:
    """Tests for sensitive value redaction."""

    def test_api_key_redacted(self):
        args = {"query": "test", "api_key": "sk-secret-123"}
        result = redact_sensitive(args)
        assert result["query"] == "test"
        assert result["api_key"] == "[REDACTED]"

    def test_nested_redaction(self):
        args = {"config": {"password": "secret", "host": "localhost"}}
        result = redact_sensitive(args)
        assert result["config"]["password"] == "[REDACTED]"
        assert result["config"]["host"] == "localhost"

    def test_no_sensitive_keys(self):
        args = {"query": "hello", "limit": 10}
        result = redact_sensitive(args)
        assert result == args

    def test_original_not_mutated(self):
        args = {"api_key": "secret"}
        _ = redact_sensitive(args)
        assert args["api_key"] == "secret"


class TestAuditLogger:
    """Tests for AuditLogger class."""

    def test_log_entry_created(self, audit_logger):
        audit_id = audit_logger.log_tool_call(
            tool_name="query_transactions",
            tool_args={"date_from": "2025-01-01"},
            result_summary="returned 47 transactions",
        )
        assert audit_id is not None
        recent = audit_logger.get_recent(limit=1)
        assert len(recent) == 1
        assert recent[0]["tool_name"] == "query_transactions"
        assert recent[0]["result_summary"] == "returned 47 transactions"

    def test_error_logged(self, audit_logger):
        audit_id = audit_logger.log_tool_call(
            tool_name="query_transactions",
            tool_args={},
            result_summary="",
            error="DuckDB connection failed",
        )
        recent = audit_logger.get_recent(limit=1)
        assert recent[0]["error"] == "DuckDB connection failed"

    def test_args_redacted_in_log(self, audit_logger):
        audit_logger.log_tool_call(
            tool_name="test",
            tool_args={"api_key": "sk-secret", "query": "hello"},
            result_summary="ok",
        )
        recent = audit_logger.get_recent(limit=1)
        stored_args = json.loads(recent[0]["tool_args"])
        assert stored_args["api_key"] == "[REDACTED]"
        assert stored_args["query"] == "hello"

    def test_export_csv(self, audit_logger, tmp_path):
        # Insert a few entries
        for i in range(5):
            audit_logger.log_tool_call(
                tool_name=f"tool_{i}",
                tool_args={},
                result_summary=f"result {i}",
            )
        csv_path = str(tmp_path / "export.csv")
        count = audit_logger.export_csv(csv_path)
        assert count == 5
        assert os.path.isfile(csv_path)
        with open(csv_path, "r") as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert len(rows) == 6  # header + 5 data rows

    def test_get_recent_ordering(self, audit_logger):
        audit_logger.log_tool_call("first", {}, "r1")
        audit_logger.log_tool_call("second", {}, "r2")
        audit_logger.log_tool_call("third", {}, "r3")
        recent = audit_logger.get_recent(limit=2)
        assert len(recent) == 2
        assert recent[0]["tool_name"] == "third"  # most recent first

    def test_langfuse_trace_id_stored(self, audit_logger):
        audit_logger.log_tool_call(
            "test", {}, "ok",
            langfuse_trace_id="trace-abc-123",
        )
        recent = audit_logger.get_recent(limit=1)
        assert recent[0]["langfuse_trace_id"] == "trace-abc-123"
