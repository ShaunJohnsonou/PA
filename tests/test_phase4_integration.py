"""Phase 4 Integration Test - Financial Ledger.

Run this script locally (no Docker needed) to verify Phase 4 components:
  1. DuckDB schema creation
  2. Generic + FNB parser table parsing
  3. Amount-to-cents conversion
  4. Transaction insertion + querying
  5. Validation rules engine
  6. Spending analysis + anomaly detection

Usage:
    cd c:\\Users\\ShaunJohnson\\repos\\PA
    python tests/test_phase4_integration.py
"""
import asyncio
import json
import os
import sys
import tempfile

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_amount_parsing():
    """Test the amount-to-cents conversion utility."""
    from mcp_servers.document_catalog.finance.parsers.generic_parser import _parse_amount_to_cents

    assert _parse_amount_to_cents("R 1,234.56") == 123456, "Standard ZAR amount"
    assert _parse_amount_to_cents("-R1234.56") == -123456, "Negative amount"
    assert _parse_amount_to_cents("(1234.56)") == -123456, "Accounting negative"
    assert _parse_amount_to_cents("0.99") == 99, "Sub-rand amount"
    assert _parse_amount_to_cents("") is None, "Empty string"
    assert _parse_amount_to_cents("   ") is None, "Whitespace"
    assert _parse_amount_to_cents("R 0.01") == 1, "One cent"
    print("  [PASS] Amount parsing: all assertions passed")


def test_date_parsing():
    """Test the date parser."""
    from mcp_servers.document_catalog.finance.parsers.generic_parser import _parse_date

    assert _parse_date("01/03/2025") == "2025-03-01", "DD/MM/YYYY"
    assert _parse_date("2025-03-01") == "2025-03-01", "ISO format"
    assert _parse_date("01 Mar 2025") == "2025-03-01", "DD Mon YYYY"
    assert _parse_date("") is None, "Empty"
    assert _parse_date("not a date") is None, "Invalid"
    print("  [PASS] Date parsing: all assertions passed")


def test_merchant_normalisation():
    """Test regex-based merchant normalisation."""
    from mcp_servers.document_catalog.finance.merchant_normaliser import normalise_merchant

    assert normalise_merchant("POS PURCHASE PICK N PAY SANDTON") == "Pick n Pay"
    assert normalise_merchant("UBER EATS *UBER EATS") == "Uber Eats"
    assert normalise_merchant("WOOLWORTHS FOOD ROSEBANK") == "Woolworths"
    assert normalise_merchant("MONTHLY ACCOUNT FEE") == "Bank Fee"
    assert normalise_merchant("random unknown thing") is None
    print("  [PASS] Merchant normalisation: all assertions passed")


def test_categorisation():
    """Test regex-based transaction categorisation."""
    from mcp_servers.document_catalog.finance.categoriser import classify_transaction

    cat, conf = classify_transaction("POS PURCHASE PICK N PAY")
    assert cat == "groceries", f"Expected groceries, got {cat}"

    cat, conf = classify_transaction("ENGEN FUEL STATION")
    assert cat == "fuel", f"Expected fuel, got {cat}"

    cat, conf = classify_transaction("NETFLIX.COM")
    assert cat == "subscriptions", f"Expected subscriptions, got {cat}"

    cat, conf = classify_transaction("MONTHLY ACCOUNT FEE")
    assert cat == "bank_fees", f"Expected bank_fees, got {cat}"

    cat, conf = classify_transaction("SOME UNKNOWN VENDOR XYZ")
    assert cat == "other", f"Expected other, got {cat}"

    print("  [PASS] Categorisation: all assertions passed")


def test_duckdb_schema():
    """Test DuckDB schema creation and CRUD operations."""
    from mcp_servers.document_catalog.finance.ledger_db import FinanceLedger

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_finance.duckdb")
        ledger = FinanceLedger(db_path)
        ledger.connect()

        # Test account upsert
        acc_id = ledger.upsert_account(
            bank_name="FNB",
            account_number_masked="****1234",
            account_type="cheque",
            seen_date="2025-03-01",
        )
        assert acc_id, "Account ID should be returned"

        # Upsert same account again - should return the same ID
        acc_id2 = ledger.upsert_account(
            bank_name="FNB",
            account_number_masked="****1234",
            seen_date="2025-04-01",
        )
        assert acc_id == acc_id2, "Same account should return same ID"

        # Insert a statement
        stmt_id = ledger.insert_statement(
            document_id="doc-001",
            account_id=acc_id,
            bank_name="FNB",
            period_start="2025-03-01",
            period_end="2025-03-31",
            opening_balance_cents=100000,
            closing_balance_cents=85000,
        )
        assert stmt_id, "Statement ID should be returned"

        # Insert transactions
        txns = [
            {
                "transaction_id": "txn-001",
                "statement_id": stmt_id,
                "account_id": acc_id,
                "transaction_date": "2025-03-05",
                "description_raw": "POS PURCHASE PICK N PAY",
                "amount_cents": -45678,
                "currency": "ZAR",
                "source_document_id": "doc-001",
                "source_page": 2,
                "source_row": 1,
            },
            {
                "transaction_id": "txn-002",
                "statement_id": stmt_id,
                "account_id": acc_id,
                "transaction_date": "2025-03-15",
                "description_raw": "SALARY FROM EMPLOYER",
                "amount_cents": 5000000,
                "currency": "ZAR",
                "source_document_id": "doc-001",
                "source_page": 3,
                "source_row": 5,
            },
        ]
        count = ledger.insert_transactions(txns)
        assert count == 2, f"Expected 2 transactions, got {count}"

        # Query back
        rows = ledger.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
        assert rows[0] == 2, f"Expected 2 rows, got {rows[0]}"

        # Test get_statement_by_document
        stmt = ledger.get_statement_by_document("doc-001")
        assert stmt is not None, "Statement should be found"
        assert stmt["bank_name"] == "FNB"

        ledger.close()
        print("  [PASS] DuckDB schema + CRUD: all assertions passed")


def test_validation_engine():
    """Test the validation rules engine."""
    from mcp_servers.document_catalog.finance.ledger_db import FinanceLedger
    from mcp_servers.document_catalog.finance.validation import ValidationEngine

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_finance.duckdb")
        ledger = FinanceLedger(db_path)
        ledger.connect()

        # Setup: account + statement + transactions
        acc_id = ledger.upsert_account("FNB", "****1234")
        stmt_id = ledger.insert_statement(
            document_id="doc-val-001",
            account_id=acc_id,
            bank_name="FNB",
            period_start="2025-03-01",
            period_end="2025-03-31",
            opening_balance_cents=100000,
            closing_balance_cents=54322,
        )

        txns = [
            {
                "transaction_id": "val-txn-001",
                "statement_id": stmt_id,
                "account_id": acc_id,
                "transaction_date": "2025-03-05",
                "description_raw": "PURCHASE",
                "amount_cents": -45678,
                "currency": "ZAR",
                "source_document_id": "doc-val-001",
                "source_page": 1,
                "source_row": 1,
                "balance_after_cents": 54322,
            },
        ]
        ledger.insert_transactions(txns)

        # Run validation
        engine = ValidationEngine(ledger)
        result = engine.validate_statement(stmt_id, "doc-val-001")

        assert "validation_status" in result
        assert result["passed_count"] + result["failed_count"] == 11, "Should run all 11 rules"
        print(f"  [PASS] Validation engine: {result['passed_count']} passed, {result['failed_count']} failed (status: {result['validation_status']})")

        ledger.close()


def test_generic_parser():
    """Test the generic parser with mock table data."""
    from mcp_servers.document_catalog.finance.parsers.generic_parser import GenericParser

    parser = GenericParser()

    tables = [{
        "headers": ["Date", "Description", "Debit", "Credit", "Balance"],
        "rows": [
            ["01/03/2025", "POS PURCHASE PICK N PAY", "456.78", "", "R 543.22"],
            ["15/03/2025", "SALARY", "", "50,000.00", "R 50,543.22"],
            ["20/03/2025", "UBER EATS", "150.00", "", "R 50,393.22"],
        ],
        "page_number": 2,
    }]

    result = parser.parse(tables, "FNB Statement March 2025", "FNB_Statement_March_2025.pdf", "doc-001")

    assert len(result.transactions) == 3, f"Expected 3 transactions, got {len(result.transactions)}"

    # First transaction: debit
    txn0 = result.transactions[0]
    assert txn0.amount_cents == -45678, f"Expected -45678 cents, got {txn0.amount_cents}"
    assert txn0.transaction_date == "2025-03-01"

    # Second: credit
    txn1 = result.transactions[1]
    assert txn1.amount_cents == 5000000, f"Expected 5000000 cents, got {txn1.amount_cents}"

    print(f"  [PASS] Generic parser: {len(result.transactions)} transactions parsed correctly")


async def test_query_tool():
    """Test query_transactions tool with real DuckDB data."""
    from mcp_servers.document_catalog.finance.ledger_db import FinanceLedger
    from mcp_servers.document_catalog.tools.query_transactions import handle_query_transactions

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_finance.duckdb")
        ledger = FinanceLedger(db_path)
        ledger.connect()

        acc_id = ledger.upsert_account("FNB", "****1234")
        stmt_id = ledger.insert_statement(
            document_id="doc-q-001",
            account_id=acc_id,
            bank_name="FNB",
            period_start="2025-03-01",
            period_end="2025-03-31",
            validation_status="passed",
        )
        ledger.insert_transactions([
            {"transaction_id": "q-txn-001", "statement_id": stmt_id, "account_id": acc_id,
             "transaction_date": "2025-03-05", "description_raw": "PICK N PAY",
             "amount_cents": -15000, "currency": "ZAR", "source_document_id": "doc-q-001",
             "category": "groceries", "merchant": "Pick n Pay"},
            {"transaction_id": "q-txn-002", "statement_id": stmt_id, "account_id": acc_id,
             "transaction_date": "2025-03-10", "description_raw": "SALARY",
             "amount_cents": 3000000, "currency": "ZAR", "source_document_id": "doc-q-001",
             "category": "salary", "merchant": "Employer"},
        ])

        # Test raw query
        result = await handle_query_transactions(
            date_from="2025-03-01", date_to="2025-03-31",
            ledger=ledger,
        )
        assert result["total_count"] == 2
        assert result["transactions"][0]["amount_cents"] in (-15000, 3000000)

        # Test aggregation
        result = await handle_query_transactions(
            date_from="2025-03-01", date_to="2025-03-31",
            group_by=["category"], metrics=["sum", "count"],
            ledger=ledger,
        )
        assert result["total_count"] == 2  # 2 categories

        print(f"  [PASS] Query tool: {result['total_count']} aggregated groups returned")
        ledger.close()


def main():
    print("=" * 60)
    print("  Phase 4 Integration Tests")
    print("=" * 60)

    tests = [
        ("Amount parsing", test_amount_parsing),
        ("Date parsing", test_date_parsing),
        ("Merchant normalisation", test_merchant_normalisation),
        ("Categorisation", test_categorisation),
        ("DuckDB schema + CRUD", test_duckdb_schema),
        ("Validation engine", test_validation_engine),
        ("Generic parser", test_generic_parser),
    ]

    async_tests = [
        ("Query tool", test_query_tool),
    ]

    passed = 0
    failed = 0

    for name, fn in tests:
        try:
            print(f"\n[TEST] {name}")
            fn()
            passed += 1
        except Exception as exc:
            print(f"  [FAIL]: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    for name, fn in async_tests:
        try:
            print(f"\n[TEST] {name}")
            asyncio.run(fn())
            passed += 1
        except Exception as exc:
            print(f"  [FAIL]: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 60)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
