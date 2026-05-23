#!/bin/bash
# ──────────────────────────────────────────────
#  PA Dev Setup — Automatically run after docker-compose up
#  Installs dependencies for MCP servers and registers them
#  with Hermes so they are ready to use immediately.
# ──────────────────────────────────────────────

set -e

echo "🚀 Running PA Document Intelligence setup..."

# 1. Install dependencies for the local python environment
# Reason: Skip pip install if we are running inside the Docker container 
# where the dependencies were already installed during `docker build`.
if [ -f "/.dockerenv" ] || /opt/hermes/.venv/bin/python -c "import docling, markitdown" 2>/dev/null; then
    echo "✅ Dependencies already installed. Skipping pip install."
else
    echo "📦 Installing MCP Server dependencies..."
    # Install the document_catalog package in editable mode
    /opt/hermes/.venv/bin/pip install -e /opt/hermes/mcp_servers || {
        # Fallback if the pip install -e fails due to pyproject.toml mapping issues
        echo "⚠️  Editable install failed, installing dependencies manually..."
        /opt/hermes/.venv/bin/pip install mcp python-magic docling markitdown faiss-cpu numpy openai
    }
fi

# 2. Register Document Catalog MCP Server
# Instead of using the CLI which requires user interaction, we write the config directly.
HERMES_CONFIG_DIR="${HERMES_HOME:-/opt/data}/.hermes"
HERMES_CONFIG_FILE="$HERMES_CONFIG_DIR/config.yaml"

mkdir -p "$HERMES_CONFIG_DIR"

echo "⚙️  Configuring MCP Servers in $HERMES_CONFIG_FILE..."

# Reason: Write environment variables to a dedicated .env file because the Hermes gateway
# explicitly strips environment variables from the MCP child process.
# We also map the HERMES_LANGFUSE_* variables to standard LANGFUSE_* variables so the 
# langfuse.openai drop-in wrapper can automatically pick them up and trace embeddings.
cat << EOF > "/hermes-vault/.env"
AZURE_API_KEY=${AZURE_API_KEY:-}
AZURE_API_BASE=${AZURE_API_BASE:-}
AZURE_EMBEDDING_API_BASE=${AZURE_EMBEDDING_API_BASE:-}
AZURE_API_VERSION=${AZURE_API_VERSION:-2024-12-01-preview}
AZURE_EMBEDDING_DEPLOYMENT=${AZURE_EMBEDDING_DEPLOYMENT:-text-embedding-3-large}
LANGFUSE_PUBLIC_KEY=${HERMES_LANGFUSE_PUBLIC_KEY:-}
LANGFUSE_SECRET_KEY=${HERMES_LANGFUSE_SECRET_KEY:-}
LANGFUSE_HOST=${HERMES_LANGFUSE_BASE_URL:-}
EOF
echo "✅ Wrote environment variables to /hermes-vault/.env"

# Reason: the MCP server runs as a child process, so we need to
# explicitly forward the Azure credentials for embedding generation.
HERMES_CONFIG_FILE="$HERMES_CONFIG_DIR/config.yaml"

# Reason: the MCP server runs as a child process, so we need to
# explicitly forward the Azure credentials for embedding generation.
# We append to the config.yaml so we don't overwrite other Hermes settings.
touch "$HERMES_CONFIG_FILE"

# Remove any existing mcp_servers block to prevent duplicates
sed -i '/^mcp_servers:/,$d' "$HERMES_CONFIG_FILE"

cat << EOF >> "$HERMES_CONFIG_FILE"
mcp_servers:
  document_catalog:
    command: "/opt/hermes/.venv/bin/python"
    args: ["-m", "mcp_servers.document_catalog.server"]
    env:
      HERMES_VAULT_PATH: "/hermes-vault"
      PYTHONPATH: "/opt/hermes"
      AZURE_API_KEY: "${AZURE_API_KEY:-}"
      AZURE_API_BASE: "${AZURE_API_BASE:-}"
      AZURE_EMBEDDING_API_BASE: "${AZURE_EMBEDDING_API_BASE:-}"
      AZURE_API_VERSION: "${AZURE_API_VERSION:-2024-12-01-preview}"
      AZURE_EMBEDDING_DEPLOYMENT: "${AZURE_EMBEDDING_DEPLOYMENT:-text-embedding-3-large}"
EOF

echo "✅ Setup complete. MCP Server 'document_catalog' is ready to use!"
