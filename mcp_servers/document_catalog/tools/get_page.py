"""``get_document_page`` MCP tool handler.

Retrieves pre-extracted page content from the artifact directory.
Falls back to splitting the full Markdown if per-page data is unavailable.
"""
from __future__ import annotations

import json
import logging
import math
import os

from ..catalog_db import CatalogDB
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_get_document_page(
    document_id: str,
    page_number: int,
    include_tables: bool = True,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
) -> dict:
    """Retrieve extracted content for a specific page.

    Flow:
        1. Look up document in catalog
        2. Verify extraction_status == "extracted"
        3. Try loading pages.json for structured per-page data
        4. Fall back to splitting document.md by page markers
        5. Optionally include tables for this page from tables.json

    Args:
        document_id: UUID of the document.
        page_number: 1-indexed page number to retrieve.
        include_tables: If True, include tables from that page.
        vault: VaultManager instance.
        catalog: CatalogDB instance.

    Returns:
        Structured dict with page text, tables, and metadata.
    """
    # ── 1. Look up document ──────────────────────────────────────
    logger.debug("get_document_page requested for document_id=%s, page=%d", document_id, page_number)
    doc = catalog.get_by_id(document_id)
    if doc is None:
        logger.error("Document '%s' not found", document_id)
        return _error("not_found", f"Document '{document_id}' not found")

    # ── 2. Check extraction status ───────────────────────────────
    if doc.extraction_status != "extracted":
        logger.warning("Document %s is not extracted (status=%s)", document_id, doc.extraction_status)
        return _error(
            "not_extracted",
            f"Document has not been extracted yet (status: {doc.extraction_status}). "
            "Use extract_document first."
        )

    # ── 3. Resolve extraction directory ──────────────────────────
    extraction_dir = vault.get_extraction_dir(doc.sha256_hash)
    if not os.path.isdir(extraction_dir):
        logger.critical("Extraction directory missing for extracted document %s at %s", document_id, extraction_dir)
        return _error(
            "artifacts_missing",
            "Extraction directory not found despite extracted status"
        )

    # ── 4. Try structured per-page data ──────────────────────────
    pages_path = os.path.join(extraction_dir, "pages.json")
    page_text: str | None = None
    total_pages: int = 0
    word_count: int = 0
    has_tables_on_page: bool = False
    has_images_on_page: bool = False

    if os.path.isfile(pages_path):
        try:
            with open(pages_path, "r", encoding="utf-8") as f:
                pages_data = json.load(f)
            total_pages = len(pages_data)

            if page_number < 1 or page_number > total_pages:
                return _error(
                    "page_out_of_range",
                    f"Page {page_number} requested but document has {total_pages} pages"
                )

            # Reason: pages_data is a list of dicts sorted by page_number.
            # Find the matching page (they're 1-indexed in our data).
            page_entry = None
            for p in pages_data:
                if p.get("page_number") == page_number:
                    page_entry = p
                    break

            if page_entry is None:
                # Reason: fallback to index-based access if page_number
                # keys are not present
                idx = page_number - 1
                if 0 <= idx < len(pages_data):
                    page_entry = pages_data[idx]

            if page_entry:
                page_text = page_entry.get("text", "")
                word_count = page_entry.get("word_count", len(page_text.split()))
                has_tables_on_page = page_entry.get("has_tables", False)
                has_images_on_page = page_entry.get("has_images", False)

        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to parse pages.json: %s", exc)
            # Fall through to markdown splitting

    # ── 5. Fallback: split document.md ───────────────────────────
    if page_text is None:
        md_path = os.path.join(extraction_dir, "document.md")
        if not os.path.isfile(md_path):
            return _error("artifacts_missing", "document.md not found")

        with open(md_path, "r", encoding="utf-8") as f:
            full_markdown = f.read()

        # Reason: split by form feed (\f) or horizontal rule (---) markers
        pages = _split_by_page_markers(full_markdown)
        total_pages = len(pages)

        if page_number < 1 or page_number > total_pages:
            return _error(
                "page_out_of_range",
                f"Page {page_number} requested but document has {total_pages} pages "
                "(estimated from content)"
            )

        page_text = pages[page_number - 1]
        word_count = len(page_text.split())

    # ── 6. Include tables if requested ───────────────────────────
    page_tables = []
    if include_tables:
        tables_path = os.path.join(extraction_dir, "tables.json")
        if os.path.isfile(tables_path):
            try:
                with open(tables_path, "r", encoding="utf-8") as f:
                    all_tables = json.load(f)
                page_tables = [
                    t for t in all_tables
                    if t.get("page_number") == page_number
                ]
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to parse tables.json: %s", exc)

    return {
        "document_id": document_id,
        "page_number": page_number,
        "total_pages": total_pages,
        "text": page_text,
        "word_count": word_count,
        "has_tables": has_tables_on_page or len(page_tables) > 0,
        "has_images": has_images_on_page,
        "tables": page_tables if page_tables else None,
        "original_filename": doc.original_filename,
    }


def _split_by_page_markers(markdown: str) -> list[str]:
    """Split markdown into pages by form feed or horizontal rule markers.

    If no markers are found, splits into ~3000-character chunks.
    """
    # Try form feed first
    if "\f" in markdown:
        pages = [p.strip() for p in markdown.split("\f") if p.strip()]
        if pages:
            return pages

    # Try horizontal rule (---) as page separator
    if "\n---\n" in markdown:
        pages = [p.strip() for p in markdown.split("\n---\n") if p.strip()]
        if pages:
            return pages

    # Reason: fallback — chunk by approximate page size (3000 chars)
    chunk_size = 3000
    if len(markdown) <= chunk_size:
        return [markdown]

    pages = []
    lines = markdown.split("\n")
    current_page: list[str] = []
    current_len = 0

    for line in lines:
        current_page.append(line)
        current_len += len(line) + 1  # +1 for newline
        if current_len >= chunk_size:
            pages.append("\n".join(current_page))
            current_page = []
            current_len = 0

    if current_page:
        pages.append("\n".join(current_page))

    return pages if pages else [markdown]


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("GetPage error [%s]: %s", code, message)
    return {"error": code, "message": message}
