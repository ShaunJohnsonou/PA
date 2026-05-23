"""TR-1.5 — ``list_documents`` MCP tool handler.

Translates MCP tool arguments into a catalog query, validates and clamps
inputs, and formats the paginated response.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date

from ..catalog_db import CatalogDB

logger = logging.getLogger(__name__)


async def handle_list_documents(
    document_type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    limit: int = 20,
    offset: int = 0,
    sort_by: str = "upload_time",
    sort_order: str = "desc",
    *,
    catalog: CatalogDB,
) -> dict:
    """Query the document catalog with filters.

    Validates and clamps inputs before delegating to ``CatalogDB.list_documents``.

    Returns:
        Structured dict with ``documents``, ``total_count``, and ``has_more``.
    """
    # ── Input validation ─────────────────────────────────────────
    logger.debug(
        "list_documents called: type=%s, status=%s, date_from=%s, date_to=%s, search='%s', limit=%d, offset=%d",
        document_type, status, date_from, date_to, search, limit, offset
    )

    # Validate dates
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            logger.error("Invalid date_from provided: %s", date_from)
            return _error("invalid_date", f"date_from is not a valid ISO date: {date_from}")

    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            logger.error("Invalid date_to provided: %s", date_to)
            return _error("invalid_date", f"date_to is not a valid ISO date: {date_to}")

    # Validate sort_order
    if sort_order not in ("asc", "desc"):
        logger.debug("Invalid sort_order '%s' defaulting to 'desc'", sort_order)
        sort_order = "desc"

    # Clamp pagination
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    # Truncate search to prevent excessively long LIKE patterns
    if search:
        search = search.strip()[:200]
        if not search:
            search = None

    # ── Execute query ────────────────────────────────────────────

    try:
        logger.info("Executing list_documents catalog query (limit=%d, offset=%d)", limit, offset)
        rows, total = catalog.list_documents(
            document_type=document_type,
            status=status,
            date_from=date_from,
            date_to=date_to,
            search=search,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    except ValueError as exc:
        logger.error("Invalid sort field requested: %s", exc)
        return _error("invalid_sort", str(exc))
    except Exception as exc:
        logger.critical("Database query failed during list_documents: %s", exc)
        return _error("query_error", f"Failed to query catalog: {exc}")

    # ── Format response ─────────────────────────────────────────

    documents = [
        {
            "document_id": doc.document_id,
            "original_filename": doc.original_filename,
            "document_type": doc.document_type,
            "mime_type": doc.mime_type,
            "upload_time": doc.upload_time,
            "status": doc.status,
            "extraction_status": doc.extraction_status,
            "file_size_bytes": doc.file_size_bytes,
            "summary": doc.summary,
        }
        for doc in rows
    ]

    has_more = (offset + limit) < total

    logger.debug(
        "list_documents: returned %d of %d (offset=%d, limit=%d)",
        len(documents),
        total,
        offset,
        limit,
    )

    return {
        "documents": documents,
        "total_count": total,
        "has_more": has_more,
    }


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("List error [%s]: %s", code, message)
    return {"error": code, "message": message}
