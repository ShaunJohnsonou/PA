"""Unit tests for the FAISS index manager."""
from __future__ import annotations

import os

import numpy as np
import pytest

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False

pytestmark = pytest.mark.skipif(not HAS_FAISS, reason="faiss-cpu not installed")

from mcp_servers.document_catalog.search.faiss_index import FaissIndexManager

DIM = 8  # Use small dimension for fast tests


@pytest.fixture
def index_dir(tmp_path):
    return str(tmp_path / "indexes")


@pytest.fixture
def faiss_mgr(index_dir):
    mgr = FaissIndexManager(index_dir, dimension=DIM)
    mgr.load_or_create()
    yield mgr
    mgr.close()


def _random_vectors(n: int) -> np.ndarray:
    """Generate random L2-normalised vectors."""
    vecs = np.random.randn(n, DIM).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / norms


class TestFaissIndexManager:
    def test_add_and_search(self, faiss_mgr):
        vecs = _random_vectors(10)
        chunk_ids = [f"chunk-{i}" for i in range(10)]
        doc_ids = ["doc-1"] * 10

        faiss_mgr.add_vectors(chunk_ids, doc_ids, vecs)
        assert faiss_mgr.size == 10

        # Search with the first vector — should match itself
        results = faiss_mgr.search(vecs[0], top_k=3)
        assert len(results) >= 1
        assert results[0]["chunk_id"] == "chunk-0"
        assert results[0]["score"] > 0.9  # Near-perfect match

    def test_empty_index_search(self, faiss_mgr):
        query = np.random.randn(DIM).astype(np.float32)
        results = faiss_mgr.search(query, top_k=5)
        assert results == []

    def test_persistence(self, index_dir):
        """Add vectors, save, reload — results should match."""
        mgr1 = FaissIndexManager(index_dir, dimension=DIM)
        mgr1.load_or_create()

        vecs = _random_vectors(5)
        chunk_ids = [f"chunk-{i}" for i in range(5)]
        doc_ids = ["doc-1"] * 5
        mgr1.add_vectors(chunk_ids, doc_ids, vecs)
        mgr1.close()

        # Reload
        mgr2 = FaissIndexManager(index_dir, dimension=DIM)
        mgr2.load_or_create()

        assert mgr2.size == 5
        results = mgr2.search(vecs[0], top_k=1)
        assert results[0]["chunk_id"] == "chunk-0"
        mgr2.close()

    def test_remove_document(self, faiss_mgr):
        vecs = _random_vectors(6)
        chunk_ids = [f"chunk-{i}" for i in range(6)]
        doc_ids = ["doc-1"] * 3 + ["doc-2"] * 3

        faiss_mgr.add_vectors(chunk_ids, doc_ids, vecs)

        removed = faiss_mgr.remove_document("doc-1")
        assert removed == 3

        # Search — should only return doc-2 results
        results = faiss_mgr.search(vecs[0], top_k=10)
        for r in results:
            assert r["document_id"] == "doc-2"

    def test_filter_by_document(self, faiss_mgr):
        vecs = _random_vectors(6)
        chunk_ids = [f"chunk-{i}" for i in range(6)]
        doc_ids = ["doc-1"] * 3 + ["doc-2"] * 3

        faiss_mgr.add_vectors(chunk_ids, doc_ids, vecs)

        results = faiss_mgr.search(
            vecs[0], top_k=10, filter_document_ids=["doc-2"]
        )
        for r in results:
            assert r["document_id"] == "doc-2"
