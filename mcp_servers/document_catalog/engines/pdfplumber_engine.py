"""pdfplumber-based table extraction engine.

Used as a fallback or specialized extractor for machine-generated
bank statements where exact cell boundaries matter.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time

logger = logging.getLogger(__name__)

class ConversionError(Exception):
    """Raised when pdfplumber fails to convert a document."""


class PdfPlumberEngine:
    """PDF extraction engine powered by pdfplumber.
    
    Used exclusively for extracting tables from machine-generated
    PDFs (like bank statements) to provide structured data that
    complements Docling's markdown output.
    
    Also provides PDF splitting utilities for page-by-page extraction.
    """

    def __init__(self, vault_root: str) -> None:
        self._vault_root = vault_root

    def _validate_path(self, path: str) -> None:
        """Ensure path is within the vault root."""
        resolved = os.path.realpath(path)
        vault_real = os.path.realpath(self._vault_root)
        if not resolved.startswith(vault_real + os.sep) and resolved != vault_real:
            raise ValueError(
                f"Path '{path}' is outside vault root '{self._vault_root}'"
            )

    def get_page_count(self, source_path: str) -> int:
        """Return the total number of pages in a PDF.
        
        Args:
            source_path: Absolute path to the PDF file.
            
        Returns:
            Number of pages in the PDF.
        """
        self._validate_path(source_path)
        
        try:
            import pdfplumber
        except ImportError:
            raise ConversionError("pdfplumber is not installed. Run: pip install pdfplumber")
        
        try:
            with pdfplumber.open(source_path) as pdf:
                count = len(pdf.pages)
                logger.debug("get_page_count(%s) = %d", os.path.basename(source_path), count)
                return count
        except Exception as exc:
            logger.error("Failed to get page count for %s: %s", source_path, exc)
            raise ConversionError(f"Failed to get page count: {exc}") from exc

    def split_to_single_pages(self, source_path: str, output_dir: str) -> list[str]:
        """Split a multi-page PDF into individual single-page PDF files.
        
        Uses pypdf to write each page as a separate PDF file.
        
        Args:
            source_path: Absolute path to the source PDF.
            output_dir: Directory to write single-page PDFs into.
            
        Returns:
            List of absolute paths to the single-page PDF files, ordered by page number.
            
        Raises:
            ConversionError: If splitting fails.
        """
        self._validate_path(source_path)
        
        try:
            from pypdf import PdfReader, PdfWriter
        except ImportError:
            raise ConversionError("pypdf is not installed. Run: pip install pypdf")
        
        os.makedirs(output_dir, exist_ok=True)
        page_paths = []
        
        try:
            t0 = time.monotonic()
            reader = PdfReader(source_path)
            total_pages = len(reader.pages)
            logger.info(
                "Splitting %s into %d individual pages → %s",
                os.path.basename(source_path), total_pages, output_dir,
            )
            
            for i, page in enumerate(reader.pages):
                writer = PdfWriter()
                writer.add_page(page)
                
                # Reason: zero-padded filenames for correct sort order
                page_path = os.path.join(output_dir, f"page_{i + 1:04d}.pdf")
                with open(page_path, "wb") as f:
                    writer.write(f)
                page_paths.append(page_path)
            
            duration = time.monotonic() - t0
            logger.info(
                "PDF split complete: %d pages in %.2fs (%s each)",
                total_pages, duration,
                f"{duration / total_pages:.3f}s" if total_pages > 0 else "N/A",
            )
            return page_paths
            
        except Exception as exc:
            logger.error("Failed to split PDF %s: %s", source_path, exc)
            raise ConversionError(f"PDF split failed: {exc}") from exc

    def extract_tables_by_page(self, source_path: str) -> dict[int, list[dict]]:
        """Extract tables from each page independently.
        
        Args:
            source_path: Absolute path to the PDF file.
            
        Returns:
            Dict mapping page_number (1-indexed) to list of table dicts.
            Each table dict has keys: page_number, table_index, bbox, headers, rows.
        """
        self._validate_path(source_path)
        
        try:
            import pdfplumber
        except ImportError:
            raise ConversionError("pdfplumber is not installed. Run: pip install pdfplumber")
        
        tables_by_page: dict[int, list[dict]] = {}
        
        try:
            t0 = time.monotonic()
            with pdfplumber.open(source_path) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    page_t0 = time.monotonic()
                    extracted_tables = page.find_tables()
                    page_tables = []
                    
                    for i, table in enumerate(extracted_tables):
                        data = table.extract()
                        if not data:
                            continue
                        
                        bbox = table.bbox
                        headers = [str(h) if h is not None else "" for h in data[0]] if data else []
                        rows = []
                        for row in data[1:] if len(data) > 1 else []:
                            rows.append([str(c) if c is not None else "" for c in row])
                        
                        page_tables.append({
                            "page_number": page_num,
                            "table_index": i,
                            "bbox": {
                                "x0": round(bbox[0], 2),
                                "y0": round(bbox[1], 2),
                                "x1": round(bbox[2], 2),
                                "y1": round(bbox[3], 2),
                            },
                            "headers": headers,
                            "rows": rows,
                        })
                    
                    tables_by_page[page_num] = page_tables
                    page_duration = time.monotonic() - page_t0
                    logger.debug(
                        "  pdfplumber page %d/%d: %d tables (%.3fs)",
                        page_num, len(pdf.pages), len(page_tables), page_duration,
                    )
            
            total_tables = sum(len(t) for t in tables_by_page.values())
            duration = time.monotonic() - t0
            logger.info(
                "pdfplumber table extraction: %d pages, %d tables in %.2fs",
                len(tables_by_page), total_tables, duration,
            )
            return tables_by_page
            
        except Exception as exc:
            logger.error("pdfplumber table extraction failed for %s: %s", source_path, exc)
            raise ConversionError(f"pdfplumber extraction failed: {exc}") from exc

    def extract_tables(self, source_path: str) -> list[dict]:
        """Extract tables from a PDF using pdfplumber (flat list).
        
        This is the original method — returns all tables across all pages
        as a flat list. Preserved for backward compatibility.
        
        Args:
            source_path: Absolute path to the PDF file.
            
        Returns:
            List of dictionaries representing extracted tables.
            
        Raises:
            ConversionError: If pdfplumber fails to process the file.
        """
        # Reason: delegate to the page-by-page method and flatten
        tables_by_page = self.extract_tables_by_page(source_path)
        flat = []
        for page_num in sorted(tables_by_page.keys()):
            flat.extend(tables_by_page[page_num])
        return flat
