"""OFX/QFX bank statement parser.

Parses OFX and QFX files (Open Financial Exchange) into the standard
ParseResult format.  Requires the ``ofxparse`` library; if it is not
installed the module still loads but ``OFXParser.parse()`` raises a
helpful ``ImportError``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..parsers import ParsedTransaction, ParseResult, StatementMetadata

logger = logging.getLogger(__name__)

# Reason: ofxparse is an optional dependency — allow the module to load
# even when it is missing so that other importers remain usable
try:
    import ofxparse  # type: ignore[import-untyped]

    HAS_OFXPARSE = True
except ImportError:
    HAS_OFXPARSE = False


class OFXParser:
    """Parses OFX/QFX files into ``ParseResult``.

    Uses the ``ofxparse`` library under the hood.  If the library is
    not installed, ``parse()`` raises an ``ImportError`` with
    installation instructions.
    """

    def parse(self, ofx_path: str) -> ParseResult:
        """Parse an OFX/QFX file.

        Args:
            ofx_path: Absolute path to the OFX or QFX file.

        Returns:
            A ``ParseResult`` with metadata and transactions.

        Raises:
            ImportError: If ``ofxparse`` is not installed.
        """
        if not HAS_OFXPARSE:
            raise ImportError(
                "The 'ofxparse' library is required to parse OFX files. "
                "Install it with: pip install ofxparse"
            )

        logger.info("Parsing OFX file: %s", ofx_path)
        warnings: list[str] = []
        errors: list[str] = []

        try:
            with open(ofx_path, "rb") as fh:
                ofx = ofxparse.OfxParser.parse(fh)
        except Exception as exc:
            errors.append(f"Failed to parse OFX file: {exc}")
            return ParseResult(
                metadata=StatementMetadata(),
                transactions=[],
                warnings=warnings,
                errors=errors,
            )

        # -- Extract metadata -------------------------------------------------
        metadata = self._extract_metadata(ofx)
        logger.debug(
            "OFX metadata: bank=%s, account=%s, currency=%s",
            metadata.bank_name, metadata.account_number_masked, metadata.currency,
        )

        # -- Extract transactions ---------------------------------------------
        transactions: list[ParsedTransaction] = []
        account = ofx.account if ofx.account else None
        if account is None:
            warnings.append("OFX file contains no account data")
            return ParseResult(
                metadata=metadata,
                transactions=[],
                warnings=warnings,
                errors=errors,
            )

        statement = account.statement if account.statement else None
        ofx_transactions = statement.transactions if statement else []

        for idx, ofx_txn in enumerate(ofx_transactions):
            try:
                txn = self._convert_transaction(ofx_txn, idx)
                if txn:
                    transactions.append(txn)
            except Exception as exc:
                warnings.append(f"OFX transaction {idx}: {exc}")
                logger.debug("OFX transaction %d error: %s", idx, exc)

        # -- Derive period from transactions ----------------------------------
        if transactions:
            dates = [t.transaction_date for t in transactions]
            metadata.period_start = min(dates)
            metadata.period_end = max(dates)

        # -- Statement balance info -------------------------------------------
        if statement:
            if hasattr(statement, "balance") and statement.balance is not None:
                try:
                    metadata.closing_balance_cents = _decimal_to_cents(statement.balance)
                except Exception:
                    pass

        logger.info(
            "OFX parse complete: %d transactions, %d warnings, period=%s–%s",
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

    def _extract_metadata(self, ofx: object) -> StatementMetadata:
        """Build StatementMetadata from the parsed OFX object."""
        meta = StatementMetadata()

        account = getattr(ofx, "account", None)
        if account is None:
            return meta

        # Bank / institution name
        institution = getattr(account, "institution", None)
        if institution:
            org = getattr(institution, "organization", None)
            if org:
                meta.bank_name = str(org)

        if not meta.bank_name:
            meta.bank_name = "Unknown (OFX)"

        # Account number — mask to last 4 digits for security
        acc_id = getattr(account, "account_id", None) or getattr(
            account, "number", None
        )
        if acc_id:
            acc_str = str(acc_id).strip()
            if len(acc_str) >= 4:
                meta.account_number_masked = "****" + acc_str[-4:]
            else:
                meta.account_number_masked = acc_str

        # Account type
        acc_type = getattr(account, "account_type", None) or getattr(
            account, "type", None
        )
        if acc_type:
            meta.account_type = str(acc_type)

        # Currency
        stmt = getattr(account, "statement", None)
        if stmt:
            currency = getattr(stmt, "currency", None)
            if currency:
                meta.currency = str(currency)

        return meta

    def _convert_transaction(
        self, ofx_txn: object, idx: int
    ) -> ParsedTransaction | None:
        """Convert a single ofxparse transaction to ParsedTransaction."""
        # Date
        txn_date = getattr(ofx_txn, "date", None)
        if txn_date is None:
            return None
        if isinstance(txn_date, datetime):
            iso_date = txn_date.strftime("%Y-%m-%d")
        else:
            iso_date = str(txn_date)[:10]

        # Description — prefer payee, fall back to memo
        payee = getattr(ofx_txn, "payee", None) or ""
        memo = getattr(ofx_txn, "memo", None) or ""
        description = (str(payee).strip() or str(memo).strip()).strip()
        if not description:
            description = "Unknown transaction"

        # Amount → integer cents
        amount = getattr(ofx_txn, "amount", None)
        if amount is None:
            return None
        amount_cents = _decimal_to_cents(amount)

        # OFX transaction type (informational)
        txn_type = getattr(ofx_txn, "type", None)
        raw_parts = [f"type={txn_type}"] if txn_type else []
        if payee:
            raw_parts.append(f"payee={payee}")
        if memo:
            raw_parts.append(f"memo={memo}")
        raw_text = " | ".join(raw_parts) if raw_parts else None

        # Reference / check number
        reference = getattr(ofx_txn, "checknum", None) or getattr(
            ofx_txn, "id", None
        )
        if reference:
            reference = str(reference).strip() or None

        return ParsedTransaction(
            transaction_date=iso_date,
            description_raw=description,
            reference=reference,
            amount_cents=amount_cents,
            source_row=idx,
            raw_text=raw_text,
        )


# ── Module-level utility ────────────────────────────────────────────────


def _decimal_to_cents(value: object) -> int:
    """Convert a Decimal or float amount to integer cents.

    Args:
        value: A ``Decimal``, ``float``, or ``int`` monetary amount
               (e.g. ``-123.45``).

    Returns:
        Integer cents (``-12345``).
    """
    return round(float(value) * 100)
