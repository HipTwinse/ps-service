"""Tests for Stage 3.1 transaction registry, nested SAVEPOINT failure paths, and UnitOfWork (SG-P2-004, AR-P3-002, AR-P3-003)."""

import os
import sqlite3
import tempfile
import threading
import unittest

from src.db.connection import (
    backup_database,
    create_connection,
    get_active_managed_transaction_count,
    get_connection,
    transaction,
)
from src.db.migrations import run_migrations
from src.db.models import (
    DatabaseError,
    ProductStatus,
    ProductType,
)
from src.db.repositories import CatalogRepository, OrderRepository, UserRepository
from src.db.uow import ReadSession, UnitOfWork, WriteSession


class _CustomBaseAbort(BaseException):
    """Non-Exception BaseException used to verify cleanup under cancellation/interrupts."""


class _CustomInnerError(Exception):
    """Distinct exception raised inside nested transactions to verify no cleanup masking."""


class _FaultyConnection(sqlite3.Connection):
    """SQLite connection subclass that injects failures for matching SQL prefixes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_rules: dict[str, Exception] = {}
        self.executed_sql: list[str] = []

    def execute(self, sql: str, parameters=()):
        norm = sql.strip().upper()
        self.executed_sql.append(sql.strip())
        for prefix, exc in list(self.fail_rules.items()):
            if norm.startswith(prefix.upper()):
                raise exc
        return super().execute(sql, parameters)


class TestStage31TransactionsAndUoW(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_env = os.environ.get("ENV")
        os.environ["ENV"] = "test"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stage31_uow.db")
        conn = create_connection(self.db_path)
        run_migrations(conn)
        conn.close()
        self.assertEqual(get_active_managed_transaction_count(), 0)

    def tearDown(self) -> None:
        self.assertEqual(get_active_managed_transaction_count(), 0)
        self.temp_dir.cleanup()
        if self._prev_env is None:
            os.environ.pop("ENV", None)
        else:
            os.environ["ENV"] = self._prev_env

    # ------------------------------------------------------------------
    # Transaction Registry & Nested SAVEPOINT Tests (SG-P2-004, AR-P3-002)
    # ------------------------------------------------------------------

    def test_managed_tx_registry_cleanup_on_commit_rollback_base_exception_and_begin_failure(self) -> None:
        conn = get_connection(self.db_path)
        try:
            with transaction(conn, mode="IMMEDIATE"):
                self.assertEqual(get_active_managed_transaction_count(), 1)
                conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('u_commit', 'u1', 'ACTIVE')"
                )
            self.assertEqual(get_active_managed_transaction_count(), 0)
            self.assertFalse(conn.in_transaction)

            with self.assertRaises(RuntimeError):
                with transaction(conn, mode="IMMEDIATE"):
                    self.assertEqual(get_active_managed_transaction_count(), 1)
                    conn.execute(
                        "INSERT INTO users (external_id, username, status) VALUES ('u_rb', 'u2', 'ACTIVE')"
                    )
                    raise RuntimeError("abort write")
            self.assertEqual(get_active_managed_transaction_count(), 0)
            self.assertFalse(conn.in_transaction)
            self.assertIsNone(
                conn.execute("SELECT id FROM users WHERE external_id = 'u_rb'").fetchone()
            )

            with self.assertRaises(_CustomBaseAbort):
                with transaction(conn, mode="IMMEDIATE"):
                    self.assertEqual(get_active_managed_transaction_count(), 1)
                    conn.execute(
                        "INSERT INTO users (external_id, username, status) VALUES ('u_base', 'u3', 'ACTIVE')"
                    )
                    raise _CustomBaseAbort("cancelled")
            self.assertEqual(get_active_managed_transaction_count(), 0)
            self.assertFalse(conn.in_transaction)
            self.assertIsNone(
                conn.execute("SELECT id FROM users WHERE external_id = 'u_base'").fetchone()
            )
        finally:
            conn.close()

        faulty = sqlite3.connect(self.db_path, factory=_FaultyConnection)
        faulty.fail_rules["BEGIN IMMEDIATE"] = sqlite3.OperationalError("forced begin failure")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with transaction(faulty, mode="IMMEDIATE"):
                    pass
            self.assertEqual(get_active_managed_transaction_count(), 0)
            self.assertFalse(faulty.in_transaction)
        finally:
            faulty.close()

    def test_nested_savepoint_commit_partial_rollback_and_cascade_rollback(self) -> None:
        conn = get_connection(self.db_path)
        try:
            # 1. Both outer and inner succeed
            with transaction(conn, mode="IMMEDIATE"):
                conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('outer_ok', 'o1', 'ACTIVE')"
                )
                with transaction(conn, mode="IMMEDIATE"):
                    conn.execute(
                        "INSERT INTO users (external_id, username, status) VALUES ('inner_ok', 'i1', 'ACTIVE')"
                    )
            self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE external_id = 'outer_ok'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE external_id = 'inner_ok'").fetchone())

            # 2. Inner failure caught by outer rolls back ONLY savepoint
            with transaction(conn, mode="IMMEDIATE"):
                conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('outer_keep', 'o2', 'ACTIVE')"
                )
                with self.assertRaises(_CustomInnerError):
                    with transaction(conn, mode="IMMEDIATE"):
                        conn.execute(
                            "INSERT INTO users (external_id, username, status) VALUES ('inner_discard', 'i2', 'ACTIVE')"
                        )
                        raise _CustomInnerError("inner failed")
            self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE external_id = 'outer_keep'").fetchone())
            self.assertIsNone(conn.execute("SELECT id FROM users WHERE external_id = 'inner_discard'").fetchone())

            # 3. Inner succeeds & releases, then outer raises -> whole transaction rolls back
            with self.assertRaises(RuntimeError):
                with transaction(conn, mode="IMMEDIATE"):
                    conn.execute(
                        "INSERT INTO users (external_id, username, status) VALUES ('outer_fail', 'o3', 'ACTIVE')"
                    )
                    with transaction(conn, mode="IMMEDIATE"):
                        conn.execute(
                            "INSERT INTO users (external_id, username, status) VALUES ('inner_released_then_rolled_back', 'i3', 'ACTIVE')"
                        )
                    raise RuntimeError("outer aborted after inner savepoint")
            self.assertIsNone(conn.execute("SELECT id FROM users WHERE external_id = 'outer_fail'").fetchone())
            self.assertIsNone(
                conn.execute("SELECT id FROM users WHERE external_id = 'inner_released_then_rolled_back'").fetchone()
            )
            self.assertEqual(get_active_managed_transaction_count(), 0)
        finally:
            conn.close()

    def test_ar_p3_002_savepoint_creation_failure_does_not_rollback_nonexistent_savepoint(self) -> None:
        conn = sqlite3.connect(self.db_path, factory=_FaultyConnection)
        conn.row_factory = sqlite3.Row
        try:
            with transaction(conn, mode="IMMEDIATE"):
                conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('before_sp_fail', 'u_ok', 'ACTIVE')"
                )
                conn.fail_rules["SAVEPOINT"] = sqlite3.OperationalError("simulated savepoint creation failure")
                with self.assertRaises(sqlite3.OperationalError):
                    with transaction(conn, mode="IMMEDIATE"):
                        self.fail("Body must not execute when SAVEPOINT creation fails")
                conn.fail_rules.clear()

                # Must NOT have attempted ROLLBACK TO or RELEASE on nonexistent savepoint
                sp_cleanup_cmds = [
                    sql for sql in conn.executed_sql
                    if sql.upper().startswith("ROLLBACK TO SAVEPOINT") or sql.upper().startswith("RELEASE SAVEPOINT")
                ]
                self.assertEqual(sp_cleanup_cmds, [])
                self.assertTrue(conn.in_transaction)
                # Verify subsequent valid savepoint still works at clean depth
                with transaction(conn, mode="IMMEDIATE"):
                    conn.execute(
                        "INSERT INTO users (external_id, username, status) VALUES ('after_sp_fail', 'u_ok2', 'ACTIVE')"
                    )

            self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE external_id = 'before_sp_fail'").fetchone())
            self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE external_id = 'after_sp_fail'").fetchone())
            self.assertEqual(get_active_managed_transaction_count(), 0)
        finally:
            conn.close()

    def test_ar_p3_002_savepoint_cleanup_error_never_masks_original_exception(self) -> None:
        conn = sqlite3.connect(self.db_path, factory=_FaultyConnection)
        conn.row_factory = sqlite3.Row
        try:
            with transaction(conn, mode="IMMEDIATE"):
                conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('outer_survives', 'u_keep', 'ACTIVE')"
                )
                with self.assertRaises(_CustomInnerError) as ctx:
                    with transaction(conn, mode="IMMEDIATE"):
                        conn.fail_rules["ROLLBACK TO SAVEPOINT"] = sqlite3.OperationalError("rollback cleanup broken")
                        conn.fail_rules["RELEASE SAVEPOINT"] = sqlite3.OperationalError("release cleanup broken")
                        raise _CustomInnerError("primary business error")
                self.assertEqual(str(ctx.exception), "primary business error")
                conn.fail_rules.clear()

            self.assertIsNotNone(conn.execute("SELECT id FROM users WHERE external_id = 'outer_survives'").fetchone())
            self.assertEqual(get_active_managed_transaction_count(), 0)
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()

    def test_ar_p3_002_savepoint_release_failure_cleans_up_depth_and_rolls_back(self) -> None:
        conn = sqlite3.connect(self.db_path, factory=_FaultyConnection)
        conn.row_factory = sqlite3.Row
        try:
            with self.assertRaises(sqlite3.OperationalError):
                with transaction(conn, mode="IMMEDIATE"):
                    conn.execute(
                        "INSERT INTO users (external_id, username, status) VALUES ('outer_uncommitted', 'u_no', 'ACTIVE')"
                    )
                    with transaction(conn, mode="IMMEDIATE"):
                        conn.fail_rules["RELEASE SAVEPOINT"] = sqlite3.OperationalError("release failed")
                        conn.execute(
                            "INSERT INTO users (external_id, username, status) VALUES ('inner_rel_fail', 'u_no2', 'ACTIVE')"
                        )
            conn.fail_rules.clear()
            self.assertEqual(get_active_managed_transaction_count(), 0)
            self.assertFalse(conn.in_transaction)
            self.assertIsNone(
                conn.execute("SELECT id FROM users WHERE external_id = 'outer_uncommitted'").fetchone()
            )
        finally:
            conn.close()

    def test_transaction_boundary_guards_unmanaged_thread_escalation_readonly_and_maintenance(self) -> None:
        conn = get_connection(self.db_path)
        try:
            # 1. Unmanaged external transaction is rejected
            conn.execute("BEGIN IMMEDIATE;")
            with self.assertRaisesRegex(RuntimeError, "active uncommitted transaction"):
                with transaction(conn, mode="IMMEDIATE"):
                    pass
            conn.rollback()

            # 2. Cross-thread entry into active managed transaction is rejected
            entered = threading.Event()
            release = threading.Event()
            worker_errors: list[Exception] = []

            with transaction(conn, mode="IMMEDIATE"):
                def cross_thread_worker():
                    try:
                        with transaction(conn, mode="IMMEDIATE"):
                            pass
                    except Exception as exc:
                        worker_errors.append(exc)
                    finally:
                        entered.set()

                t = threading.Thread(target=cross_thread_worker)
                t.start()
                self.assertTrue(entered.wait(timeout=5.0))
                t.join(timeout=5.0)
                self.assertFalse(t.is_alive())
            self.assertEqual(len(worker_errors), 1)
            self.assertIsInstance(worker_errors[0], RuntimeError)
            self.assertIn("different thread", str(worker_errors[0]).lower())

            # 3. Lock-mode escalation DEFERRED -> IMMEDIATE is rejected
            with transaction(conn, mode="DEFERRED"):
                with self.assertRaisesRegex(RuntimeError, "escalate"):
                    with transaction(conn, mode="IMMEDIATE"):
                        pass

            # 4. Write tx inside read_only=True is rejected
            with transaction(conn, mode="DEFERRED", read_only=True):
                with self.assertRaisesRegex(RuntimeError, "read-only"):
                    with transaction(conn, mode="DEFERRED", read_only=False):
                        pass

            # 5. backup_database and run_migrations rejected inside active managed tx
            with transaction(conn, mode="IMMEDIATE"):
                with self.assertRaises(RuntimeError):
                    backup_database(conn, os.path.join(self.temp_dir.name, "bad_backup.db"))
                with self.assertRaises(RuntimeError):
                    run_migrations(conn)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # UnitOfWork & WAL Snapshot Tests (AR-P3-003)
    # ------------------------------------------------------------------

    def test_ar_p3_003_uow_read_wal_snapshot_positive_and_select1_negative_control(self) -> None:
        writer_conn = get_connection(self.db_path)
        seed_user, _ = UserRepository(writer_conn).get_or_create_user("snapshot_u1", username="v1_initial")

        # 1. Positive control: UnitOfWork.read() anchors WAL snapshot via schema_migrations read BEFORE yielding
        reader_conn = get_connection(self.db_path)
        traced_sql: list[str] = []
        reader_conn.set_trace_callback(traced_sql.append)
        try:
            uow_read = UnitOfWork.from_connection(reader_conn)
            with uow_read.read() as rs:
                self.assertIsInstance(rs, ReadSession)
                self.assertGreaterEqual(rs.schema_version, 7)

                # Verify schema_migrations read executed before ReadSession body ran (not SELECT 1)
                self.assertTrue(
                    any("FROM SCHEMA_MIGRATIONS" in s.upper() for s in traced_sql),
                    f"Expected schema_migrations read before yielding ReadSession, got: {traced_sql}",
                )
                self.assertFalse(any(s.strip().upper().rstrip(";") == "SELECT 1" for s in traced_sql))

                # Concurrent external writer commits BEFORE ReadSession calls rs.get_user(...)
                UserRepository(writer_conn).get_or_create_user("snapshot_u1", username="v2_committed_by_writer")

                # Because snapshot was already pinned via schema_migrations read, rs STILL sees v1_initial
                snap_user = rs.get_user(seed_user.id)
                self.assertEqual(snap_user.username, "v1_initial")

            # New uow.read() snapshot sees the newly committed username
            with uow_read.read() as rs2:
                fresh_user = rs2.get_user(seed_user.id)
                self.assertEqual(fresh_user.username, "v2_committed_by_writer")
        finally:
            reader_conn.set_trace_callback(None)
            reader_conn.close()

        # 2. Negative control: raw BEGIN DEFERRED + SELECT 1 does NOT establish a WAL read snapshot
        neg_conn = get_connection(self.db_path)
        try:
            neg_conn.execute("PRAGMA query_only = ON;")
            neg_conn.execute("BEGIN DEFERRED;")
            neg_conn.execute("SELECT 1;").fetchone()

            # Concurrent external writer commits AFTER SELECT 1 but BEFORE first real table read
            UserRepository(writer_conn).get_or_create_user("snapshot_u1", username="v3_visible_after_select1")

            observed = neg_conn.execute(
                "SELECT username FROM users WHERE id = ?",
                (seed_user.id,),
            ).fetchone()["username"]
            self.assertEqual(observed, "v3_visible_after_select1")
            neg_conn.rollback()
            neg_conn.execute("PRAGMA query_only = OFF;")
        finally:
            neg_conn.close()
            writer_conn.close()

    def test_ar_p3_003_uow_read_rejects_writes_and_restores_query_only_and_checks_schema(self) -> None:
        # 1. from_db_path read rejects SQL writes and nested write transactions
        uow_path = UnitOfWork.from_db_path(self.db_path)
        with uow_path.read() as rs:
            self.assertFalse(hasattr(rs, "create_order"))
            with self.assertRaises(sqlite3.OperationalError):
                rs.conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('ro_hack', 'x', 'ACTIVE')"
                )
            with self.assertRaises(RuntimeError):
                with transaction(rs.conn, mode="IMMEDIATE"):
                    pass

        # 2. Borrowed connection preserves previous query_only (0 -> 0 and 1 -> 1) across normal, Exception, and BaseException exits
        conn = get_connection(self.db_path)
        try:
            uow_conn = UnitOfWork.from_connection(conn)
            self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 0)

            with uow_conn.read():
                self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)

            with self.assertRaises(_CustomInnerError):
                with uow_conn.read():
                    self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 1)
                    raise _CustomInnerError("read error")
            self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)

            with self.assertRaises(_CustomBaseAbort):
                with uow_conn.read():
                    raise _CustomBaseAbort("base abort in read")
            self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 0)
            self.assertFalse(conn.in_transaction)

            # Previous query_only=1 stays 1 on exit
            conn.execute("PRAGMA query_only = 1;")
            with uow_conn.read():
                self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA query_only;").fetchone()[0], 1)
            conn.execute("PRAGMA query_only = 0;")

            # Uninitialized DB without schema_migrations raises clear DatabaseError and restores query_only
            blank_db = os.path.join(self.temp_dir.name, "blank.db")
            blank_conn = get_connection(blank_db)
            try:
                with self.assertRaisesRegex(DatabaseError, "Database schema is not initialized"):
                    with UnitOfWork.from_connection(blank_conn).read():
                        pass
                self.assertEqual(blank_conn.execute("PRAGMA query_only;").fetchone()[0], 0)
                self.assertFalse(blank_conn.in_transaction)

                # Empty schema_migrations table also raises DatabaseError
                blank_conn.execute(
                    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);"
                )
                with self.assertRaisesRegex(DatabaseError, "Database schema is not initialized"):
                    with UnitOfWork.from_connection(blank_conn).read():
                        pass
                self.assertEqual(blank_conn.execute("PRAGMA query_only;").fetchone()[0], 0)
                self.assertFalse(blank_conn.in_transaction)
            finally:
                blank_conn.close()

            # 3. Failure during BEGIN DEFERRED restores PRAGMA query_only in outer finally
            faulty_ro = sqlite3.connect(self.db_path, factory=_FaultyConnection)
            faulty_ro.row_factory = sqlite3.Row
            faulty_ro.fail_rules["BEGIN DEFERRED"] = sqlite3.OperationalError("forced begin deferred failure")
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    with UnitOfWork.from_connection(faulty_ro).read():
                        pass
                faulty_ro.fail_rules.clear()
                self.assertEqual(faulty_ro.execute("PRAGMA query_only;").fetchone()[0], 0)
                self.assertFalse(faulty_ro.in_transaction)
                self.assertEqual(get_active_managed_transaction_count(), 0)
            finally:
                faulty_ro.close()

            # 4. Failure setting PRAGMA query_only = ON leaves connection clean
            faulty_pragma = sqlite3.connect(self.db_path, factory=_FaultyConnection)
            faulty_pragma.row_factory = sqlite3.Row
            faulty_pragma.fail_rules["PRAGMA QUERY_ONLY = ON"] = sqlite3.OperationalError("forced pragma failure")
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    with UnitOfWork.from_connection(faulty_pragma).read():
                        pass
                faulty_pragma.fail_rules.clear()
                self.assertEqual(faulty_pragma.execute("PRAGMA query_only;").fetchone()[0], 0)
                self.assertFalse(faulty_pragma.in_transaction)
            finally:
                faulty_pragma.close()
        finally:
            conn.close()

    def test_uow_write_composes_multi_repository_operations_atomically(self) -> None:
        uow = UnitOfWork.from_db_path(self.db_path)

        with self.assertRaisesRegex(ValueError, "DEFERRED forbidden"):
            with uow.write(mode="DEFERRED"):
                pass

        # Successful atomic write spanning Catalog, User, Order, and Audit
        with uow.write() as ws:
            self.assertIsInstance(ws, WriteSession)
            prod = ws.catalog.create_voucher_product("PSN TR", "TR", "TRY", status=ProductStatus.APPROVED)
            denom = ws.catalog.add_voucher_denomination(prod.id, "TR", "TRY", 25000, 120000)
            user, _ = ws.users.get_or_create_user("uow_buyer", username="buyer")
            order = ws.orders.create_order(
                user.id,
                120000,
                "RUB",
                [
                    {
                        "product_id": prod.id,
                        "denomination_id": denom.id,
                        "product_type_snapshot": ProductType.VOUCHER.value,
                        "quantity": 1,
                        "unit_price_minor": 120000,
                    }
                ],
            )
            ws.audit.record_event("UOW_ORDER_CREATED", "ORDER", order.id, actor_type="SYSTEM")

        # Subsequent failure rolls back ALL inner savepointed repo calls inside write()
        with self.assertRaises(_CustomInnerError):
            with uow.write() as ws:
                ws.users.get_or_create_user("uow_rolled_back_user", username="ghost")
                ws.audit.record_event("SHOULD_ROLL_BACK", "ORDER", order.id)
                raise _CustomInnerError("force full write rollback")

        with uow.read() as rs:
            events = rs.list_audit_events(entity_type="ORDER", entity_id=order.id)
            self.assertEqual([e.event_type for e in events], ["UOW_ORDER_CREATED"])
            self.assertEqual(rs.count_open_pending_orders(user.id), 1)
            row = rs.conn.execute("SELECT id FROM users WHERE external_id = 'uow_rolled_back_user'").fetchone()
            self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
