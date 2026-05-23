"""Document chunking pipeline.

Splits extracted Markdown into overlapping text chunks with metadata.
Chunks are the atomic unit for embedding and search indexing.
"""
from __future__ import annotations

import logging
import math
import re
import uuid
from datetime import datetime, timezone

from . import ChunkRecord

logger = logging.getLogger(__name__)

# Reason: deterministic namespace for UUID v5 generation
# so that re-chunking the same document produces identical chunk IDs.
_CHUNK_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def chunk_document(
    document_id: str,
    markdown: str,
    pages: list[dict] | None = None,
    *,
    target_tokens: int = 512,
    overlap_tokens: int = 64,
    max_tokens: int = 1024,
) -> list[ChunkRecord]:
    """Split a Markdown document into overlapping chunks.

    Strategy:
        1. Split by double-newline (paragraph boundaries)
        2. Identify table blocks and keep them atomic
        3. Merge short consecutive paragraphs up to target_tokens
        4. Split paragraphs exceeding max_tokens at sentence boundaries
        5. Apply overlap: last overlap_tokens of chunk[i] prepended to chunk[i+1]
        6. Propagate the most recent Markdown heading as section_title

    Args:
        document_id: UUID of the source document.
        markdown: Full extracted Markdown text.
        pages: Optional per-page data (list of dicts with page_number, text).
        target_tokens: Target chunk size in approximate tokens.
        overlap_tokens: Number of tokens to overlap between chunks.
        max_tokens: Hard cap on chunk size.

    Returns:
        List of ChunkRecord objects, deterministically ordered.
    """
    if not markdown or not markdown.strip():
        return []

    # Build a page-number lookup if page data is available
    page_map = _build_page_map(pages) if pages else None

    # Split into paragraphs
    paragraphs = _split_paragraphs(markdown)

    # Process paragraphs: track headings, keep tables atomic
    blocks: list[_Block] = []
    current_heading: str | None = None

    for para in paragraphs:
        # Track heading changes
        heading = _extract_heading(para)
        if heading is not None:
            current_heading = heading

        if _is_table_block(para):
            # Reason: tables are kept as atomic units, never split
            blocks.append(_Block(
                text=para,
                section_title=current_heading,
                is_table=True,
            ))
        else:
            blocks.append(_Block(
                text=para,
                section_title=current_heading,
                is_table=False,
            ))

    # Merge small blocks and split large ones
    merged = _merge_and_split(blocks, target_tokens, max_tokens)

    # Apply overlap
    chunks = _apply_overlap(merged, overlap_tokens)

    # Build ChunkRecords
    now = datetime.now(timezone.utc).isoformat()
    records: list[ChunkRecord] = []

    for i, chunk in enumerate(chunks):
        chunk_id = str(uuid.uuid5(_CHUNK_NAMESPACE, f"{document_id}:{i}"))
        page_num = _find_page_number(chunk.text, page_map) if page_map else None
        token_count = _estimate_tokens(chunk.text)

        records.append(ChunkRecord(
            chunk_id=chunk_id,
            document_id=document_id,
            chunk_index=i,
            page_number=page_num,
            section_title=chunk.section_title,
            text=chunk.text,
            char_count=len(chunk.text),
            token_count=token_count,
            created_at=now,
        ))

    logger.info(
        "Chunked document %s: %d chunks (target=%d, overlap=%d)",
        document_id[:8], len(records), target_tokens, overlap_tokens,
    )

    return records


# ── Internal types ──────────────────────────────────────────────


class _Block:
    """Internal intermediate representation of a text block."""

    __slots__ = ("text", "section_title", "is_table")

    def __init__(self, text: str, section_title: str | None, is_table: bool):
        self.text = text
        self.section_title = section_title
        self.is_table = is_table


# ── Helper functions ────────────────────────────────────────────


def _estimate_tokens(text: str) -> int:
    """Fast token count approximation.

    Calibrated against tiktoken cl100k_base: word_count * 1.3
    is within ±10% for English text.
    """
    return int(len(text.split()) * 1.3)


def _split_paragraphs(markdown: str) -> list[str]:
    """Split markdown by double newlines, preserving table blocks."""
    # Reason: split on blank lines but preserve tables as single blocks
    raw = re.split(r"\n\s*\n", markdown)
    return [p.strip() for p in raw if p.strip()]


def _extract_heading(paragraph: str) -> str | None:
    """Extract Markdown heading (h1-h3) from a paragraph.

    Returns the heading text if found, None otherwise.
    Only tracks h1-h3 (#, ##, ###). Deeper headings are ignored.
    """
    match = re.match(r"^(#{1,3})\s+(.+)$", paragraph.strip(), re.MULTILINE)
    if match:
        return match.group(2).strip()
    return None


def _is_table_block(paragraph: str) -> bool:
    """Return True if the paragraph is a Markdown pipe table.

    Detected by: every non-empty line contains '|' and at least
    one line matches the separator pattern (e.g., |---|---|).
    """
    lines = [l for l in paragraph.split("\n") if l.strip()]
    if len(lines) < 2:
        return False

    all_have_pipes = all("|" in l for l in lines)
    has_separator = any(
        re.match(r"^\s*\|[\s\-:|]+\|\s*$", l)
        for l in lines
    )
    return all_have_pipes and has_separator


def _merge_and_split(
    blocks: list[_Block], target_tokens: int, max_tokens: int
) -> list[_Block]:
    """Merge small consecutive blocks and split oversized ones."""
    result: list[_Block] = []

    current_text_parts: list[str] = []
    current_tokens = 0
    current_heading: str | None = None

    def flush():
        nonlocal current_text_parts, current_tokens, current_heading
        if current_text_parts:
            merged_text = "\n\n".join(current_text_parts)
            result.append(_Block(merged_text, current_heading, False))
            current_text_parts = []
            current_tokens = 0

    for block in blocks:
        block_tokens = _estimate_tokens(block.text)

        # Tables are always kept as separate chunks
        if block.is_table:
            flush()
            result.append(block)
            current_heading = block.section_title
            continue

        # Update heading context
        if block.section_title:
            current_heading = block.section_title

        # If this block alone exceeds max, split it at sentence/word boundaries
        if block_tokens > max_tokens:
            flush()
            sentences = _split_sentences(block.text)

            # Reason: if sentence splitting produced only 1 chunk (no sentence
            # boundaries found, e.g. "word word word..."), fall back to
            # splitting by words directly.
            if len(sentences) <= 1:
                sentences = block.text.split()

            sub_parts: list[str] = []
            sub_tokens = 0

            for sent in sentences:
                sent_tokens = _estimate_tokens(sent)
                if sub_tokens + sent_tokens > target_tokens and sub_parts:
                    result.append(_Block(
                        " ".join(sub_parts), current_heading, False
                    ))
                    sub_parts = []
                    sub_tokens = 0
                sub_parts.append(sent)
                sub_tokens += sent_tokens

            if sub_parts:
                result.append(_Block(
                    " ".join(sub_parts), current_heading, False
                ))
            continue

        # If adding this block would exceed target, flush first
        if current_tokens + block_tokens > target_tokens and current_text_parts:
            flush()

        current_text_parts.append(block.text)
        current_tokens += block_tokens
        if block.section_title:
            current_heading = block.section_title

    flush()
    return result


def _apply_overlap(blocks: list[_Block], overlap_tokens: int) -> list[_Block]:
    """Prepend the last overlap_tokens of chunk[i] to chunk[i+1]."""
    if not blocks or overlap_tokens <= 0:
        return blocks

    result = [blocks[0]]

    for i in range(1, len(blocks)):
        prev_text = blocks[i - 1].text
        prev_words = prev_text.split()

        # Reason: convert overlap tokens back to approximate word count
        overlap_words = int(overlap_tokens / 1.3)
        if overlap_words > 0 and len(prev_words) > overlap_words:
            overlap_text = " ".join(prev_words[-overlap_words:])
            new_text = overlap_text + "\n\n" + blocks[i].text
        else:
            new_text = blocks[i].text

        result.append(_Block(new_text, blocks[i].section_title, blocks[i].is_table))

    return result


def _split_sentences(text: str) -> list[str]:
    """Split text at sentence boundaries."""
    # Reason: simple regex-based splitting works well for financial text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]


def _build_page_map(pages: list[dict]) -> dict[str, int]:
    """Build a map of text snippet → page number for page attribution."""
    page_map: dict[str, int] = {}
    for page in pages:
        text = page.get("text", "")
        page_num = page.get("page_number", 0)
        # Reason: use first 100 chars of each page as a lookup key
        if text and page_num:
            key = text[:100].strip()
            if key:
                page_map[key] = page_num
    return page_map


def _find_page_number(chunk_text: str, page_map: dict[str, int]) -> int | None:
    """Find which page a chunk belongs to by matching its start text."""
    chunk_start = chunk_text[:100].strip()
    for key, page_num in page_map.items():
        if key in chunk_start or chunk_start in key:
            return page_num
    return None
