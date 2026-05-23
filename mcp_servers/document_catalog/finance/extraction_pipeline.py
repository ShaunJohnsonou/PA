"""Financial extraction pipeline (FR-4.2).

Orchestrates the 12-step flow that converts a classified bank
statement into validated, structured transaction rows in DuckDB.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

from .categoriser import classify_transaction, classify_transactions_llm
from .ledger_db import FinanceLedger
from .merchant_normaliser import normalise_merchant, normalise_merchant_llm
from .parsers import ParseResult, ParserRegistry
from .parsers.fnb_parser import FNBParser
from .parsers.generic_parser import GenericParser

logger = logging.getLogger(__name__)


def build_parser_registry() -> ParserRegistry:
    """Build and return the parser registry with all known parsers."""
    registry = ParserRegistry()
    registry.register(FNBParser())
    # Future: register NedBank, StandardBank, ABSA, Capitec parsers here
    registry.register(GenericParser(), is_generic=True)
    return registry


class FinancialExtractionPipeline:
    """End-to-end pipeline: tables → structured transactions → DuckDB.

    Steps (per FR-4.2):
        1. Receive classified bank_statement document
        2. Load extracted tables (Docling + pdfplumber)
        3. Select and run bank-specific parser (header + rows)
        4. Upsert account
        5. Create bank_statements row
        6. Clean descriptions and normalise merchants
        7. Classify categories
        8. Insert transactions
        9. Create extraction evidence
        10. Run validation (delegated to caller)
        11–12. Update statuses (delegated to caller)
    """

    def __init__(self, ledger: FinanceLedger, registry: ParserRegistry) -> None:
        self._ledger = ledger
        self._registry = registry

    async def extract_financial_data(
        self,
        document_id: str,
        tables: list[dict],
        full_text: str,
        filename: str,
        extraction_method: str = "docling",
    ) -> dict:
        """Run the full financial extraction pipeline.

        Args:
            document_id: UUID of the source document.
            tables: Extracted table data (from Docling or pdfplumber).
            full_text: Full markdown text of the document.
            filename: Original filename.
            extraction_method: How the tables were extracted.

        Returns:
            Dict with statement_id, account_id, transaction_count, warnings.
        """
        logger.info(
            "=== Financial extraction starting: doc=%s, file=%s, tables=%d, method=%s ===",
            document_id, filename, len(tables), extraction_method,
        )
        logger.debug("Full text length: %d chars, first 200: %s", len(full_text), full_text[:200])

        # -- 1. Select parser --------------------------------------------------
        parser = self._registry.select_parser(full_text, filename)
        if parser is None:
            logger.error("No parser available for doc=%s, file=%s", document_id, filename)
            return {"error": "no_parser", "message": "No parser available for this document"}
        logger.info("Step 1: Selected parser: %s", parser.bank_name)

        # -- 2. Parse tables ---------------------------------------------------
        logger.info("Step 2: Parsing %d tables with %s parser...", len(tables), parser.bank_name)
        for i, table in enumerate(tables):
            logger.debug(
                "  Table %d: headers=%s, rows=%d, page=%s",
                i, table.get("headers", [])[:5], len(table.get("rows", [])),
                table.get("page_number", "?"),
            )
        parse_result: ParseResult = parser.parse(tables, full_text, filename, document_id)

        logger.info(
            "Step 2 result: %d transactions, %d warnings, %d errors",
            len(parse_result.transactions), len(parse_result.warnings),
            len(parse_result.errors) if parse_result.errors else 0,
        )
        for w in parse_result.warnings:
            logger.debug("  Parser warning: %s", w)

        if parse_result.errors:
            logger.error("Parser returned errors: %s", parse_result.errors)
            return {
                "error": "parse_failed",
                "message": "; ".join(parse_result.errors),
                "warnings": parse_result.warnings,
            }

        meta = parse_result.metadata
        logger.info(
            "Step 2 metadata: bank=%s, account=%s, period=%s to %s, "
            "opening=%s, closing=%s",
            meta.bank_name, meta.account_number_masked,
            meta.period_start, meta.period_end,
            meta.opening_balance_cents, meta.closing_balance_cents,
        )

        # -- 3. Upsert account -------------------------------------------------
        account_number = meta.account_number_masked or "****0000"
        account_id = self._ledger.upsert_account(
            bank_name=meta.bank_name,
            account_number_masked=account_number,
            account_type=meta.account_type,
            currency=meta.currency,
            seen_date=meta.period_start,
        )
        logger.info("Step 3: Upserted account: id=%s, bank=%s, number=%s", account_id, meta.bank_name, account_number)

        # -- 4. Check for existing statement (re-extraction) -------------------
        existing = self._ledger.get_statement_by_document(document_id)
        if existing:
            logger.info(
                "Step 4: Re-extracting — deleting old statement %s (%d existing txns)",
                existing["statement_id"], existing.get("transaction_count", 0),
            )
            self._ledger.delete_statement_data(existing["statement_id"])
        else:
            logger.debug("Step 4: No existing statement for doc=%s (first extraction)", document_id)

        # -- 5. Create bank_statements row -------------------------------------
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
        logger.info("Step 5: Created statement: id=%s, txns=%d", statement_id, len(parse_result.transactions))

        # -- 6-7. Clean descriptions, normalise merchants, classify ------------
        logger.info("Step 6-7: Normalising %d transactions...", len(parse_result.transactions))
        transaction_rows = []
        evidence_rows = []

        merchants_found = 0
        categories_found = 0

        for idx, txn in enumerate(parse_result.transactions):
            txn_id = str(uuid.uuid4())

            # Merchant normalisation (regex first)
            merchant = normalise_merchant(txn.description_raw)
            description_clean = txn.description_raw
            if merchant:
                merchants_found += 1

            # Category classification (regex heuristic)
            category, confidence = classify_transaction(
                txn.description_raw, merchant
            )
            if category != "other":
                categories_found += 1

            if idx < 5:  # Log first 5 transactions in detail
                logger.debug(
                    "  Txn %d: date=%s, desc=%s, amount=%d, merchant=%s, category=%s (%.2f)",
                    idx, txn.transaction_date, txn.description_raw[:50],
                    txn.amount_cents, merchant, category, confidence,
                )

            transaction_rows.append({
                "transaction_id": txn_id,
                "statement_id": statement_id,
                "account_id": account_id,
                "transaction_date": txn.transaction_date,
                "posting_date": txn.posting_date,
                "description_raw": txn.description_raw,
                "description_clean": description_clean,
                "merchant": merchant,
                "counterparty": None,
                "reference": txn.reference,
                "amount_cents": txn.amount_cents,
                "currency": meta.currency,
                "balance_after_cents": txn.balance_after_cents,
                "category": category,
                "category_confidence": confidence,
                "source_document_id": document_id,
                "source_page": txn.source_page,
                "source_row": txn.source_row,
                "source_bbox": None,
                "extraction_method": extraction_method,
                "extraction_confidence": None,
            })

            evidence_rows.append({
                "evidence_id": str(uuid.uuid4()),
                "transaction_id": txn_id,
                "source_document_id": document_id,
                "source_page": txn.source_page,
                "source_row": txn.source_row,
                "source_bbox": None,
                "raw_text": txn.raw_text,
                "extraction_method": extraction_method,
                "confidence": None,
            })

        logger.info(
            "Step 6-7 result: merchants_matched=%d/%d, categories_matched=%d/%d",
            merchants_found, len(transaction_rows),
            categories_found, len(transaction_rows),
        )

        # -- 8. LLM enrichment for uncategorised transactions ------------------
        uncategorised_idxs = [
            i for i, t in enumerate(transaction_rows) if t["category"] == "other"
        ]

        if uncategorised_idxs:
            logger.info(
                "Step 8: LLM categorisation for %d uncategorised transactions",
                len(uncategorised_idxs),
            )
            try:
                descs = [transaction_rows[i]["description_raw"] for i in uncategorised_idxs]
                llm_results = await classify_transactions_llm(descs)
                llm_categorised = 0
                for idx, (cat, conf) in zip(uncategorised_idxs, llm_results):
                    if cat != "other":
                        transaction_rows[idx]["category"] = cat
                        transaction_rows[idx]["category_confidence"] = conf
                        llm_categorised += 1
                logger.info("Step 8 result: LLM categorised %d/%d", llm_categorised, len(uncategorised_idxs))
            except Exception as exc:
                logger.warning("LLM batch categorisation failed: %s", exc)
        else:
            logger.debug("Step 8: All transactions already categorised, skipping LLM")

        # LLM merchant normalisation for transactions without a merchant
        no_merchant_idxs = [
            i for i, t in enumerate(transaction_rows) if t["merchant"] is None
        ]
        if no_merchant_idxs:
            logger.info(
                "Step 8b: LLM merchant normalisation for %d un-matched transactions (max 20)",
                len(no_merchant_idxs),
            )
        # Reason: only LLM-normalise the first 20 to avoid excessive API calls
        llm_merchants = 0
        for idx in no_merchant_idxs[:20]:
            try:
                merchant = await normalise_merchant_llm(transaction_rows[idx]["description_raw"])
                if merchant:
                    transaction_rows[idx]["merchant"] = merchant
                    llm_merchants += 1
            except Exception:
                pass
        if no_merchant_idxs:
            logger.info("Step 8b result: LLM resolved %d/%d merchants", llm_merchants, min(len(no_merchant_idxs), 20))

        # -- 9. Insert into DuckDB --------------------------------------------
        logger.info("Step 9: Inserting %d transactions and %d evidence rows into DuckDB...", len(transaction_rows), len(evidence_rows))
        txn_count = self._ledger.insert_transactions(transaction_rows)
        ev_count = self._ledger.insert_evidence(evidence_rows)

        logger.info(
            "=== Financial extraction COMPLETE: statement=%s, %d transactions, %d evidence rows ===",
            statement_id, txn_count, ev_count,
        )

        return {
            "statement_id": statement_id,
            "account_id": account_id,
            "bank_name": meta.bank_name,
            "account_number_masked": account_number,
            "period_start": meta.period_start,
            "period_end": meta.period_end,
            "transaction_count": txn_count,
            "warnings": parse_result.warnings,
        }
