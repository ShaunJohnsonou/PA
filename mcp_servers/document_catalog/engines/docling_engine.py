"""Docling-based PDF extraction engine.

Uses IBM's Docling library to parse PDFs with advanced table structure
recognition, layout-aware text extraction, and optional OCR support.
This is the primary engine for ALL PDF documents.
"""
from __future__ import annotations

import logging
import math
import time
from pathlib import Path

from . import ConversionResult, ExtractedTable, PageData

logger = logging.getLogger(__name__)


class ConversionError(Exception):
    """Raised when Docling fails to convert a document."""


class DoclingEngine:
    """PDF extraction engine powered by IBM Docling.

    Docling is the primary engine for all PDF files. It provides:
    - Layout-aware text extraction with reading order
    - Table structure recognition (headers, rows, columns)
    - Per-page content segmentation
    - Optional OCR for scanned documents

    The DocumentConverter is instantiated once and reused because
    it is expensive to create (loads ML models into memory).
    """

    SUPPORTED_MIMES = {
        "application/pdf",
    }

    def __init__(self, vault_root: str) -> None:
        """
        Args:
            vault_root: Absolute path to vault, used for path restriction.
        """
        self._vault_root = vault_root
        self._converter = None  # Lazy init

    def _get_converter(self):
        """Lazy-initialise the Docling DocumentConverter.

        Reason: importing docling and creating the converter loads ML models
        which takes several seconds. We only pay this cost on first use.
        """
        if self._converter is None:
            try:
                from docling.document_converter import DocumentConverter
                self._converter = DocumentConverter()
                logger.info("Docling DocumentConverter initialised")
            except ImportError:
                raise ConversionError(
                    "docling is not installed. "
                    "Run: pip install docling"
                )
        return self._converter

    def can_handle(self, mime_type: str) -> bool:
        """Return True if this engine supports the given MIME type."""
        return mime_type in self.SUPPORTED_MIMES

    def convert(self, source_path: str) -> ConversionResult:
        """Convert a PDF to structured output using Docling.

        Args:
            source_path: Absolute path to the PDF file.
                         Must be within the vault root.

        Returns:
            ConversionResult with markdown, per-page data, and tables.

        Raises:
            ValueError: If source_path is outside the vault.
            ConversionError: If Docling fails to process the file.
        """
        # Reason: security — prevent path traversal outside the vault
        self._validate_path(source_path)

        converter = self._get_converter()
        t0 = time.monotonic()

        try:
            result = converter.convert(source_path)
            doc = result.document
        except Exception as exc:
            raise ConversionError(f"Docling conversion failed: {exc}") from exc

        duration = time.monotonic() - t0

        # ── Extract full markdown ────────────────────────────────
        markdown = doc.export_to_markdown()
        char_count = len(markdown)

        # ── Extract per-page data ────────────────────────────────
        pages: list[PageData] = []
        tables: list[ExtractedTable] = []

        # Reason: Docling's internal page model gives us per-page content
        # We iterate through pages to build our PageData objects
        page_texts: dict[int, list[str]] = {}
        page_has_tables: dict[int, bool] = {}
        page_has_images: dict[int, bool] = {}

        for item in doc.iterate_items():
            element = item
            # Try to get page number from the element's provenance
            page_num = _get_page_number(element)
            if page_num is None:
                continue

            if page_num not in page_texts:
                page_texts[page_num] = []
                page_has_tables[page_num] = False
                page_has_images[page_num] = False

            # Check element type for tables and images
            element_type = type(element).__name__.lower()

            if "table" in element_type:
                page_has_tables[page_num] = True
                table = _extract_table_data(element, page_num, len(tables))
                if table:
                    tables.append(table)

            if "picture" in element_type or "image" in element_type:
                page_has_images[page_num] = True

            # Get text content
            text = _get_element_text(element)
            if text:
                page_texts[page_num].append(text)

        # Build PageData list sorted by page number
        for pnum in sorted(page_texts.keys()):
            full_text = "\n".join(page_texts[pnum])
            pages.append(PageData(
                page_number=pnum,
                text=full_text,
                word_count=len(full_text.split()),
                has_tables=page_has_tables.get(pnum, False),
                has_images=page_has_images.get(pnum, False),
            ))

        page_count = len(pages) if pages else max(1, math.ceil(char_count / 3000))

        # Get docling version
        try:
            import docling
            engine_version = getattr(docling, "__version__", "unknown")
        except Exception:
            engine_version = "unknown"

        logger.info(
            "Docling converted %s: %d pages, %d chars, %d tables in %.1fs",
            source_path, page_count, char_count, len(tables), duration,
        )

        return ConversionResult(
            markdown=markdown,
            page_count=page_count,
            char_count=char_count,
            table_count=len(tables),
            pages=pages,
            tables=tables,
            engine="docling",
            engine_version=engine_version,
            duration_seconds=duration,
        )

    def warm_up(self) -> None:
        """Pre-load ML models at server startup.

        Reason: Docling's DocumentConverter takes ~3.5 minutes to initialise
        because it downloads and loads OCR + layout models. By calling this
        at server startup, we pay the cost once instead of on the first
        tool call (which would make the agent think it's stuck).
        """
        logger.info("Docling warm-up: loading ML models (this may take a few minutes)...")
        t0 = time.monotonic()
        try:
            self._get_converter()
            logger.info("Docling warm-up complete in %.1fs", time.monotonic() - t0)
        except Exception as exc:
            logger.error("Docling warm-up FAILED after %.1fs: %s", time.monotonic() - t0, exc)
            logger.warning("Docling will be unavailable — extractions will fall back to MarkItDown")

    def convert_single_page(self, page_path: str, page_number: int) -> ConversionResult:
        """Extract a single-page PDF through Docling.

        Args:
            page_path: Absolute path to a single-page PDF file.
            page_number: The original page number (1-indexed) for metadata.

        Returns:
            ConversionResult for this single page.

        Raises:
            ConversionError: If Docling fails to process the page.
        """
        self._validate_path(page_path)
        converter = self._get_converter()
        t0 = time.monotonic()

        try:
            result = converter.convert(page_path)
            doc = result.document
        except Exception as exc:
            raise ConversionError(
                f"Docling failed on page {page_number}: {exc}"
            ) from exc

        duration = time.monotonic() - t0
        markdown = doc.export_to_markdown()

        # Extract tables and page data for this single page
        page_texts: list[str] = []
        page_has_tables = False
        page_has_images = False
        tables: list[ExtractedTable] = []

        for item in doc.iterate_items():
            element_type = type(item).__name__.lower()

            if "table" in element_type:
                page_has_tables = True
                table = _extract_table_data(item, page_number, len(tables))
                if table:
                    tables.append(table)

            if "picture" in element_type or "image" in element_type:
                page_has_images = True

            text = _get_element_text(item)
            if text:
                page_texts.append(text)

        full_text = "\n".join(page_texts)
        page_data = PageData(
            page_number=page_number,
            text=full_text,
            word_count=len(full_text.split()),
            has_tables=page_has_tables,
            has_images=page_has_images,
        )

        return ConversionResult(
            markdown=markdown,
            page_count=1,
            char_count=len(markdown),
            table_count=len(tables),
            pages=[page_data],
            tables=tables,
            engine="docling",
            engine_version="",
            duration_seconds=duration,
        )

    def convert_by_page(
        self,
        source_path: str,
        pdfplumber_engine=None,
    ) -> ConversionResult:
        """Split a PDF into pages and extract each individually via Docling.

        This method provides per-page progress logging so long extractions
        are visible in the logs rather than appearing stuck.

        Args:
            source_path: Absolute path to the multi-page PDF.
            pdfplumber_engine: PdfPlumberEngine instance for splitting.

        Returns:
            Combined ConversionResult merging all pages.

        Raises:
            ConversionError: If splitting or all pages fail.
        """
        import os
        import shutil

        self._validate_path(source_path)

        if pdfplumber_engine is None:
            # Reason: fallback — if no pdfplumber engine provided, just
            # process the whole PDF at once (original behavior)
            logger.warning("No pdfplumber_engine provided, falling back to full-PDF extraction")
            return self.convert(source_path)

        # ── 1. Split PDF into individual pages ───────────────────────
        # Reason: create temp dir inside vault so _validate_path succeeds
        tmp_dir = os.path.join(self._vault_root, "tmp", "page_split")
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            page_paths = pdfplumber_engine.split_to_single_pages(source_path, tmp_dir)
        except Exception as exc:
            logger.error("PDF splitting failed, falling back to full-PDF extraction: %s", exc)
            return self.convert(source_path)

        total_pages = len(page_paths)
        if total_pages == 0:
            raise ConversionError("PDF has zero pages")

        logger.info(
            "Starting page-by-page Docling extraction: %d pages from %s",
            total_pages, os.path.basename(source_path),
        )

        # ── 2. Extract each page individually ────────────────────────
        all_markdowns: list[str] = []
        all_pages: list[PageData] = []
        all_tables: list[ExtractedTable] = []
        total_chars = 0
        failed_pages: list[int] = []
        t0_total = time.monotonic()

        for i, page_path in enumerate(page_paths):
            page_num = i + 1
            t0_page = time.monotonic()

            try:
                page_result = self.convert_single_page(page_path, page_num)
                all_markdowns.append(page_result.markdown)
                all_pages.extend(page_result.pages)
                all_tables.extend(page_result.tables)
                total_chars += page_result.char_count

                page_duration = time.monotonic() - t0_page
                logger.info(
                    "  Docling page %d/%d: %d chars, %d tables (%.1fs)",
                    page_num, total_pages,
                    page_result.char_count, page_result.table_count,
                    page_duration,
                )
            except Exception as exc:
                failed_pages.append(page_num)
                page_duration = time.monotonic() - t0_page
                logger.warning(
                    "  Docling page %d/%d: FAILED (%.1fs): %s",
                    page_num, total_pages, page_duration, exc,
                )

        total_duration = time.monotonic() - t0_total

        # ── 3. Clean up temp files ───────────────────────────────────
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.debug("Cleaned up temp directory: %s", tmp_dir)
        except Exception as exc:
            logger.warning("Failed to clean up temp dir %s: %s", tmp_dir, exc)

        # ── 4. Check if we got anything ──────────────────────────────
        if len(all_markdowns) == 0:
            raise ConversionError(
                f"All {total_pages} pages failed Docling extraction"
            )

        # ── 5. Combine results ───────────────────────────────────────
        # Reason: join page markdowns with form-feed separators so
        # downstream page-splitting logic can find boundaries
        combined_markdown = "\n\f\n".join(all_markdowns)

        # Get docling version
        try:
            import docling
            engine_version = getattr(docling, "__version__", "unknown")
        except Exception:
            engine_version = "unknown"

        warnings = []
        if failed_pages:
            warnings.append(
                f"Docling failed on pages: {failed_pages}. "
                f"{len(all_markdowns)}/{total_pages} pages extracted successfully."
            )

        logger.info(
            "Page-by-page Docling extraction COMPLETE: %d/%d pages, "
            "%d chars, %d tables in %.1fs (%.1fs/page avg)",
            len(all_markdowns), total_pages,
            total_chars, len(all_tables),
            total_duration,
            total_duration / total_pages if total_pages > 0 else 0,
        )

        return ConversionResult(
            markdown=combined_markdown,
            page_count=len(all_markdowns),
            char_count=total_chars,
            table_count=len(all_tables),
            pages=all_pages,
            tables=all_tables,
            engine="docling",
            engine_version=engine_version,
            duration_seconds=total_duration,
            warnings=warnings,
        )

    def _validate_path(self, path: str) -> None:
        """Ensure path is within the vault root."""
        import os
        resolved = os.path.realpath(path)
        vault_real = os.path.realpath(self._vault_root)
        if not resolved.startswith(vault_real + os.sep) and resolved != vault_real:
            raise ValueError(
                f"Path '{path}' is outside vault root '{self._vault_root}'"
            )


# ── Helper functions ────────────────────────────────────────────────


def _get_page_number(element) -> int | None:
    """Extract 1-indexed page number from a Docling element's provenance."""
    try:
        # Docling elements have a `prov` list with provenance info
        if hasattr(element, "prov") and element.prov:
            prov = element.prov[0]
            if hasattr(prov, "page_no"):
                return prov.page_no  # Already 1-indexed in Docling
    except (IndexError, AttributeError):
        pass
    return None


def _get_element_text(element) -> str | None:
    """Extract text content from a Docling element."""
    try:
        if hasattr(element, "text") and element.text:
            return element.text.strip()
        if hasattr(element, "export_to_markdown"):
            md = element.export_to_markdown()
            if md:
                return md.strip()
    except Exception:
        pass
    return None


def _extract_table_data(
    element, page_number: int, global_index: int
) -> ExtractedTable | None:
    """Convert a Docling table element into our ExtractedTable structure."""
    try:
        if hasattr(element, "export_to_dataframe"):
            df = element.export_to_dataframe()
            headers = list(df.columns)
            rows = [list(row) for row in df.values]
            return ExtractedTable(
                page_number=page_number,
                table_index=global_index,
                headers=headers,
                rows=rows,
                caption=getattr(element, "caption", None),
            )

        # Reason: fallback for table elements that don't support dataframe export
        if hasattr(element, "export_to_markdown"):
            md = element.export_to_markdown()
            if md:
                headers, rows = _parse_markdown_table(md)
                if headers:
                    return ExtractedTable(
                        page_number=page_number,
                        table_index=global_index,
                        headers=headers,
                        rows=rows,
                    )
    except Exception as exc:
        logger.warning("Failed to extract table on page %d: %s", page_number, exc)
    return None


def _parse_markdown_table(md_text: str) -> tuple[list[str], list[list[str]]]:
    """Parse a simple Markdown table into headers and rows.

    Returns (headers, rows). Returns ([], []) if parsing fails.
    """
    lines = [l.strip() for l in md_text.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return [], []

    # Reason: skip separator line (e.g. |---|---|)
    def parse_row(line: str) -> list[str]:
        cells = [c.strip() for c in line.split("|")]
        # Remove empty first/last cells from leading/trailing pipes
        if cells and not cells[0]:
            cells = cells[1:]
        if cells and not cells[-1]:
            cells = cells[:-1]
        return cells

    headers = parse_row(lines[0])

    # Find data rows (skip separator lines)
    rows = []
    for line in lines[1:]:
        if set(line.replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            continue  # separator line
        row = parse_row(line)
        if len(row) == len(headers):
            rows.append(row)

    return headers, rows
