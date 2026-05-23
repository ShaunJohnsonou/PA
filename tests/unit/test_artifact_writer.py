"""Unit tests for the artifact writer."""
from __future__ import annotations

import json
import os

import pytest

from mcp_servers.document_catalog.artifact_writer import (
    write_artifacts,
    write_failed_meta,
)
from mcp_servers.document_catalog.engines import ConversionResult, ExtractedTable, PageData


@pytest.fixture
def vault_dir(tmp_path):
    """Create a vault directory structure for testing."""
    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir()
    return str(tmp_path)


def _make_result(**overrides) -> ConversionResult:
    """Create a ConversionResult with sensible defaults."""
    defaults = dict(
        markdown="# Test Document\n\nHello world.",
        page_count=1,
        char_count=30,
        table_count=0,
        engine="test_engine",
        engine_version="1.0.0",
        duration_seconds=0.5,
    )
    defaults.update(overrides)
    return ConversionResult(**defaults)


class TestWriteArtifacts:
    """Tests for write_artifacts()."""

    def test_write_markdown_only(self, vault_dir):
        """With no pages/tables, should produce document.md + conversion_meta.json."""
        result = _make_result()
        written = write_artifacts(vault_dir, "abcdef1234567890" * 4, result)

        assert "document.md" in written
        assert "conversion_meta.json" in written
        assert "pages.json" not in written
        assert "tables.json" not in written

    def test_write_all_artifacts(self, vault_dir):
        """With pages and tables, all 4 files should be created."""
        result = _make_result(
            pages=[PageData(page_number=1, text="Page one", word_count=2)],
            tables=[ExtractedTable(
                page_number=1, table_index=0,
                headers=["A", "B"], rows=[["1", "2"]],
            )],
            table_count=1,
        )
        sha = "abcdef1234567890" * 4
        written = write_artifacts(vault_dir, sha, result)

        assert set(written) == {"document.md", "pages.json", "tables.json", "conversion_meta.json"}

        # Verify files exist on disk
        artifact_dir = os.path.join(vault_dir, "extracted", sha)
        for f in written:
            assert os.path.isfile(os.path.join(artifact_dir, f))

    def test_markdown_content_correct(self, vault_dir):
        """document.md should contain the exact markdown from the result."""
        md = "# Title\n\nSome content here."
        result = _make_result(markdown=md)
        sha = "abcdef1234567890" * 4
        write_artifacts(vault_dir, sha, result)

        md_path = os.path.join(vault_dir, "extracted", sha, "document.md")
        with open(md_path, "r", encoding="utf-8") as f:
            assert f.read() == md

    def test_conversion_meta_structure(self, vault_dir):
        """conversion_meta.json should contain engine info and stats."""
        result = _make_result(engine="docling", engine_version="2.5.0")
        sha = "abcdef1234567890" * 4
        write_artifacts(vault_dir, sha, result)

        meta_path = os.path.join(vault_dir, "extracted", sha, "conversion_meta.json")
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        assert meta["engine"] == "docling"
        assert meta["engine_version"] == "2.5.0"
        assert meta["status"] == "success"
        assert "extracted_at" in meta

    def test_re_extraction_replaces(self, vault_dir):
        """Second write should replace existing artifacts."""
        sha = "abcdef1234567890" * 4
        result_v1 = _make_result(markdown="# Version 1")
        write_artifacts(vault_dir, sha, result_v1)

        result_v2 = _make_result(markdown="# Version 2")
        write_artifacts(vault_dir, sha, result_v2)

        md_path = os.path.join(vault_dir, "extracted", sha, "document.md")
        with open(md_path, "r", encoding="utf-8") as f:
            assert f.read() == "# Version 2"

    def test_no_leftover_tmp_dir(self, vault_dir):
        """After successful write, no .tmp directory should remain."""
        sha = "abcdef1234567890" * 4
        write_artifacts(vault_dir, sha, _make_result())

        tmp_dir = os.path.join(vault_dir, "extracted", sha + ".tmp")
        bak_dir = os.path.join(vault_dir, "extracted", sha + ".bak")
        assert not os.path.exists(tmp_dir)
        assert not os.path.exists(bak_dir)


class TestWriteFailedMeta:
    """Tests for write_failed_meta()."""

    def test_failed_meta_written(self, vault_dir):
        """A failed extraction should still produce a conversion_meta.json."""
        sha = "deadbeef12345678" * 4
        write_failed_meta(vault_dir, sha, "docling", "PDF is corrupt")

        meta_path = os.path.join(vault_dir, "extracted", sha, "conversion_meta.json")
        assert os.path.isfile(meta_path)

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        assert meta["status"] == "failed"
        assert meta["error_message"] == "PDF is corrupt"
        assert meta["engine"] == "docling"
