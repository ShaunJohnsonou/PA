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
# Reason: entrypoint.sh and setup_dev.sh now handle all config syncing
# automatically on container boot (SOUL.md, MCP config, skills).
# No manual docker cp or injection needed.
echo "🐳 Starting Docker containers..."
docker compose up -d --build

echo ""
echo "✅ Personal Assistant is up and running!"
echo "   - Hermes Gateway: http://localhost:9119"
echo "   - Langfuse:       http://localhost:3000"
echo "   - DB Viewer:      http://localhost:8642"
echo ""
echo "💡 To verify, send this to your agent:"
echo '   "SYSTEM DIAGNOSTIC REQUEST: list your tools and confirm you are Shaun'\''s personal assistant"'
