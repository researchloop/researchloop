# ResearchLoop

**Run AI-automated research experiments on your HPC cluster. Monitor from anywhere.**

[![CI](https://github.com/researchloop/researchloop/actions/workflows/ci.yml/badge.svg)](https://github.com/researchloop/researchloop/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/researchloop.svg)](https://pypi.org/project/researchloop/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

---

ResearchLoop submits AI-powered research experiments to your SLURM or SGE cluster, then reports back the results. You describe a research idea in natural language, it handles the rest: submitting the job, running a multi-step pipeline with [Claude Code](https://docs.anthropic.com/en/docs/claude-code), red-teaming the results, generating a report, and notifying you when it's done.

```bash
pip install researchloop

# Submit an experiment to your cluster
researchloop sprint run "Investigate whether batch normalization improves convergence" --study my-project

# Start an auto-loop: 5 experiments, each building on the last
researchloop loop start --study my-project --count 5 --context "Focus on improving F1 score"
```

Monitor everything from a web dashboard, Slack, or the CLI -- no need to SSH in and check on jobs.

## Why ResearchLoop?

If you run experiments on shared HPC clusters, you know the pain: SSH in, write a script, submit with sbatch, wait, check logs, repeat. ResearchLoop automates this loop:

1. **You describe what to investigate** (via CLI, dashboard, or Slack)
2. **ResearchLoop submits a job** to your cluster via SSH
3. **Claude runs the full experiment** -- writes code, runs it, analyzes results
4. **A red-team step critiques the work** and Claude fixes any issues
5. **You get a report** with a summary, PDF, and all artifacts

The **auto-loop** feature takes this further: after each experiment, Claude analyzes the results and proposes the next one. You set how many iterations, and walk away.

## Get started in 5 minutes

**Prerequisites:** Python 3.10+, SSH access to an HPC cluster, [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed on the cluster.

### 1. Install and initialize

```bash
pip install researchloop
researchloop init
```

### 2. Edit `researchloop.toml`

```toml
shared_secret = "pick-a-secret"
orchestrator_url = "http://localhost:8080"

[[cluster]]
name = "my-cluster"
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
name = "my-project"
cluster = "my-cluster"
description = "Investigating X"
```

### 3. Start the server and run your first sprint

```bash
researchloop serve &
researchloop connect http://localhost:8080
researchloop sprint run "Try approach X on dataset Y" --study my-project
```

That's it. ResearchLoop SSHes to your cluster, submits the job, and you can monitor progress from the dashboard at `http://localhost:8080/dashboard/`.

## Three ways to interact

### Web dashboard

Browse to `/dashboard/` to see all your studies, sprints, and loops. Submit new sprints, start loops with custom GPU/memory settings, refresh live status from the cluster, and read reports -- all from the browser.

### Slack bot

Chat with the bot to start sprints, check status, or discuss research ideas. The bot maintains conversation context across a thread, so you can have a back-and-forth about what to try next.

```
You: What should I investigate next based on the results from sp-a3f7b2?
Bot: Based on the findings, I'd suggest... [ACTION: sprint_run {"study": "my-project", "idea": "..."}]
```

See the [Slack setup guide](https://researchloop.github.io/researchloop/slack/) for configuration.

### CLI

```bash
researchloop sprint run "idea" --study my-project   # Submit a sprint
researchloop sprint list                             # List recent sprints
researchloop sprint show sp-a3f7b2                   # View details
researchloop loop start --study my-project --count 5 # Auto-loop
researchloop loop stop loop-b4e1c9                   # Stop a loop
```

## Customizing your studies

Each study can have its own context, cluster settings, and configuration:

```toml
[[study]]
name = "sae-research"
cluster = "my-cluster"
max_sprint_duration_hours = 12
red_team_max_rounds = 2
allow_loop = true

# Tell Claude what this study is about and how to approach it
context = """
You are researching sparse autoencoder architectures.
Always train for 200M samples. Use batch size 1024.
Validate on the variation models listed in ~/reference/models.txt.
"""

# Or point to a file with detailed instructions
claude_md_path = "./studies/sae-research/CLAUDE.md"

# Override GPU/memory for this study
[study.job_options]
gres = "gpu:a100:2"
mem = "128G"
```

The context hierarchy is: **global** > **cluster** > **study**. All levels are merged and included in every sprint's prompt.

## Deployment

For production, deploy the orchestrator as a Docker container on Fly.io, Railway, or any platform that supports persistent volumes:

```bash
pip install researchloop
# See deployment guide for Docker/Fly.io setup
```

Full deployment guide: [researchloop.github.io/researchloop/deployment](https://researchloop.github.io/researchloop/deployment/)

## Documentation

Full docs at **[researchloop.github.io/researchloop](https://researchloop.github.io/researchloop/)**, including:

- [Configuration reference](https://researchloop.github.io/researchloop/configuration/) -- all TOML options and environment variables
- [Deployment guide](https://researchloop.github.io/researchloop/deployment/) -- Docker, Fly.io, SSH key setup
- [Dashboard guide](https://researchloop.github.io/researchloop/dashboard/) -- web UI features and authentication
- [Slack integration](https://researchloop.github.io/researchloop/slack/) -- setup, commands, conversational mode
- [CLI reference](https://researchloop.github.io/researchloop/cli/) -- all commands with examples
- [Security](https://researchloop.github.io/researchloop/security/) -- authentication, CSRF, webhook tokens
- [Development](https://researchloop.github.io/researchloop/development/) -- contributing, testing, architecture

## Contributing

```bash
git clone https://github.com/researchloop/researchloop.git
cd researchloop
uv sync
uv run pytest tests/ -m "not integration"   # Unit tests
uv run ruff check . && uv run pyright researchloop/  # Lint + type check
```

Integration tests run against a real SLURM scheduler in Docker -- see [development guide](https://researchloop.github.io/researchloop/development/).

## License

MIT
