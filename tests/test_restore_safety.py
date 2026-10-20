"""Tests for SQLite restore safety, WAL sidecar handling, concurrency lock checks, and recovery."""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.connection import (
    backup_database,
    create_connection,
    restore_database,
)


class TestRestoreSafetyRemediation(unittest.TestCase):
    """P0 remediation tests for safe SQLite database restoration."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.live_db_path = Path(self.temp_dir.name) / "live.db"
        self.backup_path = Path(self.temp_dir.name) / "backup.db"

        # Initialize live DB with data
        conn = create_connection(self.live_db_path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO users VALUES (1, 'Alice');")
        conn.commit()

        # Create valid backup
        backup_database(conn, self.backup_path)
        conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_active_connection_with_lock_aborts_restore_safely(self):
        """Restoring while another connection holds a write lock must fail and protect the target."""
        active_conn = create_connection(self.live_db_path)
        active_conn.execute("BEGIN EXCLUSIVE;")
        active_conn.execute("INSERT INTO users VALUES (2, 'Bob');")

        try:
            with self.assertRaises((RuntimeError, sqlite3.OperationalError, PermissionError)) as cm:
                restore_database(self.backup_path, self.live_db_path)
            self.assertTrue(
                "locked" in str(cm.exception).lower()
                or "in use" in str(cm.exception).lower()
                or "access" in str(cm.exception).lower()
                or "permission" in str(cm.exception).lower()
            )
        finally:
            active_conn.rollback()
            active_conn.close()

    def test_wal_database_restores_and_passes_integrity_check(self):
        """Restoring onto a WAL database cleans up sidecars and passes integrity check."""
        conn = create_connection(self.live_db_path, enable_wal=True)
        conn.execute("INSERT INTO users VALUES (99, 'Ephemeral');")
        conn.commit()
        conn.close()

        restore_database(self.backup_path, self.live_db_path)

        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        self.assertEqual(cur.fetchone()[0], "ok")

        cur.execute("SELECT name FROM users ORDER BY id ASC;")
        names = [r["name"] for r in cur.fetchall()]
        self.assertEqual(names, ["Alice"])
        check_conn.close()

    def test_sidecar_unlink_failure_aborts_restore_immediately(self):
        """If removing a -wal sidecar fails (e.g. locked by OS), restore must immediately abort."""
        orig_exists = Path.exists
        orig_unlink = Path.unlink

        def fake_exists(self_path):
            if str(self_path).endswith("-wal"):
                return True
            return orig_exists(self_path)

        def fake_unlink(self_path, missing_ok=False):
            if str(self_path).endswith("-wal"):
                raise OSError("Simulated Windows file lock on -wal sidecar")
            return orig_unlink(self_path, missing_ok=missing_ok)

        with patch.object(Path, "exists", fake_exists), patch.object(Path, "unlink", fake_unlink):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)
            self.assertIn("Failed to remove stale WAL sidecar", str(cm.exception))

    def test_os_replace_failure_cleans_up_temp_and_preserves_target(self):
        """If os.replace fails, temp file is removed and target remains intact."""
        with patch("os.replace", side_effect=OSError("Simulated filesystem replace failure")):
            with self.assertRaises(OSError):
                restore_database(self.backup_path, self.live_db_path)

        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT name FROM users;")
        self.assertEqual(cur.fetchone()["name"], "Alice")
        check_conn.close()

        tmp_files = list(self.live_db_path.parent.glob("*.restore_tmp_*"))
        self.assertEqual(tmp_files, [])


if __name__ == "__main__":
    unittest.main()
