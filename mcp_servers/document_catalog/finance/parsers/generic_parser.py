"""Generic bank statement parser.

Attempts to parse any bank statement by detecting common column
patterns (date, description, debit, credit, balance) in extracted
tables. Used as a fallback when no bank-specific parser matches.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import ParsedTransaction, ParseResult, StatementMetadata, StatementParser

logger = logging.getLogger(__name__)

# Reason: common column name patterns across SA banks
_DATE_PATTERNS = re.compile(r"(date|datum|transaction.?date|trans.?date|posting)", re.I)
_DESC_PATTERNS = re.compile(r"(description|details|particulars|narrative|transaction)", re.I)
_DEBIT_PATTERNS = re.compile(r"(debit|debet|charges|withdrawal|amount.*-|dr)", re.I)
_CREDIT_PATTERNS = re.compile(r"(credit|krediet|deposits?|amount.*\+|cr)", re.I)
_AMOUNT_PATTERNS = re.compile(r"(amount|bedrag|value)", re.I)
_BALANCE_PATTERNS = re.compile(r"(balance|saldo|running)", re.I)
_REF_PATTERNS = re.compile(r"(reference|ref|ref.?no)", re.I)


class GenericParser(StatementParser):
    """Generic parser that uses column-name matching."""

    @property
    def bank_name(self) -> str:
        return "generic"

    def can_parse(self, text: str, filename: str) -> bool:
        # Reason: generic parser always returns True as the last resort
        return True

    def parse(
        self,
        tables: list[dict],
        full_text: str,
        filename: str,
        document_id: str,
    ) -> ParseResult:
        logger.info(
            "GenericParser.parse: doc=%s, file=%s, tables=%d",
            document_id, filename, len(tables),
        )
        metadata = self._extract_metadata(full_text, filename)
        transactions: list[ParsedTransaction] = []
        warnings: list[str] = []

        for table_idx, table in enumerate(tables):
            headers = table.get("headers", [])
            rows = table.get("rows", [])
            page_num = table.get("page_number", 1)

            if not headers or not rows:
                logger.debug("  Table %d (page %d): skipped (no headers or rows)", table_idx, page_num)
                continue

            logger.debug(
                "  Table %d (page %d): headers=%s, rows=%d",
                table_idx, page_num, headers, len(rows),
            )

            # Map columns
            col_map = self._map_columns(headers)
            if col_map.get("date_idx") is None:
                warnings.append(f"Page {page_num}: no date column found, skipping table")
                logger.debug("  Table %d: no date column found, skipping", table_idx)
                continue

            logger.debug("  Table %d column mapping: %s", table_idx, col_map)

            parsed_count = 0
            skipped_count = 0
            for row_idx, row in enumerate(rows):
                try:
                    txn = self._parse_row(row, col_map, page_num, row_idx)
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
            warnings.append("No transactions could be parsed from any table")
            logger.warning("GenericParser: no transactions parsed from %s", filename)
        else:
            logger.info("GenericParser: parsed %d transactions from %s", len(transactions), filename)

        return ParseResult(
            metadata=metadata,
            transactions=transactions,
            warnings=warnings,
        )

    def _map_columns(self, headers: list[str]) -> dict:
        """Map header names to column indexes."""
        mapping: dict[str, int | None] = {
            "date_idx": None,
            "desc_idx": None,
            "debit_idx": None,
            "credit_idx": None,
            "amount_idx": None,
            "balance_idx": None,
            "ref_idx": None,
        }

        for i, h in enumerate(headers):
            h_clean = h.strip()
            if _DATE_PATTERNS.search(h_clean) and mapping["date_idx"] is None:
                mapping["date_idx"] = i
            elif _DESC_PATTERNS.search(h_clean) and mapping["desc_idx"] is None:
                mapping["desc_idx"] = i
            elif _DEBIT_PATTERNS.search(h_clean) and mapping["debit_idx"] is None:
                mapping["debit_idx"] = i
            elif _CREDIT_PATTERNS.search(h_clean) and mapping["credit_idx"] is None:
                mapping["credit_idx"] = i
            elif _AMOUNT_PATTERNS.search(h_clean) and mapping["amount_idx"] is None:
                mapping["amount_idx"] = i
            elif _BALANCE_PATTERNS.search(h_clean) and mapping["balance_idx"] is None:
                mapping["balance_idx"] = i
            elif _REF_PATTERNS.search(h_clean) and mapping["ref_idx"] is None:
                mapping["ref_idx"] = i

        return mapping

    def _parse_row(
        self, row: list[str], col_map: dict, page_num: int, row_idx: int
    ) -> ParsedTransaction | None:
        """Parse a single table row into a ParsedTransaction."""
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

        # Amount (integer cents)
        amount_cents = 0
        debit_idx = col_map.get("debit_idx")
        credit_idx = col_map.get("credit_idx")
        amount_idx = col_map.get("amount_idx")

        if debit_idx is not None and debit_idx < len(row):
            debit_val = _parse_amount_to_cents(row[debit_idx])
            if debit_val is not None and debit_val != 0:
                amount_cents = -abs(debit_val)

        if credit_idx is not None and credit_idx < len(row):
            credit_val = _parse_amount_to_cents(row[credit_idx])
            if credit_val is not None and credit_val != 0:
                amount_cents = abs(credit_val)

        # Reason: if separate debit/credit columns weren't found, try a
        # single amount column (negative = debit, positive = credit)
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

        raw_text = " | ".join(row)

        return ParsedTransaction(
            transaction_date=iso_date,
            description_raw=description,
            reference=ref,
            amount_cents=amount_cents,
            balance_after_cents=balance_cents,
            source_page=page_num,
            source_row=row_idx,
            raw_text=raw_text,
        )

    def _extract_metadata(self, full_text: str, filename: str) -> StatementMetadata:
        """Extract statement metadata from full text using regex patterns."""
        logger.debug("Extracting metadata from text (%d chars) and filename=%s", len(full_text), filename)
        meta = StatementMetadata()

        # Try to detect bank name from text or filename
        text_lower = full_text[:3000].lower()
        filename_lower = filename.lower()

        bank_patterns = {
            "FNB": r"(fnb|first national bank)",
            "Nedbank": r"nedbank",
            "Standard Bank": r"standard bank",
            "ABSA": r"absa",
            "Capitec": r"capitec",
        }
        for bank, pattern in bank_patterns.items():
            if re.search(pattern, text_lower) or re.search(pattern, filename_lower):
                meta.bank_name = bank
                logger.debug("  Detected bank: %s", bank)
                break

        if not meta.bank_name:
            meta.bank_name = "Unknown"
            logger.debug("  Bank not detected, using 'Unknown'")

        # Try to extract account number (masked)
        acc_match = re.search(r"(?:account|acc\.?\s*(?:no|number|#)?)\s*[:.]?\s*(\**\d[\d*\- ]{4,})", text_lower)
        if acc_match:
            raw = acc_match.group(1).strip()
            # Mask all but last 4 digits
            digits = re.sub(r"[^\d]", "", raw)
            if len(digits) >= 4:
                meta.account_number_masked = "****" + digits[-4:]
                logger.debug("  Account number: %s", meta.account_number_masked)

        # Try to extract statement period
        period_match = re.search(
            r"(?:period|statement)\s*(?:from|:)?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*(?:to|[-–])\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
            full_text[:3000], re.I,
        )
        if period_match:
            meta.period_start = _parse_date(period_match.group(1))
            meta.period_end = _parse_date(period_match.group(2))
            logger.debug("  Period: %s to %s", meta.period_start, meta.period_end)

        # Try to extract balances
        for pattern, field_name in [
            (r"opening\s*balance\s*[:.]?\s*R?\s*([\d, ]+\.\d{2})", "opening_balance_cents"),
            (r"closing\s*balance\s*[:.]?\s*R?\s*([\d, ]+\.\d{2})", "closing_balance_cents"),
        ]:
            match = re.search(pattern, full_text[:5000], re.I)
            if match:
                cents = _parse_amount_to_cents(match.group(1))
                if cents is not None:
                    setattr(meta, field_name, cents)
                    logger.debug("  %s: %d cents", field_name, cents)

        return meta


# -- Utility functions --------------------------------------------------------


def _parse_date(raw: str) -> str | None:
    """Try to parse a date string into ISO format (YYYY-MM-DD).

    Handles common SA date formats:
    - dd/mm/yyyy, dd-mm-yyyy
    - yyyy/mm/dd, yyyy-mm-dd
    - dd Mon yyyy (e.g. 01 Mar 2025)
    """
    raw = raw.strip()
    if not raw:
        return None

    formats = [
        "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y",
        "%Y/%m/%d", "%Y-%m-%d",
        "%d %b %Y", "%d %B %Y", "%d %b %y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def _parse_amount_to_cents(raw: str) -> int | None:
    """Parse a monetary string into integer cents.

    Handles:
    - R 1,234.56 → 123456
    - -R1234.56 → -123456
    - 1 234.56 → 123456
    - (1234.56) → -123456  (accounting negative)
    - Empty/whitespace → None
    """
    if not raw or not raw.strip():
        return None

    raw = raw.strip()

    # Detect accounting-style negatives: (1234.56)
    is_negative = False
    if raw.startswith("(") and raw.endswith(")"):
        is_negative = True
        raw = raw[1:-1]

    # Remove currency symbols and spaces
    raw = re.sub(r"[R$€£\s]", "", raw)

    # Detect explicit negative sign
    if raw.startswith("-"):
        is_negative = True
        raw = raw[1:]

    # Remove thousands separators (comma or space)
    raw = raw.replace(",", "").replace(" ", "")

    if not raw:
        return None

    try:
        value = float(raw)
        cents = round(value * 100)
        return -cents if is_negative else cents
    except ValueError:
        return None
