"""MarkItDown-based extraction engine for non-PDF formats.

Uses Microsoft's MarkItDown library to convert general documents
(Word, Excel, PowerPoint, HTML, CSV, images, etc.) to Markdown.
This engine handles everything that Docling cannot.
"""
from __future__ import annotations

import logging
import math
import os
import time

from . import ConversionResult

logger = logging.getLogger(__name__)


class ConversionError(Exception):
    """Raised when MarkItDown fails to convert a document."""


class MarkItDownEngine:
    """General-purpose document-to-Markdown engine.

    Wraps Microsoft's MarkItDown library. Used for all non-PDF formats:
    Word (.docx), Excel (.xlsx), PowerPoint (.pptx), HTML, CSV, JSON,
    XML, plain text, images, and ZIP/EPUB archives.

    For PDFs, use DoclingEngine instead — it provides superior
    table and layout extraction.
    """

    SUPPORTED_MIMES = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "text/html",
        "text/csv",
        "text/plain",
        "text/markdown",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/json",
        "application/xml",
        "text/xml",
        "application/zip",
        "application/epub+zip",
    }

    def __init__(self, vault_root: str) -> None:
        """
        Args:
            vault_root: Absolute path to vault, used for path restriction.
        """
        self._vault_root = vault_root
        self._converter = None  # Lazy init

    def _get_converter(self):
        """Lazy-initialise the MarkItDown converter."""
        if self._converter is None:
            try:
                from markitdown import MarkItDown
                self._converter = MarkItDown()
                logger.info("MarkItDown converter initialised")
            except ImportError:
                raise ConversionError(
                    "markitdown is not installed. "
                    "Run: pip install markitdown"
                )
        return self._converter

    def can_handle(self, mime_type: str) -> bool:
        """Return True if this engine supports the given MIME type."""
        return mime_type in self.SUPPORTED_MIMES

    def convert(self, source_path: str) -> ConversionResult:
        """Convert a non-PDF document to Markdown.

        Args:
            source_path: Absolute path to the source file.
                         Must be within the vault root.

        Returns:
            ConversionResult with markdown text and metadata.

        Raises:
            ValueError: If source_path is outside the vault.
            ConversionError: If MarkItDown fails to convert.
        """
        self._validate_path(source_path)
        converter = self._get_converter()

        t0 = time.monotonic()

        try:
            result = converter.convert(source_path)
            markdown = result.text_content or ""
        except Exception as exc:
            raise ConversionError(
                f"MarkItDown conversion failed for {source_path}: {exc}"
            ) from exc

        duration = time.monotonic() - t0
        char_count = len(markdown)

        # Reason: estimate page count from content length.
        # Page break markers (\f or \n---\n) are preferred but rare.
        page_breaks = markdown.count("\f") + markdown.count("\n---\n")
        if page_breaks > 0:
            page_count = page_breaks + 1
        else:
            page_count = max(1, math.ceil(char_count / 3000))

        # Count markdown tables (lines starting with |)
        table_lines = [l for l in markdown.split("\n") if l.strip().startswith("|")]
        # Rough estimate: groups of consecutive pipe-lines are tables
        table_count = 0
        in_table = False
        for line in markdown.split("\n"):
            stripped = line.strip()
            if stripped.startswith("|") and not in_table:
                table_count += 1
                in_table = True
            elif not stripped.startswith("|"):
                in_table = False

        # Get markitdown version
        try:
            import markitdown
            engine_version = getattr(markitdown, "__version__", "unknown")
        except Exception:
            engine_version = "unknown"

        logger.info(
            "MarkItDown converted %s: ~%d pages, %d chars, ~%d tables in %.1fs",
            source_path, page_count, char_count, table_count, duration,
        )

        return ConversionResult(
            markdown=markdown,
            page_count=page_count,
            char_count=char_count,
            table_count=table_count,
            engine="markitdown",
            engine_version=engine_version,
            duration_seconds=duration,
            # Reason: MarkItDown does not produce per-page data or structured
            # tables natively. Pages and tables remain empty — the get_page
            # handler will fall back to splitting markdown by markers.
        )

    def _validate_path(self, path: str) -> None:
        """Ensure path is within the vault root."""
        resolved = os.path.realpath(path)
        vault_real = os.path.realpath(self._vault_root)
        if not resolved.startswith(vault_real + os.sep) and resolved != vault_real:
            raise ValueError(
                f"Path '{path}' is outside vault root '{self._vault_root}'"
            )
