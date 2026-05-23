"""TR-1.1 — Content-addressed file vault manager.

Manages the vault directory structure where original documents are stored
immutably, addressed by their SHA256 hash.  All filesystem operations for
the vault are centralised here — no other module should use direct
``os.path`` / ``shutil`` calls against vault paths.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class VaultWriteError(Exception):
    """Raised when a file cannot be written to the vault."""


class VaultManager:
    """Content-addressed file vault for document originals and artifacts.

    Directory layout::

        <vault_root>/
          originals/          # immutable originals, sharded by hash
            ab/cd/abcdef...pdf
          extracted/           # per-document extraction artifacts (Phase 2)
          indexes/             # FAISS index + mapping DB (Phase 3)
          hermes_catalog.sqlite
          finance.duckdb
    """

    _SUBDIRS = ("originals", "extracted", "indexes")

    def __init__(self, vault_root: str) -> None:
        """
        Args:
            vault_root: Absolute path to the vault directory.
                        Typically read from ``HERMES_VAULT_PATH``.

        Raises:
            ValueError: If *vault_root* is empty or a relative path.
        """
        if not vault_root:
            raise ValueError("vault_root must not be empty")
        if not os.path.isabs(vault_root):
            raise ValueError(f"vault_root must be absolute, got: {vault_root}")

        # Reason: realpath resolves symlinks so that all downstream
        # comparisons are against the *true* path on disk.
        self.vault_root: str = os.path.realpath(vault_root)
        self._dirs_ensured = False

    # ── Directory management ────────────────────────────────────────

    def ensure_dirs(self) -> None:
        """Create top-level vault directories if they don't exist.

        Called lazily on the first write, not eagerly at startup.
        """
        if self._dirs_ensured:
            return
        for sub in self._SUBDIRS:
            os.makedirs(os.path.join(self.vault_root, sub), exist_ok=True)
        self._dirs_ensured = True
        logger.info("Vault directories ensured at %s", self.vault_root)

    # ── Path helpers ────────────────────────────────────────────────

    def shard_path(self, sha256_hex: str, extension: str) -> str:
        """Return the two-level sharded path for a hash.

        Example::

            shard_path("abcdef12...", ".pdf")
            → "originals/ab/cd/abcdef12....pdf"

        The first two and next two hex characters form the shard
        directories to avoid filesystem performance degradation with
        many files in a single directory.
        """
        if len(sha256_hex) < 4:
            raise ValueError(f"sha256_hex too short: {sha256_hex!r}")
        if not extension.startswith("."):
            extension = f".{extension}"
        return os.path.join(
            "originals",
            sha256_hex[:2],
            sha256_hex[2:4],
            f"{sha256_hex}{extension}",
        )

    def original_exists(self, sha256_hex: str, extension: str) -> bool:
        """Check if an original file with this hash is already stored."""
        rel = self.shard_path(sha256_hex, extension)
        return os.path.isfile(os.path.join(self.vault_root, rel))

    def store_original(self, source_path: str, sha256_hex: str, extension: str) -> str:
        """Atomically copy *source_path* into the vault.

        Steps:
        1. Create shard directories (``originals/ab/cd/``).
        2. Copy to a ``.tmp`` file in the same directory.
        3. ``os.rename()`` the ``.tmp`` to the final filename (atomic on POSIX
           when both are on the same filesystem).

        Args:
            source_path: Path to the source file.
            sha256_hex: SHA256 hex digest of the file.
            extension: File extension including the leading dot.

        Returns:
            The canonical path relative to *vault_root*.

        Raises:
            FileNotFoundError: If *source_path* does not exist.
            VaultWriteError: If the copy or rename fails.
        """
        self.ensure_dirs()

        rel_path = self.shard_path(sha256_hex, extension)
        abs_path = os.path.join(self.vault_root, rel_path)
        target_dir = os.path.dirname(abs_path)

        os.makedirs(target_dir, exist_ok=True)

        # Reason: write to a temp file first, then rename for atomicity.
        # If the process crashes mid-copy, only a .tmp file remains.
        tmp_fd = None
        tmp_path = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=target_dir, suffix=".tmp"
            )
            os.close(tmp_fd)
            tmp_fd = None
            shutil.copy2(source_path, tmp_path)
            os.rename(tmp_path, abs_path)
            tmp_path = None  # rename succeeded, don't clean up
            logger.debug("Stored original: %s", rel_path)
        except Exception as exc:
            # Clean up partial temp file on failure
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise VaultWriteError(f"Failed to store file: {exc}") from exc

        # Reason: make the original read-only to enforce immutability.
        try:
            os.chmod(abs_path, 0o444)
        except OSError:
            pass  # best-effort; may fail on some filesystems

        return rel_path

    def get_original_path(self, sha256_hex: str, extension: str) -> str | None:
        """Return the absolute path if the original exists, else ``None``."""
        rel = self.shard_path(sha256_hex, extension)
        abs_path = os.path.join(self.vault_root, rel)
        return abs_path if os.path.isfile(abs_path) else None

    def get_extraction_dir(self, sha256_hex: str, create: bool = False) -> str:
        """Return the ``extracted/<sha256>/`` directory path.

        Args:
            sha256_hex: The document hash.
            create: If ``True``, create the directory if it doesn't exist.

        Returns:
            Absolute path to the extraction directory.
        """
        path = os.path.join(self.vault_root, "extracted", sha256_hex)
        if create:
            os.makedirs(path, exist_ok=True)
        return path

    @property
    def catalog_path(self) -> str:
        """Absolute path to ``hermes_catalog.sqlite``."""
        return os.path.join(self.vault_root, "hermes_catalog.sqlite")

    # ── Security ────────────────────────────────────────────────────

    def validate_path(self, path: str) -> str:
        """Resolve and validate that *path* is within ``vault_root``.

        Follows symlinks to their final destination to prevent symlink
        traversal attacks.

        Args:
            path: The path to validate.

        Returns:
            The resolved absolute path.

        Raises:
            ValueError: If the resolved path escapes the vault root.
        """
        resolved = os.path.realpath(path)
        if resolved != self.vault_root and not resolved.startswith(
            self.vault_root + os.sep
        ):
            raise ValueError(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                f"vault root '{self.vault_root}'"
            )
        return resolved
