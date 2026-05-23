"""Unit tests for TR-1.1 — VaultManager."""
import os
import tempfile

import pytest

from mcp_servers.document_catalog.vault import VaultManager, VaultWriteError


@pytest.fixture
def vault(tmp_path):
    """Provide a VaultManager in a temp directory."""
    return VaultManager(str(tmp_path))


class TestVaultInit:
    def test_requires_absolute_path(self):
        with pytest.raises(ValueError, match="absolute"):
            VaultManager("relative/path")

    def test_requires_non_empty(self):
        with pytest.raises(ValueError, match="empty"):
            VaultManager("")

    def test_stores_realpath(self, tmp_path):
        vm = VaultManager(str(tmp_path))
        assert vm.vault_root == os.path.realpath(str(tmp_path))


class TestEnsureDirs:
    def test_creates_subdirectories(self, vault):
        vault.ensure_dirs()
        for sub in ("originals", "extracted", "indexes"):
            assert os.path.isdir(os.path.join(vault.vault_root, sub))

    def test_idempotent(self, vault):
        vault.ensure_dirs()
        vault.ensure_dirs()  # should not raise


class TestShardPath:
    def test_format(self, vault):
        result = vault.shard_path("abcdef1234567890" * 4, ".pdf")
        # Reason: os.path.join uses OS-specific separators
        assert "originals" in result
        assert result.endswith(".pdf")
        # Verify two-level sharding (ab/cd/)
        parts = result.replace("\\", "/").split("/")
        assert parts[0] == "originals"
        assert parts[1] == "ab"
        assert parts[2] == "cd"

    def test_dot_prefix_added(self, vault):
        result = vault.shard_path("abcdef1234567890" * 4, "pdf")
        assert result.endswith(".pdf")

    def test_short_hash_rejected(self, vault):
        with pytest.raises(ValueError, match="too short"):
            vault.shard_path("ab", ".pdf")


class TestStoreOriginal:
    def test_stores_file(self, vault, tmp_path):
        src = tmp_path / "test.pdf"
        src.write_bytes(b"PDF content")

        sha = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        rel = vault.store_original(str(src), sha, ".pdf")

        abs_path = os.path.join(vault.vault_root, rel)
        assert os.path.isfile(abs_path)
        with open(abs_path, "rb") as f:
            assert f.read() == b"PDF content"

    def test_returns_relative_path(self, vault, tmp_path):
        src = tmp_path / "test.txt"
        src.write_bytes(b"hello")

        sha = "0123456789abcdef" * 4
        rel = vault.store_original(str(src), sha, ".txt")

        assert not os.path.isabs(rel)
        assert "originals" in rel

    def test_missing_source_raises(self, vault):
        with pytest.raises((FileNotFoundError, VaultWriteError)):
            vault.store_original("/nonexistent/file.pdf", "abc" * 22, ".pdf")


class TestOriginalExists:
    def test_exists_after_store(self, vault, tmp_path):
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"data")
        sha = "aabbccdd" * 8
        vault.store_original(str(src), sha, ".pdf")

        assert vault.original_exists(sha, ".pdf") is True

    def test_not_exists(self, vault):
        assert vault.original_exists("0000" * 16, ".pdf") is False


class TestGetOriginalPath:
    def test_returns_abs_path(self, vault, tmp_path):
        src = tmp_path / "doc.pdf"
        src.write_bytes(b"data")
        sha = "aabbccdd" * 8
        vault.store_original(str(src), sha, ".pdf")

        result = vault.get_original_path(sha, ".pdf")
        assert result is not None
        assert os.path.isabs(result)
        assert os.path.isfile(result)

    def test_returns_none_for_missing(self, vault):
        assert vault.get_original_path("0000" * 16, ".pdf") is None


class TestValidatePath:
    def test_valid_path(self, vault):
        valid = os.path.join(vault.vault_root, "originals", "test.pdf")
        assert vault.validate_path(valid) == os.path.realpath(valid)

    def test_vault_root_itself(self, vault):
        assert vault.validate_path(vault.vault_root) == vault.vault_root

    def test_parent_traversal_blocked(self, vault):
        with pytest.raises(ValueError, match="outside"):
            vault.validate_path(os.path.join(vault.vault_root, "..", "etc", "passwd"))

    def test_deep_traversal_blocked(self, vault):
        with pytest.raises(ValueError, match="outside"):
            vault.validate_path(
                os.path.join(vault.vault_root, "originals", "..", "..", "etc", "passwd")
            )
