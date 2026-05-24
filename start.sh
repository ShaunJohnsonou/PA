#!/bin/bash
# start.sh - Quick start script for the Personal Assistant
set -e

echo "🚀 Starting Personal Assistant Deployment..."

# 1. Pull the latest code for the main repository
echo "🔄 Pulling latest code..."
git pull origin main

# 2. Crucial: Update submodules so the hermes-agent custom tools logic is pulled
echo "📦 Updating submodules (hermes-agent)..."
git submodule update --init --recursive

# 3. Spin up the docker containers (building if necessary)
echo "🐳 Starting Docker containers..."
docker compose up -d --build

# 4. Wait for the container to be healthy before syncing
echo "⏳ Waiting for container to start..."
sleep 5

# 5. Sync SOUL.md into /root/.hermes/ (where Hermes actually reads it)
# Reason: The container runs as root, so Hermes reads from /root/.hermes/,
# NOT from /opt/data/.hermes/. Our setup_dev.sh writes MCP config to the
# wrong location. We fix both SOUL.md and config.yaml here.
echo "📂 Syncing SOUL.md to container..."
docker compose cp ./hermes_config/SOUL.md agentic_layer:/root/.hermes/SOUL.md

# 6. Inject MCP server config into the REAL config.yaml at /root/.hermes/
# Reason: setup_dev.sh writes to /opt/data/.hermes/config.yaml but the
# gateway reads /root/.hermes/config.yaml. We append the mcp_servers block
# to the real config if it's missing.
echo "⚙️  Injecting MCP server config..."
docker compose exec -T agentic_layer bash -c '
  CONFIG="/root/.hermes/config.yaml"
  if ! grep -q "^mcp_servers:" "$CONFIG" 2>/dev/null; then
    cat >> "$CONFIG" <<MCPEOF

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
MCPEOF
    echo "  ✅ MCP config injected into $CONFIG"
  else
    echo "  ✅ MCP config already present in $CONFIG"
  fi
'

# 7. Copy skills directory
echo "📚 Syncing skills..."
docker compose cp ./hermes_config/skills agentic_layer:/opt/data/
docker compose exec -T agentic_layer chown -R hermes:hermes /opt/data/skills

# 8. Restart the gateway so it picks up the new SOUL.md and MCP config
echo "🔁 Restarting gateway to load updated config..."
docker compose restart agentic_layer

echo ""
echo "✅ Personal Assistant is up and running!"
echo "   - Hermes Gateway: http://localhost:9119"
echo "   - Langfuse:       http://localhost:3000"
echo "   - DB Viewer:      http://localhost:8642"
echo ""
echo "💡 To verify, send this to your agent:"
echo '   "SYSTEM DIAGNOSTIC REQUEST: list your tools and confirm you are Shaun'\''s personal assistant"'
