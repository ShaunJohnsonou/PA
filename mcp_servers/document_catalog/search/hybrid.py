"""Hybrid retriever — combines FAISS and FTS5 via Reciprocal Rank Fusion.

Provides three search modes: keyword (FTS5 only), semantic (FAISS only),
and hybrid (both merged via RRF).
"""
from __future__ import annotations

import logging
import time

from . import FTSHit, HybridHit, HybridResult
from .embeddings import EmbeddingService
from .faiss_index import FaissIndexManager
from .fts_search import FTS5Search

logger = logging.getLogger(__name__)

# Standard RRF constant — balances contribution from each ranking list
RRF_K = 60


async def hybrid_search(
    query: str,
    mode: str = "hybrid",
    top_k: int = 10,
    *,
    faiss_index: FaissIndexManager,
    fts_search: FTS5Search,
    embedding_service: EmbeddingService,
    filter_document_ids: list[str] | None = None,
    filter_document_type: str | None = None,
) -> HybridResult:
    """Execute hybrid search combining FAISS and FTS5.

    Modes:
        - "keyword": FTS5 only — no embedding API call
        - "semantic": FAISS only — requires embedding API
        - "hybrid": both, merged via Reciprocal Rank Fusion

    Args:
        query: Search query text.
        mode: One of "keyword", "semantic", "hybrid".
        top_k: Number of results to return.
        faiss_index: FAISS index manager instance.
        fts_search: FTS5 search instance.
        embedding_service: Azure OpenAI embedding service.
        filter_document_ids: Restrict to these document UUIDs.
        filter_document_type: Restrict to this document type.

    Returns:
        HybridResult with merged, ranked results.
    """
    t0 = time.monotonic()
    fts_hits: list[FTSHit] = []
    faiss_hits: list[dict] = []

    # Reason: request more candidates from each engine to ensure
    # sufficient coverage after merging and deduplication
    fetch_k = top_k * 3

    # ── Keyword search (FTS5) ────────────────────────────────────
    if mode in ("keyword", "hybrid"):
        fts_hits = fts_search.search(
            query,
            top_k=fetch_k,
            filter_document_ids=filter_document_ids,
            filter_document_type=filter_document_type,
        )

    # ── Semantic search (FAISS) ──────────────────────────────────
    if mode in ("semantic", "hybrid"):
        if embedding_service.available:
            query_vec = await embedding_service.embed_query(query)
            if query_vec is not None:
                faiss_hits = faiss_index.search(
                    query_vec,
                    top_k=fetch_k,
                    filter_document_ids=filter_document_ids,
                )
        else:
            logger.info("Semantic search skipped — embedding service unavailable")

    # ── Merge results ────────────────────────────────────────────
    if mode == "keyword":
        merged = _fts_only(fts_hits)
    elif mode == "semantic":
        merged = _faiss_only(faiss_hits, fts_search)
    else:
        merged = _reciprocal_rank_fusion(fts_hits, faiss_hits, fts_search)

    # Sort by score descending and truncate
    merged.sort(key=lambda h: h.score, reverse=True)
    total_candidates = len(merged)
    merged = merged[:top_k]

    duration_ms = int((time.monotonic() - t0) * 1000)

    logger.info(
        "Hybrid search (mode=%s): query='%s', %d candidates, %d returned, %dms",
        mode, query[:50], total_candidates, len(merged), duration_ms,
    )

    return HybridResult(
        results=merged,
        query_mode=mode,
        total_candidates=total_candidates,
        search_duration_ms=duration_ms,
    )


# ── Merge strategies ────────────────────────────────────────────


def _fts_only(fts_hits: list[FTSHit]) -> list[HybridHit]:
    """Convert FTS5 hits to HybridHits."""
    return [
        HybridHit(
            chunk_id=h.chunk_id,
            document_id=h.document_id,
            text=h.text,
            section_title=h.section_title,
            page_number=h.page_number,
            score=h.rank,
            source="keyword",
        )
        for h in fts_hits
    ]


def _faiss_only(
    faiss_hits: list[dict], fts_search: FTS5Search
) -> list[HybridHit]:
    """Convert FAISS hits to HybridHits, enriching with chunk text."""
    results = []
    for hit in faiss_hits:
        chunk = fts_search.get_chunk(hit["chunk_id"])
        results.append(HybridHit(
            chunk_id=hit["chunk_id"],
            document_id=hit["document_id"],
            text=chunk.text if chunk else "",
            section_title=chunk.section_title if chunk else None,
            page_number=chunk.page_number if chunk else None,
            score=hit["score"],
            source="semantic",
        ))
    return results


def _reciprocal_rank_fusion(
    fts_hits: list[FTSHit],
    faiss_hits: list[dict],
    fts_search: FTS5Search,
) -> list[HybridHit]:
    """Merge FTS5 and FAISS results using Reciprocal Rank Fusion.

    RRF formula: score(chunk) = sum(1 / (RRF_K + rank_in_list))
    where rank is 1-indexed.
    """
    # Track scores and metadata by chunk_id
    scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    chunk_data: dict[str, dict] = {}

    # Process FTS5 results
    for rank, hit in enumerate(fts_hits, start=1):
        cid = hit.chunk_id
        rrf_score = 1.0 / (RRF_K + rank)
        scores[cid] = scores.get(cid, 0.0) + rrf_score
        sources.setdefault(cid, set()).add("keyword")
        chunk_data[cid] = {
            "document_id": hit.document_id,
            "text": hit.text,
            "section_title": hit.section_title,
            "page_number": hit.page_number,
        }

    # Process FAISS results
    for rank, hit in enumerate(faiss_hits, start=1):
        cid = hit["chunk_id"]
        rrf_score = 1.0 / (RRF_K + rank)
        scores[cid] = scores.get(cid, 0.0) + rrf_score
        sources.setdefault(cid, set()).add("semantic")

        if cid not in chunk_data:
            # Enrich from FTS5 chunk store
            chunk = fts_search.get_chunk(cid)
            chunk_data[cid] = {
                "document_id": hit["document_id"],
                "text": chunk.text if chunk else "",
                "section_title": chunk.section_title if chunk else None,
                "page_number": chunk.page_number if chunk else None,
            }

    # Build merged results
    results = []
    for cid, score in scores.items():
        data = chunk_data.get(cid, {})
        src = sources.get(cid, set())
        source_tag = "both" if len(src) > 1 else next(iter(src))

        results.append(HybridHit(
            chunk_id=cid,
            document_id=data.get("document_id", ""),
            text=data.get("text", ""),
            section_title=data.get("section_title"),
            page_number=data.get("page_number"),
            score=score,
            source=source_tag,
        ))

    return results
