"""Tests for researchloop.db.database and migrations."""

from researchloop.db.database import Database


class TestDatabase:
    async def test_connect_and_close(self):
        db = Database(":memory:")
        await db.connect()
        assert db._conn is not None
        await db.close()
        assert db._conn is None

    async def test_context_manager(self):
        async with Database(":memory:") as db:
            assert db._conn is not None
        assert db._conn is None

    async def test_tables_created(self, db):
        """Migrations should auto-create tables."""
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {r["name"] for r in rows}
        expected = {
            "studies",
            "sprints",
            "auto_loops",
            "artifacts",
            "events",
        }
        assert expected.issubset(table_names)
        # slack_sessions should be dropped by the migration if present.
        assert "slack_sessions" not in table_names

    async def test_indexes_created(self, db):
        rows = await db.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        index_names = {r["name"] for r in rows}
        assert "idx_sprints_study_name" in index_names
        assert "idx_sprints_status" in index_names

    async def test_execute_and_fetch(self, db):
        await db.execute(
            "INSERT INTO studies (name, cluster, sprints_dir) VALUES (?, ?, ?)",
            ("test", "local", "./sp"),
        )
        row = await db.fetch_one("SELECT * FROM studies WHERE name = ?", ("test",))
        assert row is not None
        assert row["name"] == "test"
        assert row["cluster"] == "local"

    async def test_fetch_all(self, db):
        await db.execute(
            "INSERT INTO studies (name, cluster, sprints_dir) VALUES (?, ?, ?)",
            ("s1", "c1", "./sp1"),
        )
        await db.execute(
            "INSERT INTO studies (name, cluster, sprints_dir) VALUES (?, ?, ?)",
            ("s2", "c2", "./sp2"),
        )
        rows = await db.fetch_all("SELECT * FROM studies ORDER BY name")
        assert len(rows) == 2
        assert rows[0]["name"] == "s1"

    async def test_fetch_one_returns_none(self, db):
        result = await db.fetch_one(
            "SELECT * FROM studies WHERE name = ?", ("nonexistent",)
        )
        assert result is None

    async def test_double_connect_is_noop(self):
        db = Database(":memory:")
        await db.connect()
        conn1 = db._conn
        await db.connect()  # Should not create a new connection
        assert db._conn is conn1
        await db.close()
