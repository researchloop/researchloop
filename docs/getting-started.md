# Getting Started

## Prerequisites

- **Python 3.10+**
- **[uv](https://docs.astral.sh/uv/)** (recommended) or pip
- **SSH access** to an HPC cluster with SLURM or SGE
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** installed and authenticated on the HPC cluster

## Installation

### From GitHub

```bash
pip install git+https://github.com/chanind/researchloop.git
```

### For development

```bash
git clone https://github.com/chanind/researchloop.git
cd researchloop
uv sync
```

## Initialize a project

```bash
researchloop init
```

This creates a `researchloop.toml` configuration file and an `artifacts/` directory in the current directory.

## Configure

Edit `researchloop.toml` with your cluster and study settings:

```toml
shared_secret = "change-me"
orchestrator_url = "https://your-server.fly.dev"

[[cluster]]
name = "hpc"
host = "login.cluster.example.com"
user = "researcher"
key_path = "~/.ssh/id_ed25519"
scheduler_type = "slurm"
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

See the [Configuration](configuration.md) page for all available options.

## Start the orchestrator

```bash
researchloop serve
```

This starts the FastAPI server on port 8080 (configurable). The server provides:

- REST API for sprint and study management
- Web dashboard at `/dashboard/`
- Webhook endpoints for sprint runners
- Slack Events API handler

## Connect the CLI

If running the orchestrator on a remote server, connect the CLI:

```bash
researchloop connect https://your-server.fly.dev
# Prompts for the dashboard password
```

Check connection status:

```bash
researchloop status
```

## Verify cluster connectivity

Test that the orchestrator can reach your HPC cluster:

```bash
researchloop cluster list
researchloop cluster check
```

## Create a study

Scaffold a study directory with a starter `CLAUDE.md`:

```bash
researchloop study init my-research
```

Edit `studies/my-research/CLAUDE.md` to describe your research area, background, goals, and constraints. This context is given to Claude at the start of every sprint.

Then add `claude_md_path` to your study config in `researchloop.toml`:

```toml
[[study]]
name = "my-research"
cluster = "hpc"
claude_md_path = "./studies/my-research/CLAUDE.md"
```

## Run your first sprint

```bash
researchloop sprint run "implement baseline model and evaluate on test set" --study my-research
```

This will:

1. Create a sprint record in the database
2. Render all pipeline prompts (research, red-team, fix, report, summarize)
3. Generate a self-contained job script
4. SSH to the cluster, create the sprint directory, and upload the script
5. Submit the job via `sbatch`
6. Send a notification that the sprint has started

## Monitor progress

### From the CLI

```bash
researchloop sprint list
researchloop sprint show sp-a3f7b2
```

### From the dashboard

Open `http://localhost:8080/dashboard/` in your browser. The sprint detail page shows:

- Current pipeline step
- Live progress from `progress.md`
- Script output from `output.log`
- Tool usage log
- Report (markdown rendered to HTML)
- Downloadable artifacts

Click "Refresh" to pull the latest status from the cluster.

### From Slack

If Slack is configured, the bot will notify you when sprints complete or fail. You can also check status with `sprint list`.

## Run an auto-loop

Auto-loops chain multiple sprints together. After each sprint completes, Claude analyzes the results and generates the next research idea:

```bash
researchloop loop start --study my-research --count 5 --context "focus on improving F1 score"
```

Monitor loop progress:

```bash
researchloop loop status
```

Stop a running loop:

```bash
researchloop loop stop loop-abc123
```
