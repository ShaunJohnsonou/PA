"""``split_pdf`` MCP tool handler.

Splits a multi-page PDF into individual single-page PDF files,
stored in the vault under the document's extraction directory.
This enables per-page extraction by the agent.
"""
from __future__ import annotations

import json
import logging
import os

from ..catalog_db import CatalogDB
from ..engines.pdfplumber_engine import PdfPlumberEngine
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_split_pdf(
    document_id: str,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
    pdfplumber_engine: PdfPlumberEngine,
) -> dict:
    """Split a multi-page PDF into individual page files.

    The pages are stored in:
        vault/extracted/<sha256>/pages/page_0001.pdf
        vault/extracted/<sha256>/pages/page_0002.pdf
        ...

    Args:
        document_id: UUID of the ingested PDF document.
        vault: VaultManager instance.
        catalog: CatalogDB instance.
        pdfplumber_engine: PdfPlumberEngine for splitting.

    Returns:
        Dict with page_count, page list, and status info.
    """
    logger.info("split_pdf called for document_id=%s", document_id)

    # ── 1. Look up document ──────────────────────────────────────
    doc = catalog.get_by_id(document_id)
    if doc is None:
        logger.error("Document '%s' not found in catalog", document_id)
        return _error("not_found", f"Document '{document_id}' not found in catalog")

    # ── 2. Validate it's a PDF ───────────────────────────────────
    mime = doc.mime_type or ""
    if mime != "application/pdf":
        logger.warning("split_pdf called on non-PDF document: mime=%s", mime)
        return _error(
            "not_pdf",
            f"split_pdf only works on PDFs. This document is '{mime}'.",
        )

    # ── 3. Resolve original file path ────────────────────────────
    original_path = os.path.join(vault.vault_root, doc.canonical_path)
    if not os.path.isfile(original_path):
        return _error("file_missing", f"Original file not found at: {doc.canonical_path}")

    logger.debug("Original PDF at: %s", original_path)

    # ── 4. Create pages directory ────────────────────────────────
    extraction_dir = vault.get_extraction_dir(doc.sha256_hash)
    pages_dir = os.path.join(extraction_dir, "pages")

    # Reason: check if already split to avoid redundant work
    if os.path.isdir(pages_dir):
        existing_pages = sorted([
            f for f in os.listdir(pages_dir)
            if f.startswith("page_") and f.endswith(".pdf")
        ])
        if existing_pages:
            logger.info(
                "Document %s already split into %d pages, returning existing split",
                document_id, len(existing_pages),
            )
            pages_info = []
            for f in existing_pages:
                page_num = int(f.replace("page_", "").replace(".pdf", ""))
                page_path = os.path.join(pages_dir, f)
                pages_info.append({
                    "page": page_num,
                    "size_bytes": os.path.getsize(page_path),
                })

            return {
                "document_id": document_id,
                "page_count": len(existing_pages),
                "split_status": "already_split",
                "pages": pages_info,
                "message": (
                    f"PDF already split into {len(existing_pages)} pages. "
                    f"Extract each page using extract_document with page=N, "
                    f"then call finalize_extraction."
                ),
            }

    os.makedirs(pages_dir, exist_ok=True)

    # ── 5. Split the PDF ─────────────────────────────────────────
    try:
        page_paths = pdfplumber_engine.split_to_single_pages(original_path, pages_dir)
    except Exception as exc:
        logger.error("Failed to split PDF %s: %s", document_id, exc)
        return _error("split_failed", f"Failed to split PDF: {exc}")

    # ── 6. Build page info ───────────────────────────────────────
    pages_info = []
    for i, page_path in enumerate(page_paths):
        pages_info.append({
            "page": i + 1,
            "size_bytes": os.path.getsize(page_path),
        })

    # ── 7. Write split metadata ──────────────────────────────────
    split_meta = {
        "document_id": document_id,
        "original_filename": doc.original_filename,
        "sha256_hash": doc.sha256_hash,
        "page_count": len(page_paths),
        "pages": pages_info,
    }
    meta_path = os.path.join(pages_dir, "split_meta.json")
    with open(meta_path, "w") as f:
        json.dump(split_meta, f, indent=2)

    logger.info(
        "split_pdf complete: %s → %d pages in %s",
        doc.original_filename, len(page_paths), pages_dir,
    )

    return {
        "document_id": document_id,
        "page_count": len(page_paths),
        "split_status": "split",
        "pages": pages_info,
        "message": (
            f"Split {doc.original_filename} into {len(page_paths)} pages. "
            f"Now extract each page using extract_document with page=1 through "
            f"page={len(page_paths)}, then call finalize_extraction to combine "
            f"results and import transactions."
        ),
    }


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("split_pdf error [%s]: %s", code, message)
    return {"error": code, "message": message}
