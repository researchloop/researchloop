# Dashboard

The web dashboard provides a browser-based interface for monitoring and managing ResearchLoop. It is served by the orchestrator at `/dashboard/`.

## First-run setup

On first visit, the dashboard prompts you to set a password. This password is stored as a bcrypt hash in the database.

Alternatively, set the password via an environment variable before starting the server:

```bash
export RESEARCHLOOP_DASHBOARD_PASSWORD="your-password"
researchloop serve
```

## Studies page

The root dashboard page (`/dashboard/`) lists all configured studies with:

- Study name and description
- Associated cluster
- Total sprint count

Click a study name to view its detail page.

## Study detail

The study detail page shows:

- Study configuration (cluster, description, sprints directory, CLAUDE.md path)
- Recent sprints with status
- A form to submit new sprints with optional GPU, memory, and CPU overrides

## Sprints page

The sprints page (`/dashboard/sprints`) shows all sprints across all studies with:

- Sprint ID, study, status, idea, and creation time
- A form to submit new sprints (select study, enter idea, optional resource overrides)

## Sprint detail

The sprint detail page (`/dashboard/sprints/{id}`) is the most feature-rich view:

### Status and metadata

- Current status (with color coding for running, completed, failed, cancelled)
- Pipeline step detection (e.g., "running (research)", "running (red_team_round_1)")
- Study name, job ID, directory, timestamps

### Live progress

During a running sprint, the page displays:

- **progress.md** -- the researcher's progress log, updated by Claude during the sprint
- **Script output** -- the last lines of `output.log` (training runs, evaluation results)
- **Tool log** -- Claude's tool usage (file edits, bash commands, reads)
- **Recent file activity** -- files recently modified in the sprint directory

### Report

After completion, if the sprint generated a `report.md` or `findings.md`, it is rendered as HTML with syntax highlighting and table support.

If a `report.pdf` was generated, a download link is shown.

### Actions

- **Refresh** -- pulls the latest status from the cluster via SSH, reads logs, downloads PDF
- **Cancel** -- cancels the running job on the cluster
- **Delete** -- removes the sprint from the database
- **Resubmit** -- creates a new sprint with the same idea

### Artifacts

Lists all uploaded artifacts with file sizes and download links.

## Auto-loops page

The loops page (`/dashboard/loops`) shows all auto-loops with:

- Loop ID, study, status, progress (completed/total), current sprint
- A form to start new loops with study selection, sprint count, optional context, and resource overrides

## Loop detail

The loop detail page (`/dashboard/loops/{id}`) shows:

- Loop configuration (study, count, status, context)
- All sprints belonging to the loop with links to their detail pages
- Actions to stop or resume the loop

## Refresh mechanism

The "Refresh" button on sprint detail pages triggers an SSH connection to the cluster that:

1. Checks the real job status via the scheduler (`squeue`/`qstat`)
2. Reads the SLURM output log
3. Reads `sprint_log.txt` for detailed tool usage
4. Reads `progress.md` and `output.log` for live progress
5. Reads `summary.txt`, `report.md`, and `findings.md`
6. Checks for `report.pdf` and downloads it if present
7. Reads `idea.txt` (for auto-loop sprints where the idea was generated on the cluster)
8. Updates the database with all collected information

This works even if webhook delivery failed, providing a reliable fallback for monitoring sprint progress.

## Authentication

All dashboard pages require authentication. The system supports:

- **Password-based login** via the `/dashboard/login` page
- **Session cookies** with signed tokens (7-day expiry)
- **CSRF protection** on all mutating actions (forms include hidden CSRF tokens)

The signing key is auto-generated on first use and persisted in the database, so sessions survive server restarts.
