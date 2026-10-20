"""Tests for migration atomicity, rollback guarantees, and schema_migrations tracking."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.connection import create_connection
from src.db.migrations import (
    ensure_migration_table,
    get_applied_migrations,
    run_migrations,
)


class TestMigrationAtomicityRemediation(unittest.TestCase):
    """P0 remediation tests for strictly atomic migrations."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_mig.db")
        self.conn = create_connection(self.db_path)
        self.migrations_dir = Path(self.temp_dir.name) / "migrations"
        self.migrations_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_sql_error_rolls_back_entire_migration(self):
        """If a SQL statement fails mid-script, earlier table creations must be completely rolled back."""
        mig_file = self.migrations_dir / "001_faulty.sql"
        mig_file.write_text(
            """
            CREATE TABLE step_one_table (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO step_one_table (name) VALUES ('will_be_rolled_back');
            INVALID_SQL_COMMAND_SYNTAX_ERROR;
            CREATE TABLE step_three_table (id INTEGER PRIMARY KEY);
            """,
            encoding="utf-8",
        )

        with self.assertRaises(RuntimeError) as cm:
            run_migrations(self.conn, migrations_dir=self.migrations_dir)

        self.assertIn("Migration 1_faulty failed", str(cm.exception))

        # 1. Verify version was NOT recorded
        applied = get_applied_migrations(self.conn)
        self.assertNotIn(1, applied)

        # 2. Verify step_one_table was rolled back and does not exist in sqlite_master
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='step_one_table'")
        self.assertIsNone(cur.fetchone())

    def test_version_insert_conflict_rolls_back_schema(self):
        """If writing to schema_migrations fails, all schema changes must roll back atomically."""
        ensure_migration_table(self.conn)
        # Pre-seed version 1 in schema_migrations manually
        self.conn.execute("INSERT INTO schema_migrations (version, name) VALUES (1, 'pre_existing');")
        self.conn.commit()

        # Create migration file for version 1 that does table creation
        mig_file = self.migrations_dir / "001_duplicate_version.sql"
        mig_file.write_text(
            """
            CREATE TABLE orphan_attempt (id INTEGER PRIMARY KEY);
            """,
            encoding="utf-8",
        )

        # Since version 1 is already in get_applied_migrations, run_migrations ignores it.
        # But if we force a conflict (e.g. via trigger on schema_migrations):
        self.conn.execute(
            """
            CREATE TRIGGER prevent_v2 BEFORE INSERT ON schema_migrations
            WHEN NEW.version = 2
            BEGIN
                SELECT RAISE(FAIL, 'schema_migrations insert blocked by trigger');
            END;
            """
        )
        self.conn.commit()

        v2_file = self.migrations_dir / "002_blocked_by_version_insert.sql"
        v2_file.write_text(
            """
            CREATE TABLE table_that_must_rollback (id INTEGER PRIMARY KEY);
            INSERT INTO table_that_must_rollback VALUES (42);
            """,
            encoding="utf-8",
        )

        with self.assertRaises(RuntimeError) as cm:
            run_migrations(self.conn, migrations_dir=self.migrations_dir)

        self.assertIn("Migration 2_blocked_by_version_insert failed", str(cm.exception))

        # Verify table_that_must_rollback was completely rolled back
        cur = self.conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='table_that_must_rollback'")
        self.assertIsNone(cur.fetchone())

        applied = get_applied_migrations(self.conn)
        self.assertNotIn(2, applied)

    def test_idempotent_rerun_and_sequential_application(self):
        """Multiple runs must be idempotent and correctly apply only newer migrations."""
        m1 = self.migrations_dir / "001_first.sql"
        m1.write_text("CREATE TABLE t1 (id INTEGER PRIMARY KEY);", encoding="utf-8")

        first_applied = run_migrations(self.conn, migrations_dir=self.migrations_dir)
        self.assertEqual(first_applied, [1])

        # Re-running immediately must do nothing
        second_applied = run_migrations(self.conn, migrations_dir=self.migrations_dir)
        self.assertEqual(second_applied, [])

        # Add second migration on existing DB
        m2 = self.migrations_dir / "002_second.sql"
        m2.write_text("CREATE TABLE t2 (id INTEGER PRIMARY KEY);", encoding="utf-8")

        third_applied = run_migrations(self.conn, migrations_dir=self.migrations_dir)
        self.assertEqual(third_applied, [2])

        self.assertEqual(get_applied_migrations(self.conn), [1, 2])


if __name__ == "__main__":
    unittest.main()
