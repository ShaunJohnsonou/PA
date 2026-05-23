"""Unit tests for TR-1.4 — ingest_document tool handler."""
import asyncio
import json
import os

import pytest

from mcp_servers.document_catalog.catalog_db import CatalogDB
from mcp_servers.document_catalog.vault import VaultManager
from mcp_servers.document_catalog.tools.ingest import handle_ingest_document


@pytest.fixture
def vault(tmp_path):
    return VaultManager(str(tmp_path / "vault"))


@pytest.fixture
def catalog(tmp_path, vault):
    vault.ensure_dirs()
    return CatalogDB(vault.catalog_path)


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestIngestNewFile:
    def test_ingest_happy_path(self, vault, catalog, tmp_path):
        src = tmp_path / "statement.pdf"
        src.write_bytes(b"%PDF-1.4 fake pdf content")

        result = _run(handle_ingest_document(
            file_path=str(src), source="manual", vault=vault, catalog=catalog,
        ))

        assert "error" not in result
        assert result["is_duplicate"] is False
        assert result["status"] == "ingested"
        assert result["original_filename"] == "statement.pdf"
        assert result["mime_type"] is not None
        assert len(result["document_id"]) > 0
        assert len(result["sha256_hash"]) == 64

    def test_ingest_stores_file_in_vault(self, vault, catalog, tmp_path):
        src = tmp_path / "test.txt"
        src.write_bytes(b"hello world")

        result = _run(handle_ingest_document(
            file_path=str(src), vault=vault, catalog=catalog,
        ))

        sha = result["sha256_hash"]
        assert vault.original_exists(sha, ".txt")

    def test_ingest_creates_catalog_row(self, vault, catalog, tmp_path):
        src = tmp_path / "doc.csv"
        src.write_bytes(b"a,b,c\n1,2,3")

        result = _run(handle_ingest_document(
            file_path=str(src), vault=vault, catalog=catalog,
        ))

        doc = catalog.get_by_id(result["document_id"])
        assert doc is not None
        assert doc.sha256_hash == result["sha256_hash"]
        assert doc.original_filename == "doc.csv"


class TestIngestDuplicate:
    def test_duplicate_detected(self, vault, catalog, tmp_path):
        src = tmp_path / "statement.pdf"
        src.write_bytes(b"same content")

        r1 = _run(handle_ingest_document(
            file_path=str(src), vault=vault, catalog=catalog,
        ))
        r2 = _run(handle_ingest_document(
            file_path=str(src), vault=vault, catalog=catalog,
        ))

        assert r1["is_duplicate"] is False
        assert r2["is_duplicate"] is True
        assert r1["document_id"] == r2["document_id"]


class TestIngestTags:
    def test_tags_stored_as_json(self, vault, catalog, tmp_path):
        src = tmp_path / "tagged.pdf"
        src.write_bytes(b"content")

        result = _run(handle_ingest_document(
            file_path=str(src),
            tags=["finance", "2025"],
            vault=vault,
            catalog=catalog,
        ))

        doc = catalog.get_by_id(result["document_id"])
        assert json.loads(doc.tags) == ["finance", "2025"]


class TestIngestErrors:
    def test_missing_file(self, vault, catalog):
        result = _run(handle_ingest_document(
            file_path="/nonexistent/file.pdf", vault=vault, catalog=catalog,
        ))
        assert result["error"] == "file_not_found"

    def test_empty_path(self, vault, catalog):
        result = _run(handle_ingest_document(
            file_path="", vault=vault, catalog=catalog,
        ))
        assert result["error"] == "invalid_path"
