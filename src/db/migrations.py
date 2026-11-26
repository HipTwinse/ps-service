"""Database schema migration runner."""

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple

from src.db.connection import transaction

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class MigrationPreflightError(RuntimeError):
    """Raised when a migration pre-flight check detects inconsistent pre-existing data."""

    def __init__(self, migration_version: int, check_name: str, message: str) -> None:
        self.migration_version = migration_version
        self.check_name = check_name
        super().__init__(f"Migration {migration_version:03d} pre-flight failed [{check_name}]: {message}")


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
    """Splits a SQL script into individual statements with full quote/comment awareness (SG-P2-005).

    Handles:
    - Multiple statements on a single line (e.g. `CREATE TABLE a(x INT); CREATE TABLE b(y INT);`).
    - Multi-statement trigger blocks (`CREATE TRIGGER ... BEGIN ... END;`) via `sqlite3.complete_statement()`.
    - Single-quoted (`''`), double-quoted (`""`), backtick, and `[...]` identifiers.
    - `--` line comments and `/* ... */` block comments (including trailing comments at EOF).
    """
    statements: List[str] = []
    n = len(sql_script)
    stmt_start = 0
    i = 0

    in_single = False
    in_double = False
    in_backtick = False
    in_bracket = False
    in_line_comment = False
    in_block_comment = False
    has_code_in_current = False

    while i < n:
        ch = sql_script[i]
        nxt = sql_script[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        if in_single:
            if ch == "'" and nxt == "'":
                i += 2
                continue
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            if ch == '"' and nxt == '"':
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue

        if in_backtick:
            if ch == "`" and nxt == "`":
                i += 2
                continue
            if ch == "`":
                in_backtick = False
            i += 1
            continue

        if in_bracket:
            if ch == "]":
                in_bracket = False
            i += 1
            continue

        # Outside quotes and comments
        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch.isspace():
            i += 1
            continue

        has_code_in_current = True

        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "`":
            in_backtick = True
            i += 1
            continue
        if ch == "[":
            in_bracket = True
            i += 1
            continue

        if ch == ";":
            candidate = sql_script[stmt_start : i + 1]
            if sqlite3.complete_statement(candidate):
                stmt = candidate.strip()
                if stmt:
                    statements.append(stmt)
                stmt_start = i + 1
                has_code_in_current = False
            i += 1
            continue

        i += 1

    if in_single or in_double or in_backtick or in_bracket:
        raise ValueError("Unclosed quoted string or identifier in migration script.")
    if in_block_comment:
        raise ValueError("Unclosed block comment in migration script.")
    if has_code_in_current:
        leftover = sql_script[stmt_start:].strip()
        raise ValueError(f"Incomplete SQL statement in migration script: {leftover[:120]}")

    return statements


def _preflight_migration_007(conn: sqlite3.Connection) -> None:
    """Executes comprehensive data integrity pre-flight checks prior to Migration 007 DDL (AR-P3-004).

    Runs inside the same BEGIN IMMEDIATE transaction as DDL and schema_migrations insert.
    All queries use LEFT JOIN so rows orphaned with foreign_keys=OFF cannot silently bypass.
    """
    ver = 7

    # 1. VOUCHER products missing or having malformed voucher_details (or voucher_details on non-VOUCHER)
    rows = conn.execute(
        """
        SELECT p.id, p.product_type, vd.product_id AS detail_pid, vd.region, vd.currency
        FROM products p
        LEFT JOIN voucher_details vd ON vd.product_id = p.id
        WHERE p.product_type = 'VOUCHER'
          AND (
              vd.product_id IS NULL
              OR NOT ((vd.region = 'TR' AND vd.currency = 'TRY') OR (vd.region = 'US' AND vd.currency = 'USD'))
          )
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_voucher_missing_or_malformed_details",
            f"VOUCHER product(s) missing or having invalid voucher_details: product_ids={ids}",
        )

    rows = conn.execute(
        """
        SELECT vd.product_id
        FROM voucher_details vd
        LEFT JOIN products p ON p.id = vd.product_id
        WHERE p.id IS NULL OR p.product_type != 'VOUCHER'
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["product_id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_voucher_details_invalid_product",
            f"voucher_details row(s) point to missing or non-VOUCHER products: product_ids={ids}",
        )

    # 2. FIXED_PRODUCT missing or having malformed fixed_product_details (or details on non-FIXED_PRODUCT)
    rows = conn.execute(
        """
        SELECT p.id, fp.product_id AS detail_pid
        FROM products p
        LEFT JOIN fixed_product_details fp ON fp.product_id = p.id
        WHERE p.product_type = 'FIXED_PRODUCT'
          AND (
              fp.product_id IS NULL
              OR fp.price_rub_kopecks IS NULL
              OR fp.price_rub_kopecks <= 0
              OR fp.delivery_method NOT IN ('CODE', 'OFFICIAL_ACTIVATION', 'MANUAL_REVIEW')
              OR fp.approval_status NOT IN ('DRAFT', 'APPROVED', 'SUSPENDED')
              OR fp.supply_method IS NULL
              OR TRIM(fp.supply_method) = ''
              OR (fp.duration_days IS NOT NULL AND fp.duration_days <= 0)
          )
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_fixed_product_missing_or_malformed_details",
            f"FIXED_PRODUCT product(s) missing or having malformed fixed_product_details: product_ids={ids}",
        )

    rows = conn.execute(
        """
        SELECT fp.product_id
        FROM fixed_product_details fp
        LEFT JOIN products p ON p.id = fp.product_id
        WHERE p.id IS NULL OR p.product_type != 'FIXED_PRODUCT'
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["product_id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_fixed_details_invalid_product",
            f"fixed_product_details row(s) point to missing or non-FIXED_PRODUCT products: product_ids={ids}",
        )

    # 3. voucher_denominations pointing to missing/non-VOUCHER product or mismatched voucher_details
    rows = conn.execute(
        """
        SELECT d.id
        FROM voucher_denominations d
        LEFT JOIN products p ON p.id = d.product_id
        LEFT JOIN voucher_details vd ON vd.product_id = d.product_id
        WHERE p.id IS NULL
           OR p.product_type != 'VOUCHER'
           OR vd.product_id IS NULL
           OR vd.region != d.region
           OR vd.currency != d.currency
           OR NOT ((d.region = 'TR' AND d.currency = 'TRY') OR (d.region = 'US' AND d.currency = 'USD'))
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_voucher_denom_invalid_product_or_details",
            f"voucher_denominations row(s) bound to invalid product or mismatched voucher_details: denom_ids={ids}",
        )

    # 4. Orphaned inventory_items or order_items via LEFT JOIN products
    rows = conn.execute(
        """
        SELECT i.id
        FROM inventory_items i
        LEFT JOIN products p ON p.id = i.product_id
        WHERE p.id IS NULL
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_orphaned_inventory_product",
            f"inventory_items row(s) reference missing products: item_ids={ids}",
        )

    rows = conn.execute(
        """
        SELECT oi.id
        FROM order_items oi
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE p.id IS NULL
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_orphaned_order_item_product",
            f"order_items row(s) reference missing products: order_item_ids={ids}",
        )

    # 5. Mixed orders combining inventory-backed items and MANUAL_REVIEW items (OD-04)
    rows = conn.execute(
        """
        SELECT oi.order_id
        FROM order_items oi
        LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
        GROUP BY oi.order_id
        HAVING SUM(
            CASE
                WHEN oi.product_type_snapshot = 'VOUCHER'
                  OR (oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION'))
                THEN 1 ELSE 0
            END
        ) > 0
           AND SUM(
            CASE
                WHEN oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.delivery_method = 'MANUAL_REVIEW'
                THEN 1 ELSE 0
            END
        ) > 0
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["order_id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_mixed_inventory_and_manual_orders",
            f"Order(s) mix inventory-backed and MANUAL_REVIEW items (OD-04 violation): order_ids={ids}",
        )

    # 6. CANCELLED orders with remaining RESERVED inventory
    rows = conn.execute(
        """
        SELECT DISTINCT o.id AS order_id
        FROM orders o
        JOIN inventory_items i ON i.reserved_order_id = o.id
        WHERE o.status = 'CANCELLED' AND i.status = 'RESERVED'
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["order_id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_cancelled_order_with_reserved_inventory",
            f"CANCELLED order(s) still hold RESERVED inventory: order_ids={ids}",
        )

    # 7. COMPLETED orders two-sided inventory invariant (AR-P3-004)
    rows = conn.execute(
        """
        SELECT o.id AS order_id
        FROM orders o
        WHERE o.status = 'COMPLETED'
          AND (
              EXISTS (SELECT 1 FROM inventory_items i WHERE i.reserved_order_id = o.id AND i.status = 'RESERVED')
              OR NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.order_id = o.id)
              OR EXISTS (
                  SELECT 1
                  FROM order_items oi
                  LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
                  WHERE oi.order_id = o.id
                    AND (
                        oi.product_type_snapshot = 'VOUCHER'
                        OR (oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION'))
                    )
                  GROUP BY oi.product_id, oi.denomination_id
                  HAVING SUM(oi.quantity) != (
                      SELECT COUNT(*)
                      FROM inventory_items i
                      WHERE i.reserved_order_id = o.id
                        AND i.status = 'SOLD'
                        AND i.product_id = oi.product_id
                        AND ((i.denomination_id IS NULL AND oi.denomination_id IS NULL) OR i.denomination_id = oi.denomination_id)
                  )
              )
              OR EXISTS (
                  SELECT 1
                  FROM inventory_items i
                  WHERE i.reserved_order_id = o.id
                    AND i.status = 'SOLD'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM order_items oi
                        LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
                        WHERE oi.order_id = o.id
                          AND oi.product_id = i.product_id
                          AND ((oi.denomination_id IS NULL AND i.denomination_id IS NULL) OR oi.denomination_id = i.denomination_id)
                          AND (
                              oi.product_type_snapshot = 'VOUCHER'
                              OR (oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION'))
                          )
                    )
              )
          )
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["order_id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_completed_order_inventory_invariant",
            f"COMPLETED order(s) violate inventory fulfillment invariants: order_ids={ids}",
        )

    # 8. Multiple ACCEPTED receipts for the same order (AR-P3-005)
    rows = conn.execute(
        """
        SELECT order_id
        FROM payment_receipts
        WHERE review_status = 'ACCEPTED'
        GROUP BY order_id
        HAVING COUNT(*) > 1
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["order_id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_multiple_accepted_receipts",
            f"Order(s) have multiple ACCEPTED payment receipts: order_ids={ids}",
        )

    # 9. QUARANTINED/DISABLED items holding broken or invalid non-NULL reserved_order_id (AR-P3-007)
    rows = conn.execute(
        """
        SELECT i.id
        FROM inventory_items i
        LEFT JOIN orders o ON o.id = i.reserved_order_id
        WHERE i.status IN ('QUARANTINED', 'DISABLED')
          AND i.reserved_order_id IS NOT NULL
          AND (o.id IS NULL OR o.status NOT IN ('CANCELLED', 'REFUND_REVIEW'))
        LIMIT 5
        """
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        raise MigrationPreflightError(
            ver,
            "chk_007_quarantined_or_disabled_invalid_order_link",
            f"QUARANTINED/DISABLED item(s) hold invalid active/missing order link: item_ids={ids}",
        )


MIGRATION_PREFLIGHT_CHECKS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    7: _preflight_migration_007,
}


def run_migrations(
    conn: sqlite3.Connection,
    migrations_dir: Path = MIGRATIONS_DIR,
    preflight_hooks: Optional[Mapping[int, Callable[[sqlite3.Connection], None]]] = None,
) -> List[int]:
    """Applies all pending migrations in order inside individual strictly atomic transactions.

    Guarantees:
    1. Rejects execution immediately if `conn.in_transaction` is already True (never commits caller tx).
    2. Verifies uniqueness of migration version numbers and SHA-256 checksums for previously applied
       migrations that have a non-NULL stored checksum (legacy NULL-checksum rows are tolerated).
    3. Acquires a `BEGIN IMMEDIATE` write lock before checking whether each migration is already
       applied, serializing concurrent migration runners safely.
    4. Runs any registered pre-flight hook INSIDE the same `BEGIN IMMEDIATE` transaction prior to DDL.
    5. Executes all statements of each migration and inserts the `schema_migrations` record inside
       the same `BEGIN IMMEDIATE` transaction via quote/comment-aware `_split_sql_statements()`.
    6. If pre-flight, DDL, checksum verification, or version recording fails (including BaseException),
       rolls back completely: neither schema modifications nor the version record persist.

    Args:
        conn: Target sqlite3.Connection.
        migrations_dir: Directory containing migration files.
        preflight_hooks: Optional mapping of migration version -> pre-flight validator callable.

    Returns:
        List of newly applied migration versions.
    """
    if conn.in_transaction:
        raise RuntimeError(
            "Cannot execute run_migrations: connection already has an active uncommitted transaction."
        )

    hooks = MIGRATION_PREFLIGHT_CHECKS if preflight_hooks is None else preflight_hooks
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

                preflight_fn = hooks.get(version)
                if preflight_fn is not None:
                    preflight_fn(conn)

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
