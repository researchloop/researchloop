# ResearchLoop

Automated research sprint platform for HPC clusters. Orchestrates multi-step AI research pipelines (research, red-team, fix, validate, report) on SLURM/SGE clusters, with notifications and a web API.

## How it works

ResearchLoop has two sides:

1. **Orchestrator** — a lightweight server (Docker / Fly.io / Cloud Run) that manages studies, submits jobs via SSH, receives webhooks, and stores artifacts.
2. **Sprint Runner** — runs *inside* each HPC job. Chains `claude -p` calls through a sub-agent pipeline and reports results back.

```
Orchestrator (Docker)              HPC Cluster
┌──────────────────────┐           ┌──────────────────────┐
│ CLI / API / Dashboard│──SSH──▶   │ SLURM / SGE          │
│                      │           │                      │
│ SQLite metadata      │◀─webhook──│ Sprint Runner         │
│ Artifact storage     │◀─upload───│  claude -p "research" │
│ ntfy.sh notifications│           │  claude -p "red-team" │
└──────────────────────┘           │  claude -p "fix"      │
                                   │  claude -p "validate" │
                                   │  claude -p "report"   │
                                   │  claude -p "summarize"│
                                   └──────────────────────┘
```

### Core concepts

- **Study** — a sustained research effort (e.g. "hierarchy recovery"). Tied to a cluster, has its own CLAUDE.md and sprints directory.
- **Sprint** — a single research attempt within a study. Gets a short ID (`sp-a3f7b2`), its own directory, and runs the full sub-agent pipeline.
- **Auto-loop** — automatic sequential sprint execution where each sprint's idea is generated from prior results.

### Sprint pipeline

Each sprint runs these steps inside a single SLURM/SGE job:

1. **Research** — execute the research idea
2. **Red-team** — critique the work (up to N rounds, with fix steps between)
3. **Validate** — verify code runs and results are reproducible
4. **Report** — generate a comprehensive report
5. **Summarize** — write a short summary for notifications
6. **Upload** — send artifacts and completion webhook to the orchestrator

## Quickstart

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- SSH access to an HPC cluster with SLURM (or use `local` scheduler for testing)
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) installed on the HPC cluster

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

### Authenticate

Claude CLI needs to be authenticated on both the orchestrator server and the HPC cluster.

**Interactive (local machine or SSH session with a browser):**
```bash
researchloop login        # runs claude auth login
researchloop auth-status  # check if authenticated
```

**Headless server (Fly.io, Docker, CI):**
```bash
# On your local machine, generate a setup token:
claude setup-token

# Then set it as an env var on the server:
fly secrets set CLAUDE_SETUP_TOKEN="<token>" -a your-app
# or: export CLAUDE_SETUP_TOKEN="<token>"
```

**API key (alternative to Max subscription):**
```bash
# Set on the server:
fly secrets set ANTHROPIC_API_KEY="sk-ant-..." -a your-app

# Or for HPC cluster jobs, set in the cluster config:
# [cluster.environment]
# ANTHROPIC_API_KEY = "sk-ant-..."
```

You can also check auth status from Slack by sending "auth status" to the bot.

### Initialize a project

```bash
researchloop init
# Creates researchloop.toml and artifacts/ directory
```

### Configure

Edit `researchloop.toml`:

```toml
db_path = "researchloop.db"
artifact_dir = "artifacts"
shared_secret = "your-shared-secret"             # auth between runner and orchestrator
orchestrator_url = "https://your-host.com" # where the runner sends webhooks

[[cluster]]
name = "hpc"
host = "login.cluster.example.com"
user = "researcher"
key_path = "~/.ssh/id_ed25519"
scheduler_type = "slurm"                   # "slurm", "sge", or "local"
working_dir = "/scratch/researcher/researchloop"

# Optional: set env vars for SLURM jobs (only needed if not using `claude login`)
# [cluster.environment]
# ANTHROPIC_API_KEY = "sk-ant-..."

[[study]]
name = "hierarchy-recovery"
cluster = "hpc"
description = "Investigating hierarchy recovery in SAEs"
claude_md_path = "./studies/hierarchy-recovery/CLAUDE.md"
sprints_dir = "./studies/hierarchy-recovery/sprints"
max_sprint_duration_hours = 8
red_team_max_rounds = 3

[ntfy]
topic = "researchloop"                     # push notifications via ntfy.sh

[dashboard]
port = 8080
```

### Environment variables

Secrets and sensitive settings can be configured via environment variables instead of (or in addition to) the TOML file. Env vars use the `RESEARCHLOOP_` prefix and take precedence over TOML values.

| Env var | Overrides |
|---------|-----------|
| `RESEARCHLOOP_SHARED_SECRET` | `shared_secret` |
| `RESEARCHLOOP_ORCHESTRATOR_URL` | `orchestrator_url` |
| `RESEARCHLOOP_DB_PATH` | `db_path` |
| `RESEARCHLOOP_ARTIFACT_DIR` | `artifact_dir` |
| `RESEARCHLOOP_SLACK_BOT_TOKEN` | `slack.bot_token` |
| `RESEARCHLOOP_SLACK_SIGNING_SECRET` | `slack.signing_secret` |
| `RESEARCHLOOP_SLACK_CHANNEL_ID` | `slack.channel_id` |
| `RESEARCHLOOP_NTFY_TOPIC` | `ntfy.topic` |
| `RESEARCHLOOP_NTFY_URL` | `ntfy.url` |
| `RESEARCHLOOP_DASHBOARD_PASSWORD_HASH` | `dashboard.password_hash` |
| `RESEARCHLOOP_DASHBOARD_PORT` | `dashboard.port` |
| `RESEARCHLOOP_DASHBOARD_HOST` | `dashboard.host` |

This means your `researchloop.toml` can contain only non-secret structural config (clusters, studies), while secrets live in env vars or your deployment platform's secret manager.

### Run a sprint

```bash
# Submit a single sprint
researchloop sprint run "try feature absorption on layer 12" --study hierarchy-recovery

# List sprints
researchloop sprint list

# Show sprint details
researchloop sprint show sp-a3f7b2

# Cancel a running sprint
researchloop sprint cancel sp-a3f7b2
```

### Start the orchestrator server

```bash
# Start the API/webhook server
researchloop serve

# Or with Docker
docker build -t researchloop .
docker run -p 8080:8080 -v $(pwd):/app researchloop
```

### Other commands

```bash
# Studies
researchloop study list
researchloop study show hierarchy-recovery

# Auto-loops
researchloop loop start --study hierarchy-recovery --count 5
researchloop loop status
researchloop loop stop loop-abc123

# Clusters
researchloop cluster list
researchloop cluster check          # test SSH connectivity
```

## API endpoints

The orchestrator exposes these HTTP endpoints:

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/studies` | No | List all studies |
| `GET` | `/api/sprints` | No | List sprints (optional `?study_name=`) |
| `GET` | `/api/sprints/{id}` | No | Get sprint details |
| `POST` | `/api/webhook/sprint-complete` | Shared secret | Sprint completion callback |
| `POST` | `/api/webhook/heartbeat` | Shared secret | Runner heartbeat |
| `POST` | `/api/artifacts/{sprint_id}` | Shared secret | Upload artifact file |

Shared secret is passed via `X-Shared-Secret` header.

## Project structure

```
researchloop/
├── core/           Config, models, orchestrator + FastAPI app
├── db/             SQLite database, migrations, query functions
├── clusters/       SSH connection manager, job monitor
├── schedulers/     SLURM, SGE (planned), local (for testing)
├── sprints/        Sprint manager, auto-loop controller
├── studies/        Study manager (config → DB sync)
├── runner/         Runs inside HPC jobs
│   ├── pipeline.py   Sub-agent pipeline orchestration
│   ├── claude.py      Claude CLI wrapper
│   ├── upload.py      Artifact upload + webhooks
│   ├── templates/     Jinja2 prompt templates
│   └── job_templates/ SLURM job script template
├── comms/          Notification backends (ntfy.sh, Slack planned)
├── dashboard/      FastAPI ASGI app entry point
└── cli.py          Click CLI
```

## Development

```bash
# Install dev dependencies
uv sync

# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check .
uv run ruff format --check .

# Format
uv run ruff format .
```

## Roadmap

- [x] **Phase 1** — Core platform, sprint runner, SLURM scheduler, CLI, tests, CI
- [x] **Phase 2** — Auto-loop with LLM-generated ideas between sprints
- [x] **Phase 3** — Slack integration (Events API, conversational threads)
- [x] **Phase 4** — Web dashboard (FastAPI + Jinja2, password auth)
- [x] **Phase 5** — SGE scheduler, error handling, polish

## License

MIT
