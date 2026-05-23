"""``query_transactions`` MCP tool handler (FR-4.4).

Parameterised DuckDB queries for financial transaction data with
filtering, aggregation, and pagination.
"""
from __future__ import annotations

import logging

from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)

_VALID_GROUP_BY = {"month", "category", "merchant", "account"}
_VALID_METRICS = {"sum", "count", "avg", "min", "max"}
_VALID_ORDER_BY = {"transaction_date", "amount_cents", "merchant", "category"}


async def handle_query_transactions(
    account_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    merchant: str | None = None,
    description_contains: str | None = None,
    amount_min_cents: int | None = None,
    amount_max_cents: int | None = None,
    group_by: list[str] | None = None,
    metrics: list[str] | None = None,
    order_by: str = "transaction_date",
    limit: int = 50,
    offset: int = 0,
    only_validated: bool = True,
    *,
    ledger: FinanceLedger,
) -> dict:
    """Query financial transactions from DuckDB.

    Supports both raw transaction listing and aggregated views.
    """
    logger.info(
        "query_transactions called: date_from=%s, date_to=%s, category=%s, "
        "merchant=%s, account_id=%s, group_by=%s, metrics=%s, "
        "only_validated=%s, limit=%s, offset=%s",
        date_from, date_to, category, merchant, account_id,
        group_by, metrics, only_validated, limit, offset,
    )

    c = ledger.conn
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    if order_by not in _VALID_ORDER_BY:
        logger.debug("Invalid order_by=%r, defaulting to transaction_date", order_by)
        order_by = "transaction_date"

    # -- Build WHERE clause ------------------------------------------------
    where_parts: list[str] = []
    params: list = []

    if only_validated:
        where_parts.append(
            "t.statement_id IN (SELECT statement_id FROM bank_statements WHERE validation_status = 'passed')"
        )

    if account_id:
        where_parts.append("t.account_id = ?")
        params.append(account_id)
    if date_from:
        where_parts.append("t.transaction_date >= ?")
        params.append(date_from)
    if date_to:
        where_parts.append("t.transaction_date <= ?")
        params.append(date_to)
    if category:
        where_parts.append("t.category = ?")
        params.append(category)
    if merchant:
        where_parts.append("t.merchant = ?")
        params.append(merchant)
    if description_contains:
        where_parts.append("t.description_raw ILIKE ?")
        params.append(f"%{description_contains[:200]}%")
    if amount_min_cents is not None:
        where_parts.append("t.amount_cents >= ?")
        params.append(amount_min_cents)
    if amount_max_cents is not None:
        where_parts.append("t.amount_cents <= ?")
        params.append(amount_max_cents)

    where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    logger.debug("WHERE clause: %s  params=%s", where_clause[:200], params)

    # -- Aggregation mode --------------------------------------------------
    if group_by:
        logger.info("Aggregation mode: group_by=%s, metrics=%s", group_by, metrics)
        return _aggregated_query(c, group_by, metrics, where_clause, params, only_validated)

    # -- Raw transaction listing -------------------------------------------
    # Total count
    count_sql = f"SELECT COUNT(*) FROM transactions t{where_clause}"
    logger.debug("Count SQL: %s", count_sql)
    total_count = c.execute(count_sql, params).fetchone()[0]
    logger.debug("Total matching transactions: %d", total_count)

    # Date coverage
    coverage_sql = f"SELECT MIN(t.transaction_date), MAX(t.transaction_date) FROM transactions t{where_clause}"
    coverage = c.execute(coverage_sql, params).fetchone()

    # Fetch results
    query_sql = (
        f"SELECT t.* FROM transactions t{where_clause} "
        f"ORDER BY t.{order_by} "
        f"LIMIT ? OFFSET ?"
    )
    logger.debug("Query SQL: %s", query_sql)
    rows = c.execute(query_sql, params + [limit, offset]).fetchall()
    col_names = [desc[0] for desc in c.description]
    logger.debug("Fetched %d rows (limit=%d, offset=%d)", len(rows), limit, offset)

    transactions = []
    for row in rows:
        txn = dict(zip(col_names, row))
        # Convert date objects to strings for JSON serialisation
        for date_field in ("transaction_date", "posting_date", "created_at"):
            if txn.get(date_field) is not None:
                txn[date_field] = str(txn[date_field])
        transactions.append(txn)

    # Validation warnings
    warnings = []
    if not only_validated:
        warnings.append("Results include transactions from unvalidated statements")

    logger.info(
        "query_transactions complete: %d/%d returned, coverage=%s-%s",
        len(transactions), total_count,
        str(coverage[0]) if coverage[0] else "N/A",
        str(coverage[1]) if coverage[1] else "N/A",
    )

    return {
        "transactions": transactions,
        "total_count": total_count,
        "has_more": (offset + limit) < total_count,
        "date_coverage": {
            "start": str(coverage[0]) if coverage[0] else None,
            "end": str(coverage[1]) if coverage[1] else None,
        },
        "validation_warnings": warnings,
    }


def _aggregated_query(
    c, group_by: list[str], metrics: list[str] | None,
    where_clause: str, params: list, only_validated: bool,
) -> dict:
    """Execute an aggregated query with GROUP BY and metrics."""
    if not metrics:
        metrics = ["sum", "count"]

    # Map group_by values to SQL columns
    group_cols = []
    for g in group_by:
        if g == "month":
            group_cols.append("strftime(t.transaction_date, '%Y-%m') AS month")
        elif g == "category":
            group_cols.append("t.category")
        elif g == "merchant":
            group_cols.append("t.merchant")
        elif g == "account":
            group_cols.append("t.account_id")

    if not group_cols:
        logger.warning("No valid group_by columns from %s", group_by)
        return {"error": "invalid_group_by", "message": f"Valid group_by values: {_VALID_GROUP_BY}"}

    # Build metric columns
    metric_cols = []
    for m in metrics:
        if m == "sum":
            metric_cols.append("SUM(t.amount_cents) AS total_cents")
        elif m == "count":
            metric_cols.append("COUNT(*) AS transaction_count")
        elif m == "avg":
            metric_cols.append("CAST(AVG(t.amount_cents) AS BIGINT) AS avg_cents")
        elif m == "min":
            metric_cols.append("MIN(t.amount_cents) AS min_cents")
        elif m == "max":
            metric_cols.append("MAX(t.amount_cents) AS max_cents")

    select_cols = ", ".join(group_cols + metric_cols)
    group_by_cols = ", ".join(
        g.split(" AS ")[0] if " AS " in g else g
        for g in group_cols
    )

    sql = (
        f"SELECT {select_cols} FROM transactions t{where_clause} "
        f"GROUP BY {group_by_cols} "
        f"ORDER BY total_cents ASC"
        if "SUM(t.amount_cents) AS total_cents" in metric_cols
        else f"SELECT {select_cols} FROM transactions t{where_clause} "
             f"GROUP BY {group_by_cols}"
    )
    logger.debug("Aggregation SQL: %s", sql)

    rows = c.execute(sql, params).fetchall()
    col_names = [desc[0] for desc in c.description]
    logger.debug("Aggregation returned %d groups", len(rows))

    results = []
    for row in rows:
        entry = dict(zip(col_names, row))
        # Convert any date objects
        for k, v in entry.items():
            if hasattr(v, "isoformat"):
                entry[k] = str(v)
        results.append(entry)

    warnings = []
    if not only_validated:
        warnings.append("Aggregation includes unvalidated statement data")

    logger.info("Aggregation complete: %d groups returned", len(results))

    return {
        "transactions": results,
        "total_count": len(results),
        "has_more": False,
        "date_coverage": {"start": None, "end": None},
        "validation_warnings": warnings,
    }
