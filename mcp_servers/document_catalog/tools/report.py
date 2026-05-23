"""FR-5.7 — run_financial_report MCP tool handler.

Generates comprehensive financial reports from the DuckDB ledger.
All computations use integer cents — no floating-point arithmetic.
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timedelta

from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)


# ── Public handler ──────────────────────────────────────────────────


async def handle_run_financial_report(
    report_type: str,
    start_date: str,
    end_date: str,
    account_id: str | None = None,
    format: str = "structured",
    *,
    ledger: FinanceLedger,
) -> dict:
    """Generate a financial report over a date range.

    Args:
        report_type:  One of 'monthly_summary', 'annual_overview',
                      'category_breakdown'.
        start_date:   ISO date string (YYYY-MM-DD), inclusive.
        end_date:     ISO date string (YYYY-MM-DD), inclusive.
        account_id:   Optional — restrict to one account.
        format:       'structured' (default) or 'markdown'.
        ledger:       The DuckDB FinanceLedger instance.

    Returns:
        Structured report dict, or markdown string wrapped in a dict.
    """
    logger.info(
        "run_financial_report: type=%s, start=%s, end=%s, account=%s, format=%s",
        report_type, start_date, end_date, account_id, format,
    )

    # ── 1. Validate report_type ─────────────────────────────────
    valid_types = {"monthly_summary", "annual_overview", "category_breakdown"}
    if report_type not in valid_types:
        return _error(
            "invalid_report_type",
            f"report_type must be one of {sorted(valid_types)}, got '{report_type}'.",
        )

    # ── 2. Validate dates ───────────────────────────────────────
    try:
        parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except (ValueError, TypeError) as exc:
        return _error("invalid_date", f"Dates must be YYYY-MM-DD: {exc}")

    if parsed_start > parsed_end:
        return _error(
            "invalid_date_range",
            f"start_date ({start_date}) is after end_date ({end_date}).",
        )

    # ── 3. Data quality section ─────────────────────────────────
    data_quality = _build_data_quality(start_date, end_date, account_id, ledger)

    # ── 4. Dispatch to report generator ─────────────────────────
    generators = {
        "monthly_summary": _generate_monthly_summary,
        "annual_overview": _generate_annual_overview,
        "category_breakdown": _generate_category_breakdown,
    }
    report_data = generators[report_type](start_date, end_date, account_id, ledger)

    report = {
        "report_type": report_type,
        "period": {"start": start_date, "end": end_date},
        "account_id": account_id,
        "data_quality": data_quality,
        **report_data,
    }

    # ── 5. Optional markdown rendering ──────────────────────────
    if format == "markdown":
        md = _render_markdown(report)
        logger.info("Rendered markdown report (%d chars)", len(md))
        return {"report_type": report_type, "format": "markdown", "content": md}

    logger.info("run_financial_report complete: type=%s", report_type)
    return report


# ── Report generators ───────────────────────────────────────────────


def _generate_monthly_summary(
    start_date: str, end_date: str, account_id: str | None, ledger: FinanceLedger,
) -> dict:
    """Monthly income/expense/net breakdown with top categories and merchants."""
    c = ledger.conn

    # ── Month-by-month breakdown ────────────────────────────────
    acct_filter, acct_params = _account_filter(account_id)

    monthly_sql = f"""
        SELECT
            EXTRACT(YEAR FROM t.transaction_date)  AS year,
            EXTRACT(MONTH FROM t.transaction_date) AS month,
            COALESCE(SUM(CASE WHEN t.amount_cents > 0
                         THEN t.amount_cents ELSE 0 END), 0) AS income_cents,
            COALESCE(SUM(CASE WHEN t.amount_cents < 0
                         THEN t.amount_cents ELSE 0 END), 0) AS expense_cents,
            COALESCE(SUM(t.amount_cents), 0)                  AS net_cents,
            COUNT(*)                                           AS count
        FROM transactions t
        JOIN bank_statements bs ON t.statement_id = bs.statement_id
        WHERE t.transaction_date >= ?
          AND t.transaction_date <= ?
          AND bs.validation_status = 'passed'
          {acct_filter}
        GROUP BY year, month
        ORDER BY year, month
    """
    params: list = [start_date, end_date] + acct_params
    logger.debug("Monthly SQL: %s", monthly_sql.strip()[:200])

    rows = c.execute(monthly_sql, params).fetchall()
    logger.debug("Monthly breakdown: %d rows", len(rows))

    months: list[dict] = []
    for row in rows:
        months.append({
            "year": int(row[0]),
            "month": int(row[1]),
            "total_income_cents": int(row[2]),
            "total_expenses_cents": int(row[3]),
            "net_cents": int(row[4]),
            "transaction_count": int(row[5]),
        })

    # ── Top 5 categories by total spend (absolute value) ───────
    top_categories = _top_groups(
        c, "COALESCE(t.category, 'other')", "category",
        start_date, end_date, acct_filter, acct_params, limit=5,
    )

    # ── Top 5 merchants by total spend ─────────────────────────
    top_merchants = _top_groups(
        c, "COALESCE(t.merchant, 'Unknown')", "merchant",
        start_date, end_date, acct_filter, acct_params, limit=5,
    )

    return {
        "months": months,
        "top_categories": top_categories,
        "top_merchants": top_merchants,
    }


def _generate_annual_overview(
    start_date: str, end_date: str, account_id: str | None, ledger: FinanceLedger,
) -> dict:
    """Annual totals with month-by-month list and top 10 largest expenses."""
    c = ledger.conn
    acct_filter, acct_params = _account_filter(account_id)

    # ── Annual totals ───────────────────────────────────────────
    totals_sql = f"""
        SELECT
            COALESCE(SUM(CASE WHEN t.amount_cents > 0
                         THEN t.amount_cents ELSE 0 END), 0) AS income_cents,
            COALESCE(SUM(CASE WHEN t.amount_cents < 0
                         THEN t.amount_cents ELSE 0 END), 0) AS expense_cents,
            COALESCE(SUM(t.amount_cents), 0)                  AS net_cents,
            COUNT(*)                                           AS count
        FROM transactions t
        JOIN bank_statements bs ON t.statement_id = bs.statement_id
        WHERE t.transaction_date >= ?
          AND t.transaction_date <= ?
          AND bs.validation_status = 'passed'
          {acct_filter}
    """
    params: list = [start_date, end_date] + acct_params
    row = c.execute(totals_sql, params).fetchone()
    logger.debug("Annual totals: income=%s, expense=%s, net=%s", row[0], row[1], row[2])

    annual_totals = {
        "total_income_cents": int(row[0]),
        "total_expenses_cents": int(row[1]),
        "net_cents": int(row[2]),
        "transaction_count": int(row[3]),
    }

    # ── Month-by-month (reuse monthly summary logic) ───────────
    monthly_data = _generate_monthly_summary(start_date, end_date, account_id, ledger)

    # ── Top 10 largest single expenses ──────────────────────────
    largest_sql = f"""
        SELECT
            t.transaction_date,
            t.description_raw,
            t.merchant,
            t.amount_cents,
            t.category
        FROM transactions t
        JOIN bank_statements bs ON t.statement_id = bs.statement_id
        WHERE t.transaction_date >= ?
          AND t.transaction_date <= ?
          AND bs.validation_status = 'passed'
          AND t.amount_cents < 0
          {acct_filter}
        ORDER BY t.amount_cents ASC
        LIMIT 10
    """
    largest_rows = c.execute(largest_sql, params).fetchall()
    logger.debug("Top 10 largest expenses: %d rows", len(largest_rows))

    largest_expenses: list[dict] = []
    for lr in largest_rows:
        largest_expenses.append({
            "transaction_date": str(lr[0]),
            "description": lr[1],
            "merchant": lr[2],
            "amount_cents": int(lr[3]),
            "category": lr[4],
        })

    return {
        "annual_totals": annual_totals,
        "months": monthly_data["months"],
        "top_categories": monthly_data["top_categories"],
        "top_merchants": monthly_data["top_merchants"],
        "largest_expenses": largest_expenses,
    }


def _generate_category_breakdown(
    start_date: str, end_date: str, account_id: str | None, ledger: FinanceLedger,
) -> dict:
    """Transactions grouped by category with totals and percentages."""
    c = ledger.conn
    acct_filter, acct_params = _account_filter(account_id)

    # ── Total absolute spend for percentage calculation ─────────
    total_sql = f"""
        SELECT COALESCE(SUM(ABS(t.amount_cents)), 0)
        FROM transactions t
        JOIN bank_statements bs ON t.statement_id = bs.statement_id
        WHERE t.transaction_date >= ?
          AND t.transaction_date <= ?
          AND bs.validation_status = 'passed'
          {acct_filter}
    """
    params: list = [start_date, end_date] + acct_params
    total_abs = c.execute(total_sql, params).fetchone()[0]
    # Reason: avoid division by zero when no transactions exist.
    total_abs = max(total_abs, 1)
    logger.debug("Category total absolute spend: %d cents", total_abs)

    # ── Category breakdown ──────────────────────────────────────
    cat_sql = f"""
        SELECT
            COALESCE(t.category, 'other') AS category,
            SUM(t.amount_cents)           AS total_cents,
            COUNT(*)                      AS count
        FROM transactions t
        JOIN bank_statements bs ON t.statement_id = bs.statement_id
        WHERE t.transaction_date >= ?
          AND t.transaction_date <= ?
          AND bs.validation_status = 'passed'
          {acct_filter}
        GROUP BY category
        ORDER BY ABS(SUM(t.amount_cents)) DESC
    """
    rows = c.execute(cat_sql, params).fetchall()
    logger.debug("Category breakdown: %d categories", len(rows))

    categories: list[dict] = []
    for row in rows:
        total_cents = int(row[1])
        categories.append({
            "category": row[0],
            "total_cents": total_cents,
            "count": int(row[2]),
            # Reason: percentage is based on absolute values so both
            # income and expense categories are comparable.
            "percentage": round(abs(total_cents) / total_abs * 100, 1),
        })

    return {"categories": categories}


# ── Data quality ────────────────────────────────────────────────────


def _build_data_quality(
    start_date: str, end_date: str, account_id: str | None, ledger: FinanceLedger,
) -> dict:
    """Assess data quality for the report period.

    Counts included/excluded statements, produces human-readable
    warnings, and identifies coverage gaps.
    """
    c = ledger.conn
    acct_filter, acct_params = _account_filter(account_id)

    # ── Statement counts ────────────────────────────────────────
    stmt_sql = f"""
        SELECT
            statement_id,
            validation_status,
            period_start,
            period_end,
            bank_name
        FROM bank_statements
        WHERE period_start <= ? AND period_end >= ?
          {acct_filter.replace('t.account_id', 'account_id')}
    """
    params: list = [end_date, start_date] + acct_params
    rows = c.execute(stmt_sql, params).fetchall()
    cols = [desc[0] for desc in c.description]

    statements_included = 0
    statements_excluded = 0
    validation_warnings: list[str] = []

    for row in rows:
        stmt = dict(zip(cols, row))
        if stmt["validation_status"] == "passed":
            statements_included += 1
        else:
            statements_excluded += 1
            # Reason: provide actionable context so the user knows
            # which specific statement/period is problematic.
            period = str(stmt.get("period_start", "?"))
            bank = stmt.get("bank_name", "Unknown")
            status = stmt.get("validation_status", "unknown")
            validation_warnings.append(
                f"{bank} statement ({period}): {status}"
            )

    logger.debug(
        "Data quality: %d included, %d excluded, %d warnings",
        statements_included, statements_excluded, len(validation_warnings),
    )

    # ── Coverage gaps (months with no passed statements) ────────
    coverage_gaps = _find_coverage_gaps(
        c, start_date, end_date, acct_filter, acct_params,
    )
    logger.debug("Coverage gaps: %s", coverage_gaps)

    return {
        "statements_included": statements_included,
        "statements_excluded": statements_excluded,
        "validation_warnings": validation_warnings,
        "coverage_gaps": coverage_gaps,
    }


def _find_coverage_gaps(
    c,
    start_date: str,
    end_date: str,
    acct_filter: str,
    acct_params: list,
) -> list[str]:
    """Identify months within the range that have no passed statements.

    Returns a list of gap strings like '2025-04-01 to 2025-04-30'.
    """
    # Reason: we build a set of covered months from passed statements,
    # then walk the requested range to find any missing months.
    stmt_sql = f"""
        SELECT period_start, period_end
        FROM bank_statements
        WHERE validation_status = 'passed'
          AND period_start <= ? AND period_end >= ?
          {acct_filter.replace('t.account_id', 'account_id')}
    """
    rows = c.execute(stmt_sql, [end_date, start_date] + acct_params).fetchall()

    covered_months: set[str] = set()
    for row in rows:
        ps, pe = str(row[0]), str(row[1])
        try:
            s = datetime.strptime(ps, "%Y-%m-%d")
            e = datetime.strptime(pe, "%Y-%m-%d")
            current = s
            while current <= e:
                covered_months.add(current.strftime("%Y-%m"))
                # Advance to the first day of the next month
                if current.month == 12:
                    current = current.replace(year=current.year + 1, month=1, day=1)
                else:
                    current = current.replace(month=current.month + 1, day=1)
        except ValueError:
            continue

    # Walk the requested range month by month
    gaps: list[str] = []
    try:
        current = datetime.strptime(start_date[:7] + "-01", "%Y-%m-%d")
        end = datetime.strptime(end_date[:7] + "-01", "%Y-%m-%d")

        while current <= end:
            month_key = current.strftime("%Y-%m")
            if month_key not in covered_months:
                # Reason: compute the last day of the month without
                # dateutil by advancing to next month and subtracting.
                if current.month == 12:
                    last_day = current.replace(
                        year=current.year + 1, month=1, day=1,
                    )
                else:
                    last_day = current.replace(month=current.month + 1, day=1)
                last_day = last_day - timedelta(days=1)
                gaps.append(
                    f"{current.strftime('%Y-%m-%d')} to {last_day.strftime('%Y-%m-%d')}"
                )

            # Advance to next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)
    except ValueError:
        pass

    return gaps


# ── Markdown renderer ───────────────────────────────────────────────


def _render_markdown(report: dict) -> str:
    """Convert a structured report dict to a readable Markdown string."""
    lines: list[str] = []
    rtype = report.get("report_type", "report")
    period = report.get("period", {})

    lines.append(f"# Financial Report — {_title_case(rtype)}")
    lines.append(f"**Period:** {period.get('start', '?')} to {period.get('end', '?')}")
    if report.get("account_id"):
        lines.append(f"**Account:** {report['account_id']}")
    lines.append("")

    # ── Data quality ────────────────────────────────────────────
    dq = report.get("data_quality", {})
    lines.append("## Data Quality")
    lines.append(f"- Statements included: {dq.get('statements_included', 0)}")
    lines.append(f"- Statements excluded: {dq.get('statements_excluded', 0)}")
    if dq.get("validation_warnings"):
        lines.append("- **Warnings:**")
        for w in dq["validation_warnings"]:
            lines.append(f"  - {w}")
    if dq.get("coverage_gaps"):
        lines.append("- **Coverage gaps:**")
        for g in dq["coverage_gaps"]:
            lines.append(f"  - {g}")
    lines.append("")

    # ── Monthly summary / annual overview months table ──────────
    months = report.get("months", [])
    if months:
        lines.append("## Monthly Breakdown")
        lines.append("")
        lines.append("| Year | Month | Income | Expenses | Net | Txns |")
        lines.append("|------|-------|--------|----------|-----|------|")
        for m in months:
            lines.append(
                f"| {m['year']} | {m['month']:02d} "
                f"| {_fmt_cents(m['total_income_cents'])} "
                f"| {_fmt_cents(m['total_expenses_cents'])} "
                f"| {_fmt_cents(m['net_cents'])} "
                f"| {m['transaction_count']} |"
            )
        lines.append("")

    # ── Annual totals ───────────────────────────────────────────
    annual = report.get("annual_totals")
    if annual:
        lines.append("## Annual Totals")
        lines.append(f"- Total income: {_fmt_cents(annual['total_income_cents'])}")
        lines.append(f"- Total expenses: {_fmt_cents(annual['total_expenses_cents'])}")
        lines.append(f"- Net: {_fmt_cents(annual['net_cents'])}")
        lines.append(f"- Transactions: {annual['transaction_count']}")
        lines.append("")

    # ── Top categories ──────────────────────────────────────────
    top_cats = report.get("top_categories", [])
    if top_cats:
        lines.append("## Top Categories")
        lines.append("")
        lines.append("| Category | Total | Count |")
        lines.append("|----------|-------|-------|")
        for tc in top_cats:
            lines.append(
                f"| {tc['group']} | {_fmt_cents(tc['total_cents'])} | {tc['count']} |"
            )
        lines.append("")

    # ── Top merchants ───────────────────────────────────────────
    top_merch = report.get("top_merchants", [])
    if top_merch:
        lines.append("## Top Merchants")
        lines.append("")
        lines.append("| Merchant | Total | Count |")
        lines.append("|----------|-------|-------|")
        for tm in top_merch:
            lines.append(
                f"| {tm['group']} | {_fmt_cents(tm['total_cents'])} | {tm['count']} |"
            )
        lines.append("")

    # ── Largest expenses ────────────────────────────────────────
    largest = report.get("largest_expenses", [])
    if largest:
        lines.append("## Top 10 Largest Expenses")
        lines.append("")
        lines.append("| Date | Description | Merchant | Amount | Category |")
        lines.append("|------|-------------|----------|--------|----------|")
        for le in largest:
            lines.append(
                f"| {le['transaction_date']} "
                f"| {le['description'][:40]} "
                f"| {le.get('merchant') or '—'} "
                f"| {_fmt_cents(le['amount_cents'])} "
                f"| {le.get('category') or '—'} |"
            )
        lines.append("")

    # ── Category breakdown ──────────────────────────────────────
    categories = report.get("categories", [])
    if categories:
        lines.append("## Category Breakdown")
        lines.append("")
        lines.append("| Category | Total | Count | % of Total |")
        lines.append("|----------|-------|-------|------------|")
        for cat in categories:
            lines.append(
                f"| {cat['category']} "
                f"| {_fmt_cents(cat['total_cents'])} "
                f"| {cat['count']} "
                f"| {cat['percentage']:.1f}% |"
            )
        lines.append("")

    return "\n".join(lines)


# ── Private helpers ─────────────────────────────────────────────────


def _account_filter(account_id: str | None) -> tuple[str, list]:
    """Build an optional AND clause for account_id filtering.

    Returns:
        (sql_fragment, params_list)
    """
    if account_id:
        return "AND t.account_id = ?", [account_id]
    return "", []


def _top_groups(
    c,
    group_expr: str,
    alias: str,
    start_date: str,
    end_date: str,
    acct_filter: str,
    acct_params: list,
    limit: int = 5,
) -> list[dict]:
    """Query top N groups by absolute spend."""
    sql = f"""
        SELECT
            {group_expr} AS grp,
            SUM(t.amount_cents) AS total_cents,
            COUNT(*)            AS count
        FROM transactions t
        JOIN bank_statements bs ON t.statement_id = bs.statement_id
        WHERE t.transaction_date >= ?
          AND t.transaction_date <= ?
          AND bs.validation_status = 'passed'
          {acct_filter}
        GROUP BY grp
        ORDER BY ABS(SUM(t.amount_cents)) DESC
        LIMIT ?
    """
    params: list = [start_date, end_date] + acct_params + [limit]
    rows = c.execute(sql, params).fetchall()
    logger.debug("Top %d %s groups: %d rows", limit, alias, len(rows))

    return [
        {"group": row[0], "total_cents": int(row[1]), "count": int(row[2])}
        for row in rows
    ]


def _fmt_cents(cents: int) -> str:
    """Format integer cents as a currency string (R1 234.56)."""
    negative = cents < 0
    abs_cents = abs(cents)
    rands = abs_cents // 100
    remainder = abs_cents % 100
    # Reason: manual thousands-separator formatting avoids locale issues.
    rands_str = f"{rands:,}".replace(",", " ")
    formatted = f"R{rands_str}.{remainder:02d}"
    return f"-{formatted}" if negative else formatted


def _title_case(s: str) -> str:
    """Convert a snake_case string to Title Case."""
    return s.replace("_", " ").title()


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Report error [%s]: %s", code, message)
    return {"error": code, "message": message}
