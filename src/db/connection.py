"""SQLite connection management, PRAGMAs, and backup utilities."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional


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


@contextmanager
def transaction(conn: sqlite3.Connection, mode: str = "IMMEDIATE") -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite transactions with explicit locking modes.

    Using mode='IMMEDIATE' acquires a write lock at the start of the transaction,
    preventing concurrent threads/processes from conflicting during two-step checks.

    Args:
        conn: The sqlite3.Connection to manage.
        mode: SQLite transaction mode ('DEFERRED', 'IMMEDIATE', 'EXCLUSIVE').
    """
    conn.execute(f"BEGIN {mode}")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def backup_database(source_conn: sqlite3.Connection, backup_path: str | Path) -> None:
    """Safely creates a consistent online backup of the SQLite database.

    Args:
        source_conn: Source database connection.
        backup_path: Destination path for the backup file.
    """
    if source_conn.in_transaction:
        source_conn.commit()

    backup_file = Path(backup_path)
    backup_file.parent.mkdir(parents=True, exist_ok=True)

    dest_conn = sqlite3.connect(str(backup_file))
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()


def restore_database(backup_path: str | Path, target_path: str | Path) -> None:
    """Restores a database from a backup file to target location.

    Args:
        backup_path: Existing backup file path.
        target_path: Destination path for the restored database.
    """
    src_conn = sqlite3.connect(str(backup_path))
    dest_conn = get_connection(target_path, enable_wal=True)
    try:
        src_conn.backup(dest_conn)
        if dest_conn.in_transaction:
            dest_conn.commit()
    finally:
        src_conn.close()
        dest_conn.close()
