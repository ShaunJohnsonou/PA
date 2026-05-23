"""FNB (First National Bank) statement parser.

Handles FNB-specific statement layouts including:
- FNB cheque account statements
- FNB credit card statements
- FNB savings account statements
"""
from __future__ import annotations

import logging
import re

from . import ParsedTransaction, ParseResult, StatementMetadata, StatementParser
from .generic_parser import _parse_amount_to_cents, _parse_date

logger = logging.getLogger(__name__)


class FNBParser(StatementParser):
    """Parser specialised for FNB bank statements."""

    @property
    def bank_name(self) -> str:
        return "FNB"

    def can_parse(self, text: str, filename: str) -> bool:
        """Detect FNB statements by looking for FNB-specific markers."""
        combined = (text[:3000] + " " + filename).lower()
        result = bool(
            re.search(r"(fnb|first national bank)", combined)
            and re.search(r"(statement|account)", combined)
        )
        logger.debug("FNBParser.can_parse: result=%s, file=%s", result, filename)
        return result

    def parse(
        self,
        tables: list[dict],
        full_text: str,
        filename: str,
        document_id: str,
    ) -> ParseResult:
        logger.info(
            "FNBParser.parse: doc=%s, file=%s, tables=%d",
            document_id, filename, len(tables),
        )
        metadata = self._extract_fnb_metadata(full_text, filename)
        transactions: list[ParsedTransaction] = []
        warnings: list[str] = []

        for table_idx, table in enumerate(tables):
            headers = table.get("headers", [])
            rows = table.get("rows", [])
            page_num = table.get("page_number", 1)

            if not headers or not rows:
                logger.debug("  Table %d (page %d): skipped (no headers/rows)", table_idx, page_num)
                continue

            logger.debug(
                "  Table %d (page %d): headers=%s, rows=%d",
                table_idx, page_num, headers, len(rows),
            )

            # FNB typically uses: Date | Description | Amount | Balance
            # or: Date | Description | Debit | Credit | Balance
            col_map = self._map_fnb_columns(headers)
            if col_map.get("date_idx") is None:
                logger.debug("  Table %d: no date column, skipping", table_idx)
                continue

            logger.debug("  Table %d FNB column mapping: %s", table_idx, col_map)

            parsed_count = 0
            skipped_count = 0
            for row_idx, row in enumerate(rows):
                try:
                    txn = self._parse_fnb_row(row, col_map, page_num, row_idx)
                    if txn:
                        transactions.append(txn)
                        parsed_count += 1
                    else:
                        skipped_count += 1
                except Exception as exc:
                    warnings.append(f"Page {page_num}, row {row_idx}: {exc}")
                    logger.debug("  Row %d error: %s", row_idx, exc)

            logger.debug(
                "  Table %d result: parsed=%d, skipped=%d",
                table_idx, parsed_count, skipped_count,
            )

        if not transactions:
            warnings.append("No FNB transactions could be parsed")
            logger.warning("FNBParser: no transactions parsed from %s", filename)
        else:
            logger.info("FNBParser: parsed %d transactions from %s", len(transactions), filename)

        return ParseResult(
            metadata=metadata,
            transactions=transactions,
            warnings=warnings,
        )

    def _map_fnb_columns(self, headers: list[str]) -> dict:
        """Map FNB-specific column headers."""
        mapping = {
            "date_idx": None,
            "desc_idx": None,
            "debit_idx": None,
            "credit_idx": None,
            "amount_idx": None,
            "balance_idx": None,
            "ref_idx": None,
        }

        for i, h in enumerate(headers):
            h_lower = h.strip().lower()
            if h_lower in ("date", "trans date", "transaction date", "posting date"):
                if mapping["date_idx"] is None:
                    mapping["date_idx"] = i
            elif h_lower in ("description", "details", "transaction description"):
                mapping["desc_idx"] = i
            elif h_lower in ("debit", "debits", "money out"):
                mapping["debit_idx"] = i
            elif h_lower in ("credit", "credits", "money in"):
                mapping["credit_idx"] = i
            elif h_lower in ("amount",):
                mapping["amount_idx"] = i
            elif h_lower in ("balance", "running balance", "available balance"):
                mapping["balance_idx"] = i
            elif h_lower in ("reference", "ref", "ref no"):
                mapping["ref_idx"] = i

        return mapping

    def _parse_fnb_row(
        self, row: list[str], col_map: dict, page_num: int, row_idx: int
    ) -> ParsedTransaction | None:
        """Parse a single FNB table row."""
        date_idx = col_map["date_idx"]
        if date_idx is None or date_idx >= len(row):
            return None

        raw_date = row[date_idx].strip()
        if not raw_date:
            return None

        iso_date = _parse_date(raw_date)
        if iso_date is None:
            return None

        # Description
        desc_idx = col_map.get("desc_idx")
        description = row[desc_idx].strip() if desc_idx is not None and desc_idx < len(row) else ""
        if not description:
            return None

        # Amount
        amount_cents = 0

        debit_idx = col_map.get("debit_idx")
        credit_idx = col_map.get("credit_idx")
        amount_idx = col_map.get("amount_idx")

        if debit_idx is not None and debit_idx < len(row):
            val = _parse_amount_to_cents(row[debit_idx])
            if val is not None and val != 0:
                amount_cents = -abs(val)

        if credit_idx is not None and credit_idx < len(row):
            val = _parse_amount_to_cents(row[credit_idx])
            if val is not None and val != 0:
                amount_cents = abs(val)

        if amount_cents == 0 and amount_idx is not None and amount_idx < len(row):
            parsed = _parse_amount_to_cents(row[amount_idx])
            if parsed is not None:
                amount_cents = parsed

        if amount_cents == 0:
            return None

        # Balance
        balance_cents = None
        balance_idx = col_map.get("balance_idx")
        if balance_idx is not None and balance_idx < len(row):
            balance_cents = _parse_amount_to_cents(row[balance_idx])

        # Reference
        ref = None
        ref_idx = col_map.get("ref_idx")
        if ref_idx is not None and ref_idx < len(row):
            ref = row[ref_idx].strip() or None

        return ParsedTransaction(
            transaction_date=iso_date,
            description_raw=description,
            reference=ref,
            amount_cents=amount_cents,
            balance_after_cents=balance_cents,
            source_page=page_num,
            source_row=row_idx,
            raw_text=" | ".join(row),
        )

    def _extract_fnb_metadata(self, full_text: str, filename: str) -> StatementMetadata:
        """Extract FNB-specific statement metadata."""
        logger.debug("Extracting FNB metadata from text (%d chars), file=%s", len(full_text), filename)
        meta = StatementMetadata(bank_name="FNB")

        text = full_text[:5000]

        # Account number
        acc_match = re.search(r"(?:account\s*(?:no|number|#)?\s*[:.]?\s*)(\d[\d\s\-*]{6,})", text, re.I)
        if acc_match:
            digits = re.sub(r"[^\d]", "", acc_match.group(1))
            if len(digits) >= 4:
                meta.account_number_masked = "****" + digits[-4:]
                logger.debug("  FNB account: %s", meta.account_number_masked)

        # Account type
        if re.search(r"cheque|current", text, re.I):
            meta.account_type = "cheque"
        elif re.search(r"credit\s*card", text, re.I):
            meta.account_type = "credit"
        elif re.search(r"savings?", text, re.I):
            meta.account_type = "savings"
        if meta.account_type:
            logger.debug("  FNB account type: %s", meta.account_type)

        # Statement period
        period_match = re.search(
            r"(?:statement\s*period|period)\s*[:.]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*(?:to|[-–])\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
            text, re.I,
        )
        if period_match:
            meta.period_start = _parse_date(period_match.group(1))
            meta.period_end = _parse_date(period_match.group(2))
            logger.debug("  FNB period: %s to %s", meta.period_start, meta.period_end)

        # Balances
        for pattern, attr in [
            (r"opening\s*balance\s*[:.]?\s*R?\s*([\d,. ]+)", "opening_balance_cents"),
            (r"closing\s*balance\s*[:.]?\s*R?\s*([\d,. ]+)", "closing_balance_cents"),
            (r"total\s*(?:money\s*out|debits?)\s*[:.]?\s*R?\s*([\d,. ]+)", "total_debits_cents"),
            (r"total\s*(?:money\s*in|credits?)\s*[:.]?\s*R?\s*([\d,. ]+)", "total_credits_cents"),
        ]:
            match = re.search(pattern, text, re.I)
            if match:
                cents = _parse_amount_to_cents(match.group(1))
                if cents is not None:
                    setattr(meta, attr, abs(cents))
                    logger.debug("  FNB %s: %d cents", attr, abs(cents))

        return meta
