"""Unit of Work transaction coordinator with snapshot-isolated read sessions (SG-P2-004, AR-P3-003)."""

from contextlib import contextmanager
from datetime import datetime
import sqlite3
from typing import Callable, Iterator, Optional

from src.db.connection import get_connection, transaction
from src.db.models import DatabaseError
from src.db.repositories import (
    AuditRepository,
    CatalogRepository,
    DeliveryRepository,
    IdempotencyRepository,
    InventoryRepository,
    OrderRepository,
    ReceiptRepository,
    UserRepository,
)


class ReadSession:
    """Strictly read-only repository facade bound to a single WAL-snapshot DEFERRED transaction.

    Exposes only read/query methods on the shared connection; write methods are
    intentionally omitted and blocked at both Python transaction and SQLite query_only layers.
    """

    def __init__(self, conn: sqlite3.Connection, schema_version: int) -> None:
        self.conn = conn
        self.schema_version = schema_version
        self._catalog = CatalogRepository(conn)
        self._users = UserRepository(conn)
        self._inventory = InventoryRepository(conn)
        self._orders = OrderRepository(conn)
        self._receipts = ReceiptRepository(conn)
        self._audit = AuditRepository(conn)
        self._deliveries = DeliveryRepository(conn)
        self._idempotency = IdempotencyRepository(conn)

    # Catalog reads
    def get_product(self, product_id: int):
        return self._catalog.get_product(product_id)

    def get_denomination(self, denomination_id: int):
        return self._catalog.get_denomination(denomination_id)

    def get_active_denominations(self, region: str, currency: str):
        return self._catalog.get_active_denominations(region, currency)

    def get_approved_active_denominations(self, region: str, currency: str):
        return self._catalog.get_approved_active_denominations(region, currency)

    # User reads
    def get_user(self, user_id: int):
        return self._users.get_user(user_id)

    # Inventory reads
    def get_inventory_item(self, item_id: int):
        return self._inventory.get_item(item_id)

    def get_revealed_code(
        self,
        item_id: int,
        *,
        expected_order_id: Optional[int] = None,
        expected_user_id: Optional[int] = None,
    ) -> str:
        return self._inventory.get_revealed_code(
            item_id,
            expected_order_id=expected_order_id,
            expected_user_id=expected_user_id,
        )

    # Order reads
    def get_order(self, order_id: int):
        return self._orders.get_order(order_id)

    def get_order_items(self, order_id: int):
        return self._orders.get_order_items(order_id)

    def count_open_pending_orders(self, user_id: int) -> int:
        return self._orders.count_open_pending_orders(user_id)

    def list_expired_pending_orders(
        self,
        ttl_seconds: Optional[int] = None,
        limit: int = 50,
        *,
        now_utc: Optional[datetime] = None,
    ):
        return self._orders.list_expired_pending_orders(
            ttl_seconds=ttl_seconds,
            limit=limit,
            now_utc=now_utc,
        )

    # Receipt reads
    def get_receipt(self, receipt_id: int):
        return self._receipts.get_receipt(receipt_id)

    def list_receipts_for_order(self, order_id: int):
        return self._receipts.list_receipts_for_order(order_id)

    # Delivery reads
    def get_delivery(self, delivery_id: int):
        return self._deliveries.get_delivery(delivery_id)

    def list_deliveries_for_order(self, order_id: int):
        return self._deliveries.list_deliveries_for_order(order_id)

    # Audit & Idempotency reads
    def list_audit_events(self, **kwargs):
        return self._audit.list_events(**kwargs)

    def get_idempotency_record(self, scope: str, actor_key: str, idempotency_key: str):
        return self._idempotency.get_record(scope, actor_key, idempotency_key)


class WriteSession:
    """Transactional write session binding all repositories to a single SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.catalog = CatalogRepository(conn)
        self.users = UserRepository(conn)
        self.inventory = InventoryRepository(conn)
        self.orders = OrderRepository(conn)
        self.receipts = ReceiptRepository(conn)
        self.audit = AuditRepository(conn)
        self.deliveries = DeliveryRepository(conn)
        self.idempotency = IdempotencyRepository(conn)


class UnitOfWork:
    """Explicit Unit of Work managing connection lifecycle and transaction boundaries."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        owns_connection: bool = True,
        borrowed_conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._owns_connection = owns_connection
        self._borrowed_conn = borrowed_conn

    @classmethod
    def from_db_path(
        cls,
        db_path: str,
        *,
        enable_wal: bool = True,
        timeout_seconds: float = 5.0,
    ) -> "UnitOfWork":
        return cls(
            lambda: get_connection(
                db_path=db_path,
                enable_wal=enable_wal,
                timeout_seconds=timeout_seconds,
            ),
            owns_connection=True,
        )

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> "UnitOfWork":
        return cls(lambda: conn, owns_connection=False, borrowed_conn=conn)

    @contextmanager
    def read(self) -> Iterator[ReadSession]:
        """Opens a strictly read-only DEFERRED transaction with an explicit WAL snapshot (AR-P3-003).

        Guarantees:
        1. Sets PRAGMA query_only = ON prior to starting BEGIN DEFERRED.
        2. Pins WAL read snapshot via a real table read (`schema_migrations`), NOT `SELECT 1`.
        3. Raises clear DatabaseError if `schema_migrations` is missing or empty.
        4. Releases snapshot via rollback on exit.
        5. Always restores previous `PRAGMA query_only` in outer `finally` and closes owned connections.
        """
        conn = self._borrowed_conn if not self._owns_connection else self._connection_factory()
        assert conn is not None
        if conn.in_transaction:
            if self._owns_connection:
                conn.close()
            raise RuntimeError(
                "Cannot start UnitOfWork.read(): connection already has an active uncommitted transaction."
            )

        prev_query_only = 0
        query_only_modified = False
        try:
            qo_row = conn.execute("PRAGMA query_only;").fetchone()
            prev_query_only = int(qo_row[0]) if qo_row is not None else 0
            conn.execute("PRAGMA query_only = ON;")
            query_only_modified = True

            with transaction(conn, mode="DEFERRED", read_only=True):
                try:
                    ver_row = conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1;"
                    ).fetchone()
                except sqlite3.OperationalError as e:
                    if "no such table" in str(e).lower():
                        raise DatabaseError(
                            "Database schema is not initialized: schema_migrations table is missing."
                        ) from e
                    raise

                if ver_row is None:
                    raise DatabaseError(
                        "Database schema is not initialized: no migrations have been applied."
                    )

                yield ReadSession(conn, schema_version=int(ver_row["version"]))
        finally:
            if query_only_modified:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                except Exception:
                    pass
                try:
                    conn.execute(f"PRAGMA query_only = {1 if prev_query_only else 0};")
                except Exception:
                    pass
            if self._owns_connection:
                conn.close()

    @contextmanager
    def write(self, mode: str = "IMMEDIATE") -> Iterator[WriteSession]:
        """Opens a write transaction (IMMEDIATE or EXCLUSIVE) across all repositories."""
        norm_mode = mode.strip().upper() if isinstance(mode, str) else ""
        if norm_mode == "DEFERRED":
            raise ValueError("UnitOfWork.write() requires mode='IMMEDIATE' or 'EXCLUSIVE' (DEFERRED forbidden).")

        conn = self._borrowed_conn if not self._owns_connection else self._connection_factory()
        assert conn is not None
        try:
            with transaction(conn, mode=norm_mode, read_only=False):
                yield WriteSession(conn)
        finally:
            if self._owns_connection:
                conn.close()
