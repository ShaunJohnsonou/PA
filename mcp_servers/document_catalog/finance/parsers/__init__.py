"""Bank statement parsers — base class and registry.

Each bank has a different statement layout. Parsers convert extracted
table data into normalised transaction dictionaries ready for DuckDB.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class StatementMetadata:
    """Parsed metadata from a bank statement header."""

    bank_name: str = ""
    account_number_masked: str = ""
    account_type: str | None = None
    period_start: str | None = None       # ISO date
    period_end: str | None = None         # ISO date
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None
    total_debits_cents: int | None = None
    total_credits_cents: int | None = None
    currency: str = "ZAR"


@dataclass
class ParsedTransaction:
    """A single parsed transaction row."""

    transaction_date: str               # ISO date
    posting_date: str | None = None
    description_raw: str = ""
    reference: str | None = None
    amount_cents: int = 0               # positive = credit, negative = debit
    balance_after_cents: int | None = None
    source_page: int | None = None
    source_row: int | None = None
    raw_text: str | None = None         # Exact text from the PDF row


@dataclass
class ParseResult:
    """Output from a statement parser."""

    metadata: StatementMetadata
    transactions: list[ParsedTransaction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class StatementParser(ABC):
    """Abstract base class for bank statement parsers."""

    @property
    @abstractmethod
    def bank_name(self) -> str:
        """Return the bank name this parser handles."""

    @abstractmethod
    def can_parse(self, text: str, filename: str) -> bool:
        """Return True if this parser can handle the given document."""

    @abstractmethod
    def parse(
        self,
        tables: list[dict],
        full_text: str,
        filename: str,
        document_id: str,
    ) -> ParseResult:
        """Parse extracted tables into structured transactions.

        Args:
            tables: List of table dicts (from Docling or pdfplumber).
                    Each has 'headers', 'rows', 'page_number'.
            full_text: Full markdown text of the document.
            filename: Original filename.
            document_id: UUID of the source document.

        Returns:
            ParseResult with metadata and transactions.
        """


class ParserRegistry:
    """Registry of bank-specific statement parsers.

    Falls back to the generic parser if no bank-specific parser matches.
    """

    def __init__(self) -> None:
        self._parsers: list[StatementParser] = []
        self._generic: StatementParser | None = None

    def register(self, parser: StatementParser, *, is_generic: bool = False) -> None:
        """Register a parser."""
        if is_generic:
            self._generic = parser
        else:
            self._parsers.append(parser)
        logger.debug("Registered parser: %s (generic=%s)", parser.bank_name, is_generic)

    def select_parser(self, text: str, filename: str) -> StatementParser | None:
        """Select the best parser for a document.

        Tries bank-specific parsers first, then falls back to generic.
        """
        for parser in self._parsers:
            if parser.can_parse(text, filename):
                logger.info("Selected parser: %s", parser.bank_name)
                return parser

        if self._generic:
            logger.info("No bank-specific parser matched, using generic parser")
            return self._generic

        logger.warning("No parser available for document")
        return None
