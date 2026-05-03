# ──────────────────────────────────────────────
#  sync_host_hermes_to_container.ps1
#
#  Copies the full Hermes runtime state from the
#  WSL global install (~/.hermes) into the
#  running agentic_layer Docker container.
#
#  Runs from PowerShell on the Windows host.
#  Uses 'wsl' to read files, 'docker cp' to push.
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File .\scripts\sync_host_hermes_to_container.ps1
#    powershell -ExecutionPolicy Bypass -File .\scripts\sync_host_hermes_to_container.ps1 -Mode skills
#    powershell -ExecutionPolicy Bypass -File .\scripts\sync_host_hermes_to_container.ps1 -Mode config
#    powershell -ExecutionPolicy Bypass -File .\scripts\sync_host_hermes_to_container.ps1 -Mode memories
# ──────────────────────────────────────────────

param(
    [ValidateSet("all", "skills", "config", "memories")]
    [string]$Mode = "all"
)

$ErrorActionPreference = "Stop"

# ── Config ────────────────────────────────────
$ContainerName = "n8n-agentic-layer"
$ContainerData = "/opt/data"
$StageDir      = [System.IO.Path]::GetFullPath((Join-Path $env:TEMP "_hermes_sync_staging"))
# Resolve 8.3 short names (e.g. SHAUNJ~1) to full path so WSL can read it
if (Test-Path (Split-Path $StageDir)) {
    $StageDir = (Get-Item (Split-Path $StageDir)).FullName + "\_hermes_sync_staging"
}

# ── Helpers ───────────────────────────────────
function Write-Step  ($msg) { Write-Host "[..] $msg" -ForegroundColor Cyan }
function Write-OK    ($msg) { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Warn  ($msg) { Write-Host "[!!] $msg" -ForegroundColor Yellow }
function Write-Fail  ($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }

# ── Preflight ─────────────────────────────────
$hermesCheck = wsl -e bash -c "test -d ~/.hermes && echo yes || echo no"
if ($hermesCheck.Trim() -ne "yes") { Write-Fail "WSL ~/.hermes not found" }

$containerCheck = docker inspect $ContainerName 2>$null
if (-not $containerCheck) { Write-Fail "Container '$ContainerName' is not running. Start with: docker compose up -d agentic_layer" }

Write-Host ""
Write-Host "=========================================" -ForegroundColor Magenta
Write-Host "  Hermes WSL -> Container Sync" -ForegroundColor Magenta
Write-Host "  Source: WSL:~/.hermes" -ForegroundColor Magenta
Write-Host "  Target: $ContainerName`:$ContainerData" -ForegroundColor Magenta
Write-Host "=========================================" -ForegroundColor Magenta
Write-Host ""

# ── Stage from WSL ────────────────────────────
Write-Step "Staging files from WSL to $StageDir..."

if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

# Convert Windows path to WSL path using wslpath (handles 8.3 names, spaces, etc.)
$wslStageDir = (wsl -e wslpath -u $StageDir).Trim()

wsl -e bash -c "set -e; SRC=`$HOME/.hermes; DST='$wslStageDir'; [ -d `$SRC/skills ] && cp -r `$SRC/skills `$DST/; [ -d `$SRC/memories ] && cp -r `$SRC/memories `$DST/; [ -d `$SRC/sessions ] && cp -r `$SRC/sessions `$DST/; [ -d `$SRC/cron ] && cp -r `$SRC/cron `$DST/; [ -d `$SRC/plugins ] && cp -r `$SRC/plugins `$DST/; [ -f `$SRC/SOUL.md ] && cp `$SRC/SOUL.md `$DST/; [ -f `$SRC/.env ] && cp `$SRC/.env `$DST/; [ -f `$SRC/auth.json ] && cp `$SRC/auth.json `$DST/; [ -f `$SRC/config.yaml ] && cp `$SRC/config.yaml `$DST/; [ -f `$SRC/gateway.json ] && cp `$SRC/gateway.json `$DST/; [ -f `$SRC/state.db ] && cp `$SRC/state.db `$DST/; if [ -d `$HOME/.config/himalaya ]; then mkdir -p `$DST/himalaya; cp -r `$HOME/.config/himalaya/. `$DST/himalaya/; fi; echo done"

# Also stage Gmail MCP credentials from Windows home
$gmailMcpSrc = Join-Path $env:USERPROFILE ".gmail-mcp"
if (Test-Path $gmailMcpSrc) {
    $gmailMcpDst = Join-Path $StageDir "gmail-mcp"
    New-Item -ItemType Directory -Path $gmailMcpDst -Force | Out-Null
    Copy-Item "$gmailMcpSrc\*" $gmailMcpDst -Force
    Write-OK "Gmail MCP credentials staged"
}

Write-OK "Files staged"

# ── Sync helper functions ─────────────────────
function Sync-Dir ($name, $label) {
    $src = Join-Path $StageDir $name
    if (Test-Path $src) {
        Write-Step "Syncing $label..."
        docker cp "${src}/." "${ContainerName}:${ContainerData}/${name}/"
        $count = (Get-ChildItem -Path $src -Recurse -File).Count
        Write-OK "$label : $count files copied"
    } else {
        Write-Warn "No $label found - skipping"
    }
}

function Sync-File ($name, $label) {
    $src = Join-Path $StageDir $name
    if (Test-Path $src) {
        docker cp $src "${ContainerName}:${ContainerData}/${name}"
        Write-OK "$label copied"
    } else {
        Write-Warn "$label not found - skipping"
    }
}

# ══════════════════════════════════════════════
#  1. SKILLS
# ══════════════════════════════════════════════
if ($Mode -in "all", "skills") {
    Sync-Dir "skills" "skills"
}

# ══════════════════════════════════════════════
#  2. MEMORIES + SOUL
# ══════════════════════════════════════════════
if ($Mode -in "all", "memories") {
    Sync-Dir  "memories" "memories"
    Sync-Dir  "sessions" "sessions"
    Sync-File "SOUL.md"  "SOUL.md (personality)"
}

# ══════════════════════════════════════════════
#  3. CONFIGURATION
# ══════════════════════════════════════════════
if ($Mode -in "all", "config") {
    Write-Step "Syncing configuration..."

    Sync-File ".env"          ".env (Telegram token, API keys)"
    Sync-File "auth.json"     "auth.json (provider credentials)"
    Sync-File "config.yaml"   "config.yaml (agent + Telegram settings)"
    Sync-File "gateway.json"  "gateway.json (gateway policies)"
    Sync-File "state.db"      "state.db (session database)"

    Sync-Dir "cron"    "cron jobs"
    Sync-Dir "plugins" "plugins"

    # Himalaya email config
    $himalayaSrc = Join-Path $StageDir "himalaya"
    if (Test-Path $himalayaSrc) {
        Write-Step "Syncing Himalaya email config..."
        docker exec $ContainerName mkdir -p /opt/data/.config/himalaya
        docker cp "${himalayaSrc}/." "${ContainerName}:/opt/data/.config/himalaya/"
        Write-OK "Himalaya email config copied (Gmail IMAP/SMTP)"
    } else {
        Write-Warn "No Himalaya email config found - skipping"
    }

    # Gmail MCP credentials
    $gmailMcpStaged = Join-Path $StageDir "gmail-mcp"
    if (Test-Path $gmailMcpStaged) {
        Write-Step "Syncing Gmail MCP credentials..."
        docker exec $ContainerName mkdir -p /opt/data/.gmail-mcp
        docker cp "${gmailMcpStaged}/." "${ContainerName}:/opt/data/.gmail-mcp/"
        Write-OK "Gmail MCP credentials copied"
    } else {
        Write-Warn "No Gmail MCP credentials found - skipping"
    }
}

# ── Fix ownership ────────────────────────────
Write-Step "Fixing permissions..."
docker exec $ContainerName chown -R hermes:hermes $ContainerData 2>$null

# ── Cleanup ──────────────────────────────────
Remove-Item -Recurse -Force $StageDir -ErrorAction SilentlyContinue

Write-Host ""
Write-OK "Sync complete - container '$ContainerName' has your WSL Hermes config."
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
Write-Host "    - Restart the gateway to pick up the new config:" -ForegroundColor Gray
Write-Host "        docker compose restart agentic_layer" -ForegroundColor Yellow
Write-Host ""
Write-Host "    - Your Telegram config was synced from .env" -ForegroundColor Gray
Write-Host "      If you need to reconfigure:" -ForegroundColor Gray
Write-Host "        docker exec -it $ContainerName hermes gateway setup" -ForegroundColor Yellow
Write-Host ""
