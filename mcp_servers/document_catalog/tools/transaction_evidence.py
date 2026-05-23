"""``get_transaction_evidence`` MCP tool handler (FR-4.6).

Retrieves the extraction provenance for a specific transaction:
which document, which page, which row, and the raw extracted text.
"""
from __future__ import annotations

import logging

from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)


async def handle_get_transaction_evidence(
    transaction_id: str,
    *,
    ledger: FinanceLedger,
) -> dict:
    """Return provenance data for a single transaction."""
    logger.info("get_transaction_evidence called: txn=%s", transaction_id)
    c = ledger.conn

    # -- 1. Load transaction -----------------------------------------------
    logger.debug("Querying transactions table for id=%s", transaction_id)
    txn_row = c.execute(
        "SELECT * FROM transactions WHERE transaction_id = ?",
        [transaction_id],
    ).fetchone()

    if txn_row is None:
        logger.warning("Transaction not found: %s", transaction_id)
        return {"error": "not_found", "message": f"Transaction '{transaction_id}' not found"}

    txn_cols = [desc[0] for desc in c.description]
    transaction = dict(zip(txn_cols, txn_row))
    logger.debug(
        "Transaction found: date=%s, desc=%s, amount=%s",
        transaction.get("transaction_date"),
        transaction.get("description_raw", "")[:60],
        transaction.get("amount_cents"),
    )

    # Convert dates to strings
    for date_field in ("transaction_date", "posting_date", "created_at"):
        if transaction.get(date_field) is not None:
            transaction[date_field] = str(transaction[date_field])

    # -- 2. Load evidence --------------------------------------------------
    logger.debug("Querying extraction_evidence for txn=%s", transaction_id)
    ev_row = c.execute(
        "SELECT * FROM extraction_evidence WHERE transaction_id = ?",
        [transaction_id],
    ).fetchone()

    evidence = None
    if ev_row:
        ev_cols = [desc[0] for desc in c.description]
        evidence = dict(zip(ev_cols, ev_row))
        logger.debug(
            "Evidence found: page=%s, row=%s, method=%s",
            evidence.get("source_page"), evidence.get("source_row"),
            evidence.get("extraction_method"),
        )
    else:
        logger.debug("No extraction evidence found for txn=%s", transaction_id)

    # -- 3. Load statement summary -----------------------------------------
    stmt_id = transaction["statement_id"]
    logger.debug("Querying bank_statements for statement_id=%s", stmt_id)
    stmt_row = c.execute(
        "SELECT statement_id, document_id, bank_name, account_number_masked, "
        "period_start, period_end, validation_status "
        "FROM bank_statements WHERE statement_id = ?",
        [stmt_id],
    ).fetchone()

    statement = None
    if stmt_row:
        stmt_cols = [desc[0] for desc in c.description]
        statement = dict(zip(stmt_cols, stmt_row))
        for k, v in statement.items():
            if hasattr(v, "isoformat"):
                statement[k] = str(v)
        logger.debug(
            "Statement found: bank=%s, period=%s to %s, validation=%s",
            statement.get("bank_name"), statement.get("period_start"),
            statement.get("period_end"), statement.get("validation_status"),
        )
    else:
        logger.warning("Statement not found for statement_id=%s", stmt_id)

    # -- 4. Load validation results for the statement ----------------------
    validations = []
    if statement:
        logger.debug("Querying validation_results for statement_id=%s", stmt_id)
        val_rows = c.execute(
            "SELECT rule_name, passed, severity, notes "
            "FROM validation_results WHERE statement_id = ?",
            [stmt_id],
        ).fetchall()
        val_cols = [desc[0] for desc in c.description]
        validations = [dict(zip(val_cols, r)) for r in val_rows]
        logger.debug("Found %d validation results", len(validations))

    logger.info(
        "get_transaction_evidence complete: txn=%s, has_evidence=%s, has_statement=%s, validations=%d",
        transaction_id, evidence is not None, statement is not None, len(validations),
    )

    return {
        "transaction": transaction,
        "evidence": evidence,
        "statement": statement,
        "validation": validations,
    }
