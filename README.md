# 🤖 Personal Assistant (PA)

A self-hosted, containerized personal assistant powered by [Hermes Agent](https://github.com/NousResearch/hermes-agent) and SQLite. Manage emails, documents, and automate workflows — all through Telegram.

## Architecture

```
┌──────────────┐    ┌──────────────┐
│  Hermes      │───▶│    SQLite    │
│  Agent       │    │ (Local File) │
│  :9119       │    │              │
└──────┬───────┘    └──────────────┘
       │
  ┌────┴────┐
  │Telegram │  ← You chat here
  │   Bot   │
  └─────────┘
```

**Hermes Agent** runs as a gateway with:
- **Gmail MCP** — Read, search, send, label emails directly
- **SQLite MCP** — Query/write to your database in natural language
- **Telegram** — Chat interface for interacting with the agent

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac) or Docker Engine (Linux)
- [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) (Windows only — for sync scripts)
- A [Telegram Bot Token](https://t.me/BotFather) (free)
- A [Google Cloud Project](https://console.cloud.google.com/) with Gmail API enabled + OAuth 2.0 credentials

## Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/ShaunJohnsonCALI/PA.git
cd PA

# Copy the env template and fill in your values
cp .env.example .env
# Edit .env with your credentials
```

### 2. Set Up Gmail OAuth

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials)
2. Create an **OAuth 2.0 Client ID** (Application type: **Web application**)
3. Add `http://localhost:3000/oauth2callback` as an **Authorized redirect URI**
4. Once created, click **Download JSON**

```bash
# Create the credentials directory
mkdir -p ~/.gmail-mcp

# Option A: Use the JSON downloaded from Google Console (recommended)
mv ~/Downloads/client_secret_XXXXX.json ~/.gmail-mcp/gcp-oauth.keys.json

# Authenticate (opens browser for Google sign-in)
npx -y @gongrzhe/server-gmail-autoauth-mcp auth

# Protect the files
chmod 600 ~/.gmail-mcp/*.json
```

### 3. Initialize the SQLite Database

Run the setup script to create the local SQLite database file (`pa_index.db`):

```bash
python3 scripts/setup_db.py
```

### 4. Set Up Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the bot token → paste into `.env` as `TELEGRAM_BOT_TOKEN`
3. Message [@userinfobot](https://t.me/userinfobot) to get your user ID → paste into `.env` as `TELEGRAM_ALLOWED_USERS`

### 5. Launch

```bash
# Build and start all services
docker compose up -d --build

# Check everything is running
docker ps
```

### 6. Sync Credentials into the Container

After the first boot, sync your host credentials (Gmail OAuth, Hermes config) into the container:

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\sync_host_hermes_to_container.ps1
```

**Linux/WSL (Bash):**
```bash
bash scripts/sync_host_hermes_to_container.sh
```

### 7. Configure Hermes MCP Servers

Once the container is running, add the MCP servers to the Hermes config:

```bash
docker exec personal_assistant python3 -c "
import yaml
with open('/opt/data/config.yaml', 'r') as f:
    config = yaml.safe_load(f)
config['mcp_servers'] = {
    'gmail': {
        'command': 'npx',
        'args': ['-y', '@gongrzhe/server-gmail-autoauth-mcp'],
        'env': {
            'GMAIL_CREDENTIALS_PATH': '/opt/data/.gmail-mcp/credentials.json',
            'GMAIL_OAUTH_PATH': '/opt/data/.gmail-mcp/gcp-oauth.keys.json'
        }
    },
    'sqlite': {
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-sqlite', '/opt/data/pa_index.db']
    }
}
with open('/opt/data/config.yaml', 'w') as f:
    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
print('MCP servers configured')
"

# Restart to pick up changes
docker compose restart agentic_layer
```

## Project Structure

```text
PA/
├── .env.example                  # ← Copy to .env, fill in your secrets
├── docker-compose.yml            # Services configuration
├── docker_files/
│   └── hermes.Dockerfile         # Ubuntu 24.04 + Hermes Agent build
├── init/
│   └── init_sqlite.sql           # DB schema (processed_emails, payments, documents)
├── scripts/
│   ├── setup_db.py               # Initializes the SQLite database
│   ├── sync_host_hermes_to_container.ps1  # Windows sync script
│   └── sync_host_hermes_to_container.sh   # Linux/WSL sync script
├── hermes_config/
│   └── SOUL.md                   # Agent personality (customisable)
├── document_storage/             # Your personal files (git-ignored)
└── read_doc.py                   # Document reader utility
```

## Database Schema

The SQLite database (`pa_index.db`) includes three custom tables:

- **`processed_emails`** — Tracks emails processed by the agent (with categories like `payment_receipt`, `invoice_received`, etc.)
- **`payments`** — Structured payment data extracted from emails
- **`documents`** — Document index tracking the files in your `document_storage` folder

## MCP Tools Available

### Gmail (19 tools)
`send_email`, `draft_email`, `read_email`, `search_emails`, `modify_email`, `delete_email`, `list_email_labels`, `batch_modify_emails`, `create_label`, `create_filter`, `download_attachment`, and more.

### SQLite (4 tools)
`read_query`, `write_query`, `create_table`, `list_tables`

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No messaging platforms enabled` | Ensure `TELEGRAM_BOT_TOKEN` is set in `.env` |
| Gateway crash-loops with lock errors | Stop container, remove stale locks: `docker run --rm -v pa_hermes_data:/d alpine rm -f /d/gateway.pid /d/gateway.lock` |
| Gmail MCP auth fails | Re-run `npx -y @gongrzhe/server-gmail-autoauth-mcp auth` and re-sync credentials |
| SQLite cannot find database | Ensure you ran `python3 scripts/setup_db.py` before starting the container |

## Teardown

```bash
# Stop (data preserved in volumes)
docker compose down

# Stop AND delete ALL data
docker compose down -v
```
