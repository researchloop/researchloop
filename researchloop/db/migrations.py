"""Schema creation and index definitions for the researchloop database."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import Database

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS studies (
    name TEXT PRIMARY KEY,
    cluster TEXT NOT NULL,
    description TEXT,
    claude_md_path TEXT,
    sprints_dir TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    config_json TEXT  -- full StudyConfig as JSON
);

CREATE TABLE IF NOT EXISTS sprints (
    id TEXT PRIMARY KEY,  -- e.g. "sp-a3f7b2"
    study_name TEXT NOT NULL REFERENCES studies(name),
    idea TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    job_id TEXT,  -- SLURM/SGE job ID
    directory TEXT,  -- full path on cluster
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    summary TEXT,
    session_id TEXT,  -- claude session ID for --resume
    webhook_token TEXT,  -- per-sprint token for webhook auth
    loop_id TEXT,  -- auto-loop ID if part of a loop
    metadata_json TEXT,
    FOREIGN KEY (study_name) REFERENCES studies(name)
);

CREATE TABLE IF NOT EXISTS auto_loops (
    id TEXT PRIMARY KEY,
    study_name TEXT NOT NULL,
    total_count INTEGER NOT NULL,
    completed_count INTEGER NOT NULL DEFAULT 0,
    current_sprint_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',  -- running, completed, stopped, failed
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    stopped_at TEXT,
    metadata_json TEXT,
    FOREIGN KEY (study_name) REFERENCES studies(name)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,  -- local storage path
    size INTEGER,
    content_type TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sprint_id) REFERENCES sprints(id)
);

CREATE TABLE IF NOT EXISTS slack_sessions (
    thread_ts TEXT PRIMARY KEY,
    sprint_id TEXT,
    session_id TEXT,  -- claude --resume session ID
    study_name TEXT,
    messages_json TEXT,  -- JSON array of {role, text}
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sprint_id) REFERENCES sprints(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id TEXT,
    event_type TEXT NOT NULL,
    data_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (sprint_id) REFERENCES sprints(id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_sprints_study_name ON sprints(study_name);
CREATE INDEX IF NOT EXISTS idx_sprints_status ON sprints(status);
CREATE INDEX IF NOT EXISTS idx_events_sprint_id ON events(sprint_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_sprint_id ON artifacts(sprint_id);
"""


async def _add_column_if_missing(
    db: Database,
    table: str,
    column: str,
    col_type: str = "TEXT",
) -> None:
    """Add a column to a table if it doesn't exist."""
    assert db._conn is not None
    cursor = await db._conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        await db._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


async def run_migrations(db: Database) -> None:
    """Create all tables and indexes if they do not already exist."""
    assert db._conn is not None, "Database must be connected before running migrations"
    await db._conn.executescript(SCHEMA_SQL + INDEXES_SQL)

    # Incremental column migrations for existing databases.
    await _add_column_if_missing(db, "sprints", "webhook_token", "TEXT")
    await _add_column_if_missing(db, "sprints", "loop_id", "TEXT")
    await _add_column_if_missing(db, "auto_loops", "metadata_json", "TEXT")
    await _add_column_if_missing(db, "slack_sessions", "messages_json", "TEXT")

    # Make idea column nullable — already applied, skip on future runs.
    # The migration checks if the column is still NOT NULL before acting.
    await _make_idea_nullable(db)

    await db._conn.commit()


async def _make_idea_nullable(db: Database) -> None:
    """Remove NOT NULL from sprints.idea if needed."""
    assert db._conn is not None

    # Recover from a previously failed migration.
    tables = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    table_names = {r[0] for r in await tables.fetchall()}
    if "_sprints_old" in table_names:
        await db._conn.execute("PRAGMA foreign_keys=OFF")
        if "sprints" not in table_names:
            await db._conn.execute("ALTER TABLE _sprints_old RENAME TO sprints")
        else:
            # Keep whichever has more rows.
            old_cursor = await db._conn.execute("SELECT count(*) FROM _sprints_old")
            old_n = await old_cursor.fetchone()
            new_cursor = await db._conn.execute("SELECT count(*) FROM sprints")
            new_n = await new_cursor.fetchone()
            old_count = old_n[0] if old_n else 0
            new_count = new_n[0] if new_n else 0
            if old_count > new_count:
                await db._conn.execute("DROP TABLE sprints")
                await db._conn.execute("ALTER TABLE _sprints_old RENAME TO sprints")
            else:
                await db._conn.execute("DROP TABLE _sprints_old")
        await db._conn.execute("PRAGMA foreign_keys=ON")

    cursor = await db._conn.execute("PRAGMA table_info(sprints)")
    rows = await cursor.fetchall()
    for row in rows:
        if row[1] == "idea" and row[3] == 1:  # notnull=1
            # Disable FK checks during rebuild.
            await db._conn.execute("PRAGMA foreign_keys=OFF")
            # Rebuild: rename → create new → copy → drop old.
            await db._conn.execute("ALTER TABLE sprints RENAME TO _sprints_old")
            for stmt in SCHEMA_SQL.strip().split(";"):
                stmt = stmt.strip()
                if stmt.startswith("CREATE TABLE IF NOT EXISTS sprints"):
                    await db._conn.execute(stmt)
                    break
            await db._conn.execute("INSERT INTO sprints SELECT * FROM _sprints_old")
            await db._conn.execute("DROP TABLE _sprints_old")
            await db._conn.execute("PRAGMA foreign_keys=ON")
            return
