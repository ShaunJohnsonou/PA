"""Financial validation rules engine (FR-4.3).

Validates extracted bank statements against 11 integrity rules.
Each rule produces a validation_results row in DuckDB.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime

from .ledger_db import FinanceLedger

logger = logging.getLogger(__name__)

# Configurable thresholds
MAX_SINGLE_TRANSACTION_CENTS = 10_000_000_00  # R10M in cents


class ValidationEngine:
    """Runs all 11 validation rules against an extracted statement."""

    def __init__(self, ledger: FinanceLedger) -> None:
        self._ledger = ledger

    def validate_statement(self, statement_id: str, document_id: str) -> dict:
        """Run all validation rules and store results.

        Returns:
            Dict with validation_status, passed_count, failed_count, results.
        """
        logger.info(
            "=== Validation starting: statement=%s, document=%s ===",
            statement_id, document_id,
        )
        c = self._ledger.conn

        # Load statement
        stmt = c.execute(
            "SELECT * FROM bank_statements WHERE statement_id = ?",
            [statement_id],
        ).fetchone()
        if stmt is None:
            logger.error("Statement not found: %s", statement_id)
            return {"error": "statement_not_found"}

        cols = [desc[0] for desc in c.description]
        stmt_dict = dict(zip(cols, stmt))
        logger.debug(
            "Statement loaded: bank=%s, period=%s to %s, opening=%s, closing=%s",
            stmt_dict.get("bank_name"), stmt_dict.get("period_start"),
            stmt_dict.get("period_end"), stmt_dict.get("opening_balance_cents"),
            stmt_dict.get("closing_balance_cents"),
        )

        # Load transactions
        txn_rows = c.execute(
            "SELECT * FROM transactions WHERE statement_id = ? ORDER BY transaction_date, source_row",
            [statement_id],
        ).fetchall()
        txn_cols = [desc[0] for desc in c.description]
        transactions = [dict(zip(txn_cols, r)) for r in txn_rows]
        logger.debug("Loaded %d transactions for validation", len(transactions))

        # Run all rules
        results = []
        rules = [
            self._rule_balance_equation,
            self._rule_running_balance,
            self._rule_period_consistency,
            self._rule_date_validity,
            self._rule_page_continuity,
            self._rule_currency_consistency,
            self._rule_summary_totals,
            self._rule_duplicate_detection,
            self._rule_sign_consistency,
            self._rule_evidence_completeness,
            self._rule_amount_reasonableness,
        ]

        for rule_fn in rules:
            rule_name = rule_fn.__name__.replace("_rule_", "")
            try:
                result = rule_fn(stmt_dict, transactions)
                results.append(result)
                status = "PASS" if result["passed"] else f"FAIL ({result['severity']})"
                logger.debug(
                    "  Rule %-25s: %s%s",
                    rule_name, status,
                    f" — {result.get('notes', '')}" if not result["passed"] else "",
                )
            except Exception as exc:
                logger.warning("Rule %s CRASHED: %s", rule_name, exc)
                results.append({
                    "rule_name": rule_name,
                    "passed": False,
                    "severity": "error",
                    "notes": f"Rule execution failed: {exc}",
                })

        # Store results in DuckDB
        logger.debug("Storing %d validation results in DuckDB...", len(results))
        for r in results:
            self._ledger.insert_validation_result(
                document_id=document_id,
                statement_id=statement_id,
                rule_name=r["rule_name"],
                passed=r["passed"],
                expected_value=r.get("expected_value"),
                actual_value=r.get("actual_value"),
                severity=r["severity"],
                notes=r.get("notes"),
            )

        # Determine overall status
        has_errors = any(
            not r["passed"] and r["severity"] == "error" for r in results
        )
        validation_status = "needs_review" if has_errors else "passed"

        # Update statement
        c.execute(
            "UPDATE bank_statements SET validation_status = ? WHERE statement_id = ?",
            [validation_status, statement_id],
        )
        logger.debug("Updated statement %s validation_status=%s", statement_id, validation_status)

        passed_count = sum(1 for r in results if r["passed"])
        failed_count = len(results) - passed_count

        logger.info(
            "=== Validation COMPLETE: statement=%s -> %s (%d/%d passed) ===",
            statement_id, validation_status, passed_count, len(results),
        )

        return {
            "validation_status": validation_status,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "results": results,
        }

    # ── Individual rules ────────────────────────────────────────────

    def _rule_balance_equation(self, stmt: dict, txns: list[dict]) -> dict:
        """opening_balance + credits - debits = closing_balance"""
        opening = stmt.get("opening_balance_cents")
        closing = stmt.get("closing_balance_cents")

        if opening is None or closing is None:
            return {
                "rule_name": "balance_equation",
                "passed": True,
                "severity": "error",
                "notes": "Opening or closing balance not available, skipping",
            }

        credits_sum = sum(t["amount_cents"] for t in txns if t["amount_cents"] > 0)
        debits_sum = sum(abs(t["amount_cents"]) for t in txns if t["amount_cents"] < 0)
        calculated = opening + credits_sum - debits_sum

        passed = calculated == closing
        return {
            "rule_name": "balance_equation",
            "passed": passed,
            "expected_value": str(closing),
            "actual_value": str(calculated),
            "severity": "error",
            "notes": None if passed else f"Mismatch: opening({opening}) + credits({credits_sum}) - debits({debits_sum}) = {calculated}, expected {closing}",
        }

    def _rule_running_balance(self, stmt: dict, txns: list[dict]) -> dict:
        """Each transaction's balance_after = previous balance + amount."""
        if not txns or txns[0].get("balance_after_cents") is None:
            return {
                "rule_name": "running_balance",
                "passed": True,
                "severity": "error",
                "notes": "Running balance not available in transactions",
            }

        mismatches = []
        for i in range(1, len(txns)):
            prev_balance = txns[i - 1].get("balance_after_cents")
            curr_balance = txns[i].get("balance_after_cents")
            amount = txns[i].get("amount_cents", 0)

            if prev_balance is None or curr_balance is None:
                continue

            expected = prev_balance + amount
            if expected != curr_balance:
                mismatches.append(f"Row {i}: expected {expected}, got {curr_balance}")

        passed = len(mismatches) == 0
        return {
            "rule_name": "running_balance",
            "passed": passed,
            "severity": "error",
            "notes": "; ".join(mismatches[:5]) if mismatches else None,
        }

    def _rule_period_consistency(self, stmt: dict, txns: list[dict]) -> dict:
        """All transaction dates within period_start to period_end."""
        start = stmt.get("period_start")
        end = stmt.get("period_end")

        if not start or not end:
            return {
                "rule_name": "period_consistency",
                "passed": True,
                "severity": "error",
                "notes": "Statement period not available",
            }

        out_of_range = []
        for i, t in enumerate(txns):
            d = str(t.get("transaction_date", ""))
            if d and (d < str(start) or d > str(end)):
                out_of_range.append(f"Row {i}: {d}")

        passed = len(out_of_range) == 0
        return {
            "rule_name": "period_consistency",
            "passed": passed,
            "severity": "error",
            "notes": f"{len(out_of_range)} transactions outside period: {'; '.join(out_of_range[:3])}" if out_of_range else None,
        }

    def _rule_date_validity(self, stmt: dict, txns: list[dict]) -> dict:
        """No future dates or dates before 2000."""
        today = datetime.now().strftime("%Y-%m-%d")
        invalid = []

        for i, t in enumerate(txns):
            d = str(t.get("transaction_date", ""))
            if d and (d > today or d < "2000-01-01"):
                invalid.append(f"Row {i}: {d}")

        passed = len(invalid) == 0
        return {
            "rule_name": "date_validity",
            "passed": passed,
            "severity": "error",
            "notes": f"{len(invalid)} invalid dates: {'; '.join(invalid[:3])}" if invalid else None,
        }

    def _rule_page_continuity(self, stmt: dict, txns: list[dict]) -> dict:
        """No gaps in page numbers."""
        pages = sorted(set(t.get("source_page") for t in txns if t.get("source_page")))
        if len(pages) < 2:
            return {"rule_name": "page_continuity", "passed": True, "severity": "warning"}

        gaps = []
        for i in range(1, len(pages)):
            if pages[i] - pages[i - 1] > 1:
                gaps.append(f"Gap between pages {pages[i-1]} and {pages[i]}")

        return {
            "rule_name": "page_continuity",
            "passed": len(gaps) == 0,
            "severity": "warning",
            "notes": "; ".join(gaps) if gaps else None,
        }

    def _rule_currency_consistency(self, stmt: dict, txns: list[dict]) -> dict:
        """All transactions same currency as statement."""
        stmt_currency = stmt.get("currency", "ZAR")
        mismatches = [
            i for i, t in enumerate(txns)
            if t.get("currency") and t["currency"] != stmt_currency
        ]

        return {
            "rule_name": "currency_consistency",
            "passed": len(mismatches) == 0,
            "severity": "error",
            "notes": f"{len(mismatches)} transactions with wrong currency" if mismatches else None,
        }

    def _rule_summary_totals(self, stmt: dict, txns: list[dict]) -> dict:
        """Sum of debits/credits matches statement totals."""
        expected_debits = stmt.get("total_debits_cents")
        expected_credits = stmt.get("total_credits_cents")

        if expected_debits is None and expected_credits is None:
            return {
                "rule_name": "summary_totals",
                "passed": True,
                "severity": "error",
                "notes": "Statement totals not available",
            }

        actual_debits = sum(abs(t["amount_cents"]) for t in txns if t["amount_cents"] < 0)
        actual_credits = sum(t["amount_cents"] for t in txns if t["amount_cents"] > 0)

        issues = []
        if expected_debits is not None and actual_debits != expected_debits:
            issues.append(f"Debits: expected {expected_debits}, got {actual_debits}")
        if expected_credits is not None and actual_credits != expected_credits:
            issues.append(f"Credits: expected {expected_credits}, got {actual_credits}")

        return {
            "rule_name": "summary_totals",
            "passed": len(issues) == 0,
            "severity": "error",
            "notes": "; ".join(issues) if issues else None,
        }

    def _rule_duplicate_detection(self, stmt: dict, txns: list[dict]) -> dict:
        """Flag identical date + amount + description within same statement."""
        seen: dict[str, list[int]] = {}
        for i, t in enumerate(txns):
            key = f"{t['transaction_date']}|{t['amount_cents']}|{t['description_raw']}"
            seen.setdefault(key, []).append(i)

        duplicates = {k: v for k, v in seen.items() if len(v) > 1}

        return {
            "rule_name": "duplicate_detection",
            "passed": len(duplicates) == 0,
            "severity": "warning",
            "notes": f"{len(duplicates)} potential duplicate groups found" if duplicates else None,
        }

    def _rule_sign_consistency(self, stmt: dict, txns: list[dict]) -> dict:
        """Verify debit/credit sign convention is consistent."""
        # Reason: in most SA bank statements, debits are negative and
        # credits are positive. Check that we don't have a mixed convention.
        has_positive = any(t["amount_cents"] > 0 for t in txns)
        has_negative = any(t["amount_cents"] < 0 for t in txns)

        # Both positive and negative is expected (credits + debits)
        # Only flag if ALL are same sign (likely parsing error)
        if txns and not (has_positive and has_negative):
            return {
                "rule_name": "sign_consistency",
                "passed": False,
                "severity": "error",
                "notes": "All transactions have the same sign — possible parsing error",
            }

        return {
            "rule_name": "sign_consistency",
            "passed": True,
            "severity": "error",
        }

    def _rule_evidence_completeness(self, stmt: dict, txns: list[dict]) -> dict:
        """Every transaction has source_page and source_row."""
        missing = [
            i for i, t in enumerate(txns)
            if t.get("source_page") is None or t.get("source_row") is None
        ]

        return {
            "rule_name": "evidence_completeness",
            "passed": len(missing) == 0,
            "severity": "warning",
            "notes": f"{len(missing)} transactions missing source evidence" if missing else None,
        }

    def _rule_amount_reasonableness(self, stmt: dict, txns: list[dict]) -> dict:
        """No single transaction exceeds threshold (default R10M)."""
        large = [
            (i, t["amount_cents"])
            for i, t in enumerate(txns)
            if abs(t["amount_cents"]) > MAX_SINGLE_TRANSACTION_CENTS
        ]

        return {
            "rule_name": "amount_reasonableness",
            "passed": len(large) == 0,
            "severity": "warning",
            "notes": f"{len(large)} transactions exceed R10M threshold" if large else None,
        }
