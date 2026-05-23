"""FR-5.6 — Validation review workflow tool handlers.

Provides tools for reviewing, overriding, and managing validation
results for financial documents.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from ..catalog_db import CatalogDB
from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)

# ── Human-readable explanations for each validation rule ────────────
# Reason: LLM-facing tools benefit from contextual explanations so
# Hermes can relay meaningful information to the user without needing
# to understand the rule internals.

RULE_EXPLANATIONS: dict[str, str] = {
    "balance_equation": (
        "The opening balance plus credits minus debits does not "
        "equal the closing balance."
    ),
    "running_balance": (
        "A transaction's running balance doesn't match the previous "
        "transaction's balance plus the amount."
    ),
    "period_consistency": (
        "One or more transactions have dates outside the statement period."
    ),
    "date_validity": (
        "One or more transaction dates are invalid (future or before year 2000)."
    ),
    "page_continuity": (
        "There appear to be gaps in the extracted pages."
    ),
    "currency_consistency": (
        "Not all transactions use the same currency as the statement."
    ),
    "summary_totals": (
        "The sum of debits or credits doesn't match the statement summary totals."
    ),
    "duplicate_detection": (
        "Potential duplicate transactions were found "
        "(same date, amount, and description)."
    ),
    "sign_consistency": (
        "Transaction amount signs don't follow the expected convention."
    ),
    "evidence_completeness": (
        "Some transactions are missing source page or row information."
    ),
    "amount_reasonableness": (
        "One or more transactions exceed the reasonableness threshold."
    ),
}


# ── Tool handlers ───────────────────────────────────────────────────


async def handle_get_validation_issues(
    statement_id: str | None = None,
    document_id: str | None = None,
    severity: str | None = None,
    *,
    ledger: FinanceLedger,
) -> dict:
    """Query validation results that failed (passed=FALSE).

    Filters:
        statement_id: Limit to a specific bank statement.
        document_id:  Limit to a specific source document.
        severity:     Filter by 'error' or 'warning'.

    Returns:
        Dict with ``issues`` list, ``total_count``, ``error_count``,
        and ``warning_count``.
    """
    logger.info(
        "get_validation_issues: statement=%s, document=%s, severity=%s",
        statement_id, document_id, severity,
    )
    c = ledger.conn

    # ── Build dynamic WHERE clause ──────────────────────────────
    where_parts: list[str] = ["passed = FALSE"]
    params: list = []

    if statement_id:
        where_parts.append("statement_id = ?")
        params.append(statement_id)
    if document_id:
        where_parts.append("document_id = ?")
        params.append(document_id)
    if severity:
        if severity not in ("error", "warning"):
            return _error(
                "invalid_severity",
                f"severity must be 'error' or 'warning', got '{severity}'",
            )
        where_parts.append("severity = ?")
        params.append(severity)

    where_clause = " WHERE " + " AND ".join(where_parts)

    # ── Query validation_results ────────────────────────────────
    sql = f"SELECT * FROM validation_results{where_clause} ORDER BY validated_at DESC"
    logger.debug("Issues SQL: %s  params=%s", sql, params)

    try:
        rows = c.execute(sql, params).fetchall()
        cols = [desc[0] for desc in c.description]
    except Exception as exc:
        logger.error("Failed to query validation_results: %s", exc)
        return _error("query_failed", f"Database query failed: {exc}")

    logger.debug("Found %d failed validation results", len(rows))

    # ── Build response ──────────────────────────────────────────
    issues: list[dict] = []
    error_count = 0
    warning_count = 0

    for row in rows:
        issue = dict(zip(cols, row))

        # Convert any date/timestamp objects to strings
        for key, value in issue.items():
            if hasattr(value, "isoformat"):
                issue[key] = str(value)

        # Reason: boolean comes back from DuckDB as Python bool, but
        # the overridden column may not exist yet — default to False.
        issue["overridden"] = bool(issue.get("overridden", False))

        # Attach human-readable explanation
        rule_name = issue.get("rule_name", "")
        issue["explanation"] = RULE_EXPLANATIONS.get(rule_name, "Unknown rule.")

        if issue.get("severity") == "error":
            error_count += 1
        else:
            warning_count += 1

        issues.append(issue)

    logger.info(
        "get_validation_issues complete: %d issues (%d errors, %d warnings)",
        len(issues), error_count, warning_count,
    )

    return {
        "issues": issues,
        "total_count": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
    }


async def handle_override_validation(
    validation_id: str,
    reason: str,
    overridden_by: str | None = None,
    *,
    ledger: FinanceLedger,
    audit: logging.Logger,
) -> dict:
    """Override a failed validation result with a reason.

    Steps:
        1. Check validation_id exists in validation_results.
        2. Update the row — set overridden=TRUE, override_reason, etc.
        3. Recalculate overall statement validation_status.
        4. Log the override to the audit logger.

    Returns:
        Dict with ``success``, ``validation_id``, and
        ``statement_validation_status``.
    """
    logger.info(
        "override_validation: id=%s, reason=%s, by=%s",
        validation_id, reason[:80], overridden_by,
    )
    c = ledger.conn

    if not reason or not reason.strip():
        return _error("missing_reason", "A reason is required to override a validation result.")

    # ── 1. Check existence ──────────────────────────────────────
    row = c.execute(
        "SELECT * FROM validation_results WHERE validation_id = ?",
        [validation_id],
    ).fetchone()

    if row is None:
        return _error("not_found", f"Validation result '{validation_id}' not found.")

    cols = [desc[0] for desc in c.description]
    vr = dict(zip(cols, row))

    # Reason: prevent overriding a result that already passed — only
    # failed results should need an override.
    if vr.get("passed"):
        return _error(
            "already_passed",
            f"Validation '{validation_id}' already passed — no override needed.",
        )

    statement_id = vr.get("statement_id")
    now_iso = datetime.now(timezone.utc).isoformat()

    # ── 2. Update validation_results ────────────────────────────
    c.execute(
        "UPDATE validation_results "
        "SET overridden = TRUE, override_reason = ?, "
        "    overridden_by = ?, overridden_at = ? "
        "WHERE validation_id = ?",
        [reason.strip(), overridden_by, now_iso, validation_id],
    )
    logger.debug("Marked validation %s as overridden", validation_id)

    # ── 3. Recalculate statement status ─────────────────────────
    # Reason: a statement moves to 'passed' only when all error-severity
    # rules either passed or have been explicitly overridden.
    stmt_status = _recalculate_statement_status(c, statement_id)
    logger.debug(
        "Recalculated statement %s status -> %s", statement_id, stmt_status,
    )

    # ── 4. Audit log ────────────────────────────────────────────
    audit.info(
        "VALIDATION_OVERRIDE: validation_id=%s, statement_id=%s, "
        "rule=%s, reason=%s, overridden_by=%s",
        validation_id, statement_id, vr.get("rule_name"),
        reason.strip(), overridden_by,
    )

    logger.info(
        "override_validation complete: id=%s, statement_status=%s",
        validation_id, stmt_status,
    )

    return {
        "success": True,
        "validation_id": validation_id,
        "statement_validation_status": stmt_status,
    }


async def handle_set_document_status(
    document_id: str,
    status: str,
    reason: str | None = None,
    *,
    catalog: CatalogDB,
    audit: logging.Logger,
) -> dict:
    """Update a document's status in the catalog.

    Valid statuses: 'excluded', 'archived', 'active'.

    Returns:
        Dict with ``success``, ``document_id``, and ``new_status``.
    """
    logger.info(
        "set_document_status: doc=%s, status=%s, reason=%s",
        document_id, status, reason,
    )

    # ── 1. Validate status ──────────────────────────────────────
    allowed_statuses = {"excluded", "archived", "active"}
    if status not in allowed_statuses:
        return _error(
            "invalid_status",
            f"Status must be one of {sorted(allowed_statuses)}, got '{status}'.",
        )

    # ── 2. Check document exists ────────────────────────────────
    doc = catalog.get_by_id(document_id)
    if doc is None:
        return _error("not_found", f"Document '{document_id}' not found.")

    old_status = doc.status

    # ── 3. Update catalog ───────────────────────────────────────
    catalog.update_document(document_id, status=status)
    logger.debug(
        "Updated document %s status: %s -> %s", document_id, old_status, status,
    )

    # ── 4. Audit log ────────────────────────────────────────────
    audit.info(
        "DOCUMENT_STATUS_CHANGE: document_id=%s, old_status=%s, "
        "new_status=%s, reason=%s",
        document_id, old_status, status, reason,
    )

    logger.info(
        "set_document_status complete: doc=%s, %s -> %s",
        document_id, old_status, status,
    )

    return {
        "success": True,
        "document_id": document_id,
        "new_status": status,
    }


# ── Private helpers ─────────────────────────────────────────────────


def _recalculate_statement_status(c, statement_id: str | None) -> str:
    """Recompute a statement's validation_status after an override.

    A statement is 'passed' when every error-severity rule either
    passed (passed=TRUE) or has been explicitly overridden
    (overridden=TRUE).  Otherwise it stays 'needs_review'.

    Returns:
        The new validation_status string.
    """
    if not statement_id:
        return "needs_review"

    # Reason: only error-severity rules block a statement from passing.
    # Warnings are informational and never hold up the status.
    blocking = c.execute(
        "SELECT COUNT(*) FROM validation_results "
        "WHERE statement_id = ? "
        "  AND severity = 'error' "
        "  AND passed = FALSE "
        "  AND (overridden IS NULL OR overridden = FALSE)",
        [statement_id],
    ).fetchone()[0]

    new_status = "passed" if blocking == 0 else "needs_review"

    c.execute(
        "UPDATE bank_statements SET validation_status = ? WHERE statement_id = ?",
        [new_status, statement_id],
    )

    return new_status


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Review error [%s]: %s", code, message)
    return {"error": code, "message": message}
