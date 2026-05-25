"""``extract_document`` MCP tool handler.

Orchestrates the full extraction pipeline for a single document:
engine selection → conversion → artifact writing → catalog update.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..artifact_writer import write_artifacts, write_failed_meta
from ..catalog_db import CatalogDB
from ..engines import ConversionResult
from ..engines.docling_engine import ConversionError as DoclingError
from ..engines.docling_engine import DoclingEngine
from ..engines.markitdown_engine import ConversionError as MarkItDownError
from ..engines.markitdown_engine import MarkItDownEngine
from ..engines.pdfplumber_engine import PdfPlumberEngine
from ..engines.classifier import classify_document
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_extract_document(
    document_id: str,
    force: bool = False,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
    docling_engine: DoclingEngine,
    markitdown_engine: MarkItDownEngine,
    pdfplumber_engine: PdfPlumberEngine,
    finance_pipeline: Any = None,
) -> dict:
    """Extract a document and store the resulting artifacts.

    Orchestration flow:
        1. Look up document in catalog by document_id
        2. If extraction_status == "extracted" and force is False → return existing
        3. Get the original file path from vault
        4. Select engine: Docling for PDFs, MarkItDown for everything else
        5. Run extraction
        6. Write artifacts to extracted/<sha256>/
        7. Update catalog: extraction_status → "extracted"
        8. Return result summary

    Args:
        document_id: UUID of the document to extract.
        force: If True, re-extract even if already extracted.
        vault: VaultManager instance.
        catalog: CatalogDB instance.
        docling_engine: DoclingEngine instance.
        markitdown_engine: MarkItDownEngine instance.

    Returns:
        Structured dict with extraction results or error info.
    """
    # ── 1. Look up document ──────────────────────────────────────
    logger.debug("Attempting to retrieve document metadata for %s", document_id)
    doc = catalog.get_by_id(document_id)
    if doc is None:
        logger.error("Document '%s' not found in catalog", document_id)
        return _error("not_found", f"Document '{document_id}' not found in catalog")

    # ── 2. Check if already extracted ────────────────────────────
    if doc.extraction_status == "extracted" and not force:
        logger.debug("Document %s already extracted, checking artifact existence", document_id)
        extraction_dir = vault.get_extraction_dir(doc.sha256_hash)
        meta_path = os.path.join(extraction_dir, "conversion_meta.json")
        if os.path.isfile(meta_path):
            return {
                "document_id": document_id,
                "extraction_status": "extracted",
                "message": "Document already extracted. Use force=true to re-extract.",
                "artifacts_dir": extraction_dir,
            }

    # ── 3. Resolve the original file path ────────────────────────
    # Reason: canonical_path is relative to vault_root
    original_path = os.path.join(vault.vault_root, doc.canonical_path)
    if not os.path.isfile(original_path):
        return _error(
            "file_missing",
            f"Original file not found at: {doc.canonical_path}"
        )

    # ── 4. Select engine ─────────────────────────────────────────
    mime = doc.mime_type or ""
    engine_name = _select_engine(mime)
    logger.info("Selected engine '%s' for mime type '%s' (document_id=%s)", engine_name, mime, document_id)

    # ── 5. Run extraction ────────────────────────────────────────
    result: ConversionResult | None = None
    error_msg: str | None = None

    if engine_name == "docling":
        try:
            logger.info("Starting page-by-page Docling extraction for %s", document_id)
            result = docling_engine.convert_by_page(
                original_path,
                pdfplumber_engine=pdfplumber_engine,
            )
        except (DoclingError, Exception) as exc:
            logger.warning(
                "Docling failed for %s, falling back to MarkItDown: %s",
                document_id, exc,
            )
            # Reason: graceful degradation — if Docling fails on a PDF,
            # try MarkItDown as a fallback rather than failing entirely
            try:
                logger.debug("Calling MarkItDownEngine.convert as fallback on %s", original_path)
                result = markitdown_engine.convert(original_path)
                result.warnings.append(f"Docling failed, fell back to MarkItDown: {exc}")
            except (MarkItDownError, Exception) as fallback_exc:
                logger.critical("Both engines failed for document %s", document_id)
                error_msg = f"Both engines failed. Docling: {exc}. MarkItDown: {fallback_exc}"
    else:
        try:
            logger.debug("Calling MarkItDownEngine.convert on %s", original_path)
            result = markitdown_engine.convert(original_path)
        except (MarkItDownError, Exception) as exc:
            logger.error("MarkItDown failed for document %s: %s", document_id, exc)
            error_msg = f"MarkItDown failed: {exc}"

    # ── 6. Handle failure ────────────────────────────────────────
    if result is None:
        logger.error("Extraction failed for document %s, writing failed meta", document_id)
        write_failed_meta(
            vault.vault_root,
            doc.sha256_hash,
            engine_name,
            error_msg or "Unknown error",
        )
        catalog.update_document(
            document_id,
            extraction_status="failed",
        )
        return _error("extraction_failed", error_msg or "Unknown extraction error")

    # ── 6.5 Classify Document Type ───────────────────────────────
    document_type = doc.document_type
    
    if not document_type or document_type == "unknown" or document_type == "general":
        # Get first page text for classification
        first_page_text = ""
        if result.pages and len(result.pages) > 0:
            first_page_text = result.pages[0].text
        else:
            first_page_text = result.markdown[:2000]
            
        new_type, confidence = classify_document(doc.original_filename, first_page_text)
        logger.info("Classified %s as %s (confidence: %.2f)", doc.original_filename, new_type, confidence)
        document_type = new_type
        
        # FR-2.4: If below 0.7, fallback is effectively what the classifier returned (usually "general")
        # but we log the low confidence. The schema has no immediate "needs_review" for document_type,
        # but we could store it in tags or just accept the fallback.

    # ── 6.6 PDFPlumber Fallback for Financial Docs ───────────────
    pdfplumber_tables = None
    if document_type in ["bank_statement", "invoice"] and mime == "application/pdf":
        try:
            logger.info("Running pdfplumber table extraction for financial document %s", document_id)
            pdfplumber_tables = pdfplumber_engine.extract_tables(original_path)
        except Exception as exc:
            logger.warning("pdfplumber extraction failed for %s: %s", document_id, exc)
            if result.warnings is None:
                result.warnings = []
            result.warnings.append(f"pdfplumber extraction failed: {exc}")

    # ── 7. Write artifacts ───────────────────────────────────────
    try:
        written = write_artifacts(
            vault.vault_root,
            doc.sha256_hash,
            result,
            pdfplumber_tables=pdfplumber_tables,
        )
    except Exception as exc:
        catalog.update_document(document_id, extraction_status="failed")
        return _error("artifact_write_failed", f"Failed to write artifacts: {exc}")

    # ── 8. Update catalog ────────────────────────────────────────
    update_fields = dict(
        extraction_status="extracted",
        document_type=document_type,
    )
    # Reason: if we re-extracted (force=True), the old chunks/vectors are
    # stale. Reset indexing_status so the document gets re-indexed.
    if force:
        update_fields["indexing_status"] = "pending"
    catalog.update_document(document_id, **update_fields)

    # ── 9. Run Financial Pipeline if applicable ──────────────────
    finance_output_summary = "Not a financial document"
    if finance_pipeline and document_type in ["bank_statement", "invoice"]:
        try:
            logger.info("Triggering financial extraction pipeline for %s", document_id)
            tables_to_use = []
            if pdfplumber_tables:
                tables_to_use = pdfplumber_tables
            elif result.tables:
                for t in result.tables:
                    tables_to_use.append({
                        "page_number": t.page_number,
                        "headers": t.headers,
                        "rows": t.rows
                    })
                    
            finance_res = await finance_pipeline.extract_financial_data(
                document_id=document_id,
                tables=tables_to_use,
                full_text=result.markdown,
                filename=doc.original_filename,
                extraction_method=result.engine,
            )
            
            if "error" in finance_res:
                finance_output_summary = f"Error: {finance_res['message']}"
                logger.error("Financial extraction failed: %s", finance_res["message"])
                if result.warnings is None:
                    result.warnings = []
                result.warnings.append(f"Financial extraction error: {finance_res['message']}")
            else:
                txn_count = finance_res.get("transaction_count", 0)
                finance_output_summary = f"Success: Inserted {txn_count} transactions into ledger"
                logger.info("Financial extraction succeeded: %d transactions", txn_count)
        except Exception as exc:
            finance_output_summary = f"Failed: {exc}"
            logger.error("Financial extraction pipeline failed: %s", exc)
            if result.warnings is None:
                result.warnings = []
            result.warnings.append(f"Financial extraction pipeline failed: {exc}")

    logger.info(
        "Extracted %s (%s) with %s: %d pages, %d tables, %d chars",
        document_id,
        doc.original_filename,
        result.engine,
        result.page_count,
        result.table_count,
        result.char_count,
    )

    return {
        "document_id": document_id,
        "extraction_status": "extracted",
        "document_type": document_type,
        "engine": result.engine,
        "engine_version": result.engine_version,
        "page_count": result.page_count,
        "char_count": result.char_count,
        "table_count": result.table_count,
        "duration_seconds": round(result.duration_seconds, 2),
        "artifacts_written": written,
        "financial_pipeline_result": finance_output_summary,
        "warnings": result.warnings if result.warnings else None,
        "message": f"Successfully extracted {doc.original_filename}. Financial Pipeline: {finance_output_summary}",
    }


def _select_engine(mime_type: str) -> str:
    """Determine which extraction engine to use.

    Docling handles all PDFs. MarkItDown handles everything else.
    """
    if mime_type == "application/pdf":
        return "docling"
    return "markitdown"


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("Extract error [%s]: %s", code, message)
    return {"error": code, "message": message}
