# start.ps1 - Quick start script for the Personal Assistant (Windows)
$ErrorActionPreference = "Stop"

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

# 4. Wait for the container to be healthy before syncing
Write-Host "⏳ Waiting for container to start..." -ForegroundColor Cyan
Start-Sleep -Seconds 5

# 5. Sync configuration and skills into the container
Write-Host "📂 Syncing SOUL.md, skills, and config to container..." -ForegroundColor Cyan

docker compose exec -T agentic_layer mkdir -p /opt/data/.hermes
docker compose cp ./hermes_config/SOUL.md agentic_layer:/opt/data/.hermes/SOUL.md
docker compose cp ./hermes_config/skills agentic_layer:/opt/data/
docker compose exec -T agentic_layer chown -R hermes:hermes /opt/data/.hermes /opt/data/skills

# 6. Restart the gateway so it picks up the new SOUL.md and MCP config
Write-Host "🔁 Restarting gateway to load updated config..." -ForegroundColor Cyan
docker compose restart agentic_layer

Write-Host ""
Write-Host "✅ Personal Assistant is up and running!" -ForegroundColor Green
Write-Host "   - Hermes Gateway: http://localhost:9119" -ForegroundColor Green
Write-Host "   - Langfuse:       http://localhost:3000" -ForegroundColor Green
Write-Host "   - DB Viewer:      http://localhost:8642" -ForegroundColor Green
Write-Host ""
Write-Host "💡 To verify, send this to your agent: './hermes tools list'" -ForegroundColor Yellow
