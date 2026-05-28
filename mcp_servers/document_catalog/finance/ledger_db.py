"""DuckDB financial ledger database layer (FR-4.1).

Manages the ``finance.duckdb`` database containing accounts, bank
statements, transactions, validation results, and extraction evidence.

Design decisions:
- All monetary values are stored as BIGINT in integer cents
  (R123.45 = 12345). This eliminates floating-point rounding errors.
- DuckDB is chosen over SQLite for the financial layer because it
  excels at analytical queries (GROUP BY, window functions, Parquet
  import/export) essential for spending analysis and trend detection.
- DuckDB does not enforce FK constraints, but the application layer does.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 2


class FinanceLedger:
    """DuckDB-backed financial ledger with versioned schema."""

    def __init__(self, db_path: str) -> None:
        """Open (or create) the finance database.

        Args:
            db_path: Absolute path to the finance.duckdb file.
        """
        self._db_path = db_path
        self._conn = None

    def connect(self) -> None:
        """Open the DuckDB connection and ensure the schema exists."""
        import duckdb
        import time

        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        
        max_retries = 15
        retry_delay = 2.0
        for attempt in range(max_retries):
            try:
                self._conn = duckdb.connect(self._db_path)
                break
            except duckdb.IOException as e:
                if "lock" in str(e).lower() or "io error" in str(e).lower():
                    if attempt < max_retries - 1:
                        logger.warning(
                            "DuckDB is locked by another process. Retrying in %.1fs... (Attempt %d/%d)", 
                            retry_delay, attempt + 1, max_retries
                        )
                        time.sleep(retry_delay)
                    else:
                        logger.error("Failed to acquire DuckDB lock after %d attempts.", max_retries)
                        raise
                else:
                    raise

        self._ensure_schema()
        logger.info("FinanceLedger opened: %s (schema v%d)", self._db_path, CURRENT_SCHEMA_VERSION)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            logger.info("FinanceLedger closing: %s", self._db_path)
            self._conn.close()
            self._conn = None

    @property
    def conn(self):
        """Return the raw DuckDB connection for direct queries."""
        assert self._conn is not None, "FinanceLedger not connected"
        return self._conn

    # ── Schema management ───────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        c = self._conn

        c.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id              TEXT PRIMARY KEY,
                bank_name               TEXT NOT NULL,
                account_number_masked   TEXT,
                account_type            TEXT,
                currency                TEXT NOT NULL DEFAULT 'ZAR',
                first_seen_date         DATE,
                last_seen_date          DATE,
                created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS bank_statements (
                statement_id            TEXT PRIMARY KEY,
                document_id             TEXT NOT NULL,
                account_id              TEXT NOT NULL,
                bank_name               TEXT,
                account_number_masked   TEXT,
                period_start            DATE NOT NULL,
                period_end              DATE NOT NULL,
                opening_balance_cents   BIGINT,
                closing_balance_cents   BIGINT,
                total_debits_cents      BIGINT,
                total_credits_cents     BIGINT,
                currency                TEXT NOT NULL DEFAULT 'ZAR',
                page_count              INTEGER,
                transaction_count       INTEGER,
                extraction_status       TEXT NOT NULL DEFAULT 'pending',
                validation_status       TEXT DEFAULT 'pending',
                created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id          TEXT PRIMARY KEY,
                statement_id            TEXT NOT NULL,
                account_id              TEXT,
                transaction_date        DATE NOT NULL,
                posting_date            DATE,
                description_raw         TEXT NOT NULL,
                description_clean       TEXT,
                merchant                TEXT,
                counterparty            TEXT,
                reference               TEXT,
                amount_cents            BIGINT NOT NULL,
                currency                TEXT NOT NULL DEFAULT 'ZAR',
                balance_after_cents     BIGINT,
                category                TEXT,
                category_confidence     REAL,
                source_document_id      TEXT NOT NULL,
                source_page             INTEGER,
                source_row              INTEGER,
                source_bbox             TEXT,
                extraction_method       TEXT,
                extraction_confidence   REAL,
                txn_fingerprint         TEXT,
                created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes for transactions
        c.execute("CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(transaction_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_txn_statement ON transactions(statement_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_txn_merchant ON transactions(merchant)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_fingerprint ON transactions(txn_fingerprint)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS validation_results (
                validation_id   TEXT PRIMARY KEY,
                document_id     TEXT NOT NULL,
                statement_id    TEXT,
                rule_name       TEXT NOT NULL,
                passed          BOOLEAN NOT NULL,
                expected_value  TEXT,
                actual_value    TEXT,
                severity        TEXT NOT NULL,
                notes           TEXT,
                validated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS extraction_evidence (
                evidence_id         TEXT PRIMARY KEY,
                transaction_id      TEXT NOT NULL,
                source_document_id  TEXT NOT NULL,
                source_page         INTEGER,
                source_row          INTEGER,
                source_bbox         TEXT,
                raw_text            TEXT,
                extraction_method   TEXT,
                confidence          REAL
            )
        """)

        # Upsert schema version
        row = c.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            c.execute("INSERT INTO schema_version VALUES (?)", [CURRENT_SCHEMA_VERSION])

        # ── Schema migrations ──────────────────────────────────────
        current_version = row[0] if row else CURRENT_SCHEMA_VERSION
        if current_version < 2:
            logger.info("Migrating ledger schema v%d → v2: adding txn_fingerprint", current_version)
            try:
                c.execute("ALTER TABLE transactions ADD COLUMN txn_fingerprint TEXT")
                logger.info("Added txn_fingerprint column")
            except Exception:
                # Reason: column may already exist if migration was partially applied
                logger.debug("txn_fingerprint column already exists, skipping ALTER")
            try:
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_fingerprint ON transactions(txn_fingerprint)")
                logger.info("Created UNIQUE index on txn_fingerprint")
            except Exception:
                logger.debug("idx_txn_fingerprint index already exists, skipping")
            # Backfill fingerprints for existing rows that don't have one
            self._backfill_fingerprints()
            c.execute("UPDATE schema_version SET version = ?", [2])
            logger.info("Schema migration to v2 complete")

    # ── Account CRUD ────────────────────────────────────────────────

    def upsert_account(
        self,
        bank_name: str,
        account_number_masked: str,
        account_type: str | None = None,
        currency: str = "ZAR",
        seen_date: str | None = None,
    ) -> str:
        """Insert or update an account. Returns the account_id."""
        logger.debug(
            "upsert_account: bank=%s, number=%s, type=%s, currency=%s, seen=%s",
            bank_name, account_number_masked, account_type, currency, seen_date,
        )
        row = self._conn.execute(
            "SELECT account_id, first_seen_date, last_seen_date FROM accounts "
            "WHERE bank_name = ? AND account_number_masked = ?",
            [bank_name, account_number_masked],
        ).fetchone()

        if row:
            account_id = row[0]
            updates = {}
            if seen_date:
                if row[1] is None or str(seen_date) < str(row[1]):
                    updates["first_seen_date"] = seen_date
                if row[2] is None or str(seen_date) > str(row[2]):
                    updates["last_seen_date"] = seen_date
            if updates:
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                self._conn.execute(
                    f"UPDATE accounts SET {set_clause} WHERE account_id = ?",
                    list(updates.values()) + [account_id],
                )
                logger.debug("Updated account %s: %s", account_id, updates)
            else:
                logger.debug("Account %s already up-to-date", account_id)
            return account_id

        account_id = str(uuid.uuid4())
        self._conn.execute(
            """INSERT INTO accounts
               (account_id, bank_name, account_number_masked, account_type,
                currency, first_seen_date, last_seen_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [account_id, bank_name, account_number_masked, account_type,
             currency, seen_date, seen_date],
        )
        logger.info("Created account %s: %s %s", account_id, bank_name, account_number_masked)
        return account_id

    # ── Statement CRUD ──────────────────────────────────────────────

    def insert_statement(self, **fields) -> str:
        """Insert a bank_statements row. Returns the statement_id."""
        statement_id = fields.get("statement_id") or str(uuid.uuid4())
        fields["statement_id"] = statement_id

        logger.debug(
            "insert_statement: id=%s, doc=%s, account=%s, period=%s to %s, txns=%s",
            statement_id, fields.get("document_id"), fields.get("account_id"),
            fields.get("period_start"), fields.get("period_end"),
            fields.get("transaction_count"),
        )

        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        self._conn.execute(
            f"INSERT INTO bank_statements ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        logger.info("Inserted statement %s", statement_id)
        return statement_id

    def get_statement_by_document(self, document_id: str) -> dict | None:
        """Look up a statement by its source document_id."""
        logger.debug("get_statement_by_document: doc=%s", document_id)
        row = self._conn.execute(
            "SELECT * FROM bank_statements WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if row is None:
            logger.debug("No statement found for doc=%s", document_id)
            return None
        cols = [desc[0] for desc in self._conn.description]
        result = dict(zip(cols, row))
        logger.debug("Found statement %s for doc=%s", result.get("statement_id"), document_id)
        return result

    # ── Transaction CRUD ────────────────────────────────────────────

    @staticmethod
    def compute_fingerprint(
        account_id: str | None,
        txn_date: str,
        amount_cents: int,
        description_raw: str,
    ) -> str:
        """Compute a dedup fingerprint for a transaction.

        Reason: This creates a short, deterministic hash from the business
        key fields. Two transactions with the same account, date, amount,
        and description will produce the same fingerprint, allowing the
        UNIQUE index to prevent duplicates at the database level.

        Args:
            account_id: Account identifier (can be None).
            txn_date: Transaction date as string (YYYY-MM-DD).
            amount_cents: Amount in integer cents.
            description_raw: Raw bank description.

        Returns:
            16-char hex string (first 64 bits of SHA-256).
        """
        key = f"{account_id or ''}|{txn_date}|{amount_cents}|{description_raw}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def insert_transactions(self, transactions: list[dict]) -> int:
        """Bulk-insert transaction rows with dedup via fingerprint.

        Computes a txn_fingerprint for each row if not already set.
        Uses INSERT OR IGNORE so duplicate fingerprints are silently
        skipped rather than raising errors.

        Returns:
            Count of actually inserted rows.
        """
        if not transactions:
            logger.debug("insert_transactions: no transactions to insert")
            return 0

        logger.info("insert_transactions: processing %d candidate rows...", len(transactions))

        # Reason: compute fingerprint for each transaction if not already set
        for t in transactions:
            if not t.get("txn_fingerprint"):
                t["txn_fingerprint"] = self.compute_fingerprint(
                    t.get("account_id"),
                    str(t.get("transaction_date", "")),
                    t.get("amount_cents", 0),
                    t.get("description_raw", ""),
                )

        cols = list(transactions[0].keys())
        col_str = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)

        # Reason: insert one-by-one with try/except to skip duplicates,
        # because DuckDB doesn't support INSERT OR IGNORE / ON CONFLICT
        # on non-primary-key unique constraints.
        inserted = 0
        skipped = 0
        for t in transactions:
            row = tuple(t[c] for c in cols)
            try:
                self._conn.execute(
                    f"INSERT INTO transactions ({col_str}) VALUES ({placeholders})",
                    row,
                )
                inserted += 1
            except Exception as exc:
                if "unique" in str(exc).lower() or "duplicate" in str(exc).lower() or "constraint" in str(exc).lower():
                    skipped += 1
                    logger.debug(
                        "Skipped duplicate: %s %s %s cents (fingerprint=%s)",
                        t.get("transaction_date"), t.get("description_raw", "")[:40],
                        t.get("amount_cents"), t.get("txn_fingerprint"),
                    )
                else:
                    logger.error("Failed to insert transaction: %s", exc)
                    raise

        logger.info(
            "insert_transactions complete: %d inserted, %d duplicates skipped (of %d total)",
            inserted, skipped, len(transactions),
        )
        return inserted

    def _backfill_fingerprints(self) -> None:
        """Backfill txn_fingerprint for existing rows that don't have one."""
        rows = self._conn.execute(
            "SELECT transaction_id, account_id, transaction_date, amount_cents, description_raw "
            "FROM transactions WHERE txn_fingerprint IS NULL"
        ).fetchall()
        if not rows:
            logger.debug("No rows need fingerprint backfill")
            return

        logger.info("Backfilling fingerprints for %d existing transactions...", len(rows))
        for txn_id, acct_id, txn_date, amount, desc in rows:
            fp = self.compute_fingerprint(acct_id, str(txn_date), amount, desc)
            try:
                self._conn.execute(
                    "UPDATE transactions SET txn_fingerprint = ? WHERE transaction_id = ?",
                    [fp, txn_id],
                )
            except Exception as exc:
                # Reason: if two rows produce the same fingerprint, one is a
                # duplicate. Mark the second one for deletion by setting a
                # special fingerprint.
                logger.warning(
                    "Duplicate detected during backfill: txn_id=%s, fingerprint=%s: %s",
                    txn_id, fp, exc,
                )
        logger.info("Fingerprint backfill complete")

    def detect_transfers(self) -> int:
        """Detect inter-account transfers and tag them.

        Reason: When the user transfers money between their own accounts,
        it appears as a debit on one account and a credit on the other.
        Both are real transactions, but they should be tagged as 'Transfer'
        so they don't inflate income/expense totals.

        Matching criteria:
        - Same absolute amount
        - Opposite signs (one positive, one negative)
        - Different accounts
        - Within 2 days of each other (to handle posting delays)
        - Neither already categorized as 'Transfer'

        Returns:
            Number of transactions tagged as transfers.
        """
        logger.info("Running inter-account transfer detection...")

        # Reason: find matching debit/credit pairs across different accounts
        # within a 2-day window. Tag both sides as 'Transfer'.
        result = self._conn.execute("""
            WITH transfer_pairs AS (
                SELECT
                    t1.transaction_id AS debit_id,
                    t2.transaction_id AS credit_id,
                    t1.amount_cents,
                    t1.transaction_date AS debit_date,
                    t2.transaction_date AS credit_date,
                    t1.account_id AS debit_account,
                    t2.account_id AS credit_account,
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

        if not result:
            logger.info("No inter-account transfers detected")
            return 0

        # Collect all transaction IDs to tag
        transfer_ids = set()
        for debit_id, credit_id in result:
            transfer_ids.add(debit_id)
            transfer_ids.add(credit_id)

        # Tag them as Transfer
        for txn_id in transfer_ids:
            self._conn.execute(
                "UPDATE transactions SET category = 'Transfer', category_confidence = 1.0 "
                "WHERE transaction_id = ?",
                [txn_id],
            )

        logger.info(
            "Transfer detection complete: %d pairs found, %d transactions tagged as 'Transfer'",
            len(result), len(transfer_ids),
        )
        return len(transfer_ids)

    def delete_statement_data(self, statement_id: str) -> None:
        """Remove all data for a statement (for re-extraction)."""
        logger.info("delete_statement_data: cascading delete for statement=%s", statement_id)
        self._conn.execute(
            "DELETE FROM extraction_evidence WHERE transaction_id IN "
            "(SELECT transaction_id FROM transactions WHERE statement_id = ?)",
            [statement_id],
        )
        logger.debug("  Deleted extraction_evidence for statement=%s", statement_id)
        self._conn.execute(
            "DELETE FROM validation_results WHERE statement_id = ?",
            [statement_id],
        )
        logger.debug("  Deleted validation_results for statement=%s", statement_id)
        self._conn.execute(
            "DELETE FROM transactions WHERE statement_id = ?",
            [statement_id],
        )
        logger.debug("  Deleted transactions for statement=%s", statement_id)
        self._conn.execute(
            "DELETE FROM bank_statements WHERE statement_id = ?",
            [statement_id],
        )
        logger.info("Cascade delete complete for statement=%s", statement_id)

    # ── Validation results ──────────────────────────────────────────

    def insert_validation_result(self, **fields) -> str:
        """Insert a validation_results row. Returns the validation_id."""
        validation_id = fields.get("validation_id") or str(uuid.uuid4())
        fields["validation_id"] = validation_id

        logger.debug(
            "insert_validation_result: rule=%s, passed=%s, severity=%s",
            fields.get("rule_name"), fields.get("passed"), fields.get("severity"),
        )
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        self._conn.execute(
            f"INSERT INTO validation_results ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return validation_id

    # ── Evidence ────────────────────────────────────────────────────

    def insert_evidence(self, evidence_rows: list[dict]) -> int:
        """Bulk-insert extraction evidence rows."""
        if not evidence_rows:
            logger.debug("insert_evidence: no evidence rows to insert")
            return 0

        logger.debug("insert_evidence: inserting %d rows...", len(evidence_rows))
        cols = list(evidence_rows[0].keys())
        col_str = ", ".join(cols)
        placeholders = ", ".join("?" for _ in cols)

        rows = [tuple(e[c] for c in cols) for e in evidence_rows]
        self._conn.executemany(
            f"INSERT INTO extraction_evidence ({col_str}) VALUES ({placeholders})",
            rows,
        )
        logger.info("Inserted %d evidence rows", len(rows))
        return len(rows)
