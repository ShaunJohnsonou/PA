"""TR-5.2 — Append-only audit logger for financial operations.

Provides an immutable audit trail of every tool call processed by the MCP
server.  The log is stored in a dedicated SQLite database and supports
CSV export for offline analysis.

Design constraints:
- **Append-only**: no UPDATE or DELETE operations exist in this module.
- **WAL mode**: allows concurrent reads without blocking writes.
- **Sensitive-field redaction**: API keys, passwords, and tokens are
  scrubbed before storage.
"""
from __future__ import annotations

import copy
import csv
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Reason: These dictionary keys commonly hold secrets. Any value under one
# of these keys is replaced with '[REDACTED]' before the tool arguments
# are persisted to the audit log.
_SENSITIVE_KEYS = frozenset({"api_key", "secret_key", "password", "token", "authorization"})


def redact_sensitive(args: dict) -> dict:
    """Return a deep copy of *args* with sensitive values replaced.

    Keys matching :data:`_SENSITIVE_KEYS` (case-insensitive) have their
    values replaced with ``'[REDACTED]'``.  Nested dicts are processed
    recursively.

    Args:
        args: The original tool arguments dictionary.

    Returns:
        A new dictionary with sensitive values masked.
    """
    redacted = copy.deepcopy(args)
    _redact_in_place(redacted)
    return redacted


def _redact_in_place(obj: dict) -> None:
    """Recursively redact sensitive values in *obj* in-place."""
    for key in list(obj.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            obj[key] = "[REDACTED]"
        elif isinstance(obj[key], dict):
            _redact_in_place(obj[key])


class AuditLogger:
    """Append-only SQLite audit log for MCP tool calls.

    Usage::

        audit = AuditLogger("/path/to/vault/audit.sqlite")
        audit_id = audit.log_tool_call("ingest_document", {"path": "/tmp/x.pdf"}, "ok")
        recent = audit.get_recent(limit=10)
        audit.close()
    """

    def __init__(self, db_path: str) -> None:
        """Open (or create) the audit database.

        Args:
            db_path: Absolute path to the SQLite database file,
                     typically ``<vault>/audit.sqlite``.
        """
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema()
        logger.info("AuditLogger opened: %s", db_path)

    def _ensure_schema(self) -> None:
        """Create the audit_log table if it does not exist."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id          TEXT PRIMARY KEY,
                timestamp         TEXT NOT NULL,
                user_id           TEXT,
                platform          TEXT,
                tool_name         TEXT NOT NULL,
                tool_args         TEXT,
                result_summary    TEXT,
                langfuse_trace_id TEXT,
                duration_ms       INTEGER,
                error             TEXT
            )
            """
        )
        self._conn.commit()

    # ── Logging ─────────────────────────────────────────────────────

    def log_tool_call(
        self,
        tool_name: str,
        tool_args: dict | None,
        result_summary: str | None,
        *,
        user_id: str | None = None,
        platform: str | None = None,
        langfuse_trace_id: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> str:
        """Record a single tool invocation in the audit log.

        Args:
            tool_name: Name of the MCP tool that was called.
            tool_args: Arguments passed to the tool (will be redacted).
            result_summary: Short summary of the tool's result.
            user_id: Optional identifier for the calling user.
            platform: Optional platform identifier (e.g. ``"telegram"``).
            langfuse_trace_id: Optional Langfuse trace ID for correlation.
            duration_ms: Optional execution duration in milliseconds.
            error: Optional error message if the call failed.

        Returns:
            The generated ``audit_id`` (UUID v4 hex string).
        """
        audit_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat()

        # Reason: Redact before serialising so secrets never reach disk.
        safe_args = json.dumps(redact_sensitive(tool_args)) if tool_args else None

        self._conn.execute(
            """
            INSERT INTO audit_log (
                audit_id, timestamp, user_id, platform, tool_name,
                tool_args, result_summary, langfuse_trace_id,
                duration_ms, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                timestamp,
                user_id,
                platform,
                tool_name,
                safe_args,
                result_summary,
                langfuse_trace_id,
                duration_ms,
                error,
            ),
        )
        self._conn.commit()
        logger.debug("Audit logged: %s → %s (id=%s)", tool_name, result_summary, audit_id)
        return audit_id

    # ── Queries ─────────────────────────────────────────────────────

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return the most recent audit entries.

        Args:
            limit: Maximum number of rows to return (default 50).

        Returns:
            List of dicts, newest first.
        """
        # Reason: Clamp limit to prevent accidental full-table scans.
        limit = max(1, min(limit, 500))
        rows = self._conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── Export ──────────────────────────────────────────────────────

    def export_csv(
        self,
        output_path: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> int:
        """Export audit rows to a CSV file.

        Args:
            output_path: Destination CSV file path.
            start_date: Optional ISO date — include rows ``>= start_date``.
            end_date: Optional ISO date — include rows ``<= end_date``.

        Returns:
            Number of rows written.
        """
        where_parts: list[str] = []
        params: list[str] = []

        if start_date:
            where_parts.append("timestamp >= ?")
            params.append(start_date)
        if end_date:
            where_parts.append("timestamp <= ?")
            params.append(end_date)

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        query = f"SELECT * FROM audit_log{where_clause} ORDER BY timestamp ASC"  # noqa: S608

        rows = self._conn.execute(query, params).fetchall()

        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            if not rows:
                return 0
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))

        logger.info("Exported %d audit rows to %s", len(rows), output_path)
        return len(rows)

    # ── Lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]
