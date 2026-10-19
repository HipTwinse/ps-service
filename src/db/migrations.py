"""Database schema migration runner."""

import os
import re
import sqlite3
from pathlib import Path
from typing import List, Tuple

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensures that the schema_migrations tracking table exists."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def get_applied_migrations(conn: sqlite3.Connection) -> List[int]:
    """Returns sorted list of applied migration version numbers."""
    ensure_migration_table(conn)
    cursor = conn.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
    return [row["version"] for row in cursor.fetchall()]


def discover_migration_files(migrations_dir: Path = MIGRATIONS_DIR) -> List[Tuple[int, str, Path]]:
    """Discovers and sorts all SQL migration files in the migrations directory.

    Files must match the pattern: <version>_<name>.sql (e.g., 001_initial_schema.sql)

    Returns:
        List of tuples (version, name, file_path).
    """
    if not migrations_dir.exists():
        return []

    discovered = []
    pattern = re.compile(r"^(\d+)_(.*)\.sql$")

    for file in migrations_dir.glob("*.sql"):
        match = pattern.match(file.name)
        if match:
            version = int(match.group(1))
            name = match.group(2)
            discovered.append((version, name, file))

    discovered.sort(key=lambda x: x[0])
    return discovered


def run_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> List[int]:
    """Applies all pending migrations in order inside individual transactions.

    Args:
        conn: Target sqlite3.Connection.
        migrations_dir: Directory containing migration files.

    Returns:
        List of newly applied migration versions.
    """
    ensure_migration_table(conn)
    applied = set(get_applied_migrations(conn))
    migration_files = discover_migration_files(migrations_dir)

    newly_applied: List[int] = []

    for version, name, file_path in migration_files:
        if version in applied:
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            sql_script = f.read()

        # In Python's sqlite3, executescript() issues an implicit COMMIT before running.
        # To guarantee that the migration script and its schema_migrations record are
        # executed within a single, strictly atomic transaction without naive ';' splitting,
        # we encapsulate the script and the audit record inside BEGIN IMMEDIATE ... COMMIT.
        escaped_name = name.replace("'", "''")
        wrapped_script = (
            f"BEGIN IMMEDIATE;\n"
            f"{sql_script}\n"
            f"INSERT INTO schema_migrations (version, name) VALUES ({version}, '{escaped_name}');\n"
            f"COMMIT;\n"
        )

        try:
            conn.executescript(wrapped_script)
            newly_applied.append(version)
        except Exception as e:
            if conn.in_transaction:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise RuntimeError(
                f"Migration {version}_{name} failed: {str(e)}"
            ) from e

    return newly_applied
