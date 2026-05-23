"""``index_document`` MCP tool handler.

Chunks, embeds, and indexes an extracted document into both
FAISS and FTS5 for searchability.
"""
from __future__ import annotations

import logging

from ..catalog_db import CatalogDB
from ..search.lifecycle import IndexLifecycle
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_index_document(
    document_id: str,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
    lifecycle: IndexLifecycle,
) -> dict:
    """Index an extracted document for search.

    Prerequisite: document must have extraction_status == "extracted".

    Flow:
        1. Verify document exists and is extracted
        2. Call lifecycle.index_document() to chunk + embed + index
        3. Return stats

    Returns:
        Structured dict with indexing results.
    """
    # ── 1. Look up document ──────────────────────────────────────
    logger.info("Starting index_document tool for document_id=%s", document_id)
    doc = catalog.get_by_id(document_id)
    if doc is None:
        logger.error("Document '%s' not found in catalog", document_id)
        return _error("not_found", f"Document '{document_id}' not found")

    if doc.extraction_status != "extracted":
        logger.warning("Refusing to index document %s, extraction_status='%s'", document_id, doc.extraction_status)
        return _error(
            "not_extracted",
            f"Document must be extracted before indexing "
            f"(current status: {doc.extraction_status}). "
            "Use extract_document first."
        )

    # ── 2. Index the document ────────────────────────────────────
    try:
        # Reason: if the document was previously indexed, use reindex to
        # remove stale chunks/vectors before inserting new ones.
        if doc.indexing_status in ("indexed", "partial"):
            logger.info("Re-indexing document %s (previous status: %s)", document_id, doc.indexing_status)
            stats = await lifecycle.reindex_document(document_id, doc.sha256_hash)
        else:
            logger.debug("Calling IndexLifecycle.index_document for %s (sha256: %s)", document_id, doc.sha256_hash)
            stats = await lifecycle.index_document(document_id, doc.sha256_hash)
        logger.info("Indexing complete for %s. Chunks: %d, Vectors: %d", document_id, stats.chunks_indexed, stats.vectors_added)
    except Exception as exc:
        logger.exception("Critical indexing failure for %s: %s", document_id, exc)
        catalog.update_document(document_id, indexing_status="failed")
        return _error("indexing_failed", f"Indexing failed: {exc}")

    # ── 3. Update catalog status ─────────────────────────────────
    if stats.errors and stats.vectors_added == 0:
        # Reason: if no vectors were added (e.g. embedding API unavailable)
        # but chunks were indexed into FTS5, mark as partially indexed
        logger.warning("Indexing partially succeeded for %s, marking as partial. Errors: %s", document_id, stats.errors)
        catalog.update_document(document_id, indexing_status="partial")
    else:
        logger.debug("Marking document %s as fully indexed", document_id)
        catalog.update_document(document_id, indexing_status="indexed")

    # ── 4. Return results ────────────────────────────────────────
    result = {
        "document_id": document_id,
        "original_filename": doc.original_filename,
        "chunks_indexed": stats.chunks_indexed,
        "vectors_added": stats.vectors_added,
        "duration_seconds": round(stats.duration_seconds, 2),
        "message": f"Successfully indexed {doc.original_filename}",
    }

    if stats.errors:
        result["warnings"] = stats.errors

    return result


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Index error [%s]: %s", code, message)
    return {"error": code, "message": message}
