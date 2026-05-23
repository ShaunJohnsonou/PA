"""Import orchestrator for CSV and OFX bank statement files.

Coordinates the full import flow: parse file → upsert account →
create statement → check for duplicate transactions → insert new
transactions into the DuckDB financial ledger.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ..ledger_db import FinanceLedger
from ..parsers import ParsedTransaction, ParseResult, StatementMetadata
from .csv_parser import CSVBankParser
from .ofx_parser import OFXParser

logger = logging.getLogger(__name__)


class ImportOrchestrator:
    """Orchestrates the end-to-end import of CSV or OFX bank files.

    Wires together the parsers and the ``FinanceLedger`` to turn a raw
    bank export file into deduplicated transaction rows in DuckDB.
    """

    def __init__(
        self,
        ledger: FinanceLedger,
        csv_parser: CSVBankParser | None = None,
        ofx_parser: OFXParser | None = None,
    ) -> None:
        self._ledger = ledger
        self._csv_parser = csv_parser
        self._ofx_parser = ofx_parser

    # ── CSV import ──────────────────────────────────────────────────

    def import_csv(
        self,
        csv_path: str,
        document_id: str,
        bank: str | None = None,
    ) -> dict:
        """Import a CSV bank statement.

        Args:
            csv_path: Absolute path to the CSV file.
            document_id: UUID linking this import to a document in the
                         catalog.
            bank: Optional bank name override (skips auto-detection).

        Returns:
            Dict with ``statement_id``, ``account_id``,
            ``transactions_parsed``, ``transactions_imported``,
            ``duplicates_skipped``, and ``warnings``.
        """
        if self._csv_parser is None:
            return {
                "error": "no_csv_parser",
                "message": "CSVBankParser not configured",
            }

        logger.info("=== CSV import starting: path=%s, doc=%s ===", csv_path, document_id)

        # -- Step 1: parse CSV ------------------------------------------------
        parse_result = self._csv_parser.parse(csv_path, bank=bank)

        if parse_result.errors:
            logger.error("CSV parse errors: %s", parse_result.errors)
            return {
                "error": "parse_failed",
                "message": "; ".join(parse_result.errors),
                "warnings": parse_result.warnings,
            }

        return self._ingest(
            parse_result=parse_result,
            document_id=document_id,
            extraction_method="csv_import",
        )

    # ── OFX import ──────────────────────────────────────────────────

    def import_ofx(self, ofx_path: str, document_id: str) -> dict:
        """Import an OFX/QFX bank statement.

        Args:
            ofx_path: Absolute path to the OFX/QFX file.
            document_id: UUID linking this import to a document in the
                         catalog.

        Returns:
            Same structure as ``import_csv``.
        """
        if self._ofx_parser is None:
            return {
                "error": "no_ofx_parser",
                "message": "OFXParser not configured",
            }

        logger.info("=== OFX import starting: path=%s, doc=%s ===", ofx_path, document_id)

        parse_result = self._ofx_parser.parse(ofx_path)

        if parse_result.errors:
            logger.error("OFX parse errors: %s", parse_result.errors)
            return {
                "error": "parse_failed",
                "message": "; ".join(parse_result.errors),
                "warnings": parse_result.warnings,
            }

        return self._ingest(
            parse_result=parse_result,
            document_id=document_id,
            extraction_method="ofx_import",
        )

    # ── Shared ingest pipeline ──────────────────────────────────────

    def _ingest(
        self,
        parse_result: ParseResult,
        document_id: str,
        extraction_method: str,
    ) -> dict:
        """Common pipeline: upsert account → statement → dedup → insert.

        Args:
            parse_result: Output from CSV or OFX parser.
            document_id: Source document UUID.
            extraction_method: ``"csv_import"`` or ``"ofx_import"``.

        Returns:
            Result dict with counts.
        """
        meta = parse_result.metadata

        # -- Step 2: upsert account -------------------------------------------
        account_number = meta.account_number_masked or "****0000"
        account_id = self._ledger.upsert_account(
            bank_name=meta.bank_name or "Unknown",
            account_number_masked=account_number,
            account_type=meta.account_type,
            currency=meta.currency,
            seen_date=meta.period_start,
        )
        logger.info("Upserted account: id=%s, bank=%s", account_id, meta.bank_name)

        # -- Step 3: insert statement -----------------------------------------
        statement_id = self._ledger.insert_statement(
            document_id=document_id,
            account_id=account_id,
            bank_name=meta.bank_name,
            account_number_masked=account_number,
            period_start=meta.period_start or "1900-01-01",
            period_end=meta.period_end or "2099-12-31",
            opening_balance_cents=meta.opening_balance_cents,
            closing_balance_cents=meta.closing_balance_cents,
            total_debits_cents=meta.total_debits_cents,
            total_credits_cents=meta.total_credits_cents,
            currency=meta.currency,
            transaction_count=len(parse_result.transactions),
            extraction_status="extracted",
        )
        logger.info("Inserted statement: id=%s", statement_id)

        # -- Step 4: build transaction dicts ----------------------------------
        transaction_rows = self._build_transaction_rows(
            transactions=parse_result.transactions,
            statement_id=statement_id,
            account_id=account_id,
            document_id=document_id,
            extraction_method=extraction_method,
            currency=meta.currency,
        )

        # -- Step 5: deduplicate ----------------------------------------------
        unique_rows = self._check_duplicates(transaction_rows, account_id)
        duplicates_skipped = len(transaction_rows) - len(unique_rows)
        if duplicates_skipped:
            logger.info(
                "Skipped %d duplicate transactions (of %d total)",
                duplicates_skipped, len(transaction_rows),
            )

        # -- Step 6: insert transactions --------------------------------------
        inserted = self._ledger.insert_transactions(unique_rows)
        logger.info(
            "=== Import COMPLETE: statement=%s, parsed=%d, imported=%d, "
            "duplicates=%d ===",
            statement_id, len(transaction_rows), inserted, duplicates_skipped,
        )

        return {
            "statement_id": statement_id,
            "account_id": account_id,
            "bank_name": meta.bank_name,
            "account_number_masked": account_number,
            "period_start": meta.period_start,
            "period_end": meta.period_end,
            "transactions_parsed": len(transaction_rows),
            "transactions_imported": inserted,
            "duplicates_skipped": duplicates_skipped,
            "warnings": parse_result.warnings,
        }

    # ── Transaction row builder ─────────────────────────────────────

    def _build_transaction_rows(
        self,
        transactions: list[ParsedTransaction],
        statement_id: str,
        account_id: str,
        document_id: str,
        extraction_method: str,
        currency: str,
    ) -> list[dict]:
        """Convert ParsedTransactions into dicts matching the transactions table."""
        rows: list[dict] = []
        for txn in transactions:
            rows.append({
                "transaction_id": str(uuid.uuid4()),
                "statement_id": statement_id,
                "account_id": account_id,
                "transaction_date": txn.transaction_date,
                "posting_date": txn.posting_date,
                "description_raw": txn.description_raw,
                "description_clean": txn.description_raw,
                "merchant": None,
                "counterparty": None,
                "reference": txn.reference,
                "amount_cents": txn.amount_cents,
                "currency": currency,
                "balance_after_cents": txn.balance_after_cents,
                "category": None,
                "category_confidence": None,
                "source_document_id": document_id,
                "source_page": txn.source_page,
                "source_row": txn.source_row,
                "source_bbox": None,
                "extraction_method": extraction_method,
                "extraction_confidence": None,
            })
        return rows

    # ── Deduplication ───────────────────────────────────────────────

    def _check_duplicates(
        self, transactions: list[dict], account_id: str
    ) -> list[dict]:
        """Filter out transactions that already exist in the ledger.

        A transaction is considered a duplicate if there is an existing
        row with the same ``(transaction_date, amount_cents,
        description_raw, account_id)`` tuple.

        Args:
            transactions: Candidate transaction dicts.
            account_id: The target account UUID.

        Returns:
            List of transaction dicts that are **not** duplicates.
        """
        if not transactions:
            return []

        # TODO (Future Improvement): Edge case with identical same-day transactions.
        # Currently, if two completely separate but identical purchases are made on
        # the exact same day (e.g., two Uber rides for R150.00 each), the second one 
        # is wrongly flagged as a duplicate because they share the same Date, Amount, 
        # and Description.
        # 
        # Proposed fix: Include the running `balance_after_cents` in the duplicate
        # check key (if the bank provides it per row). If the balance is different, 
        # we can safely guarantee they are two separate identical transactions.

        # Reason: fetch all existing transactions for this account to do
        # an in-memory set-based dedup — much faster than per-row queries
        try:
            existing_rows = self._ledger.conn.execute(
                "SELECT transaction_date, amount_cents, description_raw "
                "FROM transactions WHERE account_id = ?",
                [account_id],
            ).fetchall()
        except Exception as exc:
            logger.warning("Duplicate check query failed: %s — skipping dedup", exc)
            return transactions

        existing_keys: set[tuple] = set()
        for row in existing_rows:
            # Reason: normalise date to string for consistent comparison,
            # DuckDB may return date objects
            date_str = str(row[0])
            existing_keys.add((date_str, row[1], row[2]))

        unique: list[dict] = []
        for txn in transactions:
            key = (
                str(txn["transaction_date"]),
                txn["amount_cents"],
                txn["description_raw"],
            )
            if key not in existing_keys:
                unique.append(txn)
                # Reason: add to the set so that rows within the same
                # import batch also get deduped against each other
                existing_keys.add(key)
            else:
                logger.debug(
                    "Duplicate skipped: date=%s, amount=%d, desc=%s",
                    key[0], key[1], key[2][:40],
                )

        return unique
