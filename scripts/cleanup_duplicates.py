"""One-time script to remove duplicate transactions and backfill fingerprints.

Run this on the VM after deploying the fingerprint changes:
    python3 scripts/cleanup_duplicates.py /path/to/finance.duckdb

It will:
1. Show current duplicate count
2. Remove duplicates (keep the oldest row per group)
3. Backfill fingerprints for all remaining rows
4. Run transfer detection to tag inter-account transfers
5. Show final stats
"""
from __future__ import annotations

import hashlib
import sys


def compute_fingerprint(account_id, txn_date, amount_cents, description_raw):
    key = f"{account_id or ''}|{txn_date}|{amount_cents}|{description_raw}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def main(db_path: str) -> None:
    import duckdb

    print(f"Opening: {db_path}")
    conn = duckdb.connect(db_path)

    # ── 1. Show current state ────────────────────────────────────
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"\nTotal transactions: {total}")

    dupes = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT account_id, transaction_date, amount_cents, description_raw,
                   COUNT(*) as cnt
            FROM transactions
            GROUP BY account_id, transaction_date, amount_cents, description_raw
            HAVING cnt > 1
        )
    """).fetchone()[0]
    print(f"Duplicate groups: {dupes}")

    if dupes > 0:
        # Show some examples
        print("\nExample duplicates:")
        examples = conn.execute("""
            SELECT account_id, transaction_date, amount_cents, description_raw,
                   COUNT(*) as cnt
            FROM transactions
            GROUP BY account_id, transaction_date, amount_cents, description_raw
            HAVING cnt > 1
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()
        for acct, date, amount, desc, cnt in examples:
            print(f"  {cnt}x | {date} | R{amount/100:.2f} | {desc[:50]}")

    # ── 2. Remove duplicates (keep oldest per group) ─────────────
    if dupes > 0:
        print(f"\nRemoving duplicates (keeping oldest per group)...")
        deleted = conn.execute("""
            DELETE FROM transactions
            WHERE transaction_id IN (
                SELECT transaction_id FROM (
                    SELECT transaction_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY account_id, transaction_date,
                                            amount_cents, description_raw
                               ORDER BY created_at ASC
                           ) AS rn
                    FROM transactions
                ) WHERE rn > 1
            )
        """).fetchone()
        
        remaining = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        removed = total - remaining
        print(f"  Removed {removed} duplicate rows")
        print(f"  Remaining: {remaining} transactions")
    else:
        print("\nNo duplicates to remove.")

    # ── 3. Backfill fingerprints ─────────────────────────────────
    print("\nBackfilling fingerprints...")
    rows = conn.execute(
        "SELECT transaction_id, account_id, transaction_date, amount_cents, description_raw "
        "FROM transactions WHERE txn_fingerprint IS NULL"
    ).fetchall()

    if rows:
        print(f"  Computing fingerprints for {len(rows)} rows...")
        for txn_id, acct_id, txn_date, amount, desc in rows:
            fp = compute_fingerprint(acct_id, str(txn_date), amount, desc)
            conn.execute(
                "UPDATE transactions SET txn_fingerprint = ? WHERE transaction_id = ?",
                [fp, txn_id],
            )
        print(f"  Done — {len(rows)} fingerprints set")
    else:
        print("  All rows already have fingerprints")

    # ── 4. Run transfer detection ────────────────────────────────
    print("\nRunning inter-account transfer detection...")
    result = conn.execute("""
        WITH transfer_pairs AS (
            SELECT
                t1.transaction_id AS debit_id,
                t2.transaction_id AS credit_id,
                t1.amount_cents,
                ROW_NUMBER() OVER (
                    PARTITION BY t1.transaction_id
                    ORDER BY ABS(t1.transaction_date - t2.transaction_date)
                ) AS rn
            FROM transactions t1
            JOIN transactions t2
              ON t1.amount_cents = -t2.amount_cents
              AND t1.amount_cents < 0
              AND t1.account_id != t2.account_id
              AND ABS(t1.transaction_date - t2.transaction_date) <= 2
              AND (t1.category IS NULL OR t1.category != 'Transfer')
              AND (t2.category IS NULL OR t2.category != 'Transfer')
        )
        SELECT debit_id, credit_id
        FROM transfer_pairs
        WHERE rn = 1
    """).fetchall()

    if result:
        transfer_ids = set()
        for debit_id, credit_id in result:
            transfer_ids.add(debit_id)
            transfer_ids.add(credit_id)

        for txn_id in transfer_ids:
            conn.execute(
                "UPDATE transactions SET category = 'Transfer', category_confidence = 1.0 "
                "WHERE transaction_id = ?",
                [txn_id],
            )
        print(f"  Tagged {len(transfer_ids)} transactions as 'Transfer' ({len(result)} pairs)")
    else:
        print("  No inter-account transfers found")

    # ── 5. Final stats ───────────────────────────────────────────
    final = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    transfers = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE category = 'Transfer'"
    ).fetchone()[0]
    
    print(f"\n{'='*50}")
    print(f"FINAL STATE:")
    print(f"  Total transactions: {final}")
    print(f"  Tagged as Transfer: {transfers}")
    print(f"  Non-transfer transactions: {final - transfers}")

    # Verify no remaining duplicates
    remaining_dupes = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT txn_fingerprint, COUNT(*) as cnt
            FROM transactions
            WHERE txn_fingerprint IS NOT NULL
            GROUP BY txn_fingerprint
            HAVING cnt > 1
        )
    """).fetchone()[0]
    
    if remaining_dupes == 0:
        print(f"  ✅ Zero duplicate fingerprints — dedup is clean!")
    else:
        print(f"  ⚠️  {remaining_dupes} duplicate fingerprints remain")

    conn.close()
    print(f"\nDone. Database closed.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python cleanup_duplicates.py /path/to/finance.duckdb")
        sys.exit(1)
    main(sys.argv[1])
