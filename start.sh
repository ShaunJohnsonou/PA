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

# 5. Sync configuration and skills into the container
#    Reason: Hermes reads SOUL.md and skills from $HERMES_HOME/.hermes/ 
#    which is /opt/data/.hermes/ inside the container.
echo "📂 Syncing SOUL.md, skills, and config to container..."

# Ensure the .hermes directory exists
docker compose exec -T agentic_layer mkdir -p /opt/data/.hermes

# Copy SOUL.md to the correct Hermes location
docker compose cp ./hermes_config/SOUL.md agentic_layer:/opt/data/.hermes/SOUL.md

# Copy skills directory
docker compose cp ./hermes_config/skills agentic_layer:/opt/data/

# Fix ownership so the hermes user can read everything
docker compose exec -T agentic_layer chown -R hermes:hermes /opt/data/.hermes /opt/data/skills

# 6. Restart the gateway so it picks up the new SOUL.md and MCP config
echo "🔁 Restarting gateway to load updated config..."
docker compose restart agentic_layer

echo ""
echo "✅ Personal Assistant is up and running!"
echo "   - Hermes Gateway: http://localhost:9119"
echo "   - Langfuse:       http://localhost:3000"
echo "   - DB Viewer:      http://localhost:8642"
echo ""
echo "💡 To verify, send this to your agent: './hermes tools list'"
