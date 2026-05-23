"""Database Viewer — Local web dashboard for Hermes databases.

Serves a single-page dashboard at http://localhost:8642 showing:
  - SQLite document catalog (documents table)
  - DuckDB financial ledger (accounts, bank_statements, transactions,
    validation_results, extraction_evidence)

Usage:
    python tools/db_viewer.py [--vault /path/to/vault] [--port 8642]

No external dependencies — uses only Python stdlib + duckdb.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sqlite3
import sys
import urllib.parse
from datetime import datetime

# ── HTML Template ──────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Database Viewer</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --text-muted: #8b949e;
    --accent: #58a6ff;
    --accent-dim: #1f6feb;
    --green: #3fb950;
    --orange: #d29922;
    --red: #f85149;
    --purple: #bc8cff;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  header {
    background: linear-gradient(135deg, #1a1e2e 0%, #0d1117 100%);
    border-bottom: 1px solid var(--border);
    padding: 1rem 2rem;
    display: flex;
    align-items: center;
    gap: 1rem;
  }
  header h1 { font-size: 1.4rem; font-weight: 600; }
  header h1 span { color: var(--accent); }
  .stats-bar {
    display: flex;
    gap: 2rem;
    margin-left: auto;
    font-size: 0.85rem;
    color: var(--text-muted);
  }
  .stats-bar .stat-val { color: var(--accent); font-weight: 600; font-size: 1.1rem; }

  .container { max-width: 1400px; margin: 0 auto; padding: 1.5rem; }

  .tabs {
    display: flex;
    gap: 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 1.5rem;
  }
  .tab {
    padding: 0.7rem 1.5rem;
    cursor: pointer;
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: all 0.2s;
    font-size: 0.9rem;
    font-weight: 500;
  }
  .tab:hover { color: var(--text); }
  .tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .tab .badge {
    background: var(--accent-dim);
    color: #fff;
    padding: 0.1rem 0.5rem;
    border-radius: 10px;
    font-size: 0.75rem;
    margin-left: 0.5rem;
  }

  .panel { display: none; }
  .panel.active { display: block; }

  .table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  th {
    background: rgba(255,255,255,0.04);
    text-align: left;
    padding: 0.6rem 0.8rem;
    font-weight: 600;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    position: sticky;
    top: 0;
  }
  td {
    padding: 0.5rem 0.8rem;
    border-bottom: 1px solid rgba(48,54,61,0.5);
    max-width: 300px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  tr:hover td { background: rgba(88,166,255,0.04); }

  .status {
    display: inline-block;
    padding: 0.15rem 0.5rem;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .status-passed, .status-indexed, .status-extracted { background: rgba(63,185,80,0.15); color: var(--green); }
  .status-pending { background: rgba(210,153,34,0.15); color: var(--orange); }
  .status-failed, .status-needs_review { background: rgba(248,81,73,0.15); color: var(--red); }

  .amount-pos { color: var(--green); }
  .amount-neg { color: var(--red); }

  .empty-state {
    text-align: center;
    padding: 3rem;
    color: var(--text-muted);
  }
  .empty-state p { font-size: 1.1rem; margin-bottom: 0.5rem; }

  .refresh-btn {
    background: var(--accent-dim);
    color: #fff;
    border: none;
    padding: 0.4rem 1rem;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8rem;
    transition: background 0.2s;
  }
  .refresh-btn:hover { background: var(--accent); }

  .filter-bar {
    display: flex;
    gap: 0.8rem;
    margin-bottom: 1rem;
    align-items: center;
  }
  .filter-bar input, .filter-bar select {
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 0.4rem 0.8rem;
    border-radius: 6px;
    font-size: 0.82rem;
  }
  .filter-bar input:focus, .filter-bar select:focus {
    outline: none;
    border-color: var(--accent);
  }
</style>
</head>
<body>
<header>
  <h1><span>Hermes</span> Database Viewer</h1>
  <div class="stats-bar">
    <div>Documents: <span class="stat-val" id="doc-count">-</span></div>
    <div>Transactions: <span class="stat-val" id="txn-count">-</span></div>
    <div>Accounts: <span class="stat-val" id="acct-count">-</span></div>
    <button class="refresh-btn" onclick="loadAll()">Refresh</button>
  </div>
</header>

<div class="container">
  <div class="tabs" id="tabs"></div>
  <div id="panels"></div>
</div>

<script>
const TABLES = [
  {id: 'documents', label: 'Documents', db: 'sqlite'},
  {id: 'accounts', label: 'Accounts', db: 'duckdb'},
  {id: 'bank_statements', label: 'Statements', db: 'duckdb'},
  {id: 'transactions', label: 'Transactions', db: 'duckdb'},
  {id: 'validation_results', label: 'Validation', db: 'duckdb'},
  {id: 'extraction_evidence', label: 'Evidence', db: 'duckdb'},
];

const MONEY_COLS = new Set([
  'amount_cents','balance_after_cents','opening_balance_cents',
  'closing_balance_cents','total_debits_cents','total_credits_cents',
  'avg_cents','min_cents','max_cents'
]);
const STATUS_COLS = new Set([
  'status','extraction_status','indexing_status','validation_status',
  'financial_validation_status'
]);

function formatCents(val) {
  if (val == null) return '-';
  const r = (val / 100).toFixed(2);
  const cls = val >= 0 ? 'amount-pos' : 'amount-neg';
  return `<span class="${cls}">R ${r}</span>`;
}

function formatStatus(val) {
  if (!val) return '-';
  return `<span class="status status-${val}">${val}</span>`;
}

function formatCell(col, val) {
  if (val === null || val === undefined) return '<span style="color:var(--text-muted)">-</span>';
  if (MONEY_COLS.has(col)) return formatCents(val);
  if (STATUS_COLS.has(col)) return formatStatus(val);
  return String(val);
}

function buildTable(data) {
  if (!data.rows || data.rows.length === 0) {
    return '<div class="empty-state"><p>No data</p><p style="font-size:0.85rem">This table is empty</p></div>';
  }
  let html = '<div class="table-wrap"><table><thead><tr>';
  for (const col of data.columns) {
    html += `<th>${col}</th>`;
  }
  html += '</tr></thead><tbody>';
  for (const row of data.rows) {
    html += '<tr>';
    for (let i = 0; i < data.columns.length; i++) {
      html += `<td>${formatCell(data.columns[i], row[i])}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table></div>';
  return html;
}

function switchTab(id) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.id === id));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + id));
}

function initTabs() {
  const tabsEl = document.getElementById('tabs');
  const panelsEl = document.getElementById('panels');
  tabsEl.innerHTML = '';
  panelsEl.innerHTML = '';
  for (const t of TABLES) {
    const tab = document.createElement('div');
    tab.className = 'tab';
    tab.dataset.id = t.id;
    tab.innerHTML = `${t.label} <span class="badge" id="badge-${t.id}">0</span>`;
    tab.onclick = () => switchTab(t.id);
    tabsEl.appendChild(tab);

    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.id = 'panel-' + t.id;
    panel.innerHTML = '<div class="empty-state"><p>Loading...</p></div>';
    panelsEl.appendChild(panel);
  }
  switchTab('documents');
}

async function loadTable(tableName) {
  try {
    const resp = await fetch(`/api/table?name=${tableName}`);
    const data = await resp.json();
    document.getElementById('panel-' + tableName).innerHTML = buildTable(data);
    document.getElementById('badge-' + tableName).textContent = data.rows ? data.rows.length : 0;
    return data.rows ? data.rows.length : 0;
  } catch (e) {
    document.getElementById('panel-' + tableName).innerHTML =
      `<div class="empty-state"><p>Error loading ${tableName}</p><p style="font-size:0.85rem">${e.message}</p></div>`;
    return 0;
  }
}

async function loadAll() {
  const counts = {};
  for (const t of TABLES) {
    counts[t.id] = await loadTable(t.id);
  }
  document.getElementById('doc-count').textContent = counts.documents || 0;
  document.getElementById('txn-count').textContent = counts.transactions || 0;
  document.getElementById('acct-count').textContent = counts.accounts || 0;
}

initTabs();
loadAll();
</script>
</body>
</html>"""


# ── Server Logic ───────────────────────────────────────────────────


def query_sqlite(db_path: str, table: str) -> dict:
    """Query a SQLite table and return columns + rows."""
    if not os.path.isfile(db_path):
        return {"columns": [], "rows": [], "error": f"SQLite DB not found: {db_path}"}

    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = None
    try:
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 500")
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return {"columns": columns, "rows": rows}
    except Exception as exc:
        return {"columns": [], "rows": [], "error": str(exc)}
    finally:
        conn.close()


def query_duckdb(db_path: str, table: str) -> dict:
    """Query a DuckDB table and return columns + rows.

    Reason: DuckDB uses file-level locking — only one process can hold
    the lock, even for read_only connections. To avoid conflicting with
    the agentic_layer process, we copy the .duckdb file to a temporary
    location, query the copy, then delete it.
    """
    if not os.path.isfile(db_path):
        return {"columns": [], "rows": [], "error": f"DuckDB not found: {db_path}"}

    try:
        import duckdb
    except ImportError:
        return {"columns": [], "rows": [], "error": "duckdb not installed"}

    import shutil
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".duckdb")
    os.close(tmp_fd)

    try:
        shutil.copy2(db_path, tmp_path)
        conn = duckdb.connect(tmp_path, read_only=True)
        try:
            cur = conn.execute(f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT 500")
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            # Convert dates/timestamps to strings for JSON serialisation
            clean_rows = []
            for row in rows:
                clean = []
                for val in row:
                    if hasattr(val, "isoformat"):
                        clean.append(str(val))
                    else:
                        clean.append(val)
                clean_rows.append(clean)
            return {"columns": columns, "rows": clean_rows}
        except Exception as exc:
            return {"columns": [], "rows": [], "error": str(exc)}
        finally:
            conn.close()
    except Exception as exc:
        return {"columns": [], "rows": [], "error": f"Copy failed: {exc}"}
    finally:
        # Clean up temp file and any DuckDB side-files
        for suffix in ("", ".wal"):
            try:
                os.unlink(tmp_path + suffix)
            except OSError:
                pass


# Reason: restrict queryable tables to prevent SQL injection
SQLITE_TABLES = {"documents"}
DUCKDB_TABLES = {"accounts", "bank_statements", "transactions", "validation_results", "extraction_evidence"}


class ViewerHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the database viewer."""

    vault_path = ""
    sqlite_path = ""
    duckdb_path = ""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            self._serve_html()
        elif parsed.path == "/api/table":
            self._serve_table(parsed.query)
        else:
            self.send_error(404)

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

    def _serve_table(self, query_string: str):
        params = urllib.parse.parse_qs(query_string)
        table_name = params.get("name", [""])[0]

        if table_name in SQLITE_TABLES:
            data = query_sqlite(self.sqlite_path, table_name)
        elif table_name in DUCKDB_TABLES:
            data = query_duckdb(self.duckdb_path, table_name)
        else:
            data = {"columns": [], "rows": [], "error": f"Unknown table: {table_name}"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, format, *args):
        # Quieter logging
        pass


def main():
    parser = argparse.ArgumentParser(description="Hermes Database Viewer")
    parser.add_argument("--vault", default=None, help="Path to vault root (default: /hermes-vault)")
    parser.add_argument("--port", type=int, default=8642, help="Port to serve on (default: 8642)")
    args = parser.parse_args()

    # Auto-detect vault path
    vault = args.vault
    if vault is None:
        for candidate in ["/hermes-vault", os.path.expanduser("~/hermes-vault"), "."]:
            if os.path.isdir(candidate):
                vault = candidate
                break
        if vault is None:
            vault = "."

    sqlite_path = os.path.join(vault, "hermes_catalog.sqlite")
    duckdb_path = os.path.join(vault, "finance.duckdb")

    ViewerHandler.vault_path = vault
    ViewerHandler.sqlite_path = sqlite_path
    ViewerHandler.duckdb_path = duckdb_path

    print(f"Hermes Database Viewer")
    print(f"  Vault:    {os.path.abspath(vault)}")
    print(f"  SQLite:   {sqlite_path} ({'found' if os.path.isfile(sqlite_path) else 'not found'})")
    print(f"  DuckDB:   {duckdb_path} ({'found' if os.path.isfile(duckdb_path) else 'not found'})")
    print(f"  URL:      http://localhost:{args.port}")
    print()

    server = http.server.HTTPServer(("0.0.0.0", args.port), ViewerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
