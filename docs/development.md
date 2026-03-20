# Development

## Setup

```bash
git clone https://github.com/chanind/researchloop.git
cd researchloop
uv sync
```

## Running tests

### Unit tests

```bash
# Run all unit tests (339 tests, ~3s)
uv run pytest tests/ -v -m "not integration"

# Run a specific test file
uv run pytest tests/test_sprint_manager.py -v

# Run with output
uv run pytest tests/ -v -s
```

### Integration tests

Integration tests use a Docker SLURM container to test real job submission:

```bash
# Build the test container
docker build -t researchloop-slurm-test tests/docker/slurm/

# Run integration tests
uv run pytest tests/integration/ -v --timeout=120
```

The integration test container provides a minimal SLURM environment for testing job submission, status checks, and cancellation.

## Code quality

```bash
# Lint
uv run ruff check .

# Format
uv run ruff format .

# Format check (CI mode)
uv run ruff format --check .

# Type check
uv run pyright researchloop/
```

All three checks run in CI. Fix any errors before committing.

## Architecture

### Orchestrator

The orchestrator (`researchloop serve`) is a FastAPI application that coordinates all subsystems:

```
Orchestrator
  ├── Database (aiosqlite, WAL mode)
  ├── SSHManager (asyncssh connection pooling)
  ├── Schedulers (SLURM, SGE, Local)
  ├── StudyManager (config → DB sync)
  ├── SprintManager (create/submit/cancel/complete)
  ├── AutoLoopController (multi-sprint loops)
  ├── NotificationRouter (ntfy, Slack)
  ├── ConversationManager (Slack threads → Claude sessions)
  ├── JobMonitor (background polling)
  └── FastAPI app (API + Dashboard + Slack events)
```

The `Orchestrator` class initializes all subsystems in `start()` and tears them down in `stop()`, using FastAPI's lifespan context manager.

### Sprint lifecycle

1. **Create** -- `SprintManager.create_sprint()` generates an ID, creates a DB record
2. **Submit** -- `SprintManager.submit_sprint()`:
    - Resolves cluster and study config
    - Assembles context (global + cluster + study)
    - Renders all pipeline prompts via Jinja2
    - Renders the job script template (embeds prompts as base64)
    - SSH: creates sprint directory, writes CLAUDE.md, writes job script
    - Submits via scheduler (`sbatch`/`qsub`)
    - Updates DB, sends notification
3. **Running** -- heartbeats update status, logs, and progress in DB
4. **Complete** -- webhook handler updates DB, sends notification, fetches PDF
5. **Auto-loop** -- `AutoLoopController.on_sprint_complete()` generates next idea and starts new sprint

### Job script design

Job scripts are self-contained bash scripts with embedded prompts (base64-encoded). This means:

- No runner installation needed on the cluster
- No dependency on the orchestrator's file system
- Prompts are pre-rendered with all context baked in
- The script includes its own heartbeat loop, stream-json processor, and webhook sender

### Database

SQLite with WAL mode for concurrent reads. Schema in `db/migrations.py`. Auto-migrates on startup (new columns are added if missing).

### Context hierarchy

Context flows from three levels, concatenated in order:

1. **Global** -- `config.context` + files from `config.context_paths`
2. **Cluster** -- `cluster.context` + files from `cluster.context_paths`
3. **Study** -- `study.context` + file at `study.claude_md_path`

The combined context is:
- Included in the research prompt template
- Written as `CLAUDE.md` to the sprint directory (so Claude CLI picks it up)
- Used by the auto-loop idea generator

## Project structure

```
researchloop/
  core/
    config.py          TOML config loading, env var overrides
    models.py          Dataclasses, enums, ID generation
    orchestrator.py    Orchestrator class, FastAPI app factory
    credentials.py     CLI credential storage
    auth.py            Claude CLI auth checking
  db/
    database.py        Async SQLite wrapper
    migrations.py      Schema (7 tables), incremental migrations
    queries.py         CRUD functions (parameterized SQL)
  clusters/
    ssh.py             SSH connection + pooling
    monitor.py         Background job polling
  schedulers/
    base.py            ABC for schedulers
    slurm.py           SLURM implementation
    sge.py             SGE implementation
    local.py           Local subprocess scheduler
  sprints/
    manager.py         Sprint lifecycle management
    auto_loop.py       Multi-sprint loop controller
  studies/
    manager.py         Config-to-DB sync
  runner/
    main.py            Runner CLI entry point
    pipeline.py        Research pipeline
    claude.py          Claude CLI wrapper
    upload.py          Artifact upload, webhooks
    templates/         6 prompt templates (Jinja2)
    job_templates/     2 job script templates (SLURM, SGE)
  comms/
    base.py            Notifier ABC
    ntfy.py            ntfy.sh notifications
    slack.py           Slack notifications + signature verification
    conversation.py    Slack conversation manager
    router.py          Notification fan-out
  dashboard/
    app.py             ASGI entry point
    auth.py            Password auth, sessions, CSRF
    routes.py          Dashboard HTML routes
    templates/         9 HTML templates (Jinja2)
  cli.py               Click CLI
```

## CI

GitHub Actions runs on every push and PR to `main`:

| Job | What it does |
|-----|-------------|
| **lint** | `ruff check`, `ruff format --check`, `pyright` |
| **test** | `pytest` on Python 3.10, 3.12, 3.13 |
| **integration** | Builds Docker SLURM container, runs integration tests |

## Testing conventions

- Tests use in-memory SQLite (`:memory:`) for isolation
- SSH is mocked via `AsyncMock` -- no real cluster connections in unit tests
- FastAPI endpoints are tested with `TestClient`
- CLI commands are tested with Click's `CliRunner`
- Always add tests for new functionality
- Mark integration tests with `@pytest.mark.integration`
