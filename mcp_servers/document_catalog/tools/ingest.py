"""TR-1.4 — ``ingest_document`` MCP tool handler.

Orchestrates the full ingest flow:
validation → MIME detection → hashing → dedup check → vault storage → catalog insert.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from ..catalog_db import CatalogDB, DocumentRow, DuplicateHashError
from ..hashing import compute_sha256
from ..mime_detect import detect_mime_type
from ..vault import VaultManager, VaultWriteError

logger = logging.getLogger(__name__)


async def handle_ingest_document(
    file_path: str,
    source: str = "manual",
    tags: list[str] | None = None,
    document_type: str | None = None,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
) -> dict:
    """Ingest a document into the vault and catalog.

    Flow:
        1. Validate file_path exists and is readable
        2. Detect MIME type
        3. Compute SHA256 hash (streaming)
        4. Check catalog for duplicate
        5. If duplicate → return existing doc info
        6. Store original in vault (atomic copy)
        7. Insert catalog row
        8. Return success result

    Returns:
        Structured dict with ``document_id``, ``sha256_hash``,
        ``is_duplicate``, ``status``, and ``message``.
    """
    # ── 1. Validate file exists ──────────────────────────────────
    if not file_path:
        return _error("invalid_path", "file_path must not be empty")

    if not os.path.isfile(file_path):
        return _error("file_not_found", f"File does not exist: {file_path}")

    if not os.access(file_path, os.R_OK):
        return _error("permission_denied", f"Cannot read file: {file_path}")

    # ── 2. Detect MIME type ──────────────────────────────────────
    mime = detect_mime_type(file_path)
    logger.debug("Detected MIME: %s for %s", mime, file_path)

    # ── 3. Compute SHA256 ────────────────────────────────────────
    try:
        logger.debug("Computing SHA256 for file: %s", file_path)
        sha256 = compute_sha256(file_path)
        logger.debug("SHA256 computed: %s", sha256)
    except Exception as exc:
        logger.error("Failed to compute SHA256 for %s: %s", file_path, exc)
        return _error("hash_error", f"Failed to hash file: {exc}")

    # ── 4. Check for duplicate ───────────────────────────────────
    existing = catalog.get_by_hash(sha256)
    if existing is not None:
        logger.info(
            "Duplicate detected: %s already stored as %s",
            file_path,
            existing.document_id,
        )
        return {
            "document_id": existing.document_id,
            "sha256_hash": sha256,
            "is_duplicate": True,
            "original_filename": existing.original_filename,
            "mime_type": existing.mime_type,
            "status": existing.status,
            "message": "Document already exists in the vault",
        }

    # ── 5. Determine extension ───────────────────────────────────
    original_filename = os.path.basename(file_path)
    extension = Path(file_path).suffix or ".bin"

    # ── 6. Store in vault ────────────────────────────────────────
    try:
        logger.debug("Storing original file in vault (sha256=%s, ext=%s)", sha256, extension)
        canonical_path = vault.store_original(file_path, sha256, extension)
        logger.debug("Stored successfully at %s", canonical_path)
    except VaultWriteError as exc:
        logger.error("Vault write error for %s: %s", file_path, exc)
        return _error("vault_write_failed", str(exc))

    # ── 7. Insert catalog row ────────────────────────────────────
    file_size = os.path.getsize(file_path)
    tags_json = json.dumps(tags) if tags else None

    doc = DocumentRow(
        document_id=str(uuid.uuid4()),
        sha256_hash=sha256,
        original_filename=original_filename,
        canonical_path=canonical_path,
        mime_type=mime,
        file_size_bytes=file_size,
        document_type=document_type,
        source=source,
        tags=tags_json,
    )

    try:
        catalog.insert_document(doc)
    except DuplicateHashError:
        # Reason: race condition — another request ingested the same file
        # between our dedup check and the insert.  Return the existing doc.
        existing = catalog.get_by_hash(sha256)
        if existing:
            return {
                "document_id": existing.document_id,
                "sha256_hash": sha256,
                "is_duplicate": True,
                "original_filename": existing.original_filename,
                "mime_type": existing.mime_type,
                "status": existing.status,
                "message": "Document already exists (concurrent ingest)",
            }
        return _error("catalog_error", "Race condition during ingest")
    except Exception as exc:
        return _error("catalog_error", f"Failed to insert catalog row: {exc}")

    logger.info(
        "Ingested %s → %s (%s, %d bytes)",
        original_filename,
        doc.document_id,
        mime,
        file_size,
    )

    return {
        "document_id": doc.document_id,
        "sha256_hash": sha256,
        "is_duplicate": False,
        "original_filename": original_filename,
        "mime_type": mime,
        "file_size_bytes": file_size,
        "status": "ingested",
        "message": "Document ingested successfully",
    }


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Ingest error [%s]: %s", code, message)
    return {"error": code, "message": message}
