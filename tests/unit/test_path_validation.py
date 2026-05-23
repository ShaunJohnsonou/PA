"""Unit tests for TR-5.2 — Path validation.

Tests the VaultManager.validate_path() method with adversarial inputs.
"""
from __future__ import annotations

import os
import pytest
from mcp_servers.document_catalog.vault import VaultManager


@pytest.fixture
def vault(tmp_path):
    """Provide a VaultManager backed by a temp directory."""
    vault_root = str(tmp_path / "vault")
    vm = VaultManager(vault_root)
    vm.ensure_dirs()
    return vm


class TestPathValidation:
    """Tests for VaultManager.validate_path()."""

    def test_valid_path(self, vault, tmp_path):
        """A path within the vault should pass."""
        test_file = os.path.join(vault.vault_root, "originals", "test.pdf")
        # Create the file so realpath works
        os.makedirs(os.path.dirname(test_file), exist_ok=True)
        with open(test_file, "w") as f:
            f.write("test")
        result = vault.validate_path(test_file)
        assert result == os.path.realpath(test_file)

    def test_vault_root_itself(self, vault):
        """The vault root path itself should pass."""
        result = vault.validate_path(vault.vault_root)
        assert result == os.path.realpath(vault.vault_root)

    def test_parent_traversal_blocked(self, vault, tmp_path):
        """Path with ../ traversal should be blocked."""
        # Reason: This is the most common path traversal attack
        malicious = os.path.join(vault.vault_root, "..", "etc", "passwd")
        with pytest.raises(ValueError, match="outside vault root"):
            vault.validate_path(malicious)

    def test_deep_traversal_blocked(self, vault, tmp_path):
        """Deep ../ traversal should be blocked."""
        malicious = os.path.join(
            vault.vault_root, "originals", "..", "..", "..", "etc", "passwd"
        )
        with pytest.raises(ValueError, match="outside vault root"):
            vault.validate_path(malicious)

    def test_absolute_path_outside_vault(self, vault):
        """An absolute path outside the vault should be blocked."""
        with pytest.raises(ValueError, match="outside vault root"):
            vault.validate_path("C:\\Windows\\System32\\cmd.exe")
