"""Unit tests for TR-1.2 — Streaming SHA256 hash service."""
import hashlib
import os
import tempfile

import pytest

from mcp_servers.document_catalog.hashing import compute_sha256


class TestComputeSha256:
    """Tests for the compute_sha256 function."""

    def test_known_hash(self, tmp_path):
        """Hash of known content matches pre-computed digest."""
        content = b"Hello, Hermes vault!"
        expected = hashlib.sha256(content).hexdigest()

        file = tmp_path / "test.txt"
        file.write_bytes(content)

        assert compute_sha256(str(file)) == expected

    def test_empty_file(self, tmp_path):
        """Hash of empty file matches SHA256 of empty string."""
        expected = hashlib.sha256(b"").hexdigest()

        file = tmp_path / "empty.txt"
        file.write_bytes(b"")

        assert compute_sha256(str(file)) == expected

    def test_hash_is_lowercase_hex(self, tmp_path):
        """Result is 64-character lowercase hex string."""
        file = tmp_path / "test.bin"
        file.write_bytes(b"\x00\x01\x02")

        result = compute_sha256(str(file))
        assert len(result) == 64
        assert result == result.lower()
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self, tmp_path):
        """Same content always produces the same hash."""
        content = b"deterministic test content"
        file = tmp_path / "test.bin"
        file.write_bytes(content)

        hash1 = compute_sha256(str(file))
        hash2 = compute_sha256(str(file))
        assert hash1 == hash2

    def test_different_content_different_hash(self, tmp_path):
        """Different content produces different hashes."""
        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        file_a.write_bytes(b"content A")
        file_b.write_bytes(b"content B")

        assert compute_sha256(str(file_a)) != compute_sha256(str(file_b))

    def test_file_not_found(self):
        """Missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            compute_sha256("/nonexistent/path/to/file.txt")

    def test_large_file_streaming(self, tmp_path):
        """Large file can be hashed without excessive memory."""
        # Create a 1 MB file (not 100 MB to keep tests fast)
        file = tmp_path / "large.bin"
        chunk = b"\xAB" * 65536  # 64 KB
        with open(file, "wb") as f:
            for _ in range(16):  # 16 × 64 KB = 1 MB
                f.write(chunk)

        result = compute_sha256(str(file))
        assert len(result) == 64
