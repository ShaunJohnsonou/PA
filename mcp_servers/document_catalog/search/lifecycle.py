"""Index lifecycle manager.

Coordinates chunking, embedding, and indexing across FAISS and FTS5.
Handles document additions, removals, re-extractions, and full rebuilds.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import numpy as np

from . import ChunkRecord
from .chunker import chunk_document
from .embeddings import EmbeddingService
from .faiss_index import FaissIndexManager
from .fts_search import FTS5Search

logger = logging.getLogger(__name__)


@dataclass
class IndexStats:
    """Statistics from an indexing operation."""

    chunks_indexed: int = 0
    vectors_added: int = 0
    vectors_removed: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


class IndexLifecycle:
    """Coordinates index updates across FAISS and FTS5."""

    def __init__(
        self,
        faiss_mgr: FaissIndexManager,
        fts: FTS5Search,
        embeddings: EmbeddingService,
        vault_root: str,
    ):
        self._faiss = faiss_mgr
        self._fts = fts
        self._embeddings = embeddings
        self._vault_root = vault_root

    async def index_document(
        self, document_id: str, sha256_hash: str
    ) -> IndexStats:
        """Index a document's extracted content into both FAISS and FTS5.

        Flow:
            1. Load document.md from extracted/<sha256>/
            2. Load pages.json if available
            3. Chunk the document
            4. Insert chunks into FTS5 (triggers auto-populate)
            5. Generate embeddings via Azure OpenAI
            6. Add vectors to FAISS
            7. Save FAISS index to disk

        Returns IndexStats with counts and timing.
        """
        t0 = time.monotonic()
        stats = IndexStats()

        # Load extracted content
        extraction_dir = os.path.join(self._vault_root, "extracted", sha256_hash)
        md_path = os.path.join(extraction_dir, "document.md")

        if not os.path.isfile(md_path):
            stats.errors.append(f"document.md not found in {extraction_dir}")
            return stats

        with open(md_path, "r", encoding="utf-8") as f:
            markdown = f.read()

        # Load pages if available
        pages = None
        pages_path = os.path.join(extraction_dir, "pages.json")
        if os.path.isfile(pages_path):
            try:
                with open(pages_path, "r", encoding="utf-8") as f:
                    pages = json.load(f)
            except (json.JSONDecodeError, KeyError):
                pass

        # Chunk the document
        chunks = chunk_document(document_id, markdown, pages)
        if not chunks:
            stats.errors.append("Document produced zero chunks")
            return stats

        # Index into FTS5
        self._fts.index_chunks(chunks)
        stats.chunks_indexed = len(chunks)

        # Generate embeddings and add to FAISS
        if self._embeddings.available:
            vectors = await self._embeddings.embed_chunks(chunks)
            if vectors is not None and len(vectors) == len(chunks):
                vectors_array = np.stack(vectors).astype(np.float32)
                chunk_ids = [c.chunk_id for c in chunks]
                doc_ids = [c.document_id for c in chunks]
                self._faiss.add_vectors(chunk_ids, doc_ids, vectors_array)
                self._faiss.save()
                stats.vectors_added = len(vectors)
            else:
                stats.errors.append("Embedding generation failed or returned wrong count")
        else:
            stats.errors.append("Embedding service unavailable — FTS5 only")

        stats.duration_seconds = time.monotonic() - t0

        logger.info(
            "Indexed document %s: %d chunks, %d vectors in %.1fs",
            document_id[:8], stats.chunks_indexed, stats.vectors_added,
            stats.duration_seconds,
        )

        return stats

    async def reindex_document(
        self, document_id: str, sha256_hash: str
    ) -> IndexStats:
        """Remove old index entries and re-index with new chunks.

        Used after re-extraction (force=True).
        """
        # Remove old entries
        removed_chunks = self._fts.remove_document_chunks(document_id)
        removed_vectors = self._faiss.remove_document(document_id)

        # Re-index
        stats = await self.index_document(document_id, sha256_hash)
        stats.vectors_removed = removed_vectors

        return stats

    def remove_document(self, document_id: str) -> IndexStats:
        """Remove a document from all indexes."""
        t0 = time.monotonic()
        stats = IndexStats()

        stats.vectors_removed = self._faiss.remove_document(document_id)
        removed_chunks = self._fts.remove_document_chunks(document_id)
        stats.chunks_indexed = -removed_chunks  # negative = removed

        stats.duration_seconds = time.monotonic() - t0
        logger.info(
            "Removed document %s from indexes: %d chunks, %d vectors",
            document_id[:8], removed_chunks, stats.vectors_removed,
        )

        return stats
