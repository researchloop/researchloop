"""CLI entry point for researchloop."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import click
import httpx

from researchloop import __version__

# ---------------------------------------------------------------------------
# Async helper
# ---------------------------------------------------------------------------


def run_async(coro: Any) -> Any:
    """Run an async coroutine from synchronous click code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already inside an event loop (e.g. Jupyter) -- create a new one.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

STATUS_COLORS: dict[str, str] = {
    "pending": "yellow",
    "submitted": "yellow",
    "running": "blue",
    "research": "blue",
    "red_team": "magenta",
    "fixing": "cyan",
    "validating": "cyan",
    "reporting": "cyan",
    "summarizing": "cyan",
    "uploading": "cyan",
    "completed": "green",
    "failed": "red",
    "cancelled": "red",
    "stopped": "red",
}


def styled_status(status: str) -> str:
    """Return a click-styled status string."""
    color = STATUS_COLORS.get(status, "white")
    return click.style(status, fg=color, bold=True)


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a simple aligned table."""
    if not rows:
        click.echo(click.style("  (none)", dim=True))
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(click.unstyle(cell)))

    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    click.echo(click.style(header_line, bold=True))
    click.echo("  ".join("-" * w for w in col_widths))
    for row in rows:
        line = "  ".join(
            cell + " " * (col_widths[i] - len(click.unstyle(cell)))
            for i, cell in enumerate(row)
        )
        click.echo(line)


def truncate(text: str | None, length: int = 50) -> str:
    """Truncate a string and append an ellipsis if needed."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) > length:
        return text[: length - 1] + "\u2026"
    return text


# ---------------------------------------------------------------------------
# Remote API helper
# ---------------------------------------------------------------------------


def _resolve_connection(
    config: Any | None = None,
) -> tuple[str, dict[str, str]]:
    """Resolve orchestrator URL and auth headers.

    Checks (in order): config object, saved credentials.
    Returns ``(base_url, headers)``.
    """
    from researchloop.core.credentials import load_credentials

    url: str | None = None
    headers: dict[str, str] = {}

    # 1. Config / env vars (shared_secret for server-side usage).
    if config is not None:
        url = config.orchestrator_url
        if config.shared_secret:
            headers["X-Shared-Secret"] = config.shared_secret

    # 2. Saved credentials (from `researchloop connect`).
    if not url:
        creds = load_credentials()
        if creds:
            url = creds["url"]
            headers["Authorization"] = f"Bearer {creds['token']}"

    if not url:
        raise click.ClickException(
            "Not connected to an orchestrator. Run:\n  researchloop connect <url>"
        )

    return url.rstrip("/"), headers


def _reauth(url: str) -> dict[str, str]:
    """Prompt for password, get a new token, save it, return headers."""
    from researchloop.core.credentials import save_credentials

    click.echo("Session expired. Please re-authenticate.")
    password = click.prompt("Password", type=str, hide_input=True)

    resp = httpx.post(
        f"{url}/api/auth",
        json={"password": password},
        timeout=10,
    )
    if resp.status_code == 401:
        raise click.ClickException("Invalid password.")
    if resp.status_code >= 400:
        raise click.ClickException(f"Auth error {resp.status_code}: {resp.text[:200]}")

    token = resp.json()["token"]
    save_credentials(url, token)
    click.echo(click.style("Re-authenticated!", fg="green"))
    return {"Authorization": f"Bearer {token}"}


def _api_post(
    config: Any | None,
    path: str,
    body: dict | None = None,
) -> dict:
    """POST to the orchestrator API."""
    url, headers = _resolve_connection(config)
    full_url = f"{url}{path}"
    try:
        resp = httpx.post(
            full_url,
            json=body or {},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 401 and "Authorization" in headers:
            headers = _reauth(url)
            resp = httpx.post(
                full_url,
                json=body or {},
                headers=headers,
                timeout=30,
            )
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise click.ClickException(f"API error {resp.status_code}: {detail}")
        return resp.json()
    except httpx.ConnectError:
        raise click.ClickException(f"Cannot connect to orchestrator at {url}")
    except httpx.TimeoutException:
        raise click.ClickException(f"Request timed out: {full_url}")


def _api_get(config: Any | None, path: str) -> dict:
    """GET from the orchestrator API."""
    url, headers = _resolve_connection(config)
    full_url = f"{url}{path}"
    try:
        resp = httpx.get(full_url, headers=headers, timeout=30)
        if resp.status_code == 401 and "Authorization" in headers:
            headers = _reauth(url)
            resp = httpx.get(full_url, headers=headers, timeout=30)
        if resp.status_code >= 400:
            detail = resp.text[:200]
            raise click.ClickException(f"API error {resp.status_code}: {detail}")
        return resp.json()
    except httpx.ConnectError:
        raise click.ClickException(f"Cannot connect to orchestrator at {url}")
    except httpx.TimeoutException:
        raise click.ClickException(f"Request timed out: {full_url}")


# ---------------------------------------------------------------------------
# Config + DB helpers
# ---------------------------------------------------------------------------


def _load_config(config_path: str | None) -> Any:
    """Load config, raising a ClickException on failure."""
    from researchloop.core.config import load_config

    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc))


def _try_load_config(config_path: str | None) -> Any | None:
    """Try to load config; return None if not found.

    Used by commands that can work with just saved credentials.
    """
    from researchloop.core.config import load_config

    try:
        return load_config(config_path)
    except FileNotFoundError:
        return None


async def _open_db(config: Any) -> Any:
    """Open and return a connected Database."""
    from researchloop.db.database import Database

    db = Database(config.db_path)
    await db.connect()
    return db


async def _ensure_studies_synced(config: Any, db: Any) -> None:
    """Make sure every study from the config file exists in the database."""
    from researchloop.db import queries

    for study_cfg in config.studies:
        existing = await queries.get_study(db, study_cfg.name)
        if existing is None:
            await queries.create_study(
                db,
                name=study_cfg.name,
                cluster=study_cfg.cluster,
                description=study_cfg.description or None,
                claude_md_path=study_cfg.claude_md_path or None,
                sprints_dir=study_cfg.sprints_dir or study_cfg.name,
                config_json=json.dumps(
                    {
                        "max_sprint_duration_hours": (
                            study_cfg.max_sprint_duration_hours
                        ),
                        "red_team_max_rounds": study_cfg.red_team_max_rounds,
                    }
                ),
            )


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="researchloop")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(),
    default=None,
    help="Path to researchloop.toml",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """ResearchLoop: Auto-Research Sprint Platform"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--path",
    "-p",
    type=click.Path(),
    default=".",
    help="Directory to initialize",
)
def init(path: str) -> None:
    """Initialize a new ResearchLoop project with example config."""
    target = Path(path).resolve()
    target.mkdir(parents=True, exist_ok=True)

    config_dest = target / "researchloop.toml"
    example_src = Path(__file__).resolve().parent.parent / "researchloop.toml.example"

    if config_dest.exists():
        raise click.ClickException(f"Config file already exists: {config_dest}")

    if example_src.exists():
        shutil.copy2(example_src, config_dest)
    else:
        # Fall back to a minimal config if the example is not found.
        config_dest.write_text(
            "# researchloop configuration\n"
            'db_path = "researchloop.db"\n'
            'artifact_dir = "artifacts"\n\n'
            "[[cluster]]\n"
            'name = "local"\n'
            'host = "localhost"\n'
            'scheduler_type = "local"\n'
            'working_dir = "/tmp/researchloop"\n\n'
            "[[study]]\n"
            'name = "my-study"\n'
            'cluster = "local"\n'
            'description = "My research study"\n'
            'sprints_dir = "./sprints"\n'
        )

    # Create artifact directory.
    artifacts_dir = target / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    click.echo(click.style("Initialized ResearchLoop project!", fg="green", bold=True))
    click.echo(f"  Config : {config_dest}")
    click.echo(f"  Artifacts: {artifacts_dir}")
    click.echo()
    click.echo("Edit researchloop.toml to configure your clusters and studies.")


# ---------------------------------------------------------------------------
# connect / disconnect / status
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("url", required=False)
def connect(url: str | None) -> None:
    """Connect the CLI to a remote ResearchLoop orchestrator.

    Saves the URL and shared secret to ~/.config/researchloop/credentials.json.
    """
    from researchloop.core.credentials import (
        load_credentials,
        save_credentials,
    )

    if not url:
        creds = load_credentials()
        default_url = creds["url"] if creds else None
        url = click.prompt(
            "Orchestrator URL",
            type=str,
            default=default_url,
        )

    url = (url or "").rstrip("/")

    password = click.prompt("Password", type=str, hide_input=True)

    # Authenticate and get an API token.
    try:
        resp = httpx.post(
            f"{url}/api/auth",
            json={"password": password},
            timeout=10,
        )
        if resp.status_code == 401:
            raise click.ClickException("Invalid password.")
        if resp.status_code >= 400:
            raise click.ClickException(
                f"Server error {resp.status_code}: {resp.text[:200]}"
            )
    except httpx.ConnectError:
        raise click.ClickException(f"Cannot connect to {url}")
    except httpx.TimeoutException:
        raise click.ClickException(f"Connection timed out: {url}")

    token = resp.json()["token"]
    path = save_credentials(url, token)
    click.echo()
    click.echo(click.style("Connected!", fg="green", bold=True) + f"  {url}")
    click.echo(click.style(f"  Credentials saved to {path}", dim=True))
    click.echo()


@cli.command()
def disconnect() -> None:
    """Disconnect from the remote orchestrator."""
    from researchloop.core.credentials import clear_credentials

    clear_credentials()
    click.echo("Disconnected. Credentials removed.")


@cli.command()
def status() -> None:
    """Show connection status."""
    from researchloop.core.credentials import load_credentials

    creds = load_credentials()
    if creds:
        click.echo(
            click.style("Connected", fg="green", bold=True) + f"  {creds['url']}"
        )
    else:
        click.echo(
            click.style("Not connected", fg="yellow", bold=True)
            + "  Run "
            + click.style("researchloop connect", bold=True)
            + " to set up."
        )


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--host", default=None, help="Bind address (overrides config).")
@click.option("--port", default=None, type=int, help="Bind port (overrides config).")
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the ResearchLoop orchestrator server."""
    import uvicorn

    config = _load_config(ctx.obj.get("config_path"))
    bind_host = host or config.dashboard.host
    bind_port = port or config.dashboard.port

    click.echo(
        click.style("Starting ResearchLoop server", fg="green", bold=True)
        + f" on {bind_host}:{bind_port}"
    )

    uvicorn.run(
        "researchloop.dashboard.app:app",
        host=bind_host,
        port=bind_port,
        reload=False,
    )


# ===================================================================
# study commands
# ===================================================================


@cli.group()
def study() -> None:
    """Manage studies."""


# -- study list ------------------------------------------------------


async def _study_list(config_path: str | None) -> None:
    config = _load_config(config_path)
    db = await _open_db(config)
    try:
        await _ensure_studies_synced(config, db)

        from researchloop.db import queries

        studies = await queries.list_studies(db)

        click.echo(click.style("\nStudies", fg="cyan", bold=True))
        click.echo()

        rows: list[list[str]] = []
        for s in studies:
            # Count sprints for this study.
            sprints = await queries.list_sprints(db, study_name=s["name"], limit=10000)
            total = len(sprints)
            active = sum(
                1
                for sp in sprints
                if sp["status"] not in ("completed", "failed", "cancelled")
            )
            rows.append(
                [
                    click.style(s["name"], fg="white", bold=True),
                    s.get("cluster") or "",
                    truncate(s.get("description"), 40),
                    str(total),
                    click.style(str(active), fg="blue") if active else "0",
                ]
            )

        print_table(
            ["NAME", "CLUSTER", "DESCRIPTION", "SPRINTS", "ACTIVE"],
            rows,
        )
        click.echo()
    finally:
        await db.close()


@study.command("list")
@click.pass_context
def study_list(ctx: click.Context) -> None:
    """List all configured studies."""
    run_async(_study_list(ctx.obj.get("config_path")))


# -- study init -----------------------------------------------------


_STUDY_CLAUDE_MD_TEMPLATE = """\
# {name}

## Overview
<!-- Describe your research area. This context is given to Claude at the
     start of every sprint so it understands what you're studying. -->


## Background
<!-- Key papers, prior findings, domain knowledge, or links to resources
     that Claude should be aware of. -->


## Codebase
<!-- Describe any existing code, data formats, or infrastructure the sprint
     should work with. If there's a repo to clone or files to reference,
     mention them here. -->


## Goals
<!-- What are you trying to learn, build, or validate? -->


## Constraints
<!-- Any rules the sprints should follow, e.g. language versions, libraries
     to use or avoid, hardware limitations, output formats. -->

"""


@study.command("init")
@click.argument("name")
@click.option(
    "--dir",
    "directory",
    type=click.Path(),
    default=None,
    help="Directory for study files (default: ./studies/<name>)",
)
def study_init(name: str, directory: str | None) -> None:
    """Scaffold a new study directory with a starter CLAUDE.md."""
    target = Path(directory) if directory else Path("studies") / name
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    claude_md = target / "CLAUDE.md"
    if claude_md.exists():
        raise click.ClickException(f"{claude_md} already exists. Edit it directly.")

    claude_md.write_text(
        _STUDY_CLAUDE_MD_TEMPLATE.format(name=name),
        encoding="utf-8",
    )

    click.echo(click.style("Created ", fg="green") + str(claude_md))
    click.echo()
    click.echo("Edit this file to describe your research.")
    click.echo(
        "Then set "
        + click.style("claude_md_path", bold=True)
        + f' = "{claude_md.relative_to(Path.cwd())}"'
        + " in your study config."
    )


# -- study show ------------------------------------------------------


async def _study_show(config_path: str | None, name: str) -> None:
    config = _load_config(config_path)
    db = await _open_db(config)
    try:
        await _ensure_studies_synced(config, db)

        from researchloop.db import queries

        study_row = await queries.get_study(db, name)
        if study_row is None:
            raise click.ClickException(f"Study not found: {name}")

        click.echo()
        click.echo(
            click.style("Study: ", dim=True)
            + click.style(study_row["name"], fg="cyan", bold=True)
        )
        click.echo(
            click.style("  Cluster    : ", dim=True)
            + (study_row.get("cluster") or "n/a")
        )
        click.echo(
            click.style("  Description: ", dim=True)
            + (study_row.get("description") or "")
        )
        click.echo(
            click.style("  Sprints dir: ", dim=True)
            + (study_row.get("sprints_dir") or "")
        )
        click.echo(
            click.style("  CLAUDE.md  : ", dim=True)
            + (study_row.get("claude_md_path") or "")
        )
        click.echo(
            click.style("  Created    : ", dim=True)
            + (study_row.get("created_at") or "")
        )

        # Show recent sprints.
        sprints = await queries.list_sprints(db, study_name=name, limit=10)
        click.echo()
        click.echo(click.style("  Recent sprints:", bold=True))
        if not sprints:
            click.echo(click.style("    (none)", dim=True))
        else:
            rows = [
                [
                    click.style(sp["id"], fg="white", bold=True),
                    styled_status(sp["status"]),
                    truncate(sp["idea"], 45),
                    sp.get("created_at") or "",
                ]
                for sp in sprints
            ]
            # Indent table.
            headers = ["ID", "STATUS", "IDEA", "CREATED"]
            col_widths = [len(h) for h in headers]
            for row in rows:
                for i, cell in enumerate(row):
                    col_widths[i] = max(col_widths[i], len(click.unstyle(cell)))

            click.echo(
                "    "
                + "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
            )
            click.echo("    " + "  ".join("-" * w for w in col_widths))
            for row in rows:
                click.echo(
                    "    "
                    + "  ".join(
                        cell + " " * (col_widths[i] - len(click.unstyle(cell)))
                        for i, cell in enumerate(row)
                    )
                )
        click.echo()
    finally:
        await db.close()


@study.command("show")
@click.argument("name")
@click.pass_context
def study_show(ctx: click.Context, name: str) -> None:
    """Show details of a study."""
    run_async(_study_show(ctx.obj.get("config_path"), name))


# ===================================================================
# sprint commands
# ===================================================================


@cli.group()
def sprint() -> None:
    """Manage sprints."""


# -- sprint run -------------------------------------------------------


def _sprint_run(config_path: str | None, study_name: str, idea: str) -> None:
    config = _try_load_config(config_path)
    result = _api_post(
        config,
        "/api/sprints",
        {"study_name": study_name, "idea": idea},
    )

    click.echo()
    click.echo(click.style("Sprint submitted!", fg="green", bold=True))
    click.echo(
        click.style("  ID    : ", dim=True)
        + click.style(result["sprint_id"], fg="cyan", bold=True)
    )
    click.echo(click.style("  Study : ", dim=True) + study_name)
    click.echo(click.style("  Idea  : ", dim=True) + idea)
    click.echo(
        click.style("  Status: ", dim=True)
        + styled_status(result.get("status", "submitted"))
    )
    click.echo()


@sprint.command("run")
@click.argument("idea")
@click.option(
    "--study",
    "-s",
    "study_name",
    required=True,
    help="Study name",
)
@click.pass_context
def sprint_run(ctx: click.Context, idea: str, study_name: str) -> None:
    """Submit a new sprint with the given idea."""
    _sprint_run(ctx.obj.get("config_path"), study_name, idea)


# -- sprint list ------------------------------------------------------


async def _sprint_list(
    config_path: str | None,
    study_name: str | None,
    limit: int,
) -> None:
    config = _load_config(config_path)
    db = await _open_db(config)
    try:
        await _ensure_studies_synced(config, db)

        from researchloop.db import queries

        sprints = await queries.list_sprints(db, study_name=study_name, limit=limit)

        title = "Sprints"
        if study_name:
            title += f" (study: {study_name})"

        click.echo()
        click.echo(click.style(title, fg="cyan", bold=True))
        click.echo()

        rows = [
            [
                click.style(sp["id"], fg="white", bold=True),
                sp.get("study_name") or "",
                styled_status(sp["status"]),
                truncate(sp["idea"], 40),
                sp.get("created_at") or "",
            ]
            for sp in sprints
        ]

        print_table(["ID", "STUDY", "STATUS", "IDEA", "CREATED"], rows)
        click.echo()
    finally:
        await db.close()


@sprint.command("list")
@click.option("--study", "-s", "study_name", default=None, help="Filter by study name")
@click.option("--limit", "-n", default=20, type=int, help="Max sprints to show")
@click.pass_context
def sprint_list(ctx: click.Context, study_name: str | None, limit: int) -> None:
    """List sprints."""
    run_async(_sprint_list(ctx.obj.get("config_path"), study_name, limit))


# -- sprint show ------------------------------------------------------


async def _sprint_show(config_path: str | None, sprint_id: str) -> None:
    config = _load_config(config_path)
    db = await _open_db(config)
    try:
        from researchloop.db import queries

        sp = await queries.get_sprint(db, sprint_id)
        if sp is None:
            raise click.ClickException(f"Sprint not found: {sprint_id}")

        click.echo()
        click.echo(
            click.style("Sprint: ", dim=True)
            + click.style(sp["id"], fg="cyan", bold=True)
        )
        click.echo(
            click.style("  Study    : ", dim=True) + (sp.get("study_name") or "")
        )
        click.echo(click.style("  Idea     : ", dim=True) + (sp.get("idea") or ""))
        click.echo(click.style("  Status   : ", dim=True) + styled_status(sp["status"]))
        click.echo(click.style("  Job ID   : ", dim=True) + (sp.get("job_id") or "n/a"))
        click.echo(click.style("  Directory: ", dim=True) + (sp.get("directory") or ""))
        click.echo(
            click.style("  Created  : ", dim=True) + (sp.get("created_at") or "")
        )
        click.echo(
            click.style("  Started  : ", dim=True) + (sp.get("started_at") or "n/a")
        )
        click.echo(
            click.style("  Completed: ", dim=True) + (sp.get("completed_at") or "n/a")
        )

        if sp.get("error"):
            click.echo(click.style("  Error    : ", fg="red", bold=True) + sp["error"])

        if sp.get("summary"):
            click.echo()
            click.echo(click.style("  Summary:", bold=True))
            for line in sp["summary"].splitlines():
                click.echo(f"    {line}")

        # Artifacts.
        artifacts = await queries.list_artifacts(db, sprint_id)
        click.echo()
        click.echo(click.style("  Artifacts:", bold=True))
        if not artifacts:
            click.echo(click.style("    (none)", dim=True))
        else:
            for art in artifacts:
                size_str = ""
                if art.get("size"):
                    size_kb = art["size"] / 1024
                    if size_kb > 1024:
                        size_str = f" ({size_kb / 1024:.1f} MB)"
                    else:
                        size_str = f" ({size_kb:.1f} KB)"
                click.echo(
                    f"    - {art['filename']}{size_str}"
                    + click.style(f"  [{art['path']}]", dim=True)
                )
        click.echo()
    finally:
        await db.close()


@sprint.command("show")
@click.argument("sprint_id")
@click.pass_context
def sprint_show(ctx: click.Context, sprint_id: str) -> None:
    """Show details of a sprint."""
    run_async(_sprint_show(ctx.obj.get("config_path"), sprint_id))


# -- sprint cancel -----------------------------------------------------


def _sprint_cancel(config_path: str | None, sprint_id: str) -> None:
    config = _try_load_config(config_path)
    _api_post(config, f"/api/sprints/{sprint_id}/cancel")

    click.echo(
        click.style("Cancelled", fg="yellow", bold=True)
        + f" sprint {click.style(sprint_id, fg='cyan', bold=True)}"
    )


@sprint.command("cancel")
@click.argument("sprint_id")
@click.pass_context
def sprint_cancel(ctx: click.Context, sprint_id: str) -> None:
    """Cancel a running sprint."""
    _sprint_cancel(ctx.obj.get("config_path"), sprint_id)


# ===================================================================
# loop commands
# ===================================================================


@cli.group()
def loop() -> None:
    """Manage auto-loops."""


# -- loop start -------------------------------------------------------


def _loop_start(
    config_path: str | None,
    study_name: str,
    count: int,
    context: str,
) -> None:
    config = _try_load_config(config_path)
    body: dict[str, Any] = {
        "study_name": study_name,
        "count": count,
    }
    if context:
        body["context"] = context
    result = _api_post(config, "/api/loops", body)

    click.echo()
    click.echo(click.style("Auto-loop started!", fg="green", bold=True))
    click.echo(
        click.style("  ID    : ", dim=True)
        + click.style(result["loop_id"], fg="cyan", bold=True)
    )
    click.echo(click.style("  Study : ", dim=True) + study_name)
    click.echo(click.style("  Count : ", dim=True) + str(count))
    if context:
        click.echo(click.style("  Context: ", dim=True) + context[:80])
    click.echo()


@loop.command("start")
@click.option(
    "--study",
    "-s",
    "study_name",
    required=True,
    help="Study name",
)
@click.option(
    "--count",
    "-n",
    default=3,
    type=int,
    help="Number of sprints to run",
)
@click.option(
    "--context",
    "-m",
    default="",
    help="Guidance for the idea generator (e.g. topics, paper links)",
)
@click.pass_context
def loop_start(
    ctx: click.Context,
    study_name: str,
    count: int,
    context: str,
) -> None:
    """Start an auto-loop."""
    _loop_start(ctx.obj.get("config_path"), study_name, count, context)


# -- loop status -------------------------------------------------------


async def _loop_status(config_path: str | None) -> None:
    config = _load_config(config_path)
    db = await _open_db(config)
    try:
        from researchloop.db import queries

        loops = await queries.list_auto_loops(db)

        click.echo()
        click.echo(click.style("Auto-Loops", fg="cyan", bold=True))
        click.echo()

        rows = [
            [
                click.style(lp["id"], fg="white", bold=True),
                lp.get("study_name") or "",
                styled_status(lp["status"]),
                f"{lp.get('completed_count', 0)}/{lp.get('total_count', 0)}",
                lp.get("current_sprint_id") or "n/a",
                lp.get("created_at") or "",
            ]
            for lp in loops
        ]

        print_table(
            ["ID", "STUDY", "STATUS", "PROGRESS", "CURRENT SPRINT", "CREATED"],
            rows,
        )
        click.echo()
    finally:
        await db.close()


@loop.command("status")
@click.pass_context
def loop_status(ctx: click.Context) -> None:
    """Show auto-loop status."""
    run_async(_loop_status(ctx.obj.get("config_path")))


# -- loop stop ---------------------------------------------------------


async def _loop_stop(config_path: str | None, loop_id: str) -> None:
    config = _load_config(config_path)
    db = await _open_db(config)
    try:
        from researchloop.db import queries

        lp = await queries.get_auto_loop(db, loop_id)
        if lp is None:
            raise click.ClickException(f"Auto-loop not found: {loop_id}")

        if lp["status"] not in ("running", "pending"):
            raise click.ClickException(
                f"Auto-loop {loop_id} is already {lp['status']}; cannot stop."
            )

        await queries.update_auto_loop(db, loop_id, status="stopped")

        click.echo(
            click.style("Stopped", fg="yellow", bold=True)
            + f" auto-loop {click.style(loop_id, fg='cyan', bold=True)}"
        )
    finally:
        await db.close()


@loop.command("stop")
@click.argument("loop_id")
@click.pass_context
def loop_stop(ctx: click.Context, loop_id: str) -> None:
    """Stop an auto-loop."""
    run_async(_loop_stop(ctx.obj.get("config_path"), loop_id))


# ===================================================================
# cluster commands
# ===================================================================


@cli.group()
def cluster() -> None:
    """Manage clusters."""


# -- cluster list ------------------------------------------------------


@cluster.command("list")
@click.pass_context
def cluster_list(ctx: click.Context) -> None:
    """List configured clusters."""
    config = _load_config(ctx.obj.get("config_path"))

    click.echo()
    click.echo(click.style("Clusters", fg="cyan", bold=True))
    click.echo()

    rows = [
        [
            click.style(c.name, fg="white", bold=True),
            f"{c.host}:{c.port}",
            c.user or "n/a",
            c.scheduler_type,
            str(c.max_concurrent_jobs),
            c.working_dir or "n/a",
        ]
        for c in config.clusters
    ]

    print_table(
        ["NAME", "HOST", "USER", "SCHEDULER", "MAX JOBS", "WORKING DIR"],
        rows,
    )
    click.echo()


# -- cluster check -----------------------------------------------------


async def _cluster_check(config_path: str | None, cluster_name: str | None) -> None:
    config = _load_config(config_path)

    targets = config.clusters
    if cluster_name:
        targets = [c for c in targets if c.name == cluster_name]
        if not targets:
            raise click.ClickException(
                f"Cluster not found: {cluster_name}. "
                "Run 'researchloop cluster list' to see available clusters."
            )

    click.echo()
    click.echo(click.style("Cluster connectivity check", fg="cyan", bold=True))
    click.echo()

    from researchloop.clusters.ssh import SSHConnection

    for c in targets:
        label = click.style(c.name, fg="white", bold=True)
        click.echo(f"  {label} ({c.user}@{c.host}:{c.port}) ... ", nl=False)

        try:
            conn = SSHConnection(
                host=c.host,
                port=c.port,
                user=c.user,
                key_path=c.key_path,
            )
            await conn.connect()
            stdout, _stderr, exit_code = await conn.run("hostname", timeout=10)
            await conn.close()

            if exit_code == 0:
                hostname = stdout.strip()
                click.echo(
                    click.style("OK", fg="green", bold=True)
                    + click.style(f" (hostname: {hostname})", dim=True)
                )
            else:
                click.echo(
                    click.style("WARN", fg="yellow", bold=True)
                    + f" (exit code {exit_code})"
                )
        except Exception as exc:
            click.echo(
                click.style("FAIL", fg="red", bold=True)
                + click.style(f" ({exc})", dim=True)
            )

    click.echo()


@cluster.command("check")
@click.option("--name", "-n", default=None, help="Check a specific cluster")
@click.pass_context
def cluster_check(ctx: click.Context, name: str | None) -> None:
    """Check cluster connectivity."""
    run_async(_cluster_check(ctx.obj.get("config_path"), name))
