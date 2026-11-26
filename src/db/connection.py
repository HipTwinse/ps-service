"""SQLite connection management, PRAGMAs, and backup utilities."""

import os
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Generator, Optional


DEFAULT_DB_PATH = Path("data/ps_service.db")


def get_connection(
    db_path: Optional[str | Path] = None,
    enable_wal: bool = True,
    timeout_seconds: float = 5.0,
) -> sqlite3.Connection:
    """Creates a configured SQLite connection with foreign keys and safety PRAGMAs.

    Args:
        db_path: Path to SQLite database file or ':memory:'.
        enable_wal: Whether to set journal_mode=WAL (only for file databases).
        timeout_seconds: SQLite busy timeout in seconds.

    Returns:
        Configured sqlite3.Connection.
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH

    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_str = str(db_path)
    else:
        db_str = str(db_path)
        if db_str != ":memory:":
            Path(db_str).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        db_str,
        timeout=timeout_seconds,
    )
    conn.row_factory = sqlite3.Row

    # Mandatory PRAGMAs
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)};")

    if enable_wal and db_str != ":memory:":
        # WAL mode allows concurrent readers and one writer on the same host
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")

    return conn


create_connection = get_connection


@contextmanager
def get_connection_context(
    db_path: Optional[str | Path] = None,
    enable_wal: bool = True,
    timeout_seconds: float = 5.0,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a configured connection and automatically closing it."""
    conn = get_connection(db_path=db_path, enable_wal=enable_wal, timeout_seconds=timeout_seconds)
    try:
        yield conn
    finally:
        conn.close()


_VALID_TRANSACTION_MODES = {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}
_MODE_RANK = {"DEFERRED": 1, "IMMEDIATE": 2, "EXCLUSIVE": 3}


@dataclass
class _ManagedTxEntry:
    conn: sqlite3.Connection  # Strong reference held ONLY while the root managed transaction is active
    depth: int
    root_mode: str
    read_only: bool
    next_savepoint_id: int
    owner_thread_id: int


_ACTIVE_MANAGED_TX: Dict[int, _ManagedTxEntry] = {}
_TX_REGISTRY_LOCK = threading.Lock()


def get_active_managed_transaction_count() -> int:
    """Returns count of active root managed transactions in the process (for leak verification)."""
    with _TX_REGISTRY_LOCK:
        return len(_ACTIVE_MANAGED_TX)


@contextmanager
def transaction(
    conn: sqlite3.Connection,
    mode: str = "IMMEDIATE",
    *,
    read_only: bool = False,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite transactions with managed SAVEPOINT nesting and strict boundary safety.

    Design guarantees (SG-P2-004, AR-P3-002):
    - Uses an id(conn) registry protected by a lock with strict `entry.conn is conn` identity checks
      (since sqlite3.Connection does not support weakref in CPython) and guaranteed removal in root finally.
    - Refuses to join unmanaged external transactions (conn.in_transaction == True with no registry entry).
    - Enforces owner-thread affinity and forbids lock-mode escalation (e.g. DEFERRED -> IMMEDIATE)
      or write transactions nested inside read-only transactions.
    - Tracks SAVEPOINT creation explicitly: never issues ROLLBACK TO / RELEASE if SAVEPOINT creation failed,
      and ensures cleanup errors never mask the original exception.
    - Rolls back on any BaseException (including KeyboardInterrupt / CancelledError).

    Args:
        conn: The sqlite3.Connection to manage.
        mode: SQLite transaction mode ('DEFERRED', 'IMMEDIATE', 'EXCLUSIVE').
        read_only: Whether this transaction scope is strictly read-only.
    """
    norm_mode = mode.strip().upper() if isinstance(mode, str) else ""
    if norm_mode not in _VALID_TRANSACTION_MODES:
        raise ValueError(f"Invalid SQLite transaction mode: {mode!r}")
    if read_only and norm_mode != "DEFERRED":
        raise ValueError("Read-only managed transactions must use mode='DEFERRED'.")

    conn_id = id(conn)
    current_tid = threading.get_ident()
    is_root = False
    sp_name: Optional[str] = None
    entry: Optional[_ManagedTxEntry] = None

    with _TX_REGISTRY_LOCK:
        existing = _ACTIVE_MANAGED_TX.get(conn_id)
        if existing is None:
            if conn.in_transaction:
                raise RuntimeError(
                    "Cannot start managed transaction: connection already has an active uncommitted transaction."
                )
            entry = _ManagedTxEntry(
                conn=conn,
                depth=1,
                root_mode=norm_mode,
                read_only=read_only,
                next_savepoint_id=1,
                owner_thread_id=current_tid,
            )
            _ACTIVE_MANAGED_TX[conn_id] = entry
            is_root = True
        else:
            if existing.conn is not conn:
                raise RuntimeError("Managed transaction registry identity mismatch.")
            if not conn.in_transaction:
                _ACTIVE_MANAGED_TX.pop(conn_id, None)
                raise RuntimeError("Managed transaction state desynchronized: connection is not in transaction.")
            if existing.owner_thread_id != current_tid:
                raise RuntimeError("Cannot enter managed transaction from a different thread.")
            if existing.read_only and not read_only:
                raise RuntimeError("Cannot start write transaction inside a read-only transaction.")
            if _MODE_RANK[norm_mode] > _MODE_RANK[existing.root_mode]:
                raise RuntimeError(
                    f"Cannot escalate SQLite transaction mode from {existing.root_mode} to {norm_mode}."
                )
            entry = existing
            sp_name = f"sp_mgr_{entry.next_savepoint_id}"
            entry.next_savepoint_id += 1

    if is_root:
        begun = False
        try:
            conn.execute(f"BEGIN {norm_mode};")
            begun = True
            yield conn
            if read_only:
                if conn.in_transaction:
                    conn.rollback()
            else:
                conn.commit()
        except BaseException:
            if begun and conn.in_transaction:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise
        finally:
            with _TX_REGISTRY_LOCK:
                if _ACTIVE_MANAGED_TX.get(conn_id) is entry:
                    _ACTIVE_MANAGED_TX.pop(conn_id, None)
    else:
        assert entry is not None and sp_name is not None
        savepoint_created = False
        try:
            conn.execute(f"SAVEPOINT {sp_name};")
            with _TX_REGISTRY_LOCK:
                entry.depth += 1
            savepoint_created = True

            yield conn

            if conn.in_transaction:
                conn.execute(f"RELEASE SAVEPOINT {sp_name};")
        except BaseException:
            if savepoint_created and conn.in_transaction:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name};")
                except Exception:
                    pass
                if conn.in_transaction:
                    try:
                        conn.execute(f"RELEASE SAVEPOINT {sp_name};")
                    except Exception:
                        pass
            raise
        finally:
            if savepoint_created:
                with _TX_REGISTRY_LOCK:
                    entry.depth -= 1


def backup_database(source_conn: sqlite3.Connection, backup_path: str | Path) -> None:
    """Safely creates a consistent online backup of the SQLite database.

    Enforces strict transaction ownership: raises RuntimeError if source_conn has an
    active uncommitted transaction to avoid committing or interrupting caller transactions.

    Args:
        source_conn: Source database connection.
        backup_path: Destination path for the backup file.
    """
    if source_conn.in_transaction:
        raise RuntimeError(
            "Cannot backup database while connection has an active uncommitted transaction. "
            "Commit or rollback caller transaction before initiating backup."
        )

    backup_file = Path(backup_path)
    backup_file.parent.mkdir(parents=True, exist_ok=True)

    dest_conn = sqlite3.connect(str(backup_file))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()


def restore_database(backup_path: str | Path, target_path: str | Path) -> None:
    """Restores a database from a backup file to target location atomically and safely.

    CRITICAL OPERATIONAL REQUIREMENT (APPLICATION DOWNTIME MANDATORY):
    The application service and all worker processes MUST be completely stopped, and all
    database connections closed before invoking restore_database. An exclusive lock and WAL
    checkpoint truncate check is performed as an initial safeguard, but it cannot prevent
    race conditions if the application remains running. Modifying the target database concurrently
    during restoration is strictly prohibited.

    Guarantees:
    1. Best-effort lock probing and checkpoint validation detect active locks and dirty WAL frames.
    2. Restores into an isolated temporary file in the same directory/filesystem.
    3. Runs PRAGMA integrity_check on the restored temporary database before touching the target.
    4. Preserves safety copies of the original target and all its sidecars (-wal, -shm) before
       any modifications.
    5. Safely unlinks WAL sidecars. If removing any sidecar fails, restoration aborts and immediately
       rolls back, fully restoring the original database and sidecars without data loss.
    6. Performs atomic `os.replace`. If `os.replace` fails, original database and sidecars are
       fully restored from safety copies.
    7. Cleans up temporary files and safety copies; confirmed target data is never lost.

    Args:
        backup_path: Existing backup file path.
        target_path: Destination path for the restored database.

    Raises:
        FileNotFoundError: If backup_path does not exist.
        RuntimeError: If target is in active use or sidecars cannot be unlinked.
        sqlite3.DatabaseError: If backup file is corrupted or integrity check fails.
    """
    backup_file = Path(backup_path).resolve()
    target_file = Path(target_path).resolve()

    if not backup_file.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_file}")

    # Guard: check if target file exists and verify no active write locks or dirty WAL frames exist.
    # Note: PRAGMA locking_mode=EXCLUSIVE + BEGIN EXCLUSIVE and PRAGMA wal_checkpoint(TRUNCATE)
    # are defense-in-depth probes. SQLite does NOT guarantee cross-process detection of idle connections
    # or prevent newly opened connections once the probe closes.
    # Mandatory operational rule: service and all writers/readers MUST be stopped prior to restoration.
    if target_file.exists():
        try:
            test_conn = sqlite3.connect(str(target_file), timeout=0.1)
            try:
                test_conn.execute("PRAGMA locking_mode = EXCLUSIVE;")
                test_conn.execute("BEGIN EXCLUSIVE;")
                test_conn.execute("COMMIT;")
                # Safely checkpoint any pending WAL frames into main file and verify all frames backfilled
                cp_row = test_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
                if cp_row is None:
                    raise RuntimeError("Target database WAL checkpoint failed: no result returned.")

                busy, log_frames, checkpointed_frames = cp_row[0], cp_row[1], cp_row[2]
                if busy != 0:
                    raise RuntimeError(
                        f"Target database WAL checkpoint was blocked or busy (busy={busy}, log={log_frames}, checkpointed={checkpointed_frames}). "
                        f"Active writers or readers are present."
                    )
                if log_frames != checkpointed_frames:
                    raise RuntimeError(
                        f"Target database WAL checkpoint was incomplete: only {checkpointed_frames} of {log_frames} frames were checkpointed (busy={busy}). "
                        f"Active readers or uncheckpointed transactions are present."
                    )
            finally:
                test_conn.close()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            raise RuntimeError(
                f"Cannot restore database: target database '{target_file}' is in use or locked by an active connection. "
                f"Stop all operations before restoration: {e}"
            ) from e

    target_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_file.parent / f"{target_file.name}.restore_tmp_{os.getpid()}_{int(time.time() * 1000)}"

    src_conn = sqlite3.connect(str(backup_file))
    try:
        dest_conn = sqlite3.connect(str(temp_file))
        try:
            src_conn.backup(dest_conn)
            if dest_conn.in_transaction:
                dest_conn.commit()

            cur = dest_conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            res = cur.fetchone()[0]
            if res != "ok":
                raise sqlite3.DatabaseError(f"Restored database integrity check failed: {res}")
        finally:
            dest_conn.close()
    except BaseException:
        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        raise
    finally:
        src_conn.close()

    # Safety staging: preserve original target and sidecars before modification
    orig_backups: dict[Path, Path] = {}
    wal_sidecar = target_file.parent / f"{target_file.name}-wal"
    shm_sidecar = target_file.parent / f"{target_file.name}-shm"

    if target_file.exists():
        try:
            for p in (target_file, wal_sidecar, shm_sidecar):
                if p.exists():
                    bak = target_file.parent / f"{p.name}.orig_bak_{os.getpid()}_{int(time.time() * 1000)}"
                    try:
                        shutil.copy2(p, bak)
                        orig_backups[p] = bak
                    except FileNotFoundError:
                        pass
        except BaseException as staging_err:
            # Clean up partial safety backups and temp file on staging failure
            for bak in orig_backups.values():
                try:
                    bak.unlink()
                except OSError:
                    pass
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass
            if isinstance(staging_err, Exception):
                raise RuntimeError(
                    f"Failed to create safety backup of target database before restoration: {staging_err}"
                ) from staging_err
            raise

    try:
        # Step 1: Remove stale WAL sidecars before swapping target file.
        # If removing a sidecar fails, ABORT immediately and rollback to original files.
        for sidecar in (wal_sidecar, shm_sidecar):
            if sidecar.exists():
                try:
                    sidecar.unlink()
                except OSError as e:
                    raise RuntimeError(
                        f"Failed to remove stale WAL sidecar '{sidecar}': {e}. Aborting restore to prevent corruption."
                    ) from e

        # Step 2: Atomic replace of target file with restored temp file
        os.replace(temp_file, target_file)

        # Step 3: Success — clean up safety copies
        for bak in orig_backups.values():
            try:
                bak.unlink()
            except OSError:
                pass

    except BaseException as primary_err:
        # Failure rollback: restore original target and sidecars if they were modified/removed
        rollback_errors = []
        # Phase 1: Copy all safety backups back to their original paths.
        # DO NOT delete any safety backup until all files have been successfully restored!
        for orig_path, bak in orig_backups.items():
            if bak.exists():
                try:
                    shutil.copy2(bak, orig_path)
                except BaseException as rb_err:
                    rollback_errors.append(f"Failed to restore '{orig_path.name}' from safety backup: {rb_err}")

        if temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass

        if rollback_errors:
            # Leave all available safety backup copies on disk for manual operator recovery!
            err_details = "; ".join(rollback_errors)
            raise RuntimeError(
                f"CRITICAL RESTORE FAILURE: Primary restore operation failed AND rollback of original files failed. "
                f"Original target database state could NOT be guaranteed preserved! "
                f"Safety backups have been retained on disk for manual recovery. Rollback errors: {err_details}"
            ) from primary_err

        # Phase 2: All original files were successfully restored without error.
        # Now it is safe to clean up the safety backups.
        for bak in orig_backups.values():
            try:
                bak.unlink()
            except OSError:
                pass

        raise
