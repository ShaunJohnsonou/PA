"""``find_anomalies`` MCP tool handler (FR-4.8).

Scans transactions for unusual patterns: large transactions,
new merchants, duplicate charges, and category spending spikes.
"""
from __future__ import annotations

import logging

from ..finance.ledger_db import FinanceLedger

logger = logging.getLogger(__name__)

# Sensitivity multipliers for anomaly thresholds
_SENSITIVITY = {
    "low": {"large_txn_mult": 5.0, "spike_pct": 100},
    "medium": {"large_txn_mult": 3.0, "spike_pct": 50},
    "high": {"large_txn_mult": 2.0, "spike_pct": 30},
}


async def handle_find_anomalies(
    start_date: str,
    end_date: str,
    account_id: str | None = None,
    sensitivity: str = "medium",
    *,
    ledger: FinanceLedger,
) -> dict:
    """Scan transactions for anomalies."""
    logger.info(
        "find_anomalies: start=%s, end=%s, account=%s, sensitivity=%s",
        start_date, end_date, account_id, sensitivity,
    )
    c = ledger.conn

    if sensitivity not in _SENSITIVITY:
        logger.debug("Invalid sensitivity=%r, defaulting to 'medium'", sensitivity)
        sensitivity = "medium"
    thresholds = _SENSITIVITY[sensitivity]
    logger.debug("Thresholds: %s", thresholds)

    where_parts = [
        "t.transaction_date >= ?",
        "t.transaction_date <= ?",
    ]
    params: list = [start_date, end_date]

    if account_id:
        where_parts.append("t.account_id = ?")
        params.append(account_id)

    where_clause = " WHERE " + " AND ".join(where_parts)
    logger.debug("WHERE clause: %s  params=%s", where_clause[:200], params)

    anomalies: list[dict] = []

    # -- 1. Large transactions ---------------------------------------------
    logger.debug("Scanning for large transactions...")
    large = _find_large_transactions(c, where_clause, params, thresholds)
    logger.debug("Found %d large transaction anomalies", len(large))
    anomalies.extend(large)

    # -- 2. New merchants --------------------------------------------------
    logger.debug("Scanning for new merchants...")
    new_m = _find_new_merchants(c, where_clause, params, start_date)
    logger.debug("Found %d new merchant anomalies", len(new_m))
    anomalies.extend(new_m)

    # -- 3. Duplicate charges ----------------------------------------------
    logger.debug("Scanning for duplicate charges...")
    dupes = _find_duplicates(c, where_clause, params)
    logger.debug("Found %d duplicate charge anomalies", len(dupes))
    anomalies.extend(dupes)

    # -- 4. Category spikes ------------------------------------------------
    logger.debug("Scanning for category spending spikes...")
    spikes = _find_category_spikes(c, where_clause, params, start_date, thresholds)
    logger.debug("Found %d category spike anomalies", len(spikes))
    anomalies.extend(spikes)

    # Count scanned transactions
    count_sql = f"SELECT COUNT(*) FROM transactions t{where_clause}"
    scanned = c.execute(count_sql, params).fetchone()[0]

    logger.info(
        "find_anomalies complete: scanned=%d, flagged=%d (large=%d, new=%d, dupes=%d, spikes=%d)",
        scanned, len(anomalies), len(large), len(new_m), len(dupes), len(spikes),
    )

    return {
        "anomalies": anomalies,
        "total_flagged": len(anomalies),
        "scanned_transactions": scanned,
    }


def _find_large_transactions(c, where_clause: str, params: list, thresholds: dict) -> list[dict]:
    """Find transactions exceeding Nx the category monthly average."""
    mult = thresholds["large_txn_mult"]

    # Compute per-category average (absolute) over all history
    avg_sql = """
        SELECT category, CAST(AVG(ABS(amount_cents)) AS BIGINT) AS avg_abs
        FROM transactions
        WHERE amount_cents < 0
        GROUP BY category
    """
    try:
        avg_rows = c.execute(avg_sql).fetchall()
    except Exception as exc:
        logger.warning("Category average query failed: %s", exc)
        return []

    cat_avgs = {r[0]: r[1] for r in avg_rows if r[1]}
    logger.debug("Category averages: %s", {k: f"R{v/100:.2f}" for k, v in list(cat_avgs.items())[:10]})

    # Find transactions that exceed the threshold
    txn_sql = f"""
        SELECT t.transaction_id, t.transaction_date, t.description_raw,
               t.amount_cents, t.category, t.merchant
        FROM transactions t{where_clause}
        AND t.amount_cents < 0
    """
    try:
        rows = c.execute(txn_sql, params).fetchall()
    except Exception as exc:
        logger.warning("Large transaction query failed: %s", exc)
        return []

    logger.debug("Scanning %d debit transactions for large amounts (mult=%.1f)", len(rows), mult)

    anomalies = []
    for row in rows:
        txn_id, txn_date, desc, amount, cat, merchant = row
        avg = cat_avgs.get(cat)
        if avg and abs(amount) > avg * mult:
            anomalies.append({
                "type": "large_transaction",
                "transaction_id": txn_id,
                "description": f"{desc} ({merchant or 'unknown'})",
                "severity": "high" if abs(amount) > avg * mult * 2 else "medium",
                "details": f"R{abs(amount)/100:.2f} vs category avg R{avg/100:.2f} ({abs(amount)/avg:.1f}x)",
            })
            logger.debug(
                "  FLAGGED large: %s R%.2f (%.1fx avg)",
                desc[:40], abs(amount)/100, abs(amount)/avg,
            )

    return anomalies[:20]


def _find_new_merchants(c, where_clause: str, params: list, start_date: str) -> list[dict]:
    """Find merchants seen for the first time in the analysis period."""
    sql = f"""
        SELECT DISTINCT t.merchant, t.transaction_id, t.amount_cents, t.transaction_date
        FROM transactions t{where_clause}
        AND t.merchant IS NOT NULL
        AND t.merchant NOT IN (
            SELECT DISTINCT merchant FROM transactions
            WHERE merchant IS NOT NULL AND transaction_date < ?
        )
    """
    try:
        rows = c.execute(sql, params + [start_date]).fetchall()
    except Exception as exc:
        logger.warning("New merchant query failed: %s", exc)
        return []

    logger.debug("Found %d new merchants in period", len(rows))
    for r in rows[:5]:
        logger.debug("  New merchant: %s (R%.2f on %s)", r[0], abs(r[2])/100, r[3])

    return [
        {
            "type": "new_merchant",
            "transaction_id": r[1],
            "description": f"New merchant: {r[0]}",
            "severity": "low",
            "details": f"R{abs(r[2])/100:.2f} on {r[3]}",
        }
        for r in rows[:15]
    ]


def _find_duplicates(c, where_clause: str, params: list) -> list[dict]:
    """Find same amount + merchant within same day."""
    sql = f"""
        SELECT t.merchant, t.amount_cents, t.transaction_date, COUNT(*) AS cnt,
               MIN(t.transaction_id) AS first_id
        FROM transactions t{where_clause}
        AND t.merchant IS NOT NULL
        GROUP BY t.merchant, t.amount_cents, t.transaction_date
        HAVING COUNT(*) > 1
    """
    try:
        rows = c.execute(sql, params).fetchall()
    except Exception as exc:
        logger.warning("Duplicate detection query failed: %s", exc)
        return []

    logger.debug("Found %d potential duplicate groups", len(rows))
    for r in rows[:5]:
        logger.debug("  Duplicate: %s R%.2f x%d on %s", r[0], abs(r[1])/100, r[3], r[2])

    return [
        {
            "type": "duplicate_charge",
            "transaction_id": r[4],
            "description": f"Possible duplicate: {r[0]} R{abs(r[1])/100:.2f}",
            "severity": "medium",
            "details": f"{r[3]} charges on {r[2]}",
        }
        for r in rows[:10]
    ]


def _find_category_spikes(
    c, where_clause: str, params: list, start_date: str, thresholds: dict
) -> list[dict]:
    """Find categories where spending increased >X% vs prior period."""
    spike_pct = thresholds["spike_pct"]

    # Current period totals by category
    current_sql = f"""
        SELECT t.category, SUM(ABS(t.amount_cents)) AS total
        FROM transactions t{where_clause}
        AND t.amount_cents < 0
        GROUP BY t.category
    """

    # Historical average by category (before this period)
    hist_sql = """
        SELECT category,
               CAST(AVG(monthly_total) AS BIGINT) AS avg_monthly
        FROM (
            SELECT category,
                   strftime(transaction_date, '%Y-%m') AS month,
                   SUM(ABS(amount_cents)) AS monthly_total
            FROM transactions
            WHERE amount_cents < 0 AND transaction_date < ?
            GROUP BY category, strftime(transaction_date, '%Y-%m')
        ) sub
        GROUP BY category
    """

    try:
        current_rows = c.execute(current_sql, params).fetchall()
        hist_rows = c.execute(hist_sql, [start_date]).fetchall()
    except Exception as exc:
        logger.warning("Category spike query failed: %s", exc)
        return []

    hist_avgs = {r[0]: r[1] for r in hist_rows if r[1]}
    logger.debug("Historical category averages: %d categories", len(hist_avgs))

    anomalies = []
    for cat, current_total in current_rows:
        avg = hist_avgs.get(cat)
        if avg and avg > 0:
            pct_change = ((current_total - avg) / avg) * 100
            if pct_change > spike_pct:
                anomalies.append({
                    "type": "category_spike",
                    "transaction_id": None,
                    "description": f"Category '{cat}' spending up {pct_change:.0f}%",
                    "severity": "medium",
                    "details": f"R{current_total/100:.2f} vs avg R{avg/100:.2f}/month",
                })
                logger.debug(
                    "  SPIKE: %s +%.0f%% (R%.2f vs avg R%.2f)",
                    cat, pct_change, current_total/100, avg/100,
                )

    return anomalies[:10]
