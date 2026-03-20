# CLI Reference

The `researchloop` CLI provides full management of studies, sprints, auto-loops, and cluster connectivity.

## Global options

```
researchloop [OPTIONS] COMMAND

Options:
  -c, --config PATH    Path to researchloop.toml (default: auto-detect)
  --version            Show version and exit
  --help               Show help and exit
```

## Project setup

### `researchloop init`

Initialize a new ResearchLoop project in the current directory.

```bash
researchloop init
researchloop init --path /path/to/project
```

Creates a `researchloop.toml` configuration file and an `artifacts/` directory.

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `-p, --path` | `.` | Directory to initialize |

## Server

### `researchloop serve`

Start the orchestrator server. Loads configuration from `researchloop.toml` and starts the FastAPI server with the web dashboard, API endpoints, and webhook handlers.

```bash
researchloop serve
researchloop serve --host 0.0.0.0 --port 9090
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | From config | Bind address |
| `--port` | From config | Bind port |

## Connection

### `researchloop connect [URL]`

Authenticate the CLI to a remote orchestrator. Prompts for the dashboard password and saves the URL and API token to `~/.config/researchloop/credentials.json`.

```bash
researchloop connect https://my-server.fly.dev
researchloop connect  # Prompts for URL
```

### `researchloop disconnect`

Remove saved credentials.

```bash
researchloop disconnect
```

### `researchloop status`

Show connection status (connected/not connected and the server URL).

```bash
researchloop status
```

## Studies

### `researchloop study list`

List all configured studies with sprint counts.

```bash
researchloop study list
```

### `researchloop study show NAME`

Show details of a study including cluster, description, and recent sprints.

```bash
researchloop study show my-study
```

### `researchloop study init NAME`

Scaffold a new study directory with a starter `CLAUDE.md` file.

```bash
researchloop study init my-study
researchloop study init my-study --dir /path/to/study
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `--dir` | `./studies/<name>` | Directory for study files |

## Sprints

### `researchloop sprint run IDEA`

Submit a new sprint with the given research idea. Requires a study name.

```bash
researchloop sprint run "implement baseline model" --study my-study
researchloop sprint run "try learning rate 1e-3" -s my-study
```

**Options:**

| Flag | Required | Description |
|------|----------|-------------|
| `-s, --study` | Yes | Study name |

### `researchloop sprint list`

List sprints, optionally filtered by study.

```bash
researchloop sprint list
researchloop sprint list --study my-study
researchloop sprint list --limit 50
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `-s, --study` | All | Filter by study name |
| `-n, --limit` | 20 | Maximum sprints to show |

### `researchloop sprint show ID`

Show details of a sprint including status, idea, summary, artifacts, and timestamps.

```bash
researchloop sprint show sp-a3f7b2
```

### `researchloop sprint cancel ID`

Cancel a running or submitted sprint. This cancels the job on the cluster and stops any parent auto-loop.

```bash
researchloop sprint cancel sp-a3f7b2
```

## Auto-loops

### `researchloop loop start`

Start an auto-loop that runs multiple sprints sequentially with AI-generated ideas.

```bash
researchloop loop start --study my-study --count 5
researchloop loop start -s my-study -n 10 --context "focus on improving F1 score"
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `-s, --study` | Required | Study name |
| `-n, --count` | 3 | Number of sprints to run |
| `-m, --context` | None | Guidance for the idea generator |

### `researchloop loop status`

Show all auto-loops with progress and current sprint.

```bash
researchloop loop status
```

### `researchloop loop stop LOOP_ID`

Stop a running auto-loop. Cancels the current sprint if one is in progress.

```bash
researchloop loop stop loop-abc123
```

## Clusters

### `researchloop cluster list`

List all configured clusters with connection details.

```bash
researchloop cluster list
```

### `researchloop cluster check`

Test SSH connectivity to clusters. Connects via SSH and runs `hostname`.

```bash
researchloop cluster check
researchloop cluster check --name hpc
```

**Options:**

| Flag | Default | Description |
|------|---------|-------------|
| `-n, --name` | All | Check a specific cluster |

## Command routing

Commands that modify state (`sprint run`, `sprint cancel`, `loop start`) communicate with the orchestrator via its REST API. This means:

- If you're running the server locally with a `researchloop.toml`, the CLI reads the config and connects directly.
- If the server is remote, you need to run `researchloop connect` first.
- The CLI automatically re-authenticates if the API token expires.

Read-only commands (`study list`, `sprint list`, `sprint show`) read directly from the local database when a config file is available, or use the API for remote servers.
