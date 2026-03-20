# CLAUDE.md

## Project overview

ResearchLoop is an automated research sprint platform for HPC clusters. It orchestrates multi-step AI research pipelines on SLURM/SGE clusters using `claude -p` for all AI work. The orchestrator is a lightweight Docker container; all heavy compute runs on HPC.

## Architecture

Two processes:

1. **Orchestrator** (`researchloop serve`) — FastAPI server that manages studies/sprints in SQLite, submits jobs via SSH, receives webhooks from runners, stores artifacts. Also serves the web dashboard and handles Slack events.
2. **Sprint Runner** — runs inside each SLURM/SGE job on HPC. Self-contained bash scripts chain `claude -p` calls through a pipeline (research → red-team → fix → report → summarize), then upload artifacts and send a completion webhook.

Key design decisions:

- All AI work runs on HPC, never on the orchestrator (except Slack conversations and auto-loop idea generation, which use `claude -p` locally with restricted tools)
- `claude -p --output-format stream-json` for sprint steps (enables live progress), `--output-format json` for conversations
- SSH to HPC login nodes for sbatch/squeue/scancel/qsub/qdel
- Job completion via per-sprint webhook tokens (runner → orchestrator), SSH polling as fallback
- SQLite (aiosqlite, WAL mode) for metadata, with a `settings` table for persistent config (signing key, password hash)
- Jinja2 templates for all prompts and job scripts — prompts are pre-rendered by the orchestrator and embedded as base64 in the job script
- Auto-loop sprints generate their own ideas on the cluster (where Claude is authenticated) rather than on the orchestrator
- Context hierarchy: global → cluster → study (inline text + file paths at each level)

## Tech stack

Python 3.10+, uv, asyncio throughout. Key deps: click (CLI), FastAPI (API + dashboard), aiosqlite (DB), asyncssh (SSH), httpx (HTTP client), Jinja2 (templates), bcrypt + itsdangerous (dashboard auth), markdown (report rendering in dashboard).

## Commands

```bash
uv sync                              # install deps
uv run pytest tests/ -v -m "not integration"  # unit tests (339 tests, ~3s)
uv run pytest tests/integration/ -v --timeout=120  # integration tests (needs Docker)
uv run ruff check .                  # lint
uv run ruff format .                 # format
uv run pyright researchloop/         # type check
uv run researchloop --help           # CLI help
```

## Package layout

```
researchloop/
  __init__.py           — __version__
  __main__.py           — python -m researchloop entry point
  cli.py                — Click CLI (init, serve, connect, disconnect, status, study, sprint, loop, cluster commands)
  core/
    __init__.py
    config.py           — TOML config loading into dataclasses (Config, ClusterConfig, StudyConfig, SlackConfig, NtfyConfig, DashboardConfig) + env var overrides
    models.py           — SprintStatus enum, Sprint/Study/AutoLoop dataclasses, generate_sprint_id(), format_sprint_dirname()
    orchestrator.py     — Orchestrator class + create_app() FastAPI factory (API + Slack + dashboard)
    credentials.py      — CLI credential storage (~/.config/researchloop/credentials.json) for remote orchestrator auth
    auth.py             — check_claude_auth_async() helper for verifying Claude CLI auth status
  db/
    __init__.py
    database.py         — async SQLite wrapper (WAL mode, auto-migrations, fetch_one/fetch_all/execute)
    migrations.py       — CREATE TABLE statements (7 tables: studies, sprints, auto_loops, artifacts, slack_sessions, events, settings) + indexes + incremental column migrations
    queries.py          — async CRUD functions (all take Database as first arg, return dicts)
  clusters/
    __init__.py
    ssh.py              — SSHConnection (connect/run/upload_file/download_file) + SSHManager (connection pooling via asyncssh)
    monitor.py          — JobMonitor (polls active jobs via SSH, detects abandoned sprints via heartbeat timeout)
  schedulers/
    __init__.py
    base.py             — BaseScheduler ABC (submit/status/cancel)
    slurm.py            — SlurmScheduler (sbatch/squeue/sacct/scancel)
    sge.py              — SGEScheduler (qsub/qstat/qacct/qdel)
    local.py            — LocalScheduler (subprocesses, for testing)
  sprints/
    __init__.py
    manager.py          — SprintManager (create/submit/run/cancel/handle_completion + PDF fetch + idea fetch)
    auto_loop.py        — AutoLoopController (start/stop/resume/on_sprint_complete, LLM idea generation)
  studies/
    __init__.py
    manager.py          — StudyManager (config→DB sync, cluster config resolution)
  runner/
    __init__.py
    main.py             — Runner CLI entry point (researchloop-runner run)
    pipeline.py         — Pipeline class (runs the multi-step research pipeline)
    claude.py           — run_claude() wrapper + render_template()
    upload.py           — upload_artifacts(), send_webhook(), send_heartbeat()
    templates/          — 6 Jinja2 prompt templates:
      research_sprint.md.j2   — main research prompt (includes progress.md + output.log instructions)
      red_team.md.j2          — critique/red-team prompt
      fix_issues.md.j2        — fix prompt after red-team
      report.md.j2            — comprehensive report generation
      summarizer.md.j2        — short summary for notifications
      idea_generator.md.j2    — next idea generation for auto-loops
    job_templates/      — 2 job script templates:
      slurm.sh.j2             — self-contained SLURM job script (includes stream-json processing, heartbeat loop, prompt embedding)
      sge.sh.j2               — SGE equivalent
  comms/
    __init__.py
    base.py             — BaseNotifier ABC (notify_sprint_started/completed/failed)
    ntfy.py             — NtfyNotifier (ntfy.sh push notifications)
    slack.py            — SlackNotifier (chat:write + files:write) + verify_slack_signature()
    conversation.py     — ConversationManager (Slack threads → Claude sessions via --resume, action execution, markdown→Slack conversion)
    router.py           — NotificationRouter (fan-out to all configured notifiers)
  dashboard/
    __init__.py
    app.py              — ASGI app factory for `researchloop serve`
    auth.py             — Password auth (bcrypt hash/check, signed session cookies via itsdangerous, CSRF token generation/verification)
    routes.py           — Dashboard HTML routes (setup, login/logout, studies, sprints, loops, artifacts, refresh, cancel, delete, resubmit)
    templates/          — 9 Jinja2 HTML templates:
      base.html               — layout with nav and auth state
      setup.html              — first-run password setup
      login.html              — login form
      studies.html            — study list
      study_detail.html       — study detail + sprint submission form
      sprints.html            — sprint list + new sprint form
      sprint_detail.html      — sprint detail (status, progress, log, report, artifacts, actions)
      loops.html              — auto-loop list + new loop form
      loop_detail.html        — loop detail with sprint list
```

## Database

SQLite with 7 tables: `studies`, `sprints`, `auto_loops`, `artifacts`, `slack_sessions`, `events`, `settings`. Schema in `db/migrations.py`. All queries in `db/queries.py` use parameterized SQL and return plain dicts.

Key columns:
- `sprints.webhook_token` — per-sprint token for webhook auth (generated at creation)
- `sprints.loop_id` — links sprint to its auto-loop
- `sprints.metadata_json` — stores report text, has_pdf flag, heartbeat info
- `sprints.error` — stores live progress (progress.md + output.log + tool log) during running sprints
- `auto_loops.metadata_json` — stores loop context and job_options
- `settings` — key/value store for signing_key and dashboard_password_hash

## Key patterns

- All source files use `from __future__ import annotations` for 3.10 compat
- Config is loaded from `researchloop.toml` (TOML) via `core/config.py`, with env var overrides (RESEARCHLOOP_* prefix)
- Database queries are plain async functions in `db/queries.py`, not methods on Database
- Schedulers take an SSH connection object but LocalScheduler ignores it
- SprintManager.submit_sprint() handles the full workflow: render prompt templates → render job template → SSH mkdir → base64 encode + write script → submit → update DB → notify
- Prompt templates are pre-rendered by the orchestrator and embedded as base64 in the job script, so the runner has no dependency on the orchestrator's template files
- Context hierarchy: global (config.context + config.context_paths) → cluster (cluster.context + cluster.context_paths) → study (study.context + study.claude_md_path), all concatenated into study_context
- The job script uses `--output-format stream-json` and pipes through a Python filter that logs tool usage summaries and captures the session ID
- Claude sessions persist across pipeline steps via `--resume $SESSION_ID`
- The runner writes `progress.md` (researcher updates) and pipes script output to `output.log` — both are sent via heartbeat and displayed in the dashboard
- Background heartbeat loop in job script sends log tail + progress.md + output.log + recent files every 60s
- Webhook retries: completion webhook retries 3 times with 10s delay
- Per-sprint webhook tokens: each sprint gets a unique token at creation, passed via X-Webhook-Token header
- Dashboard signing key is auto-generated and persisted in DB `settings` table (survives restarts)
- Dashboard password can come from: config TOML → env var (RESEARCHLOOP_DASHBOARD_PASSWORD auto-hashes) → DB settings table (set via first-run setup page)
- CSRF protection: HMAC-based tokens derived from session token + signing secret, checked on all mutating dashboard POST routes
- Dashboard refresh: pulls live status from cluster via SSH (reads logs, progress.md, output.log, report.md, findings.md, summary.txt, idea.txt, checks for PDF)
- Slack events: deduplication via event_id set, signature verification, background task processing (return 200 immediately), bot message filtering
- Slack conversation: thread → session mapping in DB, context building with study/sprint info, action execution via [ACTION: ...] tags
- Auto-loop: sprint idea=None → job script generates idea on cluster → idea.txt read back via SSH/webhook
- CLI auth: `researchloop connect` gets a bearer token via /api/auth, stored in ~/.config/researchloop/credentials.json with 600 permissions
- CLI auto-reauth: on 401, prompts for password, gets new token, saves it
- Always ensure the codebase passes type checking with pyright and ruff check before committing. Fix any errors that exist.

## Testing

339 unit tests covering: models, config parsing, database operations, all query functions, SLURM scheduler (mock SSH), SGE scheduler (mock SSH), local scheduler (real subprocesses), study/sprint managers, auto-loop controller (with mock claude), notification router, Slack notifier + signature verification + conversation manager + Slack events API, FastAPI API endpoints (TestClient), dashboard routes + auth + setup + CSRF, CLI commands (CliRunner), runner output parsing, and template rendering.

Integration tests (in tests/integration/) use a Docker SLURM container to test real job submission.

Tests use in-memory SQLite (`:memory:`) and mock SSH via AsyncMock.

Always add tests for any new functionality!

## CI

GitHub Actions (`.github/workflows/ci.yml`):
- **lint** — ruff check + ruff format --check + pyright type check
- **test** — pytest on Python 3.10, 3.12, 3.13 (unit tests only, `-m "not integration"`)
- **integration** — builds Docker SLURM container, runs integration tests with 120s timeout
