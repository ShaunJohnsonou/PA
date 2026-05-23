"""Unit tests for the extract_document tool handler."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_servers.document_catalog.catalog_db import CatalogDB, DocumentRow
from mcp_servers.document_catalog.engines import ConversionResult, PageData
from mcp_servers.document_catalog.tools.extract import handle_extract_document


@pytest.fixture
def vault_dir(tmp_path):
    """Create a vault with originals and extracted directories."""
    (tmp_path / "originals" / "ab" / "cd").mkdir(parents=True)
    (tmp_path / "extracted").mkdir()
    return tmp_path


@pytest.fixture
def catalog_db(vault_dir):
    """Create a real CatalogDB in the temp vault."""
    db = CatalogDB(str(vault_dir / "hermes_catalog.sqlite"))
    yield db
    db.close()


@pytest.fixture
def sample_pdf(vault_dir):
    """Create a fake PDF file in the vault for testing."""
    pdf_path = vault_dir / "originals" / "ab" / "cd" / "abcdef1234.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content")
    return pdf_path


@pytest.fixture
def sample_doc(catalog_db, sample_pdf):
    """Insert a sample document row and return it."""
    doc = DocumentRow(
        document_id="test-doc-001",
        sha256_hash="abcdef1234",
        original_filename="test.pdf",
        canonical_path="originals/ab/cd/abcdef1234.pdf",
        mime_type="application/pdf",
        file_size_bytes=100,
        document_type="general",
        source="manual",
        extraction_status="pending",
    )
    catalog_db.insert_document(doc)
    return doc


def _mock_conversion_result() -> ConversionResult:
    """Create a mock conversion result."""
    return ConversionResult(
        markdown="# Test\n\nExtracted content.",
        page_count=1,
        char_count=25,
        table_count=0,
        pages=[PageData(page_number=1, text="Extracted content.", word_count=2)],
        engine="docling",
        engine_version="2.5.0",
        duration_seconds=1.5,
    )


class TestExtractDocument:
    """Tests for handle_extract_document()."""

    @pytest.mark.asyncio
    async def test_extract_pdf_uses_docling(self, vault_dir, catalog_db, sample_doc):
        """PDFs should be routed to the Docling engine."""
        mock_docling = MagicMock()
        mock_docling.convert.return_value = _mock_conversion_result()
        mock_markitdown = MagicMock()

        from mcp_servers.document_catalog.vault import VaultManager
        vault = VaultManager(str(vault_dir))
        vault.ensure_dirs()

        result = await handle_extract_document(
            document_id="test-doc-001",
            vault=vault,
            catalog=catalog_db,
            docling_engine=mock_docling,
            markitdown_engine=mock_markitdown,
        )

        assert result["extraction_status"] == "extracted"
        assert result["engine"] == "docling"
        mock_docling.convert.assert_called_once()
        mock_markitdown.convert.assert_not_called()

    @pytest.mark.asyncio
    async def test_extract_writes_artifacts(self, vault_dir, catalog_db, sample_doc):
        """Extraction should write document.md and conversion_meta.json."""
        mock_docling = MagicMock()
        mock_docling.convert.return_value = _mock_conversion_result()
        mock_markitdown = MagicMock()

        from mcp_servers.document_catalog.vault import VaultManager
        vault = VaultManager(str(vault_dir))
        vault.ensure_dirs()

        result = await handle_extract_document(
            document_id="test-doc-001",
            vault=vault,
            catalog=catalog_db,
            docling_engine=mock_docling,
            markitdown_engine=mock_markitdown,
        )

        assert "document.md" in result["artifacts_written"]
        extraction_dir = os.path.join(str(vault_dir), "extracted", "abcdef1234")
        assert os.path.isfile(os.path.join(extraction_dir, "document.md"))

    @pytest.mark.asyncio
    async def test_extract_updates_catalog_status(self, vault_dir, catalog_db, sample_doc):
        """After extraction, catalog status should be 'extracted'."""
        mock_docling = MagicMock()
        mock_docling.convert.return_value = _mock_conversion_result()

        from mcp_servers.document_catalog.vault import VaultManager
        vault = VaultManager(str(vault_dir))
        vault.ensure_dirs()

        await handle_extract_document(
            document_id="test-doc-001",
            vault=vault,
            catalog=catalog_db,
            docling_engine=mock_docling,
            markitdown_engine=MagicMock(),
        )

        updated = catalog_db.get_by_id("test-doc-001")
        assert updated.extraction_status == "extracted"

    @pytest.mark.asyncio
    async def test_extract_idempotent(self, vault_dir, catalog_db, sample_doc):
        """Second extract with force=False should return cached result."""
        mock_docling = MagicMock()
        mock_docling.convert.return_value = _mock_conversion_result()

        from mcp_servers.document_catalog.vault import VaultManager
        vault = VaultManager(str(vault_dir))
        vault.ensure_dirs()

        # First extraction
        await handle_extract_document(
            document_id="test-doc-001",
            vault=vault,
            catalog=catalog_db,
            docling_engine=mock_docling,
            markitdown_engine=MagicMock(),
        )

        # Second extraction with force=False
        result = await handle_extract_document(
            document_id="test-doc-001",
            force=False,
            vault=vault,
            catalog=catalog_db,
            docling_engine=mock_docling,
            markitdown_engine=MagicMock(),
        )

        assert result["message"] == "Document already extracted. Use force=true to re-extract."
        # Docling should have been called only once (first extraction)
        assert mock_docling.convert.call_count == 1

    @pytest.mark.asyncio
    async def test_extract_not_found(self, vault_dir, catalog_db):
        """Extracting a non-existent document should return an error."""
        from mcp_servers.document_catalog.vault import VaultManager
        vault = VaultManager(str(vault_dir))

        result = await handle_extract_document(
            document_id="does-not-exist",
            vault=vault,
            catalog=catalog_db,
            docling_engine=MagicMock(),
            markitdown_engine=MagicMock(),
        )

        assert result["error"] == "not_found"

    @pytest.mark.asyncio
    async def test_docling_failure_falls_back_to_markitdown(
        self, vault_dir, catalog_db, sample_doc
    ):
        """If Docling fails, MarkItDown should be tried as fallback."""
        from mcp_servers.document_catalog.engines.docling_engine import ConversionError

        mock_docling = MagicMock()
        mock_docling.convert.side_effect = ConversionError("Docling crashed")

        fallback_result = _mock_conversion_result()
        fallback_result.engine = "markitdown"
        mock_markitdown = MagicMock()
        mock_markitdown.convert.return_value = fallback_result

        from mcp_servers.document_catalog.vault import VaultManager
        vault = VaultManager(str(vault_dir))
        vault.ensure_dirs()

        result = await handle_extract_document(
            document_id="test-doc-001",
            vault=vault,
            catalog=catalog_db,
            docling_engine=mock_docling,
            markitdown_engine=mock_markitdown,
        )

        assert result["extraction_status"] == "extracted"
        mock_markitdown.convert.assert_called_once()
