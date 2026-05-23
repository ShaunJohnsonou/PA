"""``delete_document`` MCP tool handler.

Removes a document from the vault, search indexes, and catalog.
This is a destructive operation — the original file and all
extraction artifacts are permanently deleted.
"""
from __future__ import annotations

import logging
import os
import shutil

from ..catalog_db import CatalogDB
from ..search.lifecycle import IndexLifecycle
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_delete_document(
    document_id: str,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
    lifecycle: IndexLifecycle,
) -> dict:
    """Delete a document and all associated data.

    Flow:
        1. Look up document in catalog
        2. Remove from search indexes (FAISS + FTS5)
        3. Delete extraction artifacts from disk
        4. Delete original file from vault
        5. Remove catalog row

    Returns:
        Structured dict confirming deletion or error info.
    """
    # ── 1. Look up document ──────────────────────────────────────
    logger.info("Starting delete_document for document_id=%s", document_id)
    doc = catalog.get_by_id(document_id)
    if doc is None:
        return _error("not_found", f"Document '{document_id}' not found")

    filename = doc.original_filename

    # ── 2. Remove from search indexes ────────────────────────────
    try:
        index_stats = lifecycle.remove_document(document_id)
        logger.info(
            "Removed from indexes: %d vectors, %d chunks",
            index_stats.vectors_removed,
            abs(index_stats.chunks_indexed),
        )
    except Exception as exc:
        logger.warning("Index cleanup failed for %s: %s", document_id, exc)
        # Continue with deletion even if index cleanup fails

    # ── 3. Delete extraction artifacts ───────────────────────────
    extraction_dir = vault.get_extraction_dir(doc.sha256_hash)
    if os.path.isdir(extraction_dir):
        try:
            shutil.rmtree(extraction_dir)
            logger.info("Deleted extraction dir: %s", extraction_dir)
        except Exception as exc:
            logger.warning("Failed to delete extraction dir: %s", exc)

    # ── 4. Delete original file ──────────────────────────────────
    original_path = os.path.join(vault.vault_root, doc.canonical_path)
    if os.path.isfile(original_path):
        try:
            # Reason: originals are stored read-only (chmod 444), so we
            # need to make them writable before deleting.
            os.chmod(original_path, 0o644)
            os.unlink(original_path)
            logger.info("Deleted original: %s", doc.canonical_path)
        except Exception as exc:
            logger.warning("Failed to delete original file: %s", exc)

    # ── 5. Remove catalog row ────────────────────────────────────
    deleted = catalog.delete_document(document_id)
    if not deleted:
        logger.warning("Catalog row for %s was already missing", document_id)

    logger.info("Document %s (%s) fully deleted", document_id, filename)

    return {
        "document_id": document_id,
        "original_filename": filename,
        "message": f"Successfully deleted {filename}",
    }


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Delete error [%s]: %s", code, message)
    return {"error": code, "message": message}
