"""pdfplumber-based table extraction engine.

Used as a fallback or specialized extractor for machine-generated
bank statements where exact cell boundaries matter.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

class ConversionError(Exception):
    """Raised when pdfplumber fails to convert a document."""


class PdfPlumberEngine:
    """PDF extraction engine powered by pdfplumber.
    
    Used exclusively for extracting tables from machine-generated
    PDFs (like bank statements) to provide structured data that
    complements Docling's markdown output.
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

    def extract_tables(self, source_path: str) -> list[dict]:
        """Extract tables from a PDF using pdfplumber.
        
        Args:
            source_path: Absolute path to the PDF file.
            
        Returns:
            List of dictionaries representing extracted tables.
            
        Raises:
            ConversionError: If pdfplumber fails to process the file.
        """
        self._validate_path(source_path)
        
        try:
            import pdfplumber
        except ImportError:
            raise ConversionError(
                "pdfplumber is not installed. "
                "Run: pip install pdfplumber"
            )
            
        tables = []
        try:
            with pdfplumber.open(source_path) as pdf:
                for page_num, page in enumerate(pdf.pages, start=1):
                    extracted_tables = page.find_tables()
                    for i, table in enumerate(extracted_tables):
                        # Extract the data as a 2D list of strings
                        data = table.extract()
                        if not data:
                            continue
                            
                        # Extract bounding box (x0, top, x1, bottom)
                        bbox = table.bbox
                        
                        # Assume first row is headers if it exists
                        headers = data[0] if data else []
                        rows = data[1:] if len(data) > 1 else []
                        
                        # Clean up None values that pdfplumber returns for empty cells
                        headers = [str(h) if h is not None else "" for h in headers]
                        cleaned_rows = []
                        for row in rows:
                            cleaned_rows.append([str(c) if c is not None else "" for c in row])
                        
                        tables.append({
                            "page_number": page_num,
                            "table_index": i,
                            "bbox": {
                                "x0": round(bbox[0], 2), 
                                "y0": round(bbox[1], 2), 
                                "x1": round(bbox[2], 2), 
                                "y1": round(bbox[3], 2)
                            },
                            "headers": headers,
                            "rows": cleaned_rows
                        })
            return tables
        except Exception as exc:
            logger.error("pdfplumber extraction failed for %s: %s", source_path, exc)
            raise ConversionError(f"pdfplumber extraction failed: {exc}") from exc
