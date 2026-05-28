#!/bin/bash
# ──────────────────────────────────────────────────────────────
# reset_data.sh — Wipe all document catalog data and start fresh
#
# Usage:  bash scripts/reset_data.sh
#
# This script:
#   1. Stops the running containers
#   2. Deletes the SQLite catalog (document metadata)
#   3. Deletes the DuckDB ledger (accounts, transactions)
#   4. Deletes all stored documents and extraction artifacts
#   5. Deletes search indexes
#   6. Restarts the containers
#
# The server will recreate empty databases on next startup.
# ──────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VAULT_DIR="$PROJECT_DIR/vault"

echo ""
echo "⚠️  WARNING: This will DELETE all data:"
echo "   • SQLite catalog (document metadata)"
echo "   • DuckDB financial ledger (accounts, transactions)"
echo "   • All stored documents (originals + extracted)"
echo "   • Search indexes (FAISS + FTS)"
echo "   Vault path: $VAULT_DIR"
echo ""
read -p "Are you sure? Type 'yes' to continue: " confirm

if [ "$confirm" != "yes" ]; then
    echo "❌ Aborted."
    exit 1
fi

echo ""
echo "🛑 Stopping containers..."
cd "$PROJECT_DIR"
docker compose down 2>/dev/null || true

echo ""
echo "🗑️  Deleting databases..."
rm -f  "$VAULT_DIR/hermes_catalog.sqlite"
rm -f  "$VAULT_DIR/hermes_catalog.sqlite-wal"
rm -f  "$VAULT_DIR/hermes_catalog.sqlite-shm"
echo "   ✓ Catalog database removed"

rm -f  "$VAULT_DIR/finance.duckdb"
rm -f  "$VAULT_DIR/finance.duckdb.wal"
echo "   ✓ Financial ledger removed"

echo "🗑️  Deleting stored documents..."
rm -rf "$VAULT_DIR/originals"
echo "   ✓ Original documents removed"

rm -rf "$VAULT_DIR/extracted"
echo "   ✓ Extraction artifacts removed"

echo "🗑️  Deleting search indexes..."
rm -rf "$VAULT_DIR/indexes"
echo "   ✓ Search indexes removed"

echo ""
echo "✅ All data wiped."
echo ""
echo "🚀 Starting fresh..."
bash ./start.sh
