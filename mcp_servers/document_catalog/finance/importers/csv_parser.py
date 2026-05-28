"""CSV bank statement parser with per-bank template support.

Parses CSV exports from South African banks into the standard
ParseResult format used by the financial extraction pipeline.
Each bank's CSV layout is described by a JSON template file.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..parsers import ParsedTransaction, ParseResult, StatementMetadata

logger = logging.getLogger(__name__)


@dataclass
class BankTemplate:
    """Describes the CSV layout for a specific bank.

    Loaded from a JSON template file in the bank_templates/ directory.
    """

    bank_name: str
    skip_rows: int = 0
    date_column: str = "Date"
    date_format: str = "%d/%m/%Y"
    description_column: str = "Description"
    amount_column: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    balance_column: str | None = None
    amount_sign: str = "negative_is_debit"  # or "separate_columns"
    currency: str = "ZAR"
    encoding: str = "utf-8"
    header_patterns: list[str] = field(default_factory=list)
    content_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> BankTemplate:
        """Create a BankTemplate from a dictionary (parsed JSON)."""
        return cls(
            bank_name=data["bank_name"],
            skip_rows=data.get("skip_rows", 0),
            date_column=data.get("date_column", "Date"),
            date_format=data.get("date_format", "%d/%m/%Y"),
            description_column=data.get("description_column", "Description"),
            amount_column=data.get("amount_column"),
            debit_column=data.get("debit_column"),
            credit_column=data.get("credit_column"),
            balance_column=data.get("balance_column"),
            amount_sign=data.get("amount_sign", "negative_is_debit"),
            currency=data.get("currency", "ZAR"),
            encoding=data.get("encoding", "utf-8"),
            header_patterns=data.get("header_patterns", []),
            content_patterns=data.get("content_patterns", []),
        )


class CSVBankParser:
    """Parses bank CSV exports using per-bank JSON templates.

    Templates are loaded from a directory at init time.  The parser
    can auto-detect which bank a CSV belongs to by matching header
    patterns, or accept an explicit bank name.
    """

    def __init__(self, template_dir: str) -> None:
        """Load all JSON template files from *template_dir*.

        Args:
            template_dir: Absolute path to a directory containing
                          ``<bank>.json`` template files.
        """
        self._templates: dict[str, BankTemplate] = {}
        self._template_dir = template_dir
        self._load_templates()

    def _load_templates(self) -> None:
        """Scan template_dir for .json files and load each as a BankTemplate."""
        if not os.path.isdir(self._template_dir):
            logger.warning("Template directory does not exist: %s", self._template_dir)
            return

        for filename in sorted(os.listdir(self._template_dir)):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(self._template_dir, filename)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                template = BankTemplate.from_dict(data)
                # Reason: key by lower-cased bank_name for case-insensitive lookup
                self._templates[template.bank_name.lower()] = template
                logger.debug("Loaded bank template: %s from %s", template.bank_name, filename)
            except Exception as exc:
                logger.warning("Failed to load template %s: %s", filename, exc)

        logger.info("Loaded %d bank templates from %s", len(self._templates), self._template_dir)

    @property
    def available_banks(self) -> list[str]:
        """Return the list of available bank names (original casing)."""
        return [t.bank_name for t in self._templates.values()]

    # ── Bank detection ──────────────────────────────────────────────

    def detect_bank(self, csv_path: str) -> str | None:
        """Auto-detect the bank by comparing CSV headers against templates.

        Reads up to 10 rows of the file and checks whether every
        header_pattern from any template is present in the CSV header
        row. When multiple templates match on headers, uses
        content_patterns to differentiate (e.g. 'ABSA' in descriptions).

        Args:
            csv_path: Path to the CSV file.

        Returns:
            The bank name (original casing) or ``None`` if no match.
        """
        # Reason: collect ALL matching templates first, then pick the
        # best one using content_patterns as a tiebreaker.
        header_matches: list[BankTemplate] = []

        for template in self._templates.values():
            try:
                with open(csv_path, encoding=template.encoding, errors="replace") as fh:
                    lines: list[str] = []
                    for i, line in enumerate(fh):
                        lines.append(line)
                        if i >= 9:
                            break

                combined = " ".join(lines).lower()
                patterns = template.header_patterns
                if patterns and all(p.lower() in combined for p in patterns):
                    header_matches.append(template)
            except Exception as exc:
                logger.debug(
                    "Error probing %s with template %s: %s",
                    csv_path, template.bank_name, exc,
                )

        if not header_matches:
            logger.debug("No bank detected for %s", csv_path)
            return None

        if len(header_matches) == 1:
            logger.info("Detected bank '%s' for %s", header_matches[0].bank_name, csv_path)
            return header_matches[0].bank_name

        # Reason: multiple templates matched the same headers (e.g. ABSA and
        # FNB both use Date/Description/Amount/Balance). Read the file content
        # and check content_patterns to disambiguate.
        logger.info(
            "Multiple header matches for %s: %s — using content_patterns to disambiguate",
            csv_path, [t.bank_name for t in header_matches],
        )
        try:
            with open(csv_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read().upper()
        except Exception:
            content = ""

        for template in header_matches:
            if template.content_patterns:
                if any(cp.upper() in content for cp in template.content_patterns):
                    logger.info(
                        "Content pattern matched: detected bank '%s' for %s",
                        template.bank_name, csv_path,
                    )
                    return template.bank_name

        # Reason: no content_patterns matched — fall back to first match
        logger.info(
            "No content patterns matched, defaulting to '%s' for %s",
            header_matches[0].bank_name, csv_path,
        )
        return header_matches[0].bank_name

    # ── Main parse entry point ──────────────────────────────────────

    def parse(self, csv_path: str, bank: str | None = None) -> ParseResult:
        """Parse a CSV bank statement.

        Args:
            csv_path: Absolute path to the CSV file.
            bank: Optional bank name.  If ``None``, auto-detection is
                  attempted.

        Returns:
            A ``ParseResult`` with metadata, transactions, and any warnings.
        """
        warnings: list[str] = []
        errors: list[str] = []

        # -- Step 1: resolve bank template ------------------------------------
        if bank is None:
            bank = self.detect_bank(csv_path)
            
        if bank is None:
            logger.info("No bank format detected, using generic fallback template for %s", csv_path)
            template = BankTemplate(
                bank_name="Generic", 
                date_column="Date",
                description_column="Description",
                amount_column="Amount",
                balance_column="Balance",
            )
        else:
            template = self._templates.get(bank.lower())
            if template is None:
                errors.append(f"No template found for bank '{bank}'")
                return ParseResult(
                    metadata=StatementMetadata(),
                    transactions=[],
                    warnings=warnings,
                    errors=errors,
                )

        logger.info("Parsing CSV %s with template '%s'", csv_path, template.bank_name)

        # -- Step 2: read CSV rows --------------------------------------------
        try:
            rows, header_row = self._read_csv(csv_path, template)
        except Exception as exc:
            errors.append(f"Failed to read CSV: {exc}")
            return ParseResult(
                metadata=StatementMetadata(bank_name=template.bank_name),
                transactions=[],
                warnings=warnings,
                errors=errors,
            )

        if not header_row:
            errors.append("CSV header row is empty")
            return ParseResult(
                metadata=StatementMetadata(bank_name=template.bank_name),
                transactions=[],
                warnings=warnings,
                errors=errors,
            )

        # -- Step 3: map column names to indexes ------------------------------
        col_map = self._build_column_map(header_row, template)
        if col_map.get("date_idx") is None:
            errors.append(
                f"Date column '{template.date_column}' not found in headers: {header_row}"
            )
            return ParseResult(
                metadata=StatementMetadata(bank_name=template.bank_name),
                transactions=[],
                warnings=warnings,
                errors=errors,
            )

        logger.debug("Column map: %s", col_map)

        # -- Step 4: parse each data row --------------------------------------
        transactions: list[ParsedTransaction] = []
        for row_idx, row in enumerate(rows):
            try:
                txn = self._parse_row(row, col_map, template, row_idx)
                if txn:
                    transactions.append(txn)
            except Exception as exc:
                warnings.append(f"Row {row_idx}: {exc}")
                logger.debug("Row %d parse error: %s", row_idx, exc)

        # -- Step 5: build metadata -------------------------------------------
        metadata = StatementMetadata(
            bank_name=template.bank_name,
            currency=template.currency,
        )
        if transactions:
            dates = [t.transaction_date for t in transactions]
            metadata.period_start = min(dates)
            metadata.period_end = max(dates)

        logger.info(
            "CSV parse complete: %d transactions, %d warnings, period=%s–%s",
            len(transactions), len(warnings),
            metadata.period_start, metadata.period_end,
        )

        return ParseResult(
            metadata=metadata,
            transactions=transactions,
            warnings=warnings,
            errors=errors,
        )

    # ── Internal helpers ────────────────────────────────────────────

    def _read_csv(
        self, csv_path: str, template: BankTemplate
    ) -> tuple[list[list[str]], list[str]]:
        """Read the CSV file, skip header rows, return (data_rows, header_row)."""
        with open(csv_path, encoding=template.encoding, errors="replace", newline="") as fh:
            reader = csv.reader(fh)

            # Reason: skip_rows lets templates handle bank CSVs that have
            # informational/metadata rows above the actual data header
            for _ in range(template.skip_rows):
                try:
                    next(reader)
                except StopIteration:
                    return [], []

            try:
                header_row = next(reader)
            except StopIteration:
                return [], []

            # Reason: strip whitespace and BOM characters from header cells
            header_row = [h.strip().strip("\ufeff") for h in header_row]
            data_rows = [row for row in reader if any(cell.strip() for cell in row)]

        logger.debug(
            "Read CSV: headers=%s, data_rows=%d, encoding=%s",
            header_row, len(data_rows), template.encoding,
        )
        return data_rows, header_row

    def _build_column_map(self, headers: list[str], template: BankTemplate) -> dict:
        """Map template column names to 0-based indexes in the CSV header."""
        mapping: dict[str, int | None] = {
            "date_idx": None,
            "desc_idx": None,
            "amount_idx": None,
            "debit_idx": None,
            "credit_idx": None,
            "balance_idx": None,
        }

        # Reason: case-insensitive match so templates work with minor
        # variations in header capitalisation across CSV exports
        header_lower = [h.lower() for h in headers]

        def _find(column_name: str | None, synonyms: list[str]) -> int | None:
            targets = [column_name.lower()] if column_name else []
            targets.extend(s.lower() for s in synonyms)
            
            # 1. Exact matches
            for target in targets:
                for i, h in enumerate(header_lower):
                    if h == target:
                        return i
                        
            # 2. Substring matches (e.g. "transaction date" contains "date")
            for target in targets:
                for i, h in enumerate(header_lower):
                    if target in h:
                        return i
            return None

        mapping["date_idx"] = _find(template.date_column, ["date", "posting date", "transaction date", "time"])
        mapping["desc_idx"] = _find(template.description_column, ["description", "narration", "payee", "transaction details", "memo", "reference", "desc"])
        mapping["amount_idx"] = _find(template.amount_column, ["amount", "value", "transaction amount"])
        mapping["debit_idx"] = _find(template.debit_column, ["debit", "money out", "withdrawal", "paid out"])
        mapping["credit_idx"] = _find(template.credit_column, ["credit", "money in", "deposit", "paid in"])
        mapping["balance_idx"] = _find(template.balance_column, ["balance", "running balance", "available balance"])

        return mapping

    def _parse_row(
        self,
        row: list[str],
        col_map: dict,
        template: BankTemplate,
        row_idx: int,
    ) -> ParsedTransaction | None:
        """Parse a single CSV data row into a ParsedTransaction."""
        # -- Date -------------------------------------------------------------
        date_idx = col_map["date_idx"]
        if date_idx is None or date_idx >= len(row):
            return None
        raw_date = row[date_idx].strip()
        if not raw_date:
            return None

        iso_date = self._parse_date(raw_date, template.date_format)
        if iso_date is None:
            return None

        # -- Description ------------------------------------------------------
        desc_idx = col_map.get("desc_idx")
        description = ""
        if desc_idx is not None and desc_idx < len(row):
            description = row[desc_idx].strip()
        if not description:
            return None

        # -- Amount (integer cents) -------------------------------------------
        amount_cents = self._extract_amount(row, col_map, template)
        if amount_cents == 0:
            return None

        # -- Balance ----------------------------------------------------------
        balance_cents: int | None = None
        balance_idx = col_map.get("balance_idx")
        if balance_idx is not None and balance_idx < len(row):
            balance_cents = _parse_amount_to_cents(row[balance_idx])

        return ParsedTransaction(
            transaction_date=iso_date,
            description_raw=description,
            amount_cents=amount_cents,
            balance_after_cents=balance_cents,
            source_row=row_idx,
            raw_text=",".join(row),
        )

    def _extract_amount(
        self, row: list[str], col_map: dict, template: BankTemplate
    ) -> int:
        """Determine the transaction amount in integer cents.

        Handles two conventions:
        - ``negative_is_debit``: a single amount column where negative
          values represent debits.
        - ``separate_columns``: distinct debit and credit columns.
        """
        if template.amount_sign == "separate_columns":
            debit_idx = col_map.get("debit_idx")
            credit_idx = col_map.get("credit_idx")

            # Reason: try debit first — if the cell has a value, it's a
            # debit (negative); credit cells are positive
            if debit_idx is not None and debit_idx < len(row):
                val = _parse_amount_to_cents(row[debit_idx])
                if val is not None and val != 0:
                    return -abs(val)

            if credit_idx is not None and credit_idx < len(row):
                val = _parse_amount_to_cents(row[credit_idx])
                if val is not None and val != 0:
                    return abs(val)

            return 0

        # Default: negative_is_debit (single amount column)
        amount_idx = col_map.get("amount_idx")
        if amount_idx is not None and amount_idx < len(row):
            val = _parse_amount_to_cents(row[amount_idx])
            if val is not None:
                return val

        return 0

    @staticmethod
    def _parse_date(raw: str, fmt: str) -> str | None:
        """Parse a date string using a specific format into ISO YYYY-MM-DD."""
        raw = raw.strip()
        if not raw:
            return None
            
        formats_to_try = [
            fmt, "%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", 
            "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"
        ]
        
        for f in formats_to_try:
            try:
                dt = datetime.strptime(raw, f)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
                
        return None


# ── Module-level utility ────────────────────────────────────────────────


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
