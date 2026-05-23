"""Search subsystem — shared data classes.

Provides the common types used by the chunker, embedding service,
FAISS index, FTS5 search, and hybrid retriever.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChunkRecord:
    """A single text chunk from a document, ready for embedding and indexing."""

    chunk_id: str               # UUID v5 (deterministic from document_id:chunk_index)
    document_id: str
    chunk_index: int            # 0-indexed position within the document
    page_number: int | None
    section_title: str | None
    text: str
    char_count: int
    token_count: int            # Approximate
    bbox: str | None = None     # JSON bounding box if available
    embedding_model: str | None = None
    embedding_dim: int | None = None
    created_at: str = ""


@dataclass
class SearchHit:
    """A single hit from FAISS semantic search."""

    chunk_id: str
    document_id: str
    score: float                # Cosine similarity (0.0 to 1.0)
    faiss_id: int


@dataclass
class FTSHit:
    """A single hit from FTS5 keyword search."""

    chunk_id: str
    document_id: str
    text: str
    section_title: str | None
    page_number: int | None
    rank: float                 # BM25 score (negated so higher = better)


@dataclass
class HybridHit:
    """A merged result from hybrid search."""

    chunk_id: str
    document_id: str
    text: str
    section_title: str | None
    page_number: int | None
    score: float
    source: str                 # "keyword", "semantic", or "both"


@dataclass
class HybridResult:
    """Full response from a hybrid search query."""

    results: list[HybridHit]
    query_mode: str
    total_candidates: int       # Total before top_k truncation
    search_duration_ms: int
