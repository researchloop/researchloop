# ResearchLoop

**Automated AI research sprints on HPC clusters.**

---

ResearchLoop automates multi-step AI research pipelines on SLURM and SGE clusters. You describe a research idea, and ResearchLoop submits it to your HPC cluster where [Claude Code](https://docs.anthropic.com/en/docs/claude-code) executes a full research pipeline -- coding, red-teaming, fixing, reporting -- inside a single job. Results are reported back via webhooks, Slack, or push notifications, and you can monitor everything from a web dashboard or the CLI.

The platform is built for researchers who run experiments on shared HPC infrastructure and want to iterate faster without babysitting jobs. Define your studies, point ResearchLoop at your cluster, and let it handle the rest: job submission, progress tracking, artifact collection, and even automatic generation of follow-up research ideas.

## How it works

ResearchLoop has two components:

1. **Orchestrator** (`researchloop serve`) -- a lightweight server that manages studies and sprints in SQLite, submits jobs to HPC clusters via SSH, receives completion webhooks, stores artifacts, and serves the web dashboard.
2. **Sprint Runner** -- runs inside each SLURM/SGE job on the HPC cluster. Chains `claude -p` calls through the research pipeline, then sends artifacts and results back to the orchestrator.

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

## Core concepts

| Concept | Description |
|---------|-------------|
| **Study** | A sustained research effort (e.g., "synthetic SAE improvements"). Tied to a cluster, has its own context and configuration. |
| **Sprint** | A single research attempt within a study. Gets a short ID (`sp-a3f7b2`), its own directory, and runs the full pipeline. |
| **Auto-loop** | Automatic sequential sprint execution. After each sprint, Claude analyzes results and generates the next research idea. |

## Sprint pipeline

Each sprint runs these steps inside a single SLURM/SGE job:

1. **Research** -- execute the research idea (coding, experiments, analysis)
2. **Red-team** -- critique the work, find flaws (up to N rounds with fix steps)
3. **Fix** -- address issues found by the red-team
4. **Report** -- generate a comprehensive markdown report
5. **Summarize** -- write a short summary for notifications and the dashboard

All steps share a single Claude session (via `--resume`), so Claude maintains full context across the sprint.

## Features

- **HPC cluster integration** -- submit, monitor, and cancel jobs on SLURM and SGE clusters via SSH
- **Multi-step research pipeline** -- research, red-team, fix, report, summarize with configurable rounds
- **Auto-loop** -- chain sprints automatically with AI-generated follow-up ideas
- **Web dashboard** -- monitor studies, sprints, and loops from a browser with live status refresh
- **Slack bot** -- start sprints, check status, and have research conversations via Slack
- **CLI** -- full remote management from the command line with token-based auth
- **Progress tracking** -- live `progress.md` and `output.log` streaming from cluster to dashboard
- **Notifications** -- push notifications via ntfy.sh and Slack with PDF report attachments
- **Per-sprint security** -- webhook tokens, CSRF protection, signed session cookies
- **Context hierarchy** -- global, cluster, and study-level context files and inline configuration

## Next steps

- [Getting Started](getting-started.md) -- install and run your first sprint
- [Configuration](configuration.md) -- full `researchloop.toml` reference
- [Deployment](deployment.md) -- Docker and Fly.io deployment guides
- [CLI Reference](cli.md) -- all available commands
