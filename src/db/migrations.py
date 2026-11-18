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


def run_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> List[int]:
    """Applies all pending migrations in order inside individual strictly atomic transactions.

    Guarantees:
    1. Rejects execution immediately if `conn.in_transaction` is already True (never commits caller tx).
    2. Verifies uniqueness of migration version numbers and SHA-256 checksums of previously applied files.
    3. Schema modifications and the corresponding schema_migrations record run in ONE atomic transaction.
    4. Encapsulates the entire migration script and migration audit record inside a single
       `BEGIN IMMEDIATE; ... COMMIT;` block executed via `executescript()`.
    5. Does not use naive `;` splitting, protecting triggers and complex multi-statement SQL.
    6. If any statement, checksum verification, or version recording fails, rolls back completely:
       neither schema modifications nor the version record persist.

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
    cur = conn.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC")
    applied_checksums: Dict[int, Optional[str]] = {
        row["version"]: row["checksum"] for row in cur.fetchall()
    }
    migration_files = discover_migration_files(migrations_dir)

    newly_applied: List[int] = []

    for version, name, file_path in migration_files:
        with open(file_path, "r", encoding="utf-8") as f:
            sql_script = f.read()

        file_checksum = hashlib.sha256(sql_script.encode("utf-8")).hexdigest()

        if version in applied_checksums:
            stored_checksum = applied_checksums[version]
            if stored_checksum is not None and stored_checksum != file_checksum:
                raise RuntimeError(
                    f"Migration {version}_{name} checksum mismatch: applied SQL file has been modified."
                )
            continue

        escaped_name = name.replace("'", "''")
        wrapped_script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql_script}\n"
            f"INSERT INTO schema_migrations (version, name, checksum) VALUES ({version}, '{escaped_name}', '{file_checksum}');\n"
            "COMMIT;\n"
        )

        try:
            conn.executescript(wrapped_script)
            newly_applied.append(version)
            applied_checksums[version] = file_checksum
        except Exception as e:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise RuntimeError(
                f"Migration {version}_{name} failed: {str(e)}"
            ) from e

    return newly_applied
