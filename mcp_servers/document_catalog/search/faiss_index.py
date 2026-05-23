"""FAISS index manager for dense vector search.

Manages the FAISS index and a companion SQLite database that maps
FAISS internal vector IDs to chunk/document metadata.
"""
from __future__ import annotations

import logging
import os
import sqlite3

import numpy as np

logger = logging.getLogger(__name__)


class FaissIndexManager:
    """FAISS index with SQLite vector-to-chunk mapping.

    Uses IndexFlatIP (inner product) on L2-normalised vectors,
    which is equivalent to cosine similarity. Optimal for
    collections under 100K vectors with perfect recall.
    """

    def __init__(self, index_dir: str, dimension: int = 3072):
        """
        Args:
            index_dir: Path to the indexes/ directory in the vault.
            dimension: Vector dimensionality (must match embedding model).
        """
        self._index_dir = index_dir
        self._dimension = dimension
        self._index_path = os.path.join(index_dir, "faiss.index")
        self._map_path = os.path.join(index_dir, "faiss_map.sqlite")
        self._index = None
        self._map_conn: sqlite3.Connection | None = None
        self._next_id = 0

    def load_or_create(self) -> None:
        """Load existing index and mapping, or create new ones."""
        import faiss

        os.makedirs(self._index_dir, exist_ok=True)

        # Load or create FAISS index
        if os.path.isfile(self._index_path):
            self._index = faiss.read_index(self._index_path)
            logger.info(
                "Loaded FAISS index: %d vectors, dim=%d",
                self._index.ntotal, self._dimension,
            )
        else:
            self._index = faiss.IndexFlatIP(self._dimension)
            logger.info("Created new FAISS IndexFlatIP (dim=%d)", self._dimension)

        # Load or create mapping database
        self._map_conn = sqlite3.connect(self._map_path, timeout=10.0)
        self._map_conn.execute("PRAGMA journal_mode=WAL")
        self._map_conn.execute("""
            CREATE TABLE IF NOT EXISTS vector_map (
                faiss_id    INTEGER PRIMARY KEY,
                chunk_id    TEXT NOT NULL,
                document_id TEXT NOT NULL,
                deleted     BOOLEAN DEFAULT FALSE
            )
        """)
        self._map_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_chunk ON vector_map(chunk_id)"
        )
        self._map_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_vm_document ON vector_map(document_id)"
        )
        self._map_conn.commit()

        # Determine next available ID
        row = self._map_conn.execute(
            "SELECT MAX(faiss_id) FROM vector_map"
        ).fetchone()
        self._next_id = (row[0] or -1) + 1

    def add_vectors(
        self,
        chunk_ids: list[str],
        document_ids: list[str],
        vectors: np.ndarray,
    ) -> None:
        """Add vectors to the index and mapping.

        Args:
            chunk_ids: List of chunk UUIDs.
            document_ids: Corresponding document UUIDs.
            vectors: numpy array of shape (N, dimension), dtype float32.
        """
        assert self._index is not None, "Index not loaded"
        assert self._map_conn is not None, "Mapping DB not loaded"
        assert vectors.shape[1] == self._dimension, (
            f"Vector dim {vectors.shape[1]} != index dim {self._dimension}"
        )

        n = vectors.shape[0]
        assert len(chunk_ids) == n and len(document_ids) == n

        # Add to FAISS
        self._index.add(vectors)

        # Add to mapping
        rows = [
            (self._next_id + i, chunk_ids[i], document_ids[i], False)
            for i in range(n)
        ]
        self._map_conn.executemany(
            "INSERT INTO vector_map (faiss_id, chunk_id, document_id, deleted) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        self._map_conn.commit()
        self._next_id += n

        logger.info("Added %d vectors to FAISS index (total: %d)", n, self._index.ntotal)

    def remove_document(self, document_id: str) -> int:
        """Soft-delete all vectors belonging to a document.

        Reason: FAISS IndexFlatIP doesn't support removal. We mark
        vectors as deleted in the mapping DB and exclude them from
        search results. The index is compacted during rebuild().

        Returns:
            Number of vectors marked as deleted.
        """
        assert self._map_conn is not None

        cur = self._map_conn.execute(
            "UPDATE vector_map SET deleted = TRUE WHERE document_id = ? AND deleted = FALSE",
            (document_id,),
        )
        self._map_conn.commit()
        count = cur.rowcount
        logger.info("Soft-deleted %d vectors for document %s", count, document_id[:8])
        return count

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        filter_document_ids: list[str] | None = None,
    ) -> list[dict]:
        """Search for nearest neighbours.

        Args:
            query_vector: Shape (dimension,), dtype float32.
            top_k: Number of results to return.
            filter_document_ids: If set, only return hits from these documents.

        Returns:
            List of dicts with chunk_id, document_id, score, faiss_id.
        """
        assert self._index is not None
        assert self._map_conn is not None

        if self._index.ntotal == 0:
            return []

        # Reason: request more results than needed to account for
        # deleted vectors and document filtering
        search_k = min(top_k * 3, self._index.ntotal)
        query = query_vector.reshape(1, -1).astype(np.float32)

        scores, ids = self._index.search(query, search_k)

        # Build set of valid IDs (not deleted, matching filter)
        results = []
        for score, faiss_id in zip(scores[0], ids[0]):
            if faiss_id == -1:
                continue

            row = self._map_conn.execute(
                "SELECT chunk_id, document_id, deleted FROM vector_map WHERE faiss_id = ?",
                (int(faiss_id),),
            ).fetchone()

            if row is None or row[2]:  # deleted
                continue

            chunk_id, doc_id = row[0], row[1]

            if filter_document_ids and doc_id not in filter_document_ids:
                continue

            results.append({
                "chunk_id": chunk_id,
                "document_id": doc_id,
                "score": float(score),
                "faiss_id": int(faiss_id),
            })

            if len(results) >= top_k:
                break

        return results

    def save(self) -> None:
        """Persist the FAISS index to disk."""
        if self._index is not None:
            import faiss
            faiss.write_index(self._index, self._index_path)
            logger.info("Saved FAISS index to %s (%d vectors)", self._index_path, self._index.ntotal)

    def close(self) -> None:
        """Save index and close mapping DB."""
        self.save()
        if self._map_conn:
            self._map_conn.close()
            self._map_conn = None

    @property
    def size(self) -> int:
        """Number of vectors currently in the index."""
        return self._index.ntotal if self._index else 0
