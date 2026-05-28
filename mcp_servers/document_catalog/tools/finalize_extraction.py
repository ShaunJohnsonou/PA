"""``finalize_extraction`` MCP tool handler.

Combines per-page extraction artifacts into a unified result,
writes combined artifacts, updates the catalog, and runs
the financial pipeline for bank statements/invoices.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from ..artifact_writer import write_artifacts
from ..catalog_db import CatalogDB
from ..engines import ConversionResult, ExtractedTable, PageData
from ..engines.classifier import classify_document
from ..engines.pdfplumber_engine import PdfPlumberEngine
from ..vault import VaultManager

logger = logging.getLogger(__name__)


async def handle_finalize_extraction(
    document_id: str,
    *,
    vault: VaultManager,
    catalog: CatalogDB,
    pdfplumber_engine: PdfPlumberEngine,
    finance_pipeline: Any = None,
    ledger: Any = None,
) -> dict:
    """Combine per-page extraction results and run the financial pipeline.

    This tool should be called after all pages have been individually
    extracted via extract_document(page=N).

    Args:
        document_id: UUID of the document.
        vault: VaultManager instance.
        catalog: CatalogDB instance.
        pdfplumber_engine: PdfPlumberEngine for table extraction.
        finance_pipeline: FinancialExtractionPipeline instance (optional).

    Returns:
        Dict with combined extraction results and financial pipeline status.
    """
    logger.info("finalize_extraction called for document_id=%s", document_id)

    # ── 1. Look up document ──────────────────────────────────────
    doc = catalog.get_by_id(document_id)
    if doc is None:
        logger.error("Document '%s' not found in catalog", document_id)
        return _error("not_found", f"Document '{document_id}' not found in catalog")

    # ── 2. Find per-page artifacts ───────────────────────────────
    extraction_dir = vault.get_extraction_dir(doc.sha256_hash)
    pages_dir = os.path.join(extraction_dir, "pages")

    if not os.path.isdir(pages_dir):
        return _error(
            "not_split",
            "No split pages found. Run split_pdf first, then extract each page.",
        )

    # Read split metadata
    split_meta_path = os.path.join(pages_dir, "split_meta.json")
    if not os.path.isfile(split_meta_path):
        return _error(
            "no_split_meta",
            "Split metadata not found. Run split_pdf first.",
        )

    with open(split_meta_path) as f:
        split_meta = json.load(f)

    total_pages = split_meta.get("page_count", 0)
    logger.info("Finalising %d-page document: %s", total_pages, doc.original_filename)

    # ── 3. Collect per-page extraction results ───────────────────
    all_markdowns: list[str] = []
    all_pages: list[PageData] = []
    all_tables: list[ExtractedTable] = []
    pages_extracted = 0
    pages_failed = 0
    total_chars = 0

    for page_num in range(1, total_pages + 1):
        page_md_path = os.path.join(pages_dir, f"page_{page_num:04d}.md")
        page_tables_path = os.path.join(pages_dir, f"page_{page_num:04d}_tables.json")
        page_meta_path = os.path.join(pages_dir, f"page_{page_num:04d}_meta.json")

        if not os.path.isfile(page_md_path):
            logger.warning(
                "Page %d/%d not extracted (no markdown file at %s)",
                page_num, total_pages, page_md_path,
            )
            pages_failed += 1
            continue

        # Read markdown
        with open(page_md_path, encoding="utf-8") as f:
            page_markdown = f.read()
        all_markdowns.append(page_markdown)
        total_chars += len(page_markdown)

        # Read page metadata
        has_tables = False
        has_images = False
        if os.path.isfile(page_meta_path):
            with open(page_meta_path) as f:
                pmeta = json.load(f)
                has_tables = pmeta.get("has_tables", False)
                has_images = pmeta.get("has_images", False)

        all_pages.append(PageData(
            page_number=page_num,
            text=page_markdown,
            word_count=len(page_markdown.split()),
            has_tables=has_tables,
            has_images=has_images,
        ))

        # Read tables
        if os.path.isfile(page_tables_path):
            with open(page_tables_path) as f:
                page_tables = json.load(f)
                for t in page_tables:
                    all_tables.append(ExtractedTable(
                        page_number=t.get("page_number", page_num),
                        table_index=t.get("table_index", len(all_tables)),
                        headers=t.get("headers", []),
                        rows=t.get("rows", []),
                        caption=t.get("caption"),
                    ))

        pages_extracted += 1
        logger.debug(
            "  Loaded page %d/%d: %d chars, tables_file=%s",
            page_num, total_pages, len(page_markdown),
            "yes" if os.path.isfile(page_tables_path) else "no",
        )

    if pages_extracted == 0:
        return _error(
            "no_pages_extracted",
            f"None of {total_pages} pages have been extracted yet. "
            f"Use extract_document with page=N to extract each page first.",
        )

    logger.info(
        "Collected %d/%d pages: %d chars, %d tables (%d pages failed)",
        pages_extracted, total_pages, total_chars, len(all_tables), pages_failed,
    )

    # ── 4. Build combined ConversionResult ───────────────────────
    # Reason: join page markdowns with form-feed separators so
    # downstream page-splitting logic can find boundaries
    combined_markdown = "\n\f\n".join(all_markdowns)

    try:
        import docling
        engine_version = getattr(docling, "__version__", "unknown")
    except Exception:
        engine_version = "unknown"

    warnings = []
    if pages_failed > 0:
        warnings.append(
            f"{pages_failed}/{total_pages} pages were not extracted. "
            f"Use extract_document with page=N to extract missing pages."
        )

    result = ConversionResult(
        markdown=combined_markdown,
        page_count=pages_extracted,
        char_count=total_chars,
        table_count=len(all_tables),
        pages=all_pages,
        tables=all_tables,
        engine="docling",
        engine_version=engine_version,
        duration_seconds=0,
        warnings=warnings,
    )

    # ── 5. Classify document type ────────────────────────────────
    document_type = doc.document_type
    if not document_type or document_type in ("unknown", "general"):
        first_page_text = all_pages[0].text if all_pages else combined_markdown[:2000]
        document_type, confidence = classify_document(doc.original_filename, first_page_text)
        logger.info("Classified %s as %s (confidence: %.2f)", doc.original_filename, document_type, confidence)

    # ── 6. Run pdfplumber for structured tables ──────────────────
    original_path = os.path.join(vault.vault_root, doc.canonical_path)
    pdfplumber_tables = None
    if document_type in ("bank_statement", "invoice") and os.path.isfile(original_path):
        try:
            logger.info("Running pdfplumber table extraction on original PDF for %s", document_id)
            pdfplumber_tables = pdfplumber_engine.extract_tables(original_path)
            logger.info("pdfplumber found %d tables", len(pdfplumber_tables))
        except Exception as exc:
            logger.warning("pdfplumber extraction failed: %s", exc)
            warnings.append(f"pdfplumber extraction failed: {exc}")

    # ── 7. Write combined artifacts ──────────────────────────────
    try:
        written = write_artifacts(
            vault.vault_root,
            doc.sha256_hash,
            result,
            pdfplumber_tables=pdfplumber_tables,
        )
        logger.info("Combined artifacts written: %s", written)
    except Exception as exc:
        logger.error("Failed to write combined artifacts: %s", exc)
        catalog.update_document(document_id, extraction_status="failed")
        return _error("artifact_write_failed", f"Failed to write artifacts: {exc}")

    # ── 8. Update catalog ────────────────────────────────────────
    catalog.update_document(
        document_id,
        extraction_status="extracted",
        document_type=document_type,
        indexing_status="pending",
    )
    logger.info("Catalog updated: extraction_status=extracted, document_type=%s", document_type)

    # ── 9. Run financial pipeline ────────────────────────────────
    finance_output_summary = "Not a financial document"
    if finance_pipeline and document_type in ("bank_statement", "invoice"):
        try:
            logger.info("Triggering financial extraction pipeline for %s", document_id)
            tables_to_use = []
            if pdfplumber_tables:
                tables_to_use = pdfplumber_tables
            elif all_tables:
                for t in all_tables:
                    tables_to_use.append({
                        "page_number": t.page_number,
                        "headers": t.headers,
                        "rows": t.rows,
                    })

            finance_res = await finance_pipeline.extract_financial_data(
                document_id=document_id,
                tables=tables_to_use,
                full_text=combined_markdown,
                filename=doc.original_filename,
                extraction_method="docling",
            )

            if "error" in finance_res:
                finance_output_summary = f"Error: {finance_res['message']}"
                logger.error("Financial extraction failed: %s", finance_res["message"])
                warnings.append(f"Financial extraction error: {finance_res['message']}")
            else:
                txn_count = finance_res.get("transaction_count", 0)
                finance_output_summary = f"Success: Inserted {txn_count} transactions into ledger"
                logger.info("Financial extraction succeeded: %d transactions", txn_count)
        except Exception as exc:
            finance_output_summary = f"Failed: {exc}"
            logger.error("Financial extraction pipeline failed: %s", exc)
            warnings.append(f"Financial extraction pipeline failed: {exc}")

    # ── 10. Run transfer detection ───────────────────────────────
    transfers_tagged = 0
    if ledger and document_type in ("bank_statement", "invoice"):
        try:
            logger.info("Running inter-account transfer detection...")
            transfers_tagged = ledger.detect_transfers()
            if transfers_tagged > 0:
                logger.info("Tagged %d transactions as inter-account transfers", transfers_tagged)
        except Exception as exc:
            logger.warning("Transfer detection failed (non-fatal): %s", exc)
            warnings.append(f"Transfer detection failed: {exc}")

    logger.info(
        "finalize_extraction COMPLETE: %s — %d/%d pages, %d tables, %d chars. Finance: %s. Transfers: %d",
        doc.original_filename, pages_extracted, total_pages,
        len(all_tables), total_chars, finance_output_summary, transfers_tagged,
    )

    return {
        "document_id": document_id,
        "extraction_status": "extracted",
        "document_type": document_type,
        "total_pages": total_pages,
        "pages_extracted": pages_extracted,
        "pages_failed": pages_failed,
        "total_tables": len(all_tables),
        "total_chars": total_chars,
        "financial_pipeline_result": finance_output_summary,
        "transfers_tagged": transfers_tagged,
        "warnings": warnings if warnings else None,
        "message": (
            f"Finalised {doc.original_filename}: "
            f"{pages_extracted}/{total_pages} pages, "
            f"{len(all_tables)} tables, {total_chars} chars. "
            f"Financial Pipeline: {finance_output_summary}. "
            f"Transfers tagged: {transfers_tagged}"
        ),
    }


def _error(code: str, message: str) -> dict:
    """Return a structured error response."""
    logger.warning("finalize_extraction error [%s]: %s", code, message)
    return {"error": code, "message": message}
