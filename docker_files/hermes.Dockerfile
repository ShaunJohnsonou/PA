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
      ripgrep ffmpeg gcc libffi-dev \
      procps tini ca-certificates gnupg && \
    rm -rf /var/lib/apt/lists/*

# ── Node.js 22 LTS (Hermes requires >= 20) ──
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# ── Install uv (fast Python package manager) ─
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# ── Non-root user ─────────────────────────────
RUN useradd -u 10000 -m -d /opt/data hermes

WORKDIR /opt/hermes

# ── Clone hermes-agent ────────────────────────
ARG HERMES_BRANCH=main
RUN git clone --depth 1 --branch ${HERMES_BRANCH} \
      https://github.com/NousResearch/hermes-agent.git .

# ── Install Node dependencies + build assets ─
ENV npm_config_install_links=false
RUN npm install --prefer-offline --no-audit && \
    (cd web && npm install --prefer-offline --no-audit) && \
    (cd ui-tui && npm install --prefer-offline --no-audit) && \
    npm cache clean --force && \
    cd web && npm run build && \
    cd ../ui-tui && npm run build

# ── Python virtualenv + install ───────────────
RUN uv venv && \
    uv pip install --no-cache-dir -e ".[all]"

# ── Permissions ───────────────────────────────
RUN chmod -R a+rX /opt/hermes

# ── Runtime config ────────────────────────────
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_HOME=/opt/data
ENV PATH="/opt/data/.local/bin:/opt/hermes/.venv/bin:${PATH}"

VOLUME ["/opt/data"]
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["hermes", "gateway", "run"]
