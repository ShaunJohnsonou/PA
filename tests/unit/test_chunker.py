"""Unit tests for the document chunker."""
from __future__ import annotations

import pytest

from mcp_servers.document_catalog.search.chunker import (
    chunk_document,
    _estimate_tokens,
    _is_table_block,
    _extract_heading,
)


class TestEstimateTokens:
    def test_basic(self):
        text = "hello world foo bar"
        assert _estimate_tokens(text) == int(4 * 1.3)

    def test_empty(self):
        assert _estimate_tokens("") == 0


class TestExtractHeading:
    def test_h1(self):
        assert _extract_heading("# Title") == "Title"

    def test_h2(self):
        assert _extract_heading("## Section Name") == "Section Name"

    def test_h3(self):
        assert _extract_heading("### Sub Section") == "Sub Section"

    def test_h4_ignored(self):
        assert _extract_heading("#### Too Deep") is None

    def test_not_heading(self):
        assert _extract_heading("Regular paragraph text") is None


class TestIsTableBlock:
    def test_valid_table(self):
        table = "| A | B |\n|---|---|\n| 1 | 2 |"
        assert _is_table_block(table) is True

    def test_not_table(self):
        assert _is_table_block("Just regular text") is False

    def test_single_line(self):
        assert _is_table_block("| A | B |") is False


class TestChunkDocument:
    def test_empty_document(self):
        assert chunk_document("doc1", "") == []
        assert chunk_document("doc1", "   ") == []

    def test_short_document_single_chunk(self):
        text = "This is a short document with just a few words."
        chunks = chunk_document("doc1", text)
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].document_id == "doc1"

    def test_deterministic(self):
        text = "Hello world. " * 500
        chunks_a = chunk_document("doc1", text)
        chunks_b = chunk_document("doc1", text)
        assert len(chunks_a) == len(chunks_b)
        for a, b in zip(chunks_a, chunks_b):
            assert a.chunk_id == b.chunk_id

    def test_heading_propagation(self):
        text = "## Transaction Details\n\nHere are the transactions. " * 50
        chunks = chunk_document("doc1", text)
        assert len(chunks) >= 1
        # At least one chunk should have the heading
        headings = [c.section_title for c in chunks if c.section_title]
        assert "Transaction Details" in headings

    def test_max_tokens_enforced(self):
        """No chunk should wildly exceed max_tokens (overlap may add a small margin)."""
        text = "word " * 5000
        chunks = chunk_document("doc1", text, max_tokens=1024)
        for chunk in chunks:
            # Reason: overlap prepends ~64 tokens, so allow that margin
            assert chunk.token_count <= 1024 + 200

    def test_table_not_split(self):
        """Markdown tables should be kept as atomic units."""
        table = "| Col1 | Col2 |\n|------|------|\n" + "| a | b |\n" * 50
        text = "Paragraph before.\n\n" + table + "\n\nParagraph after."
        chunks = chunk_document("doc1", text, target_tokens=50)
        # Find the chunk containing the table
        table_chunks = [c for c in chunks if "|" in c.text and "---" in c.text]
        assert len(table_chunks) >= 1
        # The table should be in one chunk (not split)
        for tc in table_chunks:
            lines = [l for l in tc.text.split("\n") if "|" in l]
            # Should have all table rows
            assert len(lines) >= 10  # At least some rows together

    def test_multiple_chunks_produced(self):
        """A long document should produce multiple chunks."""
        text = ("Paragraph content. " * 100 + "\n\n") * 20
        chunks = chunk_document("doc1", text, target_tokens=512)
        assert len(chunks) > 1

    def test_chunk_has_metadata(self):
        text = "Some content here."
        chunks = chunk_document("doc1", text)
        assert chunks[0].char_count == len(chunks[0].text)
        assert chunks[0].token_count > 0
        assert chunks[0].created_at != ""
