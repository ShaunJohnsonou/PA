# ──────────────────────────────────────────────
#  Hermes Agent — Ubuntu-based container
#  Clones NousResearch/hermes-agent and installs
#  from source so you can run isolated instances.
# ──────────────────────────────────────────────

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# ── System dependencies ──────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential curl git openssh-client \
    python3 python3-pip python3-venv python3-dev \
    ripgrep ffmpeg gcc libffi-dev sqlite3 \
    libmagic1 \
    procps tini ca-certificates gnupg && \
    rm -rf /var/lib/apt/lists/*

# ── Node.js 22 LTS (Hermes requires >= 20) ──
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*



# ── Non-root user ─────────────────────────────
RUN useradd -u 10000 -m -d /opt/data hermes

WORKDIR /opt/hermes

# ── Pre-install heavy Python dependencies to cache them ──
RUN python3 -m venv .venv && \
    .venv/bin/pip install --no-cache-dir docling markitdown python-magic faiss-cpu numpy openai pdfplumber duckdb pyarrow

# ── Copy hermes-agent submodule ───────────────
COPY hermes-agent/ .

# ── Install Node dependencies + build assets ─
ENV npm_config_install_links=false
RUN npm install --prefer-offline --no-audit && \
    (cd web && npm install --prefer-offline --no-audit) && \
    (cd ui-tui && npm install --prefer-offline --no-audit) && \
    npm cache clean --force && \
    cd web && npm run build && \
    cd ../ui-tui && npm run build

# ── Install Hermes Python dependencies ──────────
RUN .venv/bin/pip install --no-cache-dir -e ".[all]" "langfuse<3.0.0" && \
    .venv/bin/hermes plugins enable observability/langfuse

# ── Copy PA init files ────────────────────────
COPY init/init_sqlite.sql /opt/hermes/init/init_sqlite.sql
COPY scripts/entrypoint.sh /opt/hermes/scripts/entrypoint.sh
RUN chmod +x /opt/hermes/scripts/entrypoint.sh

# ── Permissions ───────────────────────────────
# Reason: Removed chmod -R a+rX /opt/hermes which takes 9+ minutes
# due to the massive size of node_modules and .venv. Umask 022 already 
# ensures files copied/created are readable by all users.

# ── Runtime config ────────────────────────────
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_HOME=/opt/data
ENV PATH="/opt/data/.local/bin:/opt/hermes/.venv/bin:${PATH}"

VOLUME ["/opt/data"]
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes/scripts/entrypoint.sh"]
CMD ["hermes", "gateway", "run"]
