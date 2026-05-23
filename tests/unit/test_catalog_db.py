"""Unit tests for TR-1.3 — CatalogDB."""
import json
import os

import pytest

from mcp_servers.document_catalog.catalog_db import CatalogDB, DocumentRow, DuplicateHashError


@pytest.fixture
def db(tmp_path):
    """Provide a fresh CatalogDB in a temp directory."""
    db_path = str(tmp_path / "test_catalog.sqlite")
    catalog = CatalogDB(db_path)
    yield catalog
    catalog.close()


def _make_doc(
    doc_id: str = "doc-001",
    sha256: str = "a" * 64,
    filename: str = "test.pdf",
    **overrides,
) -> DocumentRow:
    """Helper to create a DocumentRow with defaults."""
    defaults = dict(
        document_id=doc_id,
        sha256_hash=sha256,
        original_filename=filename,
        canonical_path=f"originals/aa/aa/{sha256}.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        source="manual",
        status="ingested",
    )
    defaults.update(overrides)
    return DocumentRow(**defaults)


class TestSchemaCreation:
    def test_creates_tables(self, db):
        """Fresh DB has the documents table."""
        row = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        assert row is not None

    def test_schema_version_set(self, db):
        row = db._conn.execute("SELECT version FROM schema_version").fetchone()
        assert row[0] == CatalogDB.CURRENT_SCHEMA_VERSION


class TestInsertAndGet:
    def test_insert_and_get_by_hash(self, db):
        doc = _make_doc()
        db.insert_document(doc)

        found = db.get_by_hash(doc.sha256_hash)
        assert found is not None
        assert found.document_id == "doc-001"
        assert found.original_filename == "test.pdf"

    def test_insert_and_get_by_id(self, db):
        doc = _make_doc()
        db.insert_document(doc)

        found = db.get_by_id("doc-001")
        assert found is not None
        assert found.sha256_hash == "a" * 64

    def test_duplicate_hash_raises(self, db):
        doc1 = _make_doc(doc_id="doc-001", sha256="a" * 64)
        doc2 = _make_doc(doc_id="doc-002", sha256="a" * 64)

        db.insert_document(doc1)
        with pytest.raises(DuplicateHashError):
            db.insert_document(doc2)

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_by_hash("nonexistent") is None
        assert db.get_by_id("nonexistent") is None

    def test_timestamps_auto_set(self, db):
        doc = _make_doc()
        db.insert_document(doc)

        found = db.get_by_id("doc-001")
        assert found.created_at != ""
        assert found.updated_at != ""
        assert found.upload_time != ""


class TestUpdateDocument:
    def test_update_fields(self, db):
        doc = _make_doc()
        db.insert_document(doc)

        db.update_document("doc-001", status="processed", document_type="bank_statement")

        found = db.get_by_id("doc-001")
        assert found.status == "processed"
        assert found.document_type == "bank_statement"

    def test_update_sets_updated_at(self, db):
        doc = _make_doc()
        db.insert_document(doc)
        original_updated = db.get_by_id("doc-001").updated_at

        db.update_document("doc-001", status="processed")
        new_updated = db.get_by_id("doc-001").updated_at
        assert new_updated >= original_updated


class TestListDocuments:
    def _insert_batch(self, db, count=5):
        for i in range(count):
            db.insert_document(
                _make_doc(
                    doc_id=f"doc-{i:03d}",
                    sha256=f"{i:064d}",
                    filename=f"file_{i}.pdf",
                    document_type="bank_statement" if i % 2 == 0 else "invoice",
                    status="ingested",
                )
            )

    def test_list_all(self, db):
        self._insert_batch(db, 5)
        rows, total = db.list_documents()
        assert total == 5
        assert len(rows) == 5

    def test_filter_by_type(self, db):
        self._insert_batch(db, 5)
        rows, total = db.list_documents(document_type="bank_statement")
        assert total == 3  # indices 0, 2, 4

    def test_filter_by_status(self, db):
        self._insert_batch(db, 5)
        db.update_document("doc-000", status="processed")
        rows, total = db.list_documents(status="processed")
        assert total == 1

    def test_search_filename(self, db):
        self._insert_batch(db, 5)
        rows, total = db.list_documents(search="file_3")
        assert total == 1
        assert rows[0].original_filename == "file_3.pdf"

    def test_pagination(self, db):
        self._insert_batch(db, 10)
        rows, total = db.list_documents(limit=3, offset=0)
        assert len(rows) == 3
        assert total == 10

        rows2, _ = db.list_documents(limit=3, offset=3)
        assert len(rows2) == 3
        # No overlap
        ids1 = {r.document_id for r in rows}
        ids2 = {r.document_id for r in rows2}
        assert ids1.isdisjoint(ids2)

    def test_empty_result(self, db):
        rows, total = db.list_documents(document_type="nonexistent")
        assert rows == []
        assert total == 0

    def test_sort_injection_blocked(self, db):
        with pytest.raises(ValueError, match="Invalid sort_by"):
            db.list_documents(sort_by="1; DROP TABLE documents")

    def test_limit_clamping(self, db):
        self._insert_batch(db, 5)
        rows, _ = db.list_documents(limit=999)
        assert len(rows) == 5  # clamped to 100, but only 5 exist


class TestDeleteDocument:
    def test_delete_existing(self, db):
        db.insert_document(_make_doc())
        assert db.delete_document("doc-001") is True
        assert db.get_by_id("doc-001") is None

    def test_delete_nonexistent(self, db):
        assert db.delete_document("nonexistent") is False
