"""Unit tests for TR-5.5 — CSV bank import parser."""
from __future__ import annotations

import json
import os
import pytest

# Reason: Import may fail if the module hasn't been created yet.
# Tests should be runnable once the module exists.
from mcp_servers.document_catalog.finance.importers.csv_parser import (
    BankTemplate,
    CSVBankParser,
)


def _write_fnb_csv(path: str) -> str:
    """Write a sample FNB-format CSV file."""
    content = """Date,Description,Amount,Balance
01/03/2025,SALARY DEPOSIT,25000.00,25000.00
02/03/2025,PICK N PAY SANDTON,-450.50,24549.50
03/03/2025,WOOLWORTHS FOOD,-1230.00,23319.50
05/03/2025,TRANSFER TO SAVINGS,-5000.00,18319.50
10/03/2025,UBER TRIP,-125.75,18193.75"""
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(content)
    return path


def _write_nedbank_csv(path: str) -> str:
    """Write a sample Nedbank-format CSV with separate debit/credit columns."""
    content = """Transaction Date,Description,Debit,Credit,Balance
2025-03-01,SALARY PAYMENT,,25000.00,25000.00
2025-03-02,CHECKERS PURCHASE,350.00,,24650.00
2025-03-03,MUNICIPAL ACCOUNT,2500.00,,22150.00"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


@pytest.fixture
def template_dir(tmp_path):
    """Create a temp directory with bank template JSON files."""
    tdir = str(tmp_path / "templates")
    os.makedirs(tdir, exist_ok=True)
    fnb = {
        "bank_name": "FNB",
        "skip_rows": 0,
        "date_column": "Date",
        "date_format": "%d/%m/%Y",
        "description_column": "Description",
        "amount_column": "Amount",
        "balance_column": "Balance",
        "amount_sign": "negative_is_debit",
        "currency": "ZAR",
        "encoding": "utf-8-sig",
        "header_patterns": ["Date", "Description", "Amount", "Balance"]
    }
    nedbank = {
        "bank_name": "Nedbank",
        "skip_rows": 0,
        "date_column": "Transaction Date",
        "date_format": "%Y-%m-%d",
        "description_column": "Description",
        "debit_column": "Debit",
        "credit_column": "Credit",
        "balance_column": "Balance",
        "amount_sign": "separate_columns",
        "currency": "ZAR",
        "encoding": "utf-8",
        "header_patterns": ["Transaction Date", "Description", "Debit", "Credit", "Balance"]
    }
    with open(os.path.join(tdir, "fnb.json"), "w") as f:
        json.dump(fnb, f)
    with open(os.path.join(tdir, "nedbank.json"), "w") as f:
        json.dump(nedbank, f)
    return tdir


class TestCSVParser:
    def test_parse_fnb_csv(self, template_dir, tmp_path):
        csv_path = _write_fnb_csv(str(tmp_path / "fnb_statement.csv"))
        parser = CSVBankParser(template_dir)
        result = parser.parse(csv_path, bank="FNB")
        assert len(result.transactions) == 5
        # First transaction: salary deposit 25000.00 = 2500000 cents
        assert result.transactions[0].amount_cents == 2500000
        assert result.transactions[0].description_raw == "SALARY DEPOSIT"
        # Second: debit of 450.50 = -45050 cents
        assert result.transactions[1].amount_cents == -45050

    def test_parse_nedbank_separate_columns(self, template_dir, tmp_path):
        csv_path = _write_nedbank_csv(str(tmp_path / "nedbank_statement.csv"))
        parser = CSVBankParser(template_dir)
        result = parser.parse(csv_path, bank="Nedbank")
        assert len(result.transactions) == 3
        # Salary: credit of 25000.00 = 2500000 cents
        assert result.transactions[0].amount_cents == 2500000
        # Checkers: debit of 350.00 = -35000 cents
        assert result.transactions[1].amount_cents == -35000

    def test_detect_bank_from_headers(self, template_dir, tmp_path):
        csv_path = _write_fnb_csv(str(tmp_path / "unknown.csv"))
        parser = CSVBankParser(template_dir)
        detected = parser.detect_bank(csv_path)
        assert detected == "FNB"

    def test_unknown_bank_returns_none(self, template_dir, tmp_path):
        csv_path = str(tmp_path / "weird.csv")
        with open(csv_path, "w") as f:
            f.write("Col1,Col2,Col3\n1,2,3\n")
        parser = CSVBankParser(template_dir)
        detected = parser.detect_bank(csv_path)
        assert detected is None

    def test_empty_csv_returns_empty(self, template_dir, tmp_path):
        csv_path = str(tmp_path / "empty.csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write("Date,Description,Amount,Balance\n")
        parser = CSVBankParser(template_dir)
        result = parser.parse(csv_path, bank="FNB")
        assert len(result.transactions) == 0
