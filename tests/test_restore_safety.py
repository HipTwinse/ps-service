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

    def test_wal_unlink_success_shm_unlink_failure_rolls_back_and_preserves_confirmed_data(self):
        """If WAL is unlinked but SHM unlink fails, rollback restores WAL and preserves all confirmed data."""
        # Add confirmed records to live DB in WAL mode
        live_conn = create_connection(self.live_db_path, enable_wal=True)
        live_conn.execute("INSERT INTO users VALUES (42, 'ConfirmedWalUser');")
        live_conn.commit()
        live_conn.close()

        # Ensure -shm and -wal sidecars exist on disk
        shm_path = self.live_db_path.parent / f"{self.live_db_path.name}-shm"
        wal_path = self.live_db_path.parent / f"{self.live_db_path.name}-wal"
        if not shm_path.exists():
            shm_path.write_bytes(b"dummy shm state")
        if not wal_path.exists():
            wal_path.write_bytes(b"dummy wal state")

        orig_unlink = Path.unlink

        def selective_unlink(self_path, missing_ok=False):
            if str(self_path).endswith("-shm"):
                raise OSError("Simulated Windows file lock on -shm sidecar")
            return orig_unlink(self_path, missing_ok=missing_ok)

        with patch.object(Path, "unlink", selective_unlink):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)
            self.assertIn("Failed to remove stale WAL sidecar", str(cm.exception))

        # Check database integrity and confirmed data preservation
        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT name FROM users ORDER BY id ASC;")
        names = [r["name"] for r in cur.fetchall()]
        self.assertIn("ConfirmedWalUser", names)
        self.assertIn("Alice", names)
        check_conn.close()

    def test_both_sidecars_unlinked_os_replace_fails_rolls_back_and_preserves_confirmed_data(self):
        """If both sidecars are unlinked but os.replace fails, target and sidecars roll back with data intact."""
        live_conn = create_connection(self.live_db_path, enable_wal=True)
        live_conn.execute("INSERT INTO users VALUES (77, 'ConfirmedPreReplaceUser');")
        live_conn.commit()
        live_conn.close()

        with patch("os.replace", side_effect=OSError("Simulated disk I/O replace error")):
            with self.assertRaises(OSError):
                restore_database(self.backup_path, self.live_db_path)

        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT name FROM users ORDER BY id ASC;")
        names = [r["name"] for r in cur.fetchall()]
        self.assertIn("ConfirmedPreReplaceUser", names)
        self.assertIn("Alice", names)
        check_conn.close()

    def test_open_connection_without_active_transaction_aborts_restore(self):
        """An open idle connection (in_transaction=False) must cause restore to abort and protect target."""
        idle_conn = create_connection(self.live_db_path, enable_wal=True)
        # Connection is open and idle
        self.assertFalse(idle_conn.in_transaction)

        try:
            with self.assertRaises((RuntimeError, sqlite3.OperationalError)) as cm:
                restore_database(self.backup_path, self.live_db_path)
            self.assertTrue(
                "locked" in str(cm.exception).lower()
                or "in use" in str(cm.exception).lower()
            )
        finally:
            idle_conn.close()

        # Target data must remain completely intact
        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT name FROM users;")
        self.assertEqual(cur.fetchone()["name"], "Alice")
        check_conn.close()

    def test_existing_database_with_uncheckpointed_wal_preserves_data_on_failed_restore(self):
        """A database with uncheckpointed WAL frames preserves all data if restore fails."""
        live_conn = create_connection(self.live_db_path, enable_wal=True)
        live_conn.execute("INSERT INTO users VALUES (55, 'UncheckpointedUser');")
        live_conn.commit()
        live_conn.close()

        # Simulate restore failure during replace
        with patch("os.replace", side_effect=OSError("Simulated failure")):
            with self.assertRaises(OSError):
                restore_database(self.backup_path, self.live_db_path)

        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT name FROM users ORDER BY id ASC;")
        names = [r["name"] for r in cur.fetchall()]
        self.assertIn("UncheckpointedUser", names)
        check_conn.close()

    def test_corrupted_backup_preserves_target_and_wal_data(self):
        """Corrupted backup file raises DatabaseError and leaves target database and its data untouched."""
        live_conn = create_connection(self.live_db_path, enable_wal=True)
        live_conn.execute("INSERT INTO users VALUES (88, 'ExistingIntactUser');")
        live_conn.commit()
        live_conn.close()

        corrupt_backup = Path(self.temp_dir.name) / "corrupt.db"
        with open(corrupt_backup, "wb") as f:
            f.write(b"NOT A VALID SQLITE DATABASE FILE GARBAGE BYTES 12345")

        with self.assertRaises(sqlite3.DatabaseError):
            restore_database(corrupt_backup, self.live_db_path)

        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT name FROM users ORDER BY id ASC;")
        names = [r["name"] for r in cur.fetchall()]
        self.assertIn("ExistingIntactUser", names)
        check_conn.close()

    def test_rollback_failure_on_restoring_main_db_raises_critical_error(self):
        """If restoring main DB file during rollback fails, a CRITICAL error is raised."""
        import shutil
        orig_copy2 = shutil.copy2

        def failing_copy2(src, dst):
            if "orig_bak" in str(src) and str(dst).endswith("live.db"):
                raise OSError("Disk hardware failure during rollback copy")
            return orig_copy2(src, dst)

        with patch("os.replace", side_effect=OSError("Replace failed")), \
             patch("shutil.copy2", side_effect=failing_copy2):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)

            self.assertIn("CRITICAL RESTORE FAILURE", str(cm.exception))
            self.assertIn("live.db", str(cm.exception))

    def test_rollback_failure_on_restoring_wal_raises_critical_error(self):
        """If restoring WAL sidecar during rollback fails, a CRITICAL error is raised."""
        import shutil
        orig_copy2 = shutil.copy2
        wal_path = self.live_db_path.parent / f"{self.live_db_path.name}-wal"

        def failing_copy2(src, dst):
            # If target DB is being staged into safety copy, create wal_path so it gets staged too
            if ".orig_bak_" in str(dst) and str(src).endswith("live.db"):
                wal_path.write_bytes(b"dummy wal data to ensure staging")
            # Fail when restoring WAL from safety backup during rollback
            if "orig_bak" in str(src) and str(dst).endswith("-wal"):
                raise OSError("Simulated permission error restoring WAL during rollback")
            return orig_copy2(src, dst)

        with patch("os.replace", side_effect=OSError("Replace failed")), \
             patch("shutil.copy2", side_effect=failing_copy2):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)

            self.assertIn("CRITICAL RESTORE FAILURE", str(cm.exception))
            self.assertIn("-wal", str(cm.exception))

            # Safety backups must be preserved on critical rollback failure for manual recovery
            retained_backups = list(self.live_db_path.parent.glob("*.orig_bak_*"))
            self.assertGreater(len(retained_backups), 0)

            # Clean up retained backups
            for b in retained_backups:
                b.unlink()

    def test_staging_copy_failure_cleans_up_partial_backups_and_preserves_target(self):
        """If staging copy fails, partial backup files are cleaned up and target remains intact."""
        import shutil
        orig_copy2 = shutil.copy2
        wal_path = self.live_db_path.parent / f"{self.live_db_path.name}-wal"
        call_count = 0

        def fail_on_second_copy(src, dst):
            nonlocal call_count
            call_count += 1
            # On first copy (target DB), create wal_path so second copy (wal_path) will run
            if call_count == 1:
                wal_path.write_bytes(b"dummy wal data for partial staging test")
                return orig_copy2(src, dst)
            if call_count >= 2:
                raise OSError("Out of disk space during safety staging")
            return orig_copy2(src, dst)

        with patch("shutil.copy2", side_effect=fail_on_second_copy):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)

            self.assertIn("Failed to create safety backup of target database", str(cm.exception))

        # Partial backups must be cleaned up
        partial_backups = list(self.live_db_path.parent.glob("*.orig_bak_*"))
        self.assertEqual(partial_backups, [])

        # Target DB preserved
        check_conn = create_connection(self.live_db_path)
        cur = check_conn.cursor()
        cur.execute("PRAGMA integrity_check;")
        self.assertEqual(cur.fetchone()[0], "ok")
        check_conn.close()

    def test_blocked_or_busy_wal_checkpoint_aborts_restore(self):
        """If wal_checkpoint returns busy=1, restore aborts with RuntimeError."""
        real_connect = sqlite3.connect

        class FakeCursor:
            def fetchone(self):
                return (1, 5, 2)  # busy=1

        class FakeConn:
            def __init__(self, *args, **kwargs):
                self._conn = real_connect(*args, **kwargs)
            def execute(self, sql, *args):
                if "wal_checkpoint" in sql:
                    return FakeCursor()
                return self._conn.execute(sql, *args)
            def close(self):
                self._conn.close()

        with patch("sqlite3.connect", side_effect=lambda *a, **kw: FakeConn(*a, **kw)):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)

            self.assertIn("WAL checkpoint was blocked or busy", str(cm.exception))

    def test_incomplete_wal_checkpoint_aborts_restore(self):
        """If wal_checkpoint returns (0, 5, 2) [busy=0, log=5, checkpointed=2], restore aborts with RuntimeError."""
        real_connect = sqlite3.connect

        class FakeCursor:
            def fetchone(self):
                return (0, 5, 2)  # busy=0, log=5, checkpointed=2

        class FakeConn:
            def __init__(self, *args, **kwargs):
                self._conn = real_connect(*args, **kwargs)
            def execute(self, sql, *args):
                if "wal_checkpoint" in sql:
                    return FakeCursor()
                return self._conn.execute(sql, *args)
            def close(self):
                self._conn.close()

        with patch("sqlite3.connect", side_effect=lambda *a, **kw: FakeConn(*a, **kw)):
            with self.assertRaises(RuntimeError) as cm:
                restore_database(self.backup_path, self.live_db_path)

            self.assertIn("incomplete: only 2 of 5 frames were checkpointed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
