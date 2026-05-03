# n8n Workflows

Workflow automation component of the Personal Assistant stack.
See the [root README](../README.md) for full setup instructions.

## Quick Start

```bash
# 1. Start the stack
docker compose up -d

# 2. Open n8n
#    http://localhost:5678
#    Credentials are in your .env file
```

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│   n8n       │────▶│  PostgreSQL  │◀────│   Hermes     │
│  :5678      │     │  :5432       │     │  :9119       │
│  (workflow) │     │  (n8n_db)    │     │  (agent)     │
└─────────────┘     └──────────────┘     └──────────────┘
               All on  n8n-net  bridge network
```

## Services

| Service      | Image                              | Container Name      | Ports         |
|-------------|-------------------------------------|---------------------|---------------|
| n8n          | `docker.n8n.io/n8nio/n8n:latest`   | `n8n`               | `5678:5678`   |
| PostgreSQL   | `postgres:16-alpine`               | `n8n-postgres`      | `5432:5432`   |
| Hermes Agent | `hermes-agent:local`               | `n8n-agentic-layer` | `9119:9119`   |

## Connecting to Hermes from n8n

Within n8n workflows, use:
- `http://agentic_layer:9119` — via the shared Docker network

## Environment Variables

All credentials and settings live in `.env` (git-ignored). See `.env.example` for the full list.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `getaddrinfo EAI_AGAIN postgres` | n8n can't reach DB. Ensure `DB_POSTGRESDB_HOST=postgres` (not `localhost`) and both services are on `n8n-net`. |
| `EACCES permission denied` on volumes | Run `docker compose down -v` then `docker compose up -d`, or ensure volume dirs are owned by UID 1000. |
| Ollama unreachable from n8n | Use `http://ollama:11434` inside workflows (same network), or `http://host.docker.internal:11435` for host access. |

## Data Persistence

| Volume          | Mount Point                        | Contents                     |
|-----------------|------------------------------------|------------------------------|
| `postgres_data` | `/var/lib/postgresql/data`         | All n8n metadata & executions|
| `n8n_data`      | `/home/node/.n8n`                  | Workflows, credentials, keys |
| `hermes_data`   | `/opt/data`                        | Hermes config, sessions, MCP |

## Teardown

```bash
# Stop containers (data preserved)
docker compose down

# Stop AND delete all data
docker compose down -v
```
