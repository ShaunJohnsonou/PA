"""Atomic artifact writer for document extraction output.

Writes extraction results to the standardised directory structure
under ``extracted/<sha256>/``. Uses atomic directory creation to
prevent partial state on failure.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone

from .engines import ConversionMeta, ConversionResult, ExtractedTable, PageData

logger = logging.getLogger(__name__)


def write_artifacts(
    vault_root: str,
    sha256_hash: str,
    result: ConversionResult,
    *,
    meta_override: ConversionMeta | None = None,
    pdfplumber_tables: list[dict] | None = None,
) -> list[str]:
    """Write extraction artifacts atomically.

    Strategy:
        1. Write all files to ``extracted/<sha256>.tmp/``
        2. If the target directory already exists (re-extraction),
           rename it to ``<sha256>.bak/``
        3. Rename ``<sha256>.tmp/`` → ``<sha256>/``
        4. Delete the ``.bak/`` directory

    Files produced:
        - ``document.md``           — full Markdown conversion (always)
        - ``pages.json``            — per-page text + metadata (if available)
        - ``tables.json``           — structured table data (if available)
        - ``tables_pdfplumber.json``— pdfplumber table data (if available)
        - ``conversion_meta.json``  — engine info, timing, stats (always)

    Args:
        vault_root: Absolute path to the vault.
        sha256_hash: Document hash (used as directory name).
        result: The ConversionResult from an extraction engine.
        meta_override: Optional custom ConversionMeta. If None, one is
                       built from the result.
        pdfplumber_tables: Optional list of dicts from PdfPlumberEngine.

    Returns:
        List of artifact filenames written.

    Raises:
        OSError: If directory creation or rename fails.
    """
    extracted_root = os.path.join(vault_root, "extracted")
    final_dir = os.path.join(extracted_root, sha256_hash)
    tmp_dir = final_dir + ".tmp"
    bak_dir = final_dir + ".bak"

    # Reason: clean up any leftover temp dir from a previous failed write
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)

    os.makedirs(tmp_dir, exist_ok=True)
    written: list[str] = []

    try:
        # ── document.md (always) ────────────────────────────────
        md_path = os.path.join(tmp_dir, "document.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(result.markdown)
        written.append("document.md")

        # ── pages.json (if per-page data available) ─────────────
        if result.pages:
            pages_path = os.path.join(tmp_dir, "pages.json")
            pages_data = [asdict(p) for p in result.pages]
            with open(pages_path, "w", encoding="utf-8") as f:
                json.dump(pages_data, f, indent=2, ensure_ascii=False)
            written.append("pages.json")

        # ── tables.json (if tables extracted) ───────────────────
        if result.tables:
            tables_path = os.path.join(tmp_dir, "tables.json")
            tables_data = [asdict(t) for t in result.tables]
            with open(tables_path, "w", encoding="utf-8") as f:
                json.dump(tables_data, f, indent=2, ensure_ascii=False)
            written.append("tables.json")

        # ── tables_pdfplumber.json (if available) ───────────────
        if pdfplumber_tables:
            tables_path = os.path.join(tmp_dir, "tables_pdfplumber.json")
            with open(tables_path, "w", encoding="utf-8") as f:
                json.dump(pdfplumber_tables, f, indent=2, ensure_ascii=False)
            written.append("tables_pdfplumber.json")

        # ── conversion_meta.json (always) ───────────────────────
        if meta_override:
            meta = meta_override
        else:
            meta = ConversionMeta(
                engine=result.engine,
                engine_version=result.engine_version,
                duration_seconds=result.duration_seconds,
                page_count=result.page_count,
                char_count=result.char_count,
                table_count=result.table_count,
                warnings=result.warnings,
                extracted_at=datetime.now(timezone.utc).isoformat(),
                status="success",
            )

        meta_path = os.path.join(tmp_dir, "conversion_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(asdict(meta), f, indent=2, ensure_ascii=False)
        written.append("conversion_meta.json")

        # ── Atomic swap ─────────────────────────────────────────
        if os.path.exists(final_dir):
            # Reason: back up the old artifacts before replacing
            if os.path.exists(bak_dir):
                shutil.rmtree(bak_dir)
            os.rename(final_dir, bak_dir)

        os.rename(tmp_dir, final_dir)

        # Reason: clean up backup after successful swap
        if os.path.exists(bak_dir):
            shutil.rmtree(bak_dir)

        logger.info(
            "Wrote %d artifacts to extracted/%s/ (%d bytes total)",
            len(written),
            sha256_hash[:12],
            sum(
                os.path.getsize(os.path.join(final_dir, f))
                for f in written
            ),
        )

    except Exception:
        # Reason: clean up the temp directory on any failure
        # so we don't leave partial state
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return written


def write_failed_meta(
    vault_root: str,
    sha256_hash: str,
    engine: str,
    error_message: str,
) -> None:
    """Write a conversion_meta.json recording a failed extraction.

    This ensures we have an audit trail even when extraction fails.
    """
    extracted_root = os.path.join(vault_root, "extracted")
    target_dir = os.path.join(extracted_root, sha256_hash)
    os.makedirs(target_dir, exist_ok=True)

    meta = ConversionMeta(
        engine=engine,
        engine_version="",
        duration_seconds=0.0,
        page_count=0,
        char_count=0,
        table_count=0,
        extracted_at=datetime.now(timezone.utc).isoformat(),
        status="failed",
        error_message=error_message,
    )

    meta_path = os.path.join(target_dir, "conversion_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(asdict(meta), f, indent=2, ensure_ascii=False)

    logger.warning(
        "Wrote failed conversion_meta for %s: %s",
        sha256_hash[:12],
        error_message,
    )
