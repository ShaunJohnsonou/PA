#!/bin/bash
# start.sh - Quick start script for the Personal Assistant

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

# 4. Sync configuration and skills directly into the container
echo "📂 Syncing configuration and skills to container..."
docker compose cp ./hermes_config/skills agentic_layer:/opt/data/
docker compose cp ./hermes_config/SOUL.md agentic_layer:/opt/data/
docker compose exec -T agentic_layer chown -R hermes:hermes /opt/data/skills /opt/data/SOUL.md

# 5. Enable crucial plugins
echo "🔌 Enabling MCP plugin..."
docker compose exec -T agentic_layer /opt/hermes/.venv/bin/hermes plugins enable mcp

echo "✅ Personal Assistant is up and running!"
echo "   - Hermes Agent running on port 3000"
echo "   - MCP Servers running in background"
