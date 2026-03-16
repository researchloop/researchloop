# Stage 1: Build
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY researchloop/ researchloop/

# Install dependencies into a virtual environment
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python .

# Stage 2: Runtime
FROM python:3.12-slim

# Install SSH client (needed to connect to HPC clusters)
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client curl && \
    rm -rf /var/lib/apt/lists/*

# Install Claude CLI (needed for auto-loop idea generation + Slack)
RUN curl -fsSL https://claude.ai/install.sh | sh || true

WORKDIR /app

# Copy virtual environment and application from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/researchloop /app/researchloop

# Put venv and claude on PATH
ENV PATH="/root/.claude/bin:/app/.venv/bin:$PATH"

# Data directory — mount a persistent volume here
ENV RESEARCHLOOP_DB_PATH="/data/researchloop.db"
ENV RESEARCHLOOP_ARTIFACT_DIR="/data/artifacts"
RUN mkdir -p /data/artifacts

EXPOSE 8080

CMD ["researchloop", "serve"]
