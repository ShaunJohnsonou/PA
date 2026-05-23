"""Unit tests for FTS5 keyword search."""
from __future__ import annotations

import os

import pytest

from mcp_servers.document_catalog.search import ChunkRecord
from mcp_servers.document_catalog.search.fts_search import FTS5Search


@pytest.fixture
def fts_db(tmp_path):
    """Create a FTS5Search with a temp database."""
    db_path = str(tmp_path / "test_catalog.sqlite")
    fts = FTS5Search(db_path)
    yield fts
    fts.close()


def _make_chunk(
    chunk_id: str,
    document_id: str = "doc-1",
    text: str = "test",
    section_title: str | None = None,
    page_number: int | None = 1,
    chunk_index: int = 0,
) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=chunk_id,
        document_id=document_id,
        chunk_index=chunk_index,
        page_number=page_number,
        section_title=section_title,
        text=text,
        char_count=len(text),
        token_count=int(len(text.split()) * 1.3),
        created_at="2026-01-01T00:00:00Z",
    )


class TestFTS5Search:
    def test_simple_search(self, fts_db):
        chunks = [
            _make_chunk("c1", text="The bank statement shows monthly fees"),
            _make_chunk("c2", text="Property transfer document details"),
        ]
        fts_db.index_chunks(chunks)

        results = fts_db.search("bank statement")
        assert len(results) >= 1
        assert results[0].chunk_id == "c1"

    def test_phrase_search(self, fts_db):
        chunks = [
            _make_chunk("c1", text="The monthly account fee is R50"),
            _make_chunk("c2", text="This fee is charged monthly on account"),
        ]
        fts_db.index_chunks(chunks)

        results = fts_db.search('"monthly account fee"')
        assert len(results) >= 1
        assert results[0].chunk_id == "c1"

    def test_no_results(self, fts_db):
        chunks = [_make_chunk("c1", text="Hello world")]
        fts_db.index_chunks(chunks)

        results = fts_db.search("xyznonexistent")
        assert len(results) == 0

    def test_filter_by_document(self, fts_db):
        chunks = [
            _make_chunk("c1", document_id="doc-1", text="property transfer details"),
            _make_chunk("c2", document_id="doc-2", text="property transfer ownership"),
        ]
        fts_db.index_chunks(chunks)

        results = fts_db.search("property", filter_document_ids=["doc-1"])
        assert len(results) == 1
        assert results[0].document_id == "doc-1"

    def test_remove_document(self, fts_db):
        chunks = [
            _make_chunk("c1", document_id="doc-1", text="some text"),
            _make_chunk("c2", document_id="doc-2", text="other text"),
        ]
        fts_db.index_chunks(chunks)

        removed = fts_db.remove_document_chunks("doc-1")
        assert removed == 1

        results = fts_db.search("text")
        assert len(results) == 1
        assert results[0].document_id == "doc-2"

    def test_bm25_ranking(self, fts_db):
        chunks = [
            _make_chunk("c1", text="bank"),
            _make_chunk("c2", text="bank bank bank fees bank statement"),
        ]
        fts_db.index_chunks(chunks)

        results = fts_db.search("bank")
        assert len(results) >= 2
        # c2 has more occurrences, should rank higher
        assert results[0].chunk_id == "c2"

    def test_get_chunk(self, fts_db):
        chunks = [_make_chunk("c1", text="Test content here")]
        fts_db.index_chunks(chunks)

        chunk = fts_db.get_chunk("c1")
        assert chunk is not None
        assert chunk.text == "Test content here"

    def test_get_chunk_not_found(self, fts_db):
        assert fts_db.get_chunk("nonexistent") is None
