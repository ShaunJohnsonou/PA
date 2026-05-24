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

echo "✅ Personal Assistant is up and running!"
echo "   - Hermes Agent running on port 3000"
echo "   - MCP Servers running in background"
