#!/bin/bash
# ──────────────────────────────────────────────
#  sync_host_hermes_to_container.sh
#
#  ⚠️  This script only works if WSL shares the
#  same Docker engine as Windows Docker Desktop.
#  If not, use the PowerShell version instead:
#
#    .\scripts\sync_host_hermes_to_container.ps1
#
#  This copies the full Hermes runtime state from
#  ~/.hermes into the running container:
#    n8n-agentic-layer (service: agentic_layer)
#
#  Usage (from WSL):
#    bash scripts/sync_host_hermes_to_container.sh
#    bash scripts/sync_host_hermes_to_container.sh --skills-only
#    bash scripts/sync_host_hermes_to_container.sh --config-only
#    bash scripts/sync_host_hermes_to_container.sh --memories-only
# ──────────────────────────────────────────────

set -euo pipefail

# ── Must match docker-compose.yml ─────────────
CONTAINER_NAME="n8n-agentic-layer"   # agentic_layer service
CONTAINER_DATA="/opt/data"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HIMALAYA_HOME="$HOME/.config/himalaya"

# ── Colours ───────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}✅ $1${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $1${NC}"; }
fail()  { echo -e "${RED}❌ $1${NC}"; exit 1; }
step()  { echo -e "${CYAN}── $1${NC}"; }

# ── Preflight ─────────────────────────────────
[[ -d "$HERMES_HOME" ]] || fail "Hermes home not found: $HERMES_HOME"
docker inspect "$CONTAINER_NAME" > /dev/null 2>&1 || \
  fail "Container '$CONTAINER_NAME' is not running.\n   If WSL can't see Docker Desktop containers, use the PowerShell script:\n     .\\scripts\\sync_host_hermes_to_container.ps1"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  Hermes → Container Sync                 ║"
echo "║  Source: $HERMES_HOME"
echo "║  Target: $CONTAINER_NAME:$CONTAINER_DATA"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Parse flags ───────────────────────────────
SYNC_SKILLS=true; SYNC_CONFIG=true; SYNC_MEMORIES=true
case "${1:-all}" in
  --skills-only)   SYNC_CONFIG=false;  SYNC_MEMORIES=false ;;
  --config-only)   SYNC_SKILLS=false;  SYNC_MEMORIES=false ;;
  --memories-only) SYNC_SKILLS=false;  SYNC_CONFIG=false ;;
  all|"")          ;;
  *) echo "Usage: $0 [--skills-only|--config-only|--memories-only]"; exit 1 ;;
esac

# ── Helpers ───────────────────────────────────
sync_dir() {
  local src="$1" dst="$2" label="$3"
  if [[ -d "$src" ]]; then
    step "Syncing $label..."
    docker cp "$src/." "$CONTAINER_NAME:$dst/"
    info "$label: $(find "$src" -type f | wc -l) files"
  else
    warn "$label not found — skipping"
  fi
}
sync_file() {
  local src="$1" dst="$2" label="$3"
  if [[ -f "$src" ]]; then
    docker cp "$src" "$CONTAINER_NAME:$dst"
    info "$label"
  else
    warn "$label not found — skipping"
  fi
}

# ── 1. Skills ─────────────────────────────────
[[ "$SYNC_SKILLS" == true ]] && sync_dir "$HERMES_HOME/skills" "$CONTAINER_DATA/skills" "skills"

# ── 2. Memories ───────────────────────────────
if [[ "$SYNC_MEMORIES" == true ]]; then
  sync_dir  "$HERMES_HOME/memories"  "$CONTAINER_DATA/memories"  "memories"
  sync_dir  "$HERMES_HOME/sessions"  "$CONTAINER_DATA/sessions"  "sessions"
  sync_file "$HERMES_HOME/SOUL.md"   "$CONTAINER_DATA/SOUL.md"   "SOUL.md"
fi

# ── 3. Config ─────────────────────────────────
if [[ "$SYNC_CONFIG" == true ]]; then
  step "Syncing configuration..."
  sync_file "$HERMES_HOME/.env"          "$CONTAINER_DATA/.env"          ".env (Telegram + API keys)"
  sync_file "$HERMES_HOME/auth.json"     "$CONTAINER_DATA/auth.json"     "auth.json"
  sync_file "$HERMES_HOME/config.yaml"   "$CONTAINER_DATA/config.yaml"   "config.yaml"
  sync_file "$HERMES_HOME/gateway.json"  "$CONTAINER_DATA/gateway.json"  "gateway.json"
  sync_file "$HERMES_HOME/state.db"      "$CONTAINER_DATA/state.db"      "state.db"
  sync_dir  "$HERMES_HOME/cron"          "$CONTAINER_DATA/cron"          "cron"
  sync_dir  "$HERMES_HOME/plugins"       "$CONTAINER_DATA/plugins"       "plugins"

  if [[ -d "$HIMALAYA_HOME" ]]; then
    step "Syncing Himalaya email config..."
    docker exec "$CONTAINER_NAME" mkdir -p /opt/data/.config/himalaya
    docker cp "$HIMALAYA_HOME/." "$CONTAINER_NAME:/opt/data/.config/himalaya/"
    info "Himalaya config (Gmail IMAP/SMTP)"
  fi
fi

# ── Permissions ───────────────────────────────
step "Fixing permissions..."
docker exec "$CONTAINER_NAME" chown -R hermes:hermes "$CONTAINER_DATA" 2>/dev/null || true

echo ""
info "Sync complete. Restart to apply: docker compose restart agentic_layer"
echo ""
