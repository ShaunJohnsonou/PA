# start.ps1 - Quick start script for the Personal Assistant on Windows

Write-Host "🚀 Starting Personal Assistant Deployment..." -ForegroundColor Cyan

# 1. Pull the latest code for the main repository
Write-Host "🔄 Pulling latest code..." -ForegroundColor Cyan
git pull origin main

# 2. Crucial: Update submodules so the hermes-agent custom tools logic is pulled
Write-Host "📦 Updating submodules (hermes-agent)..." -ForegroundColor Cyan
git submodule update --init --recursive

# 3. Spin up the docker containers (building if necessary)
Write-Host "🐳 Starting Docker containers..." -ForegroundColor Cyan
docker compose up -d --build

# 4. Sync configuration and skills directly into the container
Write-Host "📂 Syncing configuration and skills to container..." -ForegroundColor Cyan
docker compose cp ./hermes_config/skills agentic_layer:/opt/data/
docker compose cp ./hermes_config/SOUL.md agentic_layer:/opt/data/

# Fix permissions inside the container (using wsl/bash style execution)
docker compose exec -T agentic_layer chown -R hermes:hermes /opt/data/skills /opt/data/SOUL.md

Write-Host "✅ Personal Assistant is up and running!" -ForegroundColor Green
Write-Host "   - Hermes Agent running on port 3000" -ForegroundColor Green
Write-Host "   - MCP Servers running in background" -ForegroundColor Green
