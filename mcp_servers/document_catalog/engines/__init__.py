"""Extraction engines — shared data classes.

Provides the common result types used by all extraction engines
(Docling, MarkItDown) and the artifact writer.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PageData:
    """Per-page text and metadata from a structured extraction."""

    page_number: int          # 1-indexed
    text: str
    word_count: int
    has_tables: bool = False
    has_images: bool = False


@dataclass
class ExtractedTable:
    """A single table extracted from a document."""

    page_number: int          # 1-indexed
    table_index: int          # 0-indexed within page
    headers: list[str]
    rows: list[list[str]]     # Each row is a list of cell values
    caption: str | None = None


@dataclass
class ConversionResult:
    """Unified output from any extraction engine."""

    markdown: str
    page_count: int
    char_count: int
    table_count: int = 0
    pages: list[PageData] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engine: str = ""
    engine_version: str = ""
    duration_seconds: float = 0.0


@dataclass
class ConversionMeta:
    """Metadata about a conversion, written to conversion_meta.json."""

    engine: str
    engine_version: str
    duration_seconds: float
    page_count: int
    char_count: int
    table_count: int
    warnings: list[str] = field(default_factory=list)
    extracted_at: str = ""       # ISO 8601
    status: str = "success"      # "success" or "failed"
    error_message: str | None = None
