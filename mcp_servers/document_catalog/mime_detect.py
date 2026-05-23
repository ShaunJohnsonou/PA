"""TR-1.4 (partial) — MIME type detection.

Detects MIME types using python-magic with a fallback to extension-based
lookup when libmagic is unavailable.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Reason: python-magic may not be installed or libmagic may be missing.
# We import lazily and fall back to extension-based detection.
try:
    import magic as _magic  # python-magic

    def _detect_via_libmagic(file_path: str) -> str | None:
        """Detect MIME via libmagic. Returns None on failure."""
        try:
            mime = _magic.from_file(file_path, mime=True)
            if mime and mime != "application/octet-stream":
                return mime
        except Exception as exc:
            logger.debug("libmagic detection failed: %s", exc)
        return None

except ImportError:
    logger.info("python-magic not installed; using extension-based MIME detection only")

    def _detect_via_libmagic(file_path: str) -> str | None:  # type: ignore[misc]
        return None


# Extension-based fallback map
_EXTENSION_MAP: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".xml": "application/xml",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
    ".epub": "application/epub+zip",
    ".ofx": "application/x-ofx",
    ".qfx": "application/x-ofx",
}


def detect_mime_type(file_path: str) -> str:
    """Detect the MIME type of a file.

    Strategy:
    1. Try libmagic (python-magic) for content-based detection.
    2. Fall back to file extension mapping.
    3. Default to ``application/octet-stream`` if nothing matches.

    Args:
        file_path: Path to the file.

    Returns:
        MIME type string, e.g. ``"application/pdf"``.
    """
    # Try libmagic first
    mime = _detect_via_libmagic(file_path)
    if mime:
        return mime

    # Fallback: extension-based
    ext = Path(file_path).suffix.lower()
    return _EXTENSION_MAP.get(ext, "application/octet-stream")
