"""Database schema migration runner."""

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.db.connection import transaction

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensures that the schema_migrations tracking table exists (and has checksum column).

    Enforces exclusive transaction boundary ownership: raises RuntimeError if `conn.in_transaction`
    is already True so caller transactions are never implicitly committed.
    """
    with transaction(conn, mode="IMMEDIATE"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur = conn.execute("PRAGMA table_info(schema_migrations);")
        existing_cols = {row["name"] for row in cur.fetchall()}
        if "checksum" not in existing_cols:
            conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum TEXT;")


def get_applied_migrations(conn: sqlite3.Connection) -> List[int]:
    """Returns sorted list of applied migration version numbers."""
    if conn.in_transaction:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not row:
            # Calling ensure_migration_table will safely raise RuntimeError without committing caller tx
            ensure_migration_table(conn)
    else:
        ensure_migration_table(conn)

    cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
    return [row["version"] for row in cursor.fetchall()]


def discover_migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> List[Tuple[int, str, Path]]:
    """Discovers and sorts all SQL migration files in the migrations directory.

    Files must match the pattern: <version>_<name>.sql (e.g., 001_initial_schema.sql)
    Enforces strict uniqueness of numeric version identifiers.

    Returns:
        List of tuples (version, name, file_path).

    Raises:
        ValueError: If two migration files share the same numeric version.
    """
    if not migrations_dir.exists():
        return []

    discovered: List[Tuple[int, str, Path]] = []
    seen_versions: Dict[int, Path] = {}
    pattern = re.compile(r"^(\d+)_(.*)\.sql$")

    for file in sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name):
        match = pattern.match(file.name)
        if match:
            version = int(match.group(1))
            name = match.group(2)
            if version in seen_versions:
                raise ValueError(
                    f"Duplicate migration version {version}: '{seen_versions[version].name}' and '{file.name}'."
                )
            seen_versions[version] = file
            discovered.append((version, name, file))

    discovered.sort(key=lambda x: x[0])
    return discovered


def _split_sql_statements(sql_script: str) -> List[str]:
    """Splits a SQL script into complete statements using sqlite3.complete_statement().

    Handles multi-statement triggers (BEGIN ... END;) without naive ';' splitting.
    """
    statements: List[str] = []
    buffer = ""
    for line in sql_script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""

    leftover_lines = [
        ln.strip()
        for ln in buffer.splitlines()
        if ln.strip() and not ln.strip().startswith("--")
    ]
    if leftover_lines:
        raise ValueError(f"Incomplete SQL statement in migration script: {buffer.strip()[:120]}")
    return statements


def run_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> List[int]:
    """Applies all pending migrations in order inside individual strictly atomic transactions.

    Guarantees:
    1. Rejects execution immediately if `conn.in_transaction` is already True (never commits caller tx).
    2. Verifies uniqueness of migration version numbers and SHA-256 checksums for previously applied
       migrations that have a non-NULL stored checksum (legacy NULL-checksum rows are tolerated).
    3. Acquires a `BEGIN IMMEDIATE` write lock before checking whether each migration is already
       applied, serializing concurrent migration runners safely.
    4. Executes all statements of each migration and inserts the `schema_migrations` record inside
       the same `BEGIN IMMEDIATE` transaction via `sqlite3.complete_statement()` splitting.
    5. If any statement, checksum verification, or version recording fails (including BaseException),
       rolls back completely: neither schema modifications nor the version record persist.

    Args:
        conn: Target sqlite3.Connection.
        migrations_dir: Directory containing migration files.

    Returns:
        List of newly applied migration versions.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "Cannot execute run_migrations: connection already has an active uncommitted transaction."
        )

    ensure_migration_table(conn)
    migration_files = discover_migration_files(migrations_dir)

    newly_applied: List[int] = []

    for version, name, file_path in migration_files:
        with open(file_path, "r", encoding="utf-8") as f:
            sql_script = f.read()

        file_checksum = hashlib.sha256(sql_script.encode("utf-8")).hexdigest()

        try:
            with transaction(conn, mode="IMMEDIATE"):
                row = conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (version,),
                ).fetchone()
                if row is not None:
                    stored_checksum = row["checksum"]
                    if stored_checksum is not None and stored_checksum != file_checksum:
                        raise RuntimeError(
                            f"Migration {version}_{name} checksum mismatch: applied SQL file has been modified."
                        )
                    continue

                for stmt in _split_sql_statements(sql_script):
                    conn.execute(stmt)

                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (?, ?, ?)",
                    (version, name, file_checksum),
                )
                newly_applied.append(version)
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(
                f"Migration {version}_{name} failed: {str(e)}"
            ) from e

    return newly_applied
