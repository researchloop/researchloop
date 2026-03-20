# Deployment

The ResearchLoop orchestrator is designed to run as a lightweight server. It does not need GPUs or significant compute -- all heavy work happens on the HPC cluster. A shared-CPU VM with 1-2 GB of RAM is sufficient.

## Docker

### Dockerfile

```dockerfile
FROM python:3.12-slim

# System dependencies: SSH client (HPC access), curl (Claude CLI)
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client curl git && \
    rm -rf /var/lib/apt/lists/*

# Install Claude CLI (used for auto-loop idea generation and Slack conversations)
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install researchloop
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python --no-cache researchloop

WORKDIR /app

# Copy your instance-specific config
COPY researchloop.toml .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Put venv and claude on PATH
ENV PATH="/root/.local/bin:/root/.claude/bin:/app/.venv/bin:$PATH"

# Store database and artifacts on a persistent volume
ENV RESEARCHLOOP_DB_PATH="/data/researchloop.db"
ENV RESEARCHLOOP_ARTIFACT_DIR="/data/artifacts"

EXPOSE 8080

ENTRYPOINT ["./entrypoint.sh"]
CMD ["researchloop", "serve"]
```

### Entrypoint script

The entrypoint script sets up the SSH key from a secret and ensures required directories exist:

```bash
#!/bin/bash
set -euo pipefail

# Write SSH key from secret so we can connect to HPC clusters
if [ -n "${SSH_PRIVATE_KEY:-}" ]; then
    mkdir -p ~/.ssh
    echo "$SSH_PRIVATE_KEY" > ~/.ssh/id_ed25519
    chmod 600 ~/.ssh/id_ed25519
    cat > ~/.ssh/config <<EOF
Host *
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
EOF
    chmod 600 ~/.ssh/config
    echo "SSH key configured."
fi

# Ensure data directory exists
mkdir -p /data/artifacts

# Skip Claude CLI onboarding prompt
if [ ! -f /root/.claude.json ]; then
    echo '{"hasCompletedOnboarding": true}' > /root/.claude.json
fi

exec "$@"
```

### Build and run

```bash
docker build -t researchloop .
docker run -p 8080:8080 \
    -v researchloop-data:/data \
    -e SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ed25519)" \
    -e RESEARCHLOOP_SHARED_SECRET="your-secret" \
    -e RESEARCHLOOP_ORCHESTRATOR_URL="https://your-server.com" \
    -e RESEARCHLOOP_DASHBOARD_PASSWORD="your-password" \
    researchloop
```

## Fly.io

ResearchLoop works well on [Fly.io](https://fly.io) with a persistent volume for the database and artifacts.

### fly.toml

```toml
app = "my-researchloop"
primary_region = "iad"

[build]

[[mounts]]
  source = "researchloop_data"
  destination = "/data"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

[[vm]]
  size = "shared-cpu-1x"
  memory = "2gb"
```

### Create the app and volume

```bash
fly launch --no-deploy
fly volumes create researchloop_data --region iad --size 1
```

### Set secrets

```bash
fly secrets set \
    RESEARCHLOOP_SHARED_SECRET="your-secret" \
    RESEARCHLOOP_ORCHESTRATOR_URL="https://my-researchloop.fly.dev" \
    SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ed25519)" \
    RESEARCHLOOP_DASHBOARD_PASSWORD="your-password" \
    -a my-researchloop
```

For Slack integration, also set:

```bash
fly secrets set \
    RESEARCHLOOP_SLACK_BOT_TOKEN="xoxb-..." \
    RESEARCHLOOP_SLACK_SIGNING_SECRET="..." \
    RESEARCHLOOP_SLACK_CHANNEL_ID="C0123456789" \
    RESEARCHLOOP_SLACK_ALLOWED_USER_IDS="U0123456789" \
    -a my-researchloop
```

### Deploy

```bash
fly deploy
```

To pick up new ResearchLoop versions (since it installs from git):

```bash
fly deploy --no-cache
```

### Verify

```bash
# Check the app is running
fly status

# View logs
fly logs

# Connect the CLI
researchloop connect https://my-researchloop.fly.dev
```

## Updating

Since ResearchLoop is installed from git in the Docker image, redeploy with `--no-cache` to pick up the latest version:

```bash
fly deploy --no-cache
```

The database schema is automatically migrated on startup. No manual migration steps are needed.

## Persistent data

The following data should be on a persistent volume:

- **SQLite database** (`researchloop.db`) -- all study, sprint, loop, and session metadata
- **Artifacts directory** -- uploaded sprint artifacts (reports, PDFs, data files)

Both paths are configured via environment variables (`RESEARCHLOOP_DB_PATH`, `RESEARCHLOOP_ARTIFACT_DIR`) and default to `/data/` in the Docker setup.

!!! warning "Database backups"
    SQLite WAL mode is used for concurrent read/write access. If you need backups, use `sqlite3 researchloop.db ".backup backup.db"` or a file-level snapshot of the volume.
