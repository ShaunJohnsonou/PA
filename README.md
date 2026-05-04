# 🤖 Personal Assistant (PA)

A self-hosted, containerized personal assistant powered by [Hermes Agent](https://github.com/NousResearch/hermes-agent), [n8n](https://n8n.io), and PostgreSQL. Manage emails, documents, and automate workflows — all through Telegram.

## Architecture

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Hermes      │───▶│  PostgreSQL  │◀───│     n8n      │
│  Agent       │    │    :5432     │    │    :5678     │
│  :9119       │    │   (n8n_db)   │    │  (workflows) │
└──────┬───────┘    └──────────────┘    └──────────────┘
       │
  ┌────┴────┐
  │Telegram │  ← You chat here
  │   Bot   │
  └─────────┘
```

**Hermes Agent** runs as a gateway with:
- **Gmail MCP** — Read, search, send, label emails directly
- **PostgreSQL MCP** — Query/write to your database in natural language
- **Telegram** — Chat interface for interacting with the agent

**n8n** provides visual workflow automation (email triggers, payment extraction, etc.)

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

```bash
# Create the credentials directory
mkdir -p ~/.gmail-mcp

# Copy the example and fill in your OAuth client credentials
# (from Google Cloud Console → APIs & Services → Credentials)
cp scripts/gmail-mcp/gcp-oauth.keys.example.json ~/.gmail-mcp/gcp-oauth.keys.json
# Edit ~/.gmail-mcp/gcp-oauth.keys.json with your real client_id and client_secret

# Authenticate (opens browser for Google sign-in)
npx -y @gongrzhe/server-gmail-autoauth-mcp auth

# Protect the files
chmod 600 ~/.gmail-mcp/*.json
```

### 3. Set Up Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot`
2. Copy the bot token → paste into `.env` as `TELEGRAM_BOT_TOKEN`
3. Message [@userinfobot](https://t.me/userinfobot) to get your user ID → paste into `.env` as `TELEGRAM_ALLOWED_USERS`

### 4. Launch

```bash
# Build and start all services
docker compose up -d --build

# Check everything is running
docker ps
```

### 5. Sync Credentials into the Container

After the first boot, sync your host credentials (Gmail OAuth, Hermes config) into the container:

**Windows (PowerShell):**
```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\sync_host_hermes_to_container.ps1
```

**Linux/WSL (Bash):**
```bash
bash scripts/sync_host_hermes_to_container.sh
```

### 6. Configure Hermes MCP Servers

Once the container is running, add the MCP servers to the Hermes config:

```bash
docker exec n8n-agentic-layer python3 -c "
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
    'postgres': {
        'command': 'npx',
        'args': ['-y', 'mcp-postgres-server'],
        'env': {
            'PG_HOST': 'postgres',
            'PG_PORT': '5432',
            'PG_USER': 'n8n_user',
            'PG_PASSWORD': 'n8n_password',
            'PG_DATABASE': 'n8n_db'
        }
    }
}
with open('/opt/data/config.yaml', 'w') as f:
    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
print('MCP servers configured')
"

# Restart to pick up changes
docker compose restart agentic_layer
```

### 7. Test It

- **n8n**: Open [http://localhost:5678](http://localhost:5678)
- **Telegram**: Send a message to your bot
- **Hermes Gateway API**: `http://localhost:9119`

## Project Structure

```
PA/
├── .env.example                  # ← Copy to .env, fill in your secrets
├── docker-compose.yml            # All services: Postgres, n8n, Hermes
├── docker_files/
│   └── hermes.Dockerfile         # Ubuntu 24.04 + Hermes Agent build
├── init/
│   └── init.sql                  # DB schema (processed_emails, payments, documents)
├── scripts/
│   ├── sync_host_hermes_to_container.ps1  # Windows sync script
│   ├── sync_host_hermes_to_container.sh   # Linux/WSL sync script
│   └── gmail-mcp/
│       └── gcp-oauth.keys.example.json    # OAuth template
├── n8n_tools/
│   ├── README.md                 # n8n workflow docs
│   └── workflow.json             # Importable n8n workflow
├── hermes_config/
│   └── SOUL.md                   # Agent personality (customisable)
├── document_storage/             # Your personal files (git-ignored)
│   ├── Personal/
│   └── Work/
└── read_doc.py                   # Document reader utility
```

## Services

| Service      | Container Name      | Port  | Description                        |
|-------------|---------------------|-------|------------------------------------|
| PostgreSQL  | `n8n-postgres`      | 5432  | Database for n8n + custom tables   |
| Hermes Agent| `n8n-agentic-layer` | 9119  | AI agent with Telegram gateway     |
| n8n         | `n8n`               | 5678  | Workflow automation UI             |

## Database Schema

The PostgreSQL database includes three custom tables:

- **`processed_emails`** — Tracks emails processed by the agent (with categories like `payment_receipt`, `invoice_received`, etc.)
- **`payments`** — Structured payment data extracted from emails
- **`documents`** — Document index replacing the legacy file-based tracking

## MCP Tools Available

### Gmail (19 tools)
`send_email`, `draft_email`, `read_email`, `search_emails`, `modify_email`, `delete_email`, `list_email_labels`, `batch_modify_emails`, `create_label`, `create_filter`, `download_attachment`, and more.

### PostgreSQL (6 tools)
`connect_db`, `query`, `execute`, `list_schemas`, `list_tables`, `describe_table`

## Document Storage

Place your personal documents in the `document_storage/` directory:

```
document_storage/
├── Personal/
│   ├── Finance/
│   ├── Health/
│   ├── Home/
│   ├── Legal/
│   ├── Photos/
│   └── Projects/
└── Work/
    ├── Misc/
    ├── Projects/
    └── Reports/
```

The agent can read and catalogue these via the `read_doc.py` utility and track them in the `documents` database table.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No messaging platforms enabled` | Ensure `TELEGRAM_BOT_TOKEN` is set in `.env` and passed through in `docker-compose.yml` |
| Gateway crash-loops with lock errors | Stop container, remove stale locks: `docker run --rm -v pa_hermes_data:/d alpine rm -f /d/gateway.pid /d/gateway.lock` |
| `Telegram polling conflict` | Only one bot instance can poll at a time. Stop any other containers using the same bot token |
| Gmail MCP auth fails | Re-run `npx -y @gongrzhe/server-gmail-autoauth-mcp auth` and re-sync credentials |
| n8n can't reach Postgres | Use `postgres` as hostname (not `localhost`) — both are on the `n8n-net` Docker network |

## Teardown

```bash
# Stop (data preserved in volumes)
docker compose down

# Stop AND delete ALL data
docker compose down -v
```

## License

MIT
