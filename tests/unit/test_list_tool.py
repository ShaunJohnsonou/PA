"""Unit tests for TR-1.5 — list_documents tool handler."""
import asyncio

import pytest

from mcp_servers.document_catalog.catalog_db import CatalogDB, DocumentRow
from mcp_servers.document_catalog.vault import VaultManager
from mcp_servers.document_catalog.tools.list_docs import handle_list_documents


@pytest.fixture
def vault(tmp_path):
    vm = VaultManager(str(tmp_path / "vault"))
    vm.ensure_dirs()
    return vm


@pytest.fixture
def catalog(vault):
    return CatalogDB(vault.catalog_path)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _seed(catalog, count=5):
    """Insert test documents into the catalog."""
    for i in range(count):
        catalog.insert_document(
            DocumentRow(
                document_id=f"doc-{i:03d}",
                sha256_hash=f"{i:064d}",
                original_filename=f"file_{i}.pdf",
                canonical_path=f"originals/{i:064d}.pdf",
                mime_type="application/pdf",
                file_size_bytes=1000 + i,
                document_type="bank_statement" if i % 2 == 0 else "invoice",
                source="manual",
            )
        )


class TestListAll:
    def test_returns_all(self, catalog):
        _seed(catalog, 5)
        result = _run(handle_list_documents(catalog=catalog))
        assert "error" not in result
        assert result["total_count"] == 5
        assert len(result["documents"]) == 5

    def test_empty_catalog(self, catalog):
        result = _run(handle_list_documents(catalog=catalog))
        assert result["total_count"] == 0
        assert result["documents"] == []
        assert result["has_more"] is False


class TestFilters:
    def test_filter_by_type(self, catalog):
        _seed(catalog, 6)
        result = _run(handle_list_documents(
            document_type="bank_statement", catalog=catalog,
        ))
        assert result["total_count"] == 3

    def test_filter_by_search(self, catalog):
        _seed(catalog, 5)
        result = _run(handle_list_documents(search="file_3", catalog=catalog))
        assert result["total_count"] == 1
        assert result["documents"][0]["original_filename"] == "file_3.pdf"

    def test_combined_filters(self, catalog):
        _seed(catalog, 10)
        result = _run(handle_list_documents(
            document_type="bank_statement",
            search="file_4",
            catalog=catalog,
        ))
        assert result["total_count"] == 1


class TestPagination:
    def test_has_more_flag(self, catalog):
        _seed(catalog, 10)
        result = _run(handle_list_documents(limit=3, offset=0, catalog=catalog))
        assert result["has_more"] is True
        assert len(result["documents"]) == 3

    def test_last_page(self, catalog):
        _seed(catalog, 5)
        result = _run(handle_list_documents(limit=20, offset=0, catalog=catalog))
        assert result["has_more"] is False

    def test_limit_clamped(self, catalog):
        _seed(catalog, 5)
        result = _run(handle_list_documents(limit=999, catalog=catalog))
        assert len(result["documents"]) == 5  # 5 exist, max 100


class TestValidation:
    def test_invalid_date_from(self, catalog):
        result = _run(handle_list_documents(date_from="not-a-date", catalog=catalog))
        assert result["error"] == "invalid_date"

    def test_invalid_sort_by(self, catalog):
        result = _run(handle_list_documents(
            sort_by="1; DROP TABLE", catalog=catalog,
        ))
        assert result["error"] == "invalid_sort"
