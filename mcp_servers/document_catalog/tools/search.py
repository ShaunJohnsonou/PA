"""``search_documents`` MCP tool handler.

The primary RAG retrieval tool. Translates MCP arguments into a
hybrid search query, enriches results with catalog metadata, and
returns structured output.
"""
from __future__ import annotations

import logging

from ..catalog_db import CatalogDB
from ..search.hybrid import hybrid_search
from ..search.embeddings import EmbeddingService
from ..search.faiss_index import FaissIndexManager
from ..search.fts_search import FTS5Search

logger = logging.getLogger(__name__)

_VALID_MODES = {"keyword", "semantic", "hybrid"}


async def handle_search_documents(
    query: str,
    mode: str = "hybrid",
    document_type: str | None = None,
    document_ids: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    top_k: int = 10,
    include_text: bool = True,
    min_score: float = 0.0,
    *,
    catalog: CatalogDB,
    faiss_index: FaissIndexManager,
    fts_search: FTS5Search,
    embedding_service: EmbeddingService,
) -> dict:
    """Search documents via MCP.

    Flow:
        1. Validate inputs
        2. Resolve document_ids from filters (date, type) via catalog
        3. Execute hybrid search
        4. Enrich results with catalog metadata
        5. Filter by min_score
        6. Return structured response
    """
    # ── 1. Validate inputs ───────────────────────────────────────
    logger.info("Executing search query: '%s' (mode=%s, top_k=%d)", query, mode, top_k)
    if not query or not query.strip():
        logger.error("Search query was empty")
        return _error("empty_query", "Query must not be empty")

    if mode not in _VALID_MODES:
        logger.error("Invalid search mode provided: %s", mode)
        return _error("invalid_mode", f"Mode must be one of: {_VALID_MODES}")

    top_k = max(1, min(top_k, 50))
    min_score = max(0.0, min(min_score, 1.0))
    logger.debug("Parameters clamped: top_k=%d, min_score=%.4f", top_k, min_score)

    # ── 2. Resolve document filters ──────────────────────────────
    filter_ids = list(document_ids) if document_ids else None

    # If date or type filters are specified, resolve matching doc IDs
    if document_type or date_from or date_to:
        logger.debug("Resolving document filters: type=%s, from=%s, to=%s", document_type, date_from, date_to)
        docs, _ = catalog.list_documents(
            document_type=document_type,
            date_from=date_from,
            date_to=date_to,
            limit=100,
        )
        resolved_ids = [d.document_id for d in docs]
        logger.debug("Catalog filter resolved to %d matching documents", len(resolved_ids))
        if filter_ids:
            # Intersect with explicitly provided IDs
            filter_ids = [i for i in filter_ids if i in resolved_ids]
            logger.debug("Intersected with provided document_ids, final filter_ids size=%d", len(filter_ids))
        else:
            filter_ids = resolved_ids

        if not filter_ids:
            logger.info("No documents matched the specified pre-filters (type/date)")
            return {
                "results": [],
                "query_mode": mode,
                "total_candidates": 0,
                "search_duration_ms": 0,
                "message": "No documents match the specified filters",
            }

    # ── 3. Execute hybrid search ─────────────────────────────────
    logger.debug("Calling hybrid_search backend with mode=%s and %s filters", mode, "no" if filter_ids is None else len(filter_ids))
    try:
        result = await hybrid_search(
            query=query,
            mode=mode,
            top_k=top_k,
            faiss_index=faiss_index,
            fts_search=fts_search,
            embedding_service=embedding_service,
            filter_document_ids=filter_ids,
        )
        logger.info("Search backend returned %d results in %dms (mode: %s)", len(result.results), result.search_duration_ms, result.query_mode)
    except Exception as exc:
        logger.exception("Critical failure during hybrid search backend execution: %s", exc)
        return _error("search_failed", f"Search backend failed: {exc}")

    # ── 4. Enrich with catalog metadata ──────────────────────────
    # Reason: cache doc lookups to avoid redundant DB queries
    doc_cache: dict[str, dict] = {}
    enriched_results = []

    for hit in result.results:
        if hit.score < min_score:
            logger.debug("Hit %s dropped due to min_score (%.4f < %.4f)", hit.chunk_id, hit.score, min_score)
            continue

        # Look up document metadata
        if hit.document_id not in doc_cache:
            doc = catalog.get_by_id(hit.document_id)
            if doc:
                doc_cache[hit.document_id] = {
                    "original_filename": doc.original_filename,
                    "document_type": doc.document_type,
                }
            else:
                logger.warning("Document %s missing from catalog but returned in search hits", hit.document_id)
                doc_cache[hit.document_id] = {
                    "original_filename": "unknown",
                    "document_type": "unknown",
                }

        meta = doc_cache[hit.document_id]

        entry = {
            "chunk_id": hit.chunk_id,
            "document_id": hit.document_id,
            "original_filename": meta["original_filename"],
            "document_type": meta["document_type"],
            "page_number": hit.page_number,
            "section_title": hit.section_title,
            "score": round(hit.score, 4),
            "source": hit.source,
        }

        if include_text:
            entry["text"] = hit.text

        enriched_results.append(entry)

    logger.debug("Enrichment complete. Returning %d final results", len(enriched_results))
    return {
        "results": enriched_results,
        "query_mode": result.query_mode,
        "total_candidates": result.total_candidates,
        "search_duration_ms": result.search_duration_ms,
    }


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Search error [%s]: %s", code, message)
    return {"error": code, "message": message}
