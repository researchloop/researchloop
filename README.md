# ResearchLoop

**Automated AI research sprints on HPC clusters.**

[![CI](https://github.com/chanind/researchloop/actions/workflows/ci.yml/badge.svg)](https://github.com/chanind/researchloop/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Full docs: https://researchloop.github.io/researchloop

---

ResearchLoop automates multi-step AI research pipelines on SLURM and SGE clusters. You describe a research idea, and ResearchLoop submits it to your HPC cluster where [Claude Code](https://docs.anthropic.com/en/docs/claude-code) executes a full research pipeline -- coding, red-teaming, fixing, reporting -- inside a single job. Results are reported back via webhooks, Slack, or push notifications, and you can monitor everything from a web dashboard or the CLI.

The platform is built for researchers who run experiments on shared HPC infrastructure and want to iterate faster without babysitting jobs. Define your studies, point ResearchLoop at your cluster, and let it handle the rest: job submission, progress tracking, artifact collection, and even automatic generation of follow-up research ideas.

ResearchLoop's **auto-loop** feature chains sprints together automatically. After each sprint completes, Claude analyzes the results and proposes the next experiment. You set how many iterations to run, and the system handles the rest -- turning a single research question into a sustained investigation.

## How it works

ResearchLoop has two components:

1. **Orchestrator** (`researchloop serve`) -- a lightweight server that manages studies and sprints in SQLite, submits jobs to HPC clusters via SSH, receives completion webhooks, stores artifacts, and serves the web dashboard.
2. **Sprint Runner** -- runs inside each SLURM/SGE job on the HPC cluster. Chains `claude -p` calls through the research pipeline (research, red-team, fix, report, summarize), then sends artifacts and results back to the orchestrator.

```
You (CLI / Dashboard / Slack)
        |
        v
Orchestrator (Docker / Fly.io)          HPC Cluster
+--------------------------+             +----------------------------+
| FastAPI API + Dashboard  |---SSH------>| SLURM / SGE scheduler      |
| SQLite metadata          |             |                            |
| Artifact storage         |<--webhook--| Sprint Runner               |
| Slack bot                |<--upload---| 1. claude -p "research"     |
| ntfy.sh notifications    |             | 2. claude -p "red-team"    |
+--------------------------+             | 3. claude -p "fix"          |
                                         | 4. claude -p "report"       |
                                         | 5. claude -p "summarize"    |
                                         +----------------------------+
```

### Core concepts

| Concept | Description |
|---------|-------------|
| **Study** | A sustained research effort (e.g., "synthetic SAE improvements"). Tied to a cluster, has its own context and configuration. |
| **Sprint** | A single research attempt within a study. Gets a short ID (`sp-a3f7b2`), its own directory, and runs the full pipeline. |
| **Auto-loop** | Automatic sequential sprint execution. After each sprint, Claude analyzes results and generates the next research idea. |

### Sprint pipeline

Each sprint runs these steps inside a single SLURM/SGE job:

1. **Research** -- execute the research idea (coding, experiments, analysis)
2. **Red-team** -- critique the work, find flaws (up to N rounds with fix steps)
3. **Fix** -- address issues found by the red-team
4. **Report** -- generate a comprehensive markdown report
5. **Summarize** -- write a short summary for notifications and the dashboard

All steps share a single Claude session (via `--resume`), so Claude maintains full context of the sprint's work across steps.

## Features

- **HPC cluster integration** -- submit, monitor, and cancel jobs on SLURM and SGE clusters via SSH
- **Multi-step research pipeline** -- research, red-team, fix, report, summarize with configurable rounds
- **Auto-loop** -- chain sprints automatically with AI-generated follow-up ideas
- **Web dashboard** -- monitor studies, sprints, and loops from a browser with live status refresh
- **Slack bot** -- start sprints, check status, and have research conversations via Slack DMs or channels
- **CLI** -- full remote management from the command line with token-based auth
- **Progress tracking** -- live `progress.md` and `output.log` streaming from cluster to dashboard
- **Notifications** -- push notifications via ntfy.sh and Slack with PDF report attachments
- **Per-sprint security** -- webhook tokens, CSRF protection, signed session cookies, bcrypt password hashing
- **Context hierarchy** -- global, cluster, and study-level context files and inline configuration

## Quick start

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- SSH access to an HPC cluster with SLURM or SGE
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated on the HPC cluster

### Install

```bash
pip install git+https://github.com/chanind/researchloop.git
```

Or for development:

```bash
git clone https://github.com/chanind/researchloop.git
cd researchloop
uv sync
```

### Initialize a project

```bash
researchloop init
# Creates researchloop.toml and artifacts/ directory
```

### Configure

Edit `researchloop.toml`:

```toml
shared_secret = "change-me"
orchestrator_url = "https://your-server.fly.dev"

[[cluster]]
name = "hpc"
host = "login.cluster.example.com"
user = "researcher"
key_path = "~/.ssh/id_ed25519"
scheduler_type = "slurm"                       # "slurm", "sge", or "local"
working_dir = "/scratch/researcher/researchloop"

[cluster.job_options]
gres = "gpu:1"
mem = "64G"
cpus-per-task = "8"

[[study]]
name = "my-research"
cluster = "hpc"
description = "Investigating feature X"
max_sprint_duration_hours = 8
red_team_max_rounds = 3
```

### Start the server and run a sprint

```bash
# Start the orchestrator
researchloop serve

# In another terminal, connect the CLI to the server
researchloop connect https://localhost:8080

# Submit a sprint
researchloop sprint run "try approach X on dataset Y" --study my-research

# Check status
researchloop sprint list
researchloop sprint show sp-a3f7b2
```

## Configuration reference

### Complete `researchloop.toml` example

```toml
# -- Top-level settings --
db_path = "researchloop.db"              # SQLite database location
artifact_dir = "artifacts"               # Local directory for uploaded artifacts
shared_secret = "your-secret"            # Auth between runner and orchestrator
orchestrator_url = "https://example.com" # Public URL for webhooks
claude_command = ""                      # Override claude command globally

# Global context (included in all sprints)
context = "Always use Python 3.10+ features."
context_paths = ["./global-context.md"]  # Files to include as context

# -- Cluster configuration --
[[cluster]]
name = "hpc"
host = "login.cluster.example.com"
port = 22
user = "researcher"
key_path = "~/.ssh/id_ed25519"
scheduler_type = "slurm"                 # "slurm", "sge", or "local"
working_dir = "/scratch/user/researchloop"
max_concurrent_jobs = 4
claude_command = "claude --dangerously-skip-permissions"

# Context specific to this cluster
context = "GPUs are NVIDIA L40. Check CUDA_VISIBLE_DEVICES."
context_paths = ["./cluster-notes.md"]

# Environment variables set in SLURM jobs
[cluster.environment]
# ANTHROPIC_API_KEY = "sk-ant-..."       # Only if not using claude login

# SLURM job options (passed as #SBATCH directives)
[cluster.job_options]
gres = "gpu:l40:1"
cpus-per-task = "8"
mem = "64G"

# -- Study configuration --
[[study]]
name = "my-study"
cluster = "hpc"                          # Must match a cluster name
description = "Research into X"
claude_md_path = "./studies/my-study/CLAUDE.md"  # Study-specific context file
sprints_dir = "/scratch/user/my-study"   # Where sprints go (default: working_dir/<study>)
max_sprint_duration_hours = 8            # SLURM time limit
red_team_max_rounds = 3                  # Red-team/fix cycles
allow_loop = true                        # Allow auto-loops for this study
claude_command = ""                      # Override claude command for this study

# Inline study context (included in research prompts)
context = """
Focus on improving F1 score. Use batch size 1024.
"""

# Per-study SLURM overrides
[study.job_options]
gres = "gpu:a100:2"

# -- Notifications --
[ntfy]
url = "https://ntfy.sh"                 # Self-hosted ntfy server URL
topic = "researchloop"                   # ntfy topic name

# -- Slack integration --
[slack]
bot_token = ""                           # xoxb-... (prefer env var)
signing_secret = ""                      # Slack signing secret (prefer env var)
channel_id = "C0123456789"               # Channel or user ID for notifications
allowed_user_ids = ["U0123456789"]       # Users allowed to interact with bot
restrict_to_channel = false              # If true, only respond in channel_id

# -- Dashboard --
[dashboard]
enabled = true
host = "0.0.0.0"
port = 8080
password_hash = ""                       # bcrypt hash (prefer env var or first-run setup)
```

### Environment variable overrides

All secrets and sensitive settings can be set via environment variables with the `RESEARCHLOOP_` prefix. Environment variables take precedence over TOML values.

| Environment variable | Overrides |
|---------------------|-----------|
| `RESEARCHLOOP_SHARED_SECRET` | `shared_secret` |
| `RESEARCHLOOP_ORCHESTRATOR_URL` | `orchestrator_url` |
| `RESEARCHLOOP_DB_PATH` | `db_path` |
| `RESEARCHLOOP_ARTIFACT_DIR` | `artifact_dir` |
| `RESEARCHLOOP_SLACK_BOT_TOKEN` | `slack.bot_token` |
| `RESEARCHLOOP_SLACK_SIGNING_SECRET` | `slack.signing_secret` |
| `RESEARCHLOOP_SLACK_CHANNEL_ID` | `slack.channel_id` |
| `RESEARCHLOOP_SLACK_ALLOWED_USER_IDS` | `slack.allowed_user_ids` (comma-separated) |
| `RESEARCHLOOP_NTFY_TOPIC` | `ntfy.topic` |
| `RESEARCHLOOP_NTFY_URL` | `ntfy.url` |
| `RESEARCHLOOP_DASHBOARD_PASSWORD` | Auto-hashed on startup |
| `RESEARCHLOOP_DASHBOARD_PASSWORD_HASH` | `dashboard.password_hash` |
| `RESEARCHLOOP_DASHBOARD_PORT` | `dashboard.port` |
| `RESEARCHLOOP_DASHBOARD_HOST` | `dashboard.host` |

## Deployment

### Docker

```dockerfile
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client curl git && \
    rm -rf /var/lib/apt/lists/*

# Install Claude CLI
RUN curl -fsSL https://claude.ai/install.sh | bash

# Install researchloop
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python --no-cache \
    "researchloop @ git+https://github.com/chanind/researchloop.git"

WORKDIR /app
COPY researchloop.toml .
ENV PATH="/root/.local/bin:/root/.claude/bin:/app/.venv/bin:$PATH"
ENV RESEARCHLOOP_DB_PATH="/data/researchloop.db"
ENV RESEARCHLOOP_ARTIFACT_DIR="/data/artifacts"

EXPOSE 8080
CMD ["researchloop", "serve"]
```

### Fly.io

ResearchLoop works well on [Fly.io](https://fly.io) with a persistent volume for the database and artifacts:

```toml
# fly.toml
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

Set secrets:

```bash
fly secrets set \
  RESEARCHLOOP_SHARED_SECRET="your-secret" \
  RESEARCHLOOP_ORCHESTRATOR_URL="https://my-researchloop.fly.dev" \
  SSH_PRIVATE_KEY="$(cat ~/.ssh/id_ed25519)" \
  RESEARCHLOOP_DASHBOARD_PASSWORD="your-password" \
  -a my-researchloop
```

Deploy:

```bash
fly deploy
```

### SSH key setup for Docker/Fly.io

The orchestrator needs SSH access to your HPC cluster. Add an entrypoint script that writes the key from a secret:

```bash
#!/bin/bash
set -euo pipefail
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
fi
mkdir -p /data/artifacts
exec "$@"
```

## Dashboard

The web dashboard provides a browser-based interface for managing ResearchLoop. It is served by the orchestrator at `/dashboard/`.

### Features

- **Studies list** -- overview of all configured studies with sprint counts
- **Study detail** -- view study configuration, submit new sprints with GPU/memory overrides
- **Sprint list** -- filterable list of all sprints across studies
- **Sprint detail** -- live status with progress.md display, tool log, script output, report rendering (markdown to HTML), PDF download, and artifact listing
- **Auto-loop management** -- start, stop, and resume loops with context guidance and job option overrides
- **Loop detail** -- progress tracking with links to individual loop sprints
- **Refresh** -- pull live status from the cluster via SSH (detects current pipeline step, reads logs)

### Authentication

On first visit, the dashboard prompts you to set a password. Alternatively, set `RESEARCHLOOP_DASHBOARD_PASSWORD` as an environment variable and the password is auto-hashed on startup.

Sessions use signed cookies (7-day expiry) with a signing key persisted in the database. All mutating dashboard actions are protected by CSRF tokens.

### CLI authentication

The CLI authenticates to the orchestrator using password-based token auth:

```bash
researchloop connect https://your-server.fly.dev
# Prompts for password, saves token to ~/.config/researchloop/credentials.json

researchloop status        # Check connection
researchloop disconnect    # Remove saved credentials
```

## Slack integration

### Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app
2. Enable **Event Subscriptions** with request URL: `https://your-server.fly.dev/api/slack/events`
3. Subscribe to bot events: `app_mention`, `message.im`
4. Add **OAuth Scopes**: `chat:write`, `files:write`
5. Install the app to your workspace
6. Set environment variables:

```bash
RESEARCHLOOP_SLACK_BOT_TOKEN="xoxb-..."
RESEARCHLOOP_SLACK_SIGNING_SECRET="..."
RESEARCHLOOP_SLACK_CHANNEL_ID="C0123456789"          # For notifications
RESEARCHLOOP_SLACK_ALLOWED_USER_IDS="U01,U02"        # Comma-separated
```

### Commands

| Command | Description |
|---------|-------------|
| `sprint run <study> <idea>` | Submit a new sprint |
| `sprint list` | List recent sprints |
| `auth status` | Check if Claude CLI is authenticated |
| `help` | Show available commands |

### Conversational mode

Beyond commands, the Slack bot supports free-form conversations. Messages in a thread are tracked as a Claude session (via `--resume`), so the bot remembers context within a thread. The bot can:

- Discuss research ideas and help plan sprints
- Review results from completed sprints
- Look up papers and references (web search)
- Execute actions (start sprints, loops) when you ask

### Notifications

When sprints complete or fail, the bot sends notifications to the configured channel. Completed sprint notifications include the summary and a link to the dashboard. If a PDF report was generated, it is uploaded as an attachment.

## CLI reference

```
researchloop [OPTIONS] COMMAND

Options:
  -c, --config PATH    Path to researchloop.toml
  --version            Show version
  --help               Show help

Commands:
  init                 Initialize a new project with example config
  serve                Start the orchestrator server
  connect [URL]        Authenticate CLI to a remote orchestrator
  disconnect           Remove saved credentials
  status               Show connection status

  study list           List all configured studies
  study show NAME      Show study details and recent sprints
  study init NAME      Scaffold a new study directory with starter CLAUDE.md

  sprint run IDEA      Submit a new sprint (-s/--study required)
  sprint list          List sprints (--study, --limit options)
  sprint show ID       Show sprint details, artifacts, and summary
  sprint cancel ID     Cancel a running sprint

  loop start           Start an auto-loop (-s/--study, -n/--count, -m/--context)
  loop status          Show all auto-loops
  loop stop LOOP_ID    Stop a running auto-loop

  cluster list         List configured clusters
  cluster check        Test SSH connectivity (--name for specific cluster)
```

## API endpoints

The orchestrator exposes a REST API at `/api/`:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/auth` | Password | Get API token |
| `GET` | `/api/studies` | Token/Secret | List all studies |
| `GET` | `/api/sprints` | Token/Secret | List sprints (`?study_name=`, `?limit=`) |
| `GET` | `/api/sprints/{id}` | Token/Secret | Get sprint details |
| `POST` | `/api/sprints` | Token/Secret | Create and submit a sprint |
| `POST` | `/api/sprints/{id}/cancel` | Token/Secret | Cancel a sprint |
| `POST` | `/api/loops` | Token/Secret | Start an auto-loop |
| `POST` | `/api/loops/{id}/stop` | Token/Secret | Stop an auto-loop |
| `POST` | `/api/webhook/sprint-complete` | Webhook token | Sprint completion callback |
| `POST` | `/api/webhook/heartbeat` | Webhook token | Runner heartbeat with logs |
| `POST` | `/api/artifacts/{sprint_id}` | Webhook token | Upload artifact file |
| `POST` | `/api/slack/events` | Slack signature | Slack Events API handler |

Authentication uses either a bearer token (from `/api/auth`) or the `X-Shared-Secret` header. Webhook endpoints use per-sprint `X-Webhook-Token` headers.

## Development

### Setup

```bash
git clone https://github.com/chanind/researchloop.git
cd researchloop
uv sync
```

### Run tests

```bash
# Unit tests (339 tests, ~3s)
uv run pytest tests/ -v -m "not integration"

# Integration tests (requires Docker for SLURM container)
docker build -t researchloop-slurm-test tests/docker/slurm/
uv run pytest tests/integration/ -v --timeout=120
```

### Code quality

```bash
uv run ruff check .             # Lint
uv run ruff format --check .    # Format check
uv run pyright researchloop/    # Type check
```

### Project structure

```
researchloop/
  core/
    config.py          TOML config loading into dataclasses
    models.py          SprintStatus enum, Sprint/Study/AutoLoop dataclasses
    orchestrator.py    Orchestrator class + create_app() FastAPI factory
    credentials.py     CLI credential storage (~/.config/researchloop/)
    auth.py            Claude CLI auth checking
  db/
    database.py        Async SQLite wrapper (WAL mode, auto-migrations)
    migrations.py      Schema definitions (7 tables + indexes)
    queries.py         Async CRUD functions (parameterized SQL, return dicts)
  clusters/
    ssh.py             SSHConnection + SSHManager (connection pooling)
    monitor.py         JobMonitor (polls active jobs, heartbeat tracking)
  schedulers/
    base.py            BaseScheduler ABC
    slurm.py           SlurmScheduler (sbatch/squeue/sacct/scancel)
    sge.py             SGEScheduler (qsub/qstat/qacct/qdel)
    local.py           LocalScheduler (subprocesses, for testing)
  sprints/
    manager.py         SprintManager (create/submit/cancel/handle_completion)
    auto_loop.py       AutoLoopController (start/stop/resume, idea generation)
  studies/
    manager.py         StudyManager (config-to-DB sync, cluster resolution)
  runner/
    pipeline.py        Pipeline class (research pipeline steps)
    claude.py          run_claude() wrapper + render_template()
    upload.py          upload_artifacts(), send_webhook(), send_heartbeat()
    main.py            Runner CLI entry point (researchloop-runner)
    templates/         Jinja2 prompt templates (6 templates)
    job_templates/     SLURM (slurm.sh.j2) and SGE (sge.sh.j2) job scripts
  comms/
    base.py            BaseNotifier ABC
    ntfy.py            NtfyNotifier (ntfy.sh push notifications)
    slack.py           SlackNotifier + verify_slack_signature()
    conversation.py    ConversationManager (Slack threads to Claude sessions)
    router.py          NotificationRouter (fan-out to all backends)
  dashboard/
    app.py             ASGI app entry point
    auth.py            Password auth (bcrypt + signed session cookies + CSRF)
    routes.py          Dashboard HTML routes
    templates/         Jinja2 HTML templates (9 templates)
  cli.py               Click CLI entry point
```

### CI

GitHub Actions runs on every push and PR to `main`:

- **Lint** -- `ruff check`, `ruff format --check`, `pyright`
- **Test** -- `pytest` on Python 3.10, 3.12, 3.13
- **Integration** -- builds a Docker SLURM container and runs integration tests

## License

MIT
