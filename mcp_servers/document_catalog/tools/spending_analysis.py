"""``run_spending_analysis`` MCP tool handler (FR-4.7).

Produces a structured spending analysis over a date range with
category/merchant/month breakdowns and recurring payment detection.
"""
from __future__ import annotations

import logging
from collections import Counter

from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)


async def handle_run_spending_analysis(
    start_date: str,
    end_date: str,
    account_id: str | None = None,
    group_by: str = "category",
    top_n: int = 10,
    only_validated: bool = True,
    *,
    ledger: FinanceLedger,
) -> dict:
    """Produce a structured spending analysis."""
    logger.info(
        "run_spending_analysis: start=%s, end=%s, account=%s, group_by=%s, "
        "top_n=%d, only_validated=%s",
        start_date, end_date, account_id, group_by, top_n, only_validated,
    )
    c = ledger.conn

    if group_by not in ("category", "merchant", "month"):
        logger.debug("Invalid group_by=%r, defaulting to 'category'", group_by)
        group_by = "category"

    top_n = max(1, min(top_n, 50))

    # -- Build WHERE clause ------------------------------------------------
    where_parts = [
        "t.transaction_date >= ?",
        "t.transaction_date <= ?",
    ]
    params: list = [start_date, end_date]

    if only_validated:
        where_parts.append(
            "t.statement_id IN (SELECT statement_id FROM bank_statements WHERE validation_status = 'passed')"
        )
    if account_id:
        where_parts.append("t.account_id = ?")
        params.append(account_id)

    where_clause = " WHERE " + " AND ".join(where_parts)
    logger.debug("WHERE clause: %s  params=%s", where_clause[:200], params)

    # -- Overall totals ----------------------------------------------------
    totals_sql = f"""
        SELECT
            COALESCE(SUM(CASE WHEN t.amount_cents > 0 THEN t.amount_cents ELSE 0 END), 0) AS income,
            COALESCE(SUM(CASE WHEN t.amount_cents < 0 THEN t.amount_cents ELSE 0 END), 0) AS expenses,
            COUNT(*) AS txn_count
        FROM transactions t{where_clause}
    """
    logger.debug("Totals SQL: %s", totals_sql.strip()[:200])
    totals = c.execute(totals_sql, params).fetchone()
    total_income = totals[0]
    total_expenses = totals[1]
    txn_count = totals[2]
    logger.debug(
        "Totals: income=%d cents, expenses=%d cents, txn_count=%d",
        total_income, total_expenses, txn_count,
    )

    # -- Statement count ---------------------------------------------------
    stmt_sql = f"""
        SELECT COUNT(DISTINCT t.statement_id)
        FROM transactions t{where_clause}
    """
    stmt_count = c.execute(stmt_sql, params).fetchone()[0]
    logger.debug("Statements in range: %d", stmt_count)

    # -- Grouped breakdown -------------------------------------------------
    if group_by == "month":
        group_col = "strftime(t.transaction_date, '%Y-%m')"
        group_alias = "month"
    elif group_by == "merchant":
        group_col = "COALESCE(t.merchant, 'Unknown')"
        group_alias = "merchant"
    else:
        group_col = "COALESCE(t.category, 'other')"
        group_alias = "category"

    breakdown_sql = f"""
        SELECT
            {group_col} AS {group_alias},
            SUM(t.amount_cents) AS total_cents,
            COUNT(*) AS count
        FROM transactions t{where_clause}
        GROUP BY {group_col}
        ORDER BY ABS(SUM(t.amount_cents)) DESC
        LIMIT ?
    """
    logger.debug("Breakdown SQL: %s", breakdown_sql.strip()[:200])
    breakdown_rows = c.execute(breakdown_sql, params + [top_n]).fetchall()
    breakdown_cols = [desc[0] for desc in c.description]
    logger.debug("Breakdown returned %d groups", len(breakdown_rows))

    total_abs = sum(abs(r[1]) for r in breakdown_rows) if breakdown_rows else 1
    breakdown = []
    for row in breakdown_rows:
        entry = dict(zip(breakdown_cols, row))
        entry["pct"] = round(abs(entry["total_cents"]) / max(total_abs, 1) * 100, 1)
        breakdown.append(entry)
        logger.debug(
            "  Group %s: total=%d cents, count=%d, pct=%.1f%%",
            entry.get(group_alias, "?"), entry["total_cents"],
            entry["count"], entry["pct"],
        )

    # -- Recurring payment detection ---------------------------------------
    logger.debug("Detecting recurring payments...")
    recurring = _detect_recurring(c, where_clause, params)
    logger.debug("Found %d recurring patterns", len(recurring))

    # -- Coverage warnings -------------------------------------------------
    warnings = []
    if not only_validated:
        warnings.append("Analysis includes unvalidated statement data")
    if stmt_count == 0:
        warnings.append("No statements found for the specified period")

    logger.info(
        "run_spending_analysis complete: income=%d, expenses=%d, groups=%d, recurring=%d",
        total_income, total_expenses, len(breakdown), len(recurring),
    )

    return {
        "period": {"start": start_date, "end": end_date},
        "total_income_cents": total_income,
        "total_expenses_cents": total_expenses,
        "net_cents": total_income + total_expenses,
        "breakdown": breakdown,
        "recurring_payments": recurring,
        "coverage_warnings": warnings,
        "statement_count": stmt_count,
        "transaction_count": txn_count,
    }


def _detect_recurring(c, where_clause: str, params: list) -> list[dict]:
    """Detect recurring payments using frequency analysis.

    A payment is considered recurring if:
    - Same merchant appears 3+ times in the period
    - Amounts are within 10% of each other
    """
    sql = f"""
        SELECT
            COALESCE(t.merchant, t.description_raw) AS payee,
            COUNT(*) AS occurrences,
            CAST(AVG(t.amount_cents) AS BIGINT) AS avg_cents,
            MIN(t.amount_cents) AS min_cents,
            MAX(t.amount_cents) AS max_cents
        FROM transactions t{where_clause}
        AND t.amount_cents < 0
        GROUP BY COALESCE(t.merchant, t.description_raw)
        HAVING COUNT(*) >= 3
        ORDER BY COUNT(*) DESC
        LIMIT 20
    """
    try:
        rows = c.execute(sql, params).fetchall()
    except Exception as exc:
        logger.warning("Recurring detection query failed: %s", exc)
        return []

    recurring = []
    for row in rows:
        payee, count, avg, mn, mx = row
        # Check if amounts are within 10% of each other (consistent)
        if avg != 0 and abs(mx - mn) / abs(avg) < 0.2:
            recurring.append({
                "payee": payee,
                "occurrences": count,
                "avg_amount_cents": avg,
                "min_amount_cents": mn,
                "max_amount_cents": mx,
                "likely_recurring": True,
            })
            logger.debug(
                "  Recurring: %s x%d, avg=%d cents",
                payee, count, avg,
            )

    return recurring
