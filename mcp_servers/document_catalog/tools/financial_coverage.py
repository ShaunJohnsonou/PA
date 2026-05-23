"""``get_financial_coverage`` MCP tool handler (FR-4.5).

Returns a summary of what financial data is available in the ledger,
including date gaps and validation status. Hermes should call this
before attempting financial queries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)


async def handle_get_financial_coverage(
    account_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    ledger: FinanceLedger,
) -> dict:
    """Return a coverage summary of the financial ledger."""
    logger.info(
        "get_financial_coverage called: account=%s, start=%s, end=%s",
        account_id, start_date, end_date,
    )
    c = ledger.conn

    # -- Account summaries -------------------------------------------------
    acct_where = ""
    acct_params: list = []
    if account_id:
        acct_where = " WHERE a.account_id = ?"
        acct_params = [account_id]

    accounts_sql = f"""
        SELECT
            a.account_id,
            a.bank_name,
            a.account_number_masked,
            a.account_type,
            a.currency,
            COUNT(DISTINCT bs.statement_id) AS statement_count,
            MIN(bs.period_start) AS earliest_period,
            MAX(bs.period_end) AS latest_period,
            SUM(bs.transaction_count) AS total_transactions
        FROM accounts a
        LEFT JOIN bank_statements bs ON a.account_id = bs.account_id
        {acct_where}
        GROUP BY a.account_id, a.bank_name, a.account_number_masked,
                 a.account_type, a.currency
    """
    logger.debug("Accounts query: %s  params=%s", accounts_sql.strip()[:200], acct_params)
    rows = c.execute(accounts_sql, acct_params).fetchall()
    cols = [desc[0] for desc in c.description]
    accounts = []
    for row in rows:
        acct = dict(zip(cols, row))
        for k, v in acct.items():
            if hasattr(v, "isoformat"):
                acct[k] = str(v)
        accounts.append(acct)
    logger.debug("Found %d accounts", len(accounts))

    # -- Overall stats -----------------------------------------------------
    stats_sql = """
        SELECT
            COUNT(DISTINCT statement_id) AS total_statements,
            SUM(transaction_count) AS total_transactions,
            MIN(period_start) AS date_start,
            MAX(period_end) AS date_end
        FROM bank_statements
    """
    stats = c.execute(stats_sql).fetchone()
    total_statements = stats[0] or 0
    total_transactions = stats[1] or 0
    date_start = str(stats[2]) if stats[2] else None
    date_end = str(stats[3]) if stats[3] else None
    logger.debug(
        "Overall stats: %d statements, %d transactions, range=%s to %s",
        total_statements, total_transactions, date_start, date_end,
    )

    # -- Gap analysis ------------------------------------------------------
    gaps = _find_monthly_gaps(c, account_id, start_date, end_date)
    logger.debug("Found %d coverage gaps", len(gaps))

    # -- Validation summary ------------------------------------------------
    val_sql = """
        SELECT
            SUM(CASE WHEN validation_status = 'passed' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN validation_status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
            SUM(CASE WHEN validation_status = 'pending' THEN 1 ELSE 0 END) AS pending
        FROM bank_statements
    """
    val = c.execute(val_sql).fetchone()
    val_passed = val[0] or 0
    val_review = val[1] or 0
    val_pending = val[2] or 0
    logger.debug(
        "Validation summary: passed=%d, needs_review=%d, pending=%d",
        val_passed, val_review, val_pending,
    )

    logger.info(
        "get_financial_coverage complete: %d accounts, %d statements, %d gaps",
        len(accounts), total_statements, len(gaps),
    )

    return {
        "accounts": accounts,
        "total_statements": total_statements,
        "total_transactions": total_transactions,
        "date_range": {
            "start": date_start,
            "end": date_end,
        },
        "gaps": gaps,
        "validation_summary": {
            "passed": val_passed,
            "needs_review": val_review,
            "pending": val_pending,
        },
    }


def _find_monthly_gaps(
    c, account_id: str | None, start_date: str | None, end_date: str | None,
) -> list[dict]:
    """Identify months with no statement coverage."""
    where_parts = []
    params: list = []

    if account_id:
        where_parts.append("account_id = ?")
        params.append(account_id)

    where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = c.execute(
        f"SELECT period_start, period_end FROM bank_statements{where_clause}",
        params,
    ).fetchall()

    if not rows:
        logger.debug("No statements found for gap analysis")
        return []

    # Determine overall range
    all_starts = [str(r[0]) for r in rows if r[0]]
    all_ends = [str(r[1]) for r in rows if r[1]]

    if not all_starts or not all_ends:
        return []

    range_start = start_date or min(all_starts)
    range_end = end_date or max(all_ends)

    # Build set of covered months
    covered_months: set[str] = set()
    for row in rows:
        ps, pe = str(row[0]), str(row[1])
        try:
            s = datetime.strptime(ps, "%Y-%m-%d")
            e = datetime.strptime(pe, "%Y-%m-%d")
            current = s
            while current <= e:
                covered_months.add(current.strftime("%Y-%m"))
                # Move to next month
                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1, day=1)
                else:
                    current = current.replace(month=current.month + 1, day=1)
        except ValueError:
            continue

    logger.debug("Covered months: %s", sorted(covered_months)[:12])

    # Find gaps
    gaps = []
    try:
        current = datetime.strptime(range_start[:7] + "-01", "%Y-%m-%d")
        end = datetime.strptime(range_end[:7] + "-01", "%Y-%m-%d")

        gap_start = None
        while current <= end:
            month_key = current.strftime("%Y-%m")
            if month_key not in covered_months:
                if gap_start is None:
                    gap_start = month_key
            else:
                if gap_start is not None:
                    prev_month = current - timedelta(days=1)
                    gaps.append({
                        "start": gap_start,
                        "end": prev_month.strftime("%Y-%m"),
                    })
                    gap_start = None

            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

        if gap_start is not None:
            gaps.append({"start": gap_start, "end": range_end[:7]})

    except ValueError:
        pass

    return gaps
