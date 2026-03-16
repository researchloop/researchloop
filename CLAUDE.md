# CLAUDE.md

## Project overview

ResearchLoop is an automated research sprint platform for HPC clusters. It orchestrates multi-step AI research pipelines on SLURM/SGE clusters using `claude -p` for all AI work. The orchestrator is a lightweight Docker container; all heavy compute runs on HPC.

## Architecture

Two processes:
1. **Orchestrator** (`researchloop serve`) — FastAPI server that manages studies/sprints in SQLite, submits jobs via SSH, receives webhooks from runners, stores artifacts. Also serves the web dashboard and handles Slack events.
2. **Sprint Runner** (`researchloop-runner run`) — runs inside each SLURM/SGE job on HPC. Chains `claude -p` calls through a pipeline (research → red-team → fix → validate → report → summarize), then uploads artifacts and sends a completion webhook.

Key design decisions:
- All AI work runs on HPC, never on the orchestrator
- `claude -p --output-format json` for all agent invocations (no Agent SDK dependency)
- SSH to HPC login nodes for sbatch/squeue/scancel/qsub/qdel
- Job completion via webhook (runner → orchestrator), SSH polling as fallback
- SQLite (aiosqlite, WAL mode) for metadata
- Jinja2 templates for all prompts and job scripts
- Auto-loop idea generation tries `claude -p` locally, falls back to heuristic

## Tech stack

Python 3.10+, uv, asyncio throughout. Key deps: click (CLI), FastAPI (API + dashboard), aiosqlite (DB), asyncssh (SSH), httpx (HTTP client), Jinja2 (templates), bcrypt + itsdangerous (dashboard auth).

## Commands

```bash
uv sync                              # install deps
uv run pytest tests/ -v              # run tests (175 tests, ~3s)
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run researchloop --help           # CLI help
```

## Package layout

```
researchloop/
  core/config.py        — TOML config loading into dataclasses
  core/models.py        — SprintStatus enum, Sprint/Study/AutoLoop dataclasses, ID generation
  core/orchestrator.py  — Orchestrator class + create_app() FastAPI factory (API + Slack + dashboard)
  db/database.py        — async SQLite wrapper (WAL mode, auto-migrations)
  db/migrations.py      — CREATE TABLE statements (6 tables + indexes)
  db/queries.py         — async CRUD functions (all take Database as first arg, return dicts)
  clusters/ssh.py       — SSHConnection + SSHManager (connection pooling via asyncssh)
  clusters/monitor.py   — JobMonitor (polls active jobs, detects abandoned via heartbeat)
  schedulers/base.py    — BaseScheduler ABC (submit/status/cancel/generate_script)
  schedulers/slurm.py   — SlurmScheduler (sbatch/squeue/sacct/scancel)
  schedulers/sge.py     — SGEScheduler (qsub/qstat/qacct/qdel)
  schedulers/local.py   — LocalScheduler (subprocesses, for testing)
  sprints/manager.py    — SprintManager (create/submit/cancel/handle_completion)
  sprints/auto_loop.py  — AutoLoopController (start/stop, LLM idea generation between sprints)
  studies/manager.py    — StudyManager (config→DB sync, cluster config resolution)
  runner/pipeline.py    — Pipeline class (runs the 5-step research pipeline)
  runner/claude.py      — run_claude() wrapper + render_template()
  runner/upload.py      — upload_artifacts(), send_webhook(), send_heartbeat()
  runner/templates/     — 7 Jinja2 prompt templates (.md.j2)
  runner/job_templates/ — SLURM (slurm.sh.j2) and SGE (sge.sh.j2) job script templates
  comms/base.py         — BaseNotifier ABC
  comms/ntfy.py         — NtfyNotifier (ntfy.sh push notifications)
  comms/slack.py        — SlackNotifier + verify_slack_signature()
  comms/conversation.py — ConversationManager (Slack threads → Claude sessions via --resume)
  comms/router.py       — NotificationRouter (fan-out to all backends)
  dashboard/app.py      — ASGI app entry point for `researchloop serve`
  dashboard/auth.py     — Password auth (bcrypt + signed session cookies)
  dashboard/routes.py   — Dashboard HTML routes (studies, sprints, loops, artifacts, login)
  dashboard/templates/  — Jinja2 HTML templates (base, login, studies, sprints, loops, detail pages)
  cli.py                — Click CLI (init, serve, study, sprint, loop, cluster commands)
```

## Database

SQLite with 6 tables: `studies`, `sprints`, `auto_loops`, `artifacts`, `slack_sessions`, `events`. Schema in `db/migrations.py`. All queries in `db/queries.py` use parameterized SQL and return plain dicts.

## Key patterns

- All source files use `from __future__ import annotations` for 3.10 compat
- Config is loaded from `researchloop.toml` (TOML) via `core/config.py`
- Database queries are plain async functions in `db/queries.py`, not methods on Database
- Schedulers take an SSH connection object but LocalScheduler ignores it
- SprintManager.submit_sprint() picks the job template based on scheduler_type (slurm.sh.j2 or sge.sh.j2)
- SprintManager.submit_sprint() handles the full workflow: render template → SSH mkdir → write script → submit → update DB → notify
- The runner pipeline writes `.researchloop/status.json` for heartbeat tracking and sends HTTP heartbeats to the orchestrator
- Auto-loop on_sprint_complete: collects previous summaries → renders idea_generator template → runs `claude -p` (or falls back to heuristic) → starts next sprint
- Slack integration: POST /api/slack/events handles URL verification, signature checking, `sprint run` commands, and conversational messages via ConversationManager
- Dashboard routes check auth via signed session cookie when password_hash is configured
- Tests use in-memory SQLite (`:memory:`) and mock SSH via AsyncMock

## Testing

175 pytest tests across 14 files. Tests cover: models, config parsing, database operations, all query functions, SLURM scheduler (mock SSH), SGE scheduler (mock SSH), local scheduler (real subprocesses), study/sprint managers, auto-loop controller (with mock claude), notification router, Slack notifier + signature verification + conversation manager, FastAPI API endpoints (TestClient), dashboard routes + auth, CLI commands (CliRunner), runner output parsing, and template rendering.

## CI

GitHub Actions: lint (ruff check + format) and test (pytest on Python 3.10, 3.12, 3.13).

## Current status

All phases are implemented:
- Phase 1: Core platform, sprint runner, SLURM scheduler, CLI, tests, CI
- Phase 2: Auto-loop with LLM idea generation (claude -p with heuristic fallback)
- Phase 3: Slack integration (Events API, conversational threads via --resume)
- Phase 4: Web dashboard (Jinja2 templates, bcrypt password auth, session cookies)
- Phase 5: SGE scheduler, dynamic job template selection, polish
