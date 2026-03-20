# Configuration

ResearchLoop is configured via a `researchloop.toml` file. The config file is searched in this order:

1. Explicit path via `--config` / `-c` CLI flag
2. `researchloop.toml` in the current working directory
3. `~/.config/researchloop/researchloop.toml`

## Complete reference

```toml
# ── Top-level settings ──────────────────────────────────────────

db_path = "researchloop.db"              # SQLite database location
artifact_dir = "artifacts"               # Local directory for uploaded artifacts
shared_secret = "your-secret"            # Auth token for runner-to-orchestrator communication
orchestrator_url = "https://example.com" # Public URL where runners send webhooks
claude_command = ""                      # Override the claude command globally

# Global context included in all sprints (all studies, all clusters)
context = "Always use Python 3.10+ features."
context_paths = ["./global-context.md"]  # Files whose contents are included as context


# ── Cluster configuration ───────────────────────────────────────

[[cluster]]
name = "hpc"                             # Unique cluster identifier
host = "login.cluster.example.com"       # SSH hostname
port = 22                                # SSH port
user = "researcher"                      # SSH username
key_path = "~/.ssh/id_ed25519"           # Path to SSH private key
scheduler_type = "slurm"                 # "slurm", "sge", or "local"
working_dir = "/scratch/user/researchloop"  # Base directory on the cluster
max_concurrent_jobs = 4                  # Max simultaneous jobs on this cluster
claude_command = "claude --dangerously-skip-permissions"  # Claude CLI command

# Cluster-specific context (appended after global context)
context = "GPUs are NVIDIA L40. Check CUDA_VISIBLE_DEVICES before running."
context_paths = ["./cluster-notes.md"]

# Environment variables injected into job scripts
[cluster.environment]
# ANTHROPIC_API_KEY = "sk-ant-..."       # Only needed if not using claude login

# SLURM/SGE job options (passed as #SBATCH or #$ directives)
[cluster.job_options]
gres = "gpu:l40:1"
cpus-per-task = "8"
mem = "64G"


# ── Study configuration ─────────────────────────────────────────

[[study]]
name = "my-study"                        # Unique study identifier
cluster = "hpc"                          # Must match a [[cluster]] name
description = "Research into X"          # Human-readable description

# Study context (appended after global + cluster context)
claude_md_path = "./studies/my-study/CLAUDE.md"  # File with study-specific context
context = """
Focus on improving F1 score. Use batch size 1024.
"""

# Sprint settings
sprints_dir = "/scratch/user/my-study"   # Where sprint dirs are created
                                         # Default: <working_dir>/<study_name>
max_sprint_duration_hours = 8            # SLURM --time limit
red_team_max_rounds = 3                  # Number of red-team/fix cycles
allow_loop = true                        # Whether auto-loops are allowed

# Override claude command for this study
claude_command = ""

# Per-study SLURM/SGE overrides (merged with cluster.job_options)
[study.job_options]
gres = "gpu:a100:2"


# ── Notifications ────────────────────────────────────────────────

[ntfy]
url = "https://ntfy.sh"                 # ntfy server URL (default: ntfy.sh)
topic = "researchloop"                   # ntfy topic for push notifications

[slack]
bot_token = ""                           # Slack Bot User OAuth Token (xoxb-...)
signing_secret = ""                      # Slack Signing Secret
channel_id = "C0123456789"               # Channel or user ID for notifications
allowed_user_ids = ["U0123456789"]       # Users allowed to interact with bot
restrict_to_channel = false              # If true, only respond in channel_id
                                         # (DMs are always allowed)


# ── Dashboard ────────────────────────────────────────────────────

[dashboard]
enabled = true                           # Enable the web dashboard
host = "0.0.0.0"                         # Bind address
port = 8080                              # Bind port
password_hash = ""                       # bcrypt hash (prefer env var or setup page)
```

## Context hierarchy

Context is assembled from multiple sources and concatenated in this order:

1. **Global context** -- `context` and files from `context_paths` at the top level
2. **Cluster context** -- `context` and files from `context_paths` on the cluster
3. **Study context** -- `context` inline text and the file at `claude_md_path`

The combined context is:
- Included in all sprint research prompts
- Written as `CLAUDE.md` to the sprint directory on the cluster (so Claude CLI picks it up automatically)
- Used by the auto-loop idea generator

## Environment variable overrides

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
| `RESEARCHLOOP_DASHBOARD_PASSWORD` | Auto-hashed with bcrypt on startup |
| `RESEARCHLOOP_DASHBOARD_PASSWORD_HASH` | `dashboard.password_hash` |
| `RESEARCHLOOP_DASHBOARD_PORT` | `dashboard.port` |
| `RESEARCHLOOP_DASHBOARD_HOST` | `dashboard.host` |

!!! tip "Recommended approach"
    Keep your `researchloop.toml` with only structural config (clusters, studies, context). Put secrets in environment variables or your deployment platform's secret manager.

## Study CLAUDE.md

Each study can have a `CLAUDE.md` file that provides domain-specific context to Claude. This file is included in every sprint's research prompt and written to the sprint directory on the cluster.

Scaffold a starter file:

```bash
researchloop study init my-study
```

This creates `studies/my-study/CLAUDE.md` with sections for:

- **Overview** -- what you're studying
- **Background** -- key papers, prior findings, domain knowledge
- **Codebase** -- existing code, data formats, infrastructure
- **Goals** -- what you're trying to learn or build
- **Constraints** -- rules for the sprints to follow

## Job options

Job options are merged from three levels (later values override earlier):

1. `cluster.job_options` -- defaults for all studies on this cluster
2. `study.job_options` -- overrides for a specific study
3. Per-sprint overrides -- from the dashboard form or API request

Common SLURM options:

| Option | Example | Description |
|--------|---------|-------------|
| `gres` | `gpu:l40:1` | GPU resources |
| `mem` | `64G` | Memory limit |
| `cpus-per-task` | `8` | CPU cores |
| `partition` | `gpu` | SLURM partition |
| `qos` | `high` | Quality of service |
