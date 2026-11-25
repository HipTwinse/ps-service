"""Regression tests for Phase 2 Security Gate findings SG-P2-001, SG-P2-002, and SG-P2-003."""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.db.connection import create_connection
from src.db.migrations import get_applied_migrations, run_migrations
from src.db.models import (
    ApprovalStatus,
    DeliveryMethod,
    InsufficientInventoryError,
    InvalidStateTransitionError,
    InventoryStatus,
    InventoryType,
    OrderStatus,
    ProductStatus,
    ValidationError,
)
from src.db.repositories import (
    CatalogRepository,
    InventoryRepository,
    OrderRepository,
    UserRepository,
)


class TestPhase2SecurityGateRemediation(unittest.TestCase):
    """Covers SG-P2-001 (denomination consistency), SG-P2-002 (no orphaned inventory), SG-P2-003 (catalog pricing)."""

    def setUp(self):
        os.environ["APP_ENV"] = "test"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sg_remediation.db")
        self.conn = create_connection(self.db_path)
        self.applied = run_migrations(self.conn)

        self.user_repo = UserRepository(self.conn)
        self.cat_repo = CatalogRepository(self.conn)
        self.inv_repo = InventoryRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)

        self.user, _ = self.user_repo.get_or_create_user("sg_u1", "sg_user_1")
        self.user_2, _ = self.user_repo.get_or_create_user("sg_u2", "sg_user_2")

        self.voucher_prod = self.cat_repo.create_voucher_product(
            display_name="PSN TR",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.denom_100 = self.cat_repo.add_voucher_denomination(
            product_id=self.voucher_prod.id,
            region="TR",
            currency="TRY",
            face_value_minor=10000,
            cost_rub_kopecks=30000,
        )
        self.denom_1000 = self.cat_repo.add_voucher_denomination(
            product_id=self.voucher_prod.id,
            region="TR",
            currency="TRY",
            face_value_minor=100000,
            cost_rub_kopecks=300000,
        )

        self.other_voucher = self.cat_repo.create_voucher_product(
            display_name="PSN US",
            region="US",
            currency="USD",
            status=ProductStatus.APPROVED,
        )
        self.other_denom = self.cat_repo.add_voucher_denomination(
            product_id=self.other_voucher.id,
            region="US",
            currency="USD",
            face_value_minor=1000,
            cost_rub_kopecks=95000,
        )

        self.fixed_prod = self.cat_repo.create_fixed_product(
            display_name="PS Plus Deluxe",
            price_rub_kopecks=50000,
            delivery_method=DeliveryMethod.OFFICIAL_ACTIVATION,
            supply_method="MANUAL_POOL",
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.APPROVED,
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _copy_migrations_up_to(self, max_version: int, tag: str) -> Path:
        migrations_root = Path(__file__).resolve().parent.parent / "src" / "migrations"
        target_dir = Path(self.temp_dir.name) / f"migs_up_to_{max_version}_{tag}"
        target_dir.mkdir(parents=True, exist_ok=True)
        for p in sorted(migrations_root.glob("*.sql")):
            ver = int(p.name.split("_", 1)[0])
            if ver <= max_version:
                (target_dir / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        return target_dir

    # ------------------------------------------------------------------
    # SG-P2-001: Denomination consistency
    # ------------------------------------------------------------------

    def test_sg_p2_001_exploit_100_try_cannot_reserve_1000_try(self):
        """Reproduces the original SG-P2-001 exploit: 100 TRY order must NOT reserve/reveal 1000 TRY inventory."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["CODE-100-TRY"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        self.inv_repo.import_items(
            self.voucher_prod.id, ["CODE-1000-TRY"], InventoryType.VOUCHER_CODE, self.denom_1000.id
        )

        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )

        order_items = self.order_repo.get_order_items(order.id)
        self.assertEqual(len(order_items), 1)
        self.assertEqual(order_items[0].denomination_id, self.denom_100.id)

        # Exploit attempt: reserve denom_1000 for an order that bought denom_100
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(
                order.id, self.voucher_prod.id, 1, self.denom_1000.id
            )

        # Verify 1000 TRY code remains AVAILABLE and unlinked
        rows = self.conn.execute(
            "SELECT denomination_id, status, reserved_order_id FROM inventory_items ORDER BY id ASC"
        ).fetchall()
        self.assertEqual(
            [(r["denomination_id"], r["status"], r["reserved_order_id"]) for r in rows],
            [
                (self.denom_100.id, "AVAILABLE", None),
                (self.denom_1000.id, "AVAILABLE", None),
            ],
        )

    def test_sg_p2_001_multi_denomination_order_independent_caps_and_full_lifecycle(self):
        """Single order with multiple denominations of the same VOUCHER product enforces per-denomination caps."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["M100-1", "M100-2"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        self.inv_repo.import_items(
            self.voucher_prod.id, ["M1000-1"], InventoryType.VOUCHER_CODE, self.denom_1000.id
        )

        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=330000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                },
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_1000.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 300000,
                },
            ],
        )

        # Cannot reserve 2 units of denom_100 even though total ordered product_id qty is 2
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 2, self.denom_100.id)

        res_100 = self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 1, self.denom_100.id)
        # Second reservation of denom_100 must fail
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 1, self.denom_100.id)

        self.order_repo.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order.id, OrderStatus.PAID)

        # Incomplete multi-denomination reservation (missing denom_1000) cannot finalize
        with self.assertRaises(ValidationError):
            self.inv_repo.finalize_order_sale(order.id)

        res_1000 = self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 1, self.denom_1000.id)
        sold = self.inv_repo.finalize_order_sale(order.id)
        self.assertEqual(sold, 2)

        self.assertEqual(
            self.inv_repo.get_revealed_code(res_100[0], expected_order_id=order.id, expected_user_id=self.user.id),
            "M100-1",
        )
        self.assertEqual(
            self.inv_repo.get_revealed_code(res_1000[0], expected_order_id=order.id, expected_user_id=self.user.id),
            "M1000-1",
        )

    def test_sg_p2_001_create_order_denomination_validation_and_db_triggers(self):
        """Enforces denomination rules in Python and via direct SQL INSERT/UPDATE triggers."""
        # 1. VOUCHER without denomination_id rejected
        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=30000,
                currency="RUB",
                items=[
                    {
                        "product_id": self.voucher_prod.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 1,
                        "unit_price_minor": 30000,
                    }
                ],
            )

        # 2. VOUCHER with denomination from another product rejected
        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=95000,
                currency="RUB",
                items=[
                    {
                        "product_id": self.voucher_prod.id,
                        "denomination_id": self.other_denom.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 1,
                        "unit_price_minor": 95000,
                    }
                ],
            )

        # 3. FIXED_PRODUCT with denomination_id rejected
        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=50000,
                currency="RUB",
                items=[
                    {
                        "product_id": self.fixed_prod.id,
                        "denomination_id": self.denom_100.id,
                        "product_type_snapshot": "FIXED_PRODUCT",
                        "quantity": 1,
                        "unit_price_minor": 50000,
                    }
                ],
            )

        # 4. Direct SQL bypass checks on order_items and inventory_items
        valid_order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )

        # Direct SQL INSERT: VOUCHER order_item with NULL denomination_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency
                ) VALUES (?, ?, NULL, 'VOUCHER', 1, 30000, 'RUB')
                """,
                (valid_order.id, self.voucher_prod.id),
            )

        # Direct SQL INSERT: VOUCHER order_item with foreign denomination_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency
                ) VALUES (?, ?, ?, 'VOUCHER', 1, 30000, 'RUB')
                """,
                (valid_order.id, self.voucher_prod.id, self.other_denom.id),
            )

        # Direct SQL INSERT: FIXED_PRODUCT order_item with non-NULL denomination_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency
                ) VALUES (?, ?, ?, 'FIXED_PRODUCT', 1, 50000, 'RUB')
                """,
                (valid_order.id, self.fixed_prod.id, self.denom_100.id),
            )

        # Direct SQL UPDATE: swap order_items.denomination_id to NULL or another product's denomination
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE order_items SET denomination_id = NULL WHERE order_id = ?", (valid_order.id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE order_items SET denomination_id = ? WHERE order_id = ?",
                (self.other_denom.id, valid_order.id),
            )
        if self.conn.in_transaction:
            self.conn.rollback()

        # Direct SQL UPDATE on inventory_items: cannot link RESERVED/SOLD to wrong denomination
        self.inv_repo.import_items(
            self.voucher_prod.id, ["SQL-100"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        self.inv_repo.import_items(
            self.voucher_prod.id, ["SQL-1000"], InventoryType.VOUCHER_CODE, self.denom_1000.id
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE inventory_items
                SET status = 'RESERVED', reserved_order_id = ?
                WHERE denomination_id = ?
                """,
                (valid_order.id, self.denom_1000.id),
            )
        if self.conn.in_transaction:
            self.conn.rollback()

    def test_sg_p2_001_migration_004_legacy_preflight_and_clean_upgrade(self):
        """Migration 004 safely aborts on ambiguous legacy VOUCHER order_items and succeeds on FIXED_PRODUCT legacy DB."""
        # Case A: legacy DB at 003 with VOUCHER order_item -> aborts 004 without mutating data
        db_a = os.path.join(self.temp_dir.name, "legacy_004_voucher.db")
        c = create_connection(db_a)
        try:
            migs_003 = self._copy_migrations_up_to(3, "m004_abort")
            self.assertEqual(run_migrations(c, migrations_dir=migs_003), [1, 2, 3])
            u_id = c.execute(
                "INSERT INTO users (external_id, username, status) VALUES ('lu1', 'lu1', 'ACTIVE')"
            ).lastrowid
            p_id = c.execute(
                "INSERT INTO products (product_type, display_name, status) VALUES ('VOUCHER', 'Legacy V', 'APPROVED')"
            ).lastrowid
            o_id = c.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PAID', 30000, 'RUB')",
                (u_id,),
            ).lastrowid
            c.execute(
                """
                INSERT INTO order_items (order_id, product_id, product_type_snapshot, quantity, unit_price_minor, currency)
                VALUES (?, ?, 'VOUCHER', 1, 30000, 'RUB')
                """,
                (o_id, p_id),
            )
            c.commit()

            before_items = [tuple(r) for r in c.execute("SELECT * FROM order_items ORDER BY id").fetchall()]
            with self.assertRaises(RuntimeError):
                run_migrations(c)

            self.assertEqual(get_applied_migrations(c), [1, 2, 3])
            after_items = [tuple(r) for r in c.execute("SELECT * FROM order_items ORDER BY id").fetchall()]
            self.assertEqual(after_items, before_items)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(order_items)").fetchall()}
            self.assertNotIn("denomination_id", cols)
        finally:
            c.close()

        # Case B: legacy DB at 003 with FIXED_PRODUCT order_item -> migrates cleanly through 004 & 005
        db_b = os.path.join(self.temp_dir.name, "legacy_004_fixed.db")
        c2 = create_connection(db_b)
        try:
            migs_003_b = self._copy_migrations_up_to(3, "m004_ok")
            self.assertEqual(run_migrations(c2, migrations_dir=migs_003_b), [1, 2, 3])
            u_id = c2.execute(
                "INSERT INTO users (external_id, username, status) VALUES ('lu2', 'lu2', 'ACTIVE')"
            ).lastrowid
            p_id = c2.execute(
                "INSERT INTO products (product_type, display_name, status) VALUES ('FIXED_PRODUCT', 'Legacy F', 'APPROVED')"
            ).lastrowid
            c2.execute(
                """
                INSERT INTO fixed_product_details (product_id, price_rub_kopecks, delivery_method, supply_method, approval_status)
                VALUES (?, 50000, 'CODE', 'POOL', 'APPROVED')
                """,
                (p_id,),
            )
            o_id = c2.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PAID', 50000, 'RUB')",
                (u_id,),
            ).lastrowid
            c2.execute(
                """
                INSERT INTO order_items (order_id, product_id, product_type_snapshot, quantity, unit_price_minor, currency)
                VALUES (?, ?, 'FIXED_PRODUCT', 1, 50000, 'RUB')
                """,
                (o_id, p_id),
            )
            c2.commit()

            self.assertEqual(run_migrations(c2), [4, 5, 6, 7])
            self.assertEqual(get_applied_migrations(c2), [1, 2, 3, 4, 5, 6, 7])
            row = c2.execute("SELECT product_id, denomination_id FROM order_items WHERE order_id = ?", (o_id,)).fetchone()
            self.assertEqual(row["product_id"], p_id)
            self.assertIsNone(row["denomination_id"])
        finally:
            c2.close()

    def test_sg_p2_001_concurrent_reservation_race_for_last_unit(self):
        """Two concurrent orders racing for the last AVAILABLE unit of a denomination: exactly 1 succeeds."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["RACE-LAST-1"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        order_1 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )
        order_2 = self.order_repo.create_order(
            user_id=self.user_2.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )

        barrier = threading.Barrier(2)
        successes = []
        insufficient = []
        unexpected = []

        def racer(target_order_id: int):
            c = create_connection(self.db_path, timeout_seconds=10.0)
            repo = InventoryRepository(c)
            try:
                barrier.wait(timeout=5.0)
                ids = repo.reserve_items_for_order(
                    target_order_id, self.voucher_prod.id, 1, self.denom_100.id
                )
                successes.append((target_order_id, ids))
            except InsufficientInventoryError as exc:
                insufficient.append((target_order_id, exc))
            except BaseException as exc:
                unexpected.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=racer, args=(order_1.id,))
        t2 = threading.Thread(target=racer, args=(order_2.id,))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())

        self.assertEqual(unexpected, [])
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(insufficient), 1)

    # ------------------------------------------------------------------
    # SG-P2-002: Orphaned inventory prevention
    # ------------------------------------------------------------------

    def test_sg_p2_002_cannot_delete_order_with_reserved_or_sold_inventory(self):
        """Deleting an order with RESERVED or SOLD inventory is blocked at DB level."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["DEL-GUARD-1"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )
        res_ids = self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 1, self.denom_100.id)

        # 1. Cannot delete order while inventory is RESERVED
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM orders WHERE id = ?", (order.id,))
        if self.conn.in_transaction:
            self.conn.rollback()

        row = self.conn.execute(
            "SELECT status, reserved_order_id FROM inventory_items WHERE id = ?", (res_ids[0],)
        ).fetchone()
        self.assertEqual(row["status"], "RESERVED")
        self.assertEqual(row["reserved_order_id"], order.id)

        # 2. Finalize sale -> SOLD; still cannot delete order
        self.order_repo.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order.id, OrderStatus.PAID)
        self.inv_repo.finalize_order_sale(order.id)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM orders WHERE id = ?", (order.id,))
        if self.conn.in_transaction:
            self.conn.rollback()

        row_sold = self.conn.execute(
            "SELECT status, reserved_order_id FROM inventory_items WHERE id = ?", (res_ids[0],)
        ).fetchone()
        self.assertEqual(row_sold["status"], "SOLD")
        self.assertEqual(row_sold["reserved_order_id"], order.id)

    def test_sg_p2_002_direct_sql_cannot_create_orphaned_reserved_or_sold(self):
        """CHECK constraints and triggers reject RESERVED/SOLD with NULL order and AVAILABLE with non-NULL order."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["ORPHAN-SQL-1"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )

        # Direct INSERT RESERVED/SOLD with reserved_order_id = NULL
        for st in ("RESERVED", "SOLD"):
            with self.subTest(insert_status=st):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        """
                        INSERT INTO inventory_items (
                            product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id
                        ) VALUES (?, ?, 'VOUCHER_CODE', ?, ?, ?, NULL)
                        """,
                        (self.voucher_prod.id, self.denom_100.id, b"enc", f"fp_null_{st}", st),
                    )

        # Direct UPDATE to RESERVED/SOLD with reserved_order_id = NULL
        for st in ("RESERVED", "SOLD"):
            with self.subTest(update_status=st):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(
                        "UPDATE inventory_items SET status = ?, reserved_order_id = NULL WHERE fingerprint IS NOT NULL",
                        (st,),
                    )

        # Direct UPDATE to AVAILABLE with non-NULL reserved_order_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE inventory_items SET status = 'AVAILABLE', reserved_order_id = ?",
                (order.id,),
            )
        if self.conn.in_transaction:
            self.conn.rollback()

        # Direct DELETE of order_items while RESERVED inventory references it
        self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 1, self.denom_100.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM order_items WHERE order_id = ?", (order.id,))
        if self.conn.in_transaction:
            self.conn.rollback()

    def test_sg_p2_002_cancelled_order_release_lifecycle_and_safe_cleanup(self):
        """Cancelled order releases inventory cleanly back to AVAILABLE before any deletion."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["REL-LIFE-1"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )
        res_ids = self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 1, self.denom_100.id)
        # SG-P2-004: cancelling while RESERVED inventory remains is blocked until reservations are released
        with self.assertRaises(InvalidStateTransitionError):
            self.order_repo.transition_order_status(order.id, OrderStatus.CANCELLED)

        released = self.inv_repo.release_order_reservations(order.id)
        self.assertEqual(released, 1)
        self.order_repo.transition_order_status(order.id, OrderStatus.CANCELLED)

        row = self.conn.execute(
            "SELECT status, reserved_order_id FROM inventory_items WHERE id = ?", (res_ids[0],)
        ).fetchone()
        self.assertEqual(row["status"], InventoryStatus.AVAILABLE.value)
        self.assertIsNone(row["reserved_order_id"])

        # After reservations are released, order without receipts can be deleted without orphaning inventory
        self.conn.execute("DELETE FROM orders WHERE id = ?", (order.id,))
        self.conn.commit()
        row_after = self.conn.execute(
            "SELECT status, reserved_order_id FROM inventory_items WHERE id = ?", (res_ids[0],)
        ).fetchone()
        self.assertEqual(row_after["status"], "AVAILABLE")
        self.assertIsNone(row_after["reserved_order_id"])

    def test_sg_p2_002_migration_005_legacy_preflight_and_clean_upgrade(self):
        """Migration 005 aborts on orphaned RESERVED inventory and upgrades cleanly once resolved."""
        db_005 = os.path.join(self.temp_dir.name, "legacy_005_orphan.db")
        c = create_connection(db_005)
        try:
            migs_004 = self._copy_migrations_up_to(4, "m005_abort")
            self.assertEqual(run_migrations(c, migrations_dir=migs_004), [1, 2, 3, 4])

            p_id = c.execute(
                "INSERT INTO products (product_type, display_name, status) VALUES ('FIXED_PRODUCT', 'Leg F', 'APPROVED')"
            ).lastrowid
            c.execute(
                """
                INSERT INTO fixed_product_details (product_id, price_rub_kopecks, delivery_method, supply_method, approval_status)
                VALUES (?, 50000, 'CODE', 'POOL', 'APPROVED')
                """,
                (p_id,),
            )
            c.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id
                ) VALUES (?, NULL, 'FIXED_CODE', ?, 'orphan_fp_1', 'RESERVED', NULL)
                """,
                (p_id, b"enc_bytes"),
            )
            c.commit()

            with self.assertRaises(RuntimeError):
                run_migrations(c)
            self.assertEqual(get_applied_migrations(c), [1, 2, 3, 4])

            # Resolve orphaned legacy state -> 005, 006, 007 apply cleanly
            c.execute("UPDATE inventory_items SET status = 'AVAILABLE' WHERE fingerprint = 'orphan_fp_1'")
            c.commit()
            self.assertEqual(run_migrations(c), [5, 6, 7])
            self.assertEqual(get_applied_migrations(c), [1, 2, 3, 4, 5, 6, 7])
        finally:
            c.close()

    # ------------------------------------------------------------------
    # SG-P2-003: Catalog price validation & immutable price snapshots
    # ------------------------------------------------------------------

    def test_sg_p2_003_rejects_underpriced_and_overpriced_items(self):
        """create_order rejects prices lower or higher than current catalog prices."""
        # FIXED_PRODUCT catalog price = 50000
        for bad_fixed_price in (1, 49999, 50001, 500000):
            with self.subTest(bad_fixed_price=bad_fixed_price):
                with self.assertRaises(ValidationError):
                    self.order_repo.create_order(
                        user_id=self.user.id,
                        total_amount_minor=bad_fixed_price,
                        currency="RUB",
                        items=[
                            {
                                "product_id": self.fixed_prod.id,
                                "product_type_snapshot": "FIXED_PRODUCT",
                                "quantity": 1,
                                "unit_price_minor": bad_fixed_price,
                            }
                        ],
                    )

        # VOUCHER denom_100 catalog price = 30000; denom_1000 = 300000
        for denom_id, bad_price in (
            (self.denom_100.id, 1),
            (self.denom_100.id, 29999),
            (self.denom_100.id, 30001),
            (self.denom_100.id, 300000),
            (self.denom_1000.id, 30000),
            (self.denom_1000.id, 300001),
        ):
            with self.subTest(denom_id=denom_id, bad_price=bad_price):
                with self.assertRaises(ValidationError):
                    self.order_repo.create_order(
                        user_id=self.user.id,
                        total_amount_minor=bad_price,
                        currency="RUB",
                        items=[
                            {
                                "product_id": self.voucher_prod.id,
                                "denomination_id": denom_id,
                                "product_type_snapshot": "VOUCHER",
                                "quantity": 1,
                                "unit_price_minor": bad_price,
                            }
                        ],
                    )

    def test_sg_p2_003_rejects_invalid_types_and_currencies(self):
        """Rejects bool/float/str/negative values and non-RUB order currencies."""
        for bad_curr in ("TRY", "USD", "EUR", "", "  ", 123, None):
            with self.subTest(bad_curr=bad_curr):
                with self.assertRaises(ValidationError):
                    self.order_repo.create_order(
                        user_id=self.user.id,
                        total_amount_minor=50000,
                        currency=bad_curr,
                        items=[
                            {
                                "product_id": self.fixed_prod.id,
                                "product_type_snapshot": "FIXED_PRODUCT",
                                "quantity": 1,
                                "unit_price_minor": 50000,
                            }
                        ],
                    )

        for bad_denom in (True, False, 1.5, "1", 0, -1):
            with self.subTest(bad_denom=bad_denom):
                with self.assertRaises(ValidationError):
                    self.order_repo.create_order(
                        user_id=self.user.id,
                        total_amount_minor=30000,
                        currency="RUB",
                        items=[
                            {
                                "product_id": self.voucher_prod.id,
                                "denomination_id": bad_denom,
                                "product_type_snapshot": "VOUCHER",
                                "quantity": 1,
                                "unit_price_minor": 30000,
                            }
                        ],
                    )

    def test_sg_p2_003_catalog_price_change_preserves_existing_order_snapshots(self):
        """Catalog price changes affect new orders only and never mutate or break existing orders."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["SNAP-V1"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        self.inv_repo.import_items(
            self.fixed_prod.id, ["SNAP-F1"], InventoryType.ACTIVATION_DATA
        )

        old_order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=80000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                },
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 50000,
                },
            ],
        )

        # Update catalog prices after order creation
        self.conn.execute(
            "UPDATE fixed_product_details SET price_rub_kopecks = 75000 WHERE product_id = ?",
            (self.fixed_prod.id,),
        )
        self.conn.execute(
            "UPDATE voucher_denominations SET cost_rub_kopecks = 45000 WHERE id = ?",
            (self.denom_100.id,),
        )
        self.conn.commit()

        # Existing order snapshots remain unchanged and full fulfillment works
        items = self.order_repo.get_order_items(old_order.id)
        self.assertEqual([i.unit_price_minor for i in items], [30000, 50000])

        res_v = self.inv_repo.reserve_items_for_order(old_order.id, self.voucher_prod.id, 1, self.denom_100.id)
        res_f = self.inv_repo.reserve_items_for_order(old_order.id, self.fixed_prod.id, 1)
        self.order_repo.transition_order_status(old_order.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(old_order.id, OrderStatus.PAID)
        self.assertEqual(self.inv_repo.finalize_order_sale(old_order.id), 2)
        self.assertEqual(
            self.inv_repo.get_revealed_code(res_v[0], expected_order_id=old_order.id, expected_user_id=self.user.id),
            "SNAP-V1",
        )
        self.assertEqual(
            self.inv_repo.get_revealed_code(res_f[0], expected_order_id=old_order.id, expected_user_id=self.user.id),
            "SNAP-F1",
        )

        # New order with old price 30000 must fail; new price 45000 must succeed
        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=30000,
                currency="RUB",
                items=[
                    {
                        "product_id": self.voucher_prod.id,
                        "denomination_id": self.denom_100.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 1,
                        "unit_price_minor": 30000,
                    }
                ],
            )
        new_order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=45000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 45000,
                }
            ],
        )
        self.assertEqual(new_order.total_amount_minor, 45000)

    # ------------------------------------------------------------------
    # SG-P2-001-FOLLOWUP: Direct SQL UPDATE & product_type guards
    # ------------------------------------------------------------------

    def _snapshot_order_and_inventory(self, conn: sqlite3.Connection):
        oi = [
            tuple(r)
            for r in conn.execute(
                "SELECT id, order_id, product_id, denomination_id, product_type_snapshot, quantity "
                "FROM order_items ORDER BY id ASC"
            ).fetchall()
        ]
        inv = [
            tuple(r)
            for r in conn.execute(
                "SELECT id, product_id, denomination_id, status, reserved_order_id "
                "FROM inventory_items ORDER BY id ASC"
            ).fetchall()
        ]
        prods = [
            tuple(r)
            for r in conn.execute(
                "SELECT id, product_type, status FROM products ORDER BY id ASC"
            ).fetchall()
        ]
        return oi, inv, prods

    def _assert_sql_update_blocked(self, sql: str, params=(), conn: sqlite3.Connection = None):
        target_conn = conn or self.conn
        before = self._snapshot_order_and_inventory(target_conn)
        with self.assertRaises(sqlite3.IntegrityError):
            target_conn.execute(sql, params)
        if target_conn.in_transaction:
            target_conn.rollback()
        self.assertEqual(self._snapshot_order_and_inventory(target_conn), before)

    def test_sg_p2_001_followup_blocks_order_items_rebind_on_reserved_and_sold(self):
        """Scenarios 1-4 & 12: Direct SQL UPDATE cannot change denomination_id, product_id, order_id, or combined fields after RESERVED/SOLD."""
        self.inv_repo.import_items(
            self.voucher_prod.id, ["FUP-100-A"], InventoryType.VOUCHER_CODE, self.denom_100.id
        )
        self.inv_repo.import_items(
            self.voucher_prod.id, ["FUP-1000-A"], InventoryType.VOUCHER_CODE, self.denom_1000.id
        )
        self.inv_repo.import_items(
            self.other_voucher.id, ["FUP-US-A"], InventoryType.VOUCHER_CODE, self.other_denom.id
        )

        order_1 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )
        order_2 = self.order_repo.create_order(
            user_id=self.user_2.id,
            total_amount_minor=30000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 30000,
                }
            ],
        )
        oi_id = self.order_repo.get_order_items(order_1.id)[0].id

        # Reserve -> status = RESERVED
        self.inv_repo.reserve_items_for_order(order_1.id, self.voucher_prod.id, 1, self.denom_100.id)

        # 1. Change denomination_id after RESERVED
        self._assert_sql_update_blocked(
            "UPDATE order_items SET denomination_id = ? WHERE id = ?",
            (self.denom_1000.id, oi_id),
        )
        # 3a. Change product_id (with matching valid other_denom) after RESERVED
        self._assert_sql_update_blocked(
            "UPDATE order_items SET product_id = ?, denomination_id = ? WHERE id = ?",
            (self.other_voucher.id, self.other_denom.id, oi_id),
        )
        # 4a. Change order_id after RESERVED
        self._assert_sql_update_blocked(
            "UPDATE order_items SET order_id = ? WHERE id = ?",
            (order_2.id, oi_id),
        )
        # 4a-extra. Change product_type_snapshot after RESERVED
        self._assert_sql_update_blocked(
            "UPDATE order_items SET product_type_snapshot = 'FIXED_PRODUCT' WHERE id = ?",
            (oi_id,),
        )
        # 12a. Combined multi-field UPDATE after RESERVED
        self._assert_sql_update_blocked(
            "UPDATE order_items SET order_id = ?, product_id = ?, denomination_id = ?, quantity = 5 WHERE id = ?",
            (order_2.id, self.other_voucher.id, self.other_denom.id, oi_id),
        )

        # Finalize -> status = SOLD
        self.order_repo.transition_order_status(order_1.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order_1.id, OrderStatus.PAID)
        self.inv_repo.finalize_order_sale(order_1.id)

        # 2. Change denomination_id after SOLD
        self._assert_sql_update_blocked(
            "UPDATE order_items SET denomination_id = ? WHERE id = ?",
            (self.denom_1000.id, oi_id),
        )
        # 3b. Change product_id after SOLD
        self._assert_sql_update_blocked(
            "UPDATE order_items SET product_id = ?, denomination_id = ? WHERE id = ?",
            (self.other_voucher.id, self.other_denom.id, oi_id),
        )
        # 4b. Change order_id after SOLD
        self._assert_sql_update_blocked(
            "UPDATE order_items SET order_id = ? WHERE id = ?",
            (order_2.id, oi_id),
        )
        # 4b-extra. Change product_type_snapshot after SOLD
        self._assert_sql_update_blocked(
            "UPDATE order_items SET product_type_snapshot = 'FIXED_PRODUCT' WHERE id = ?",
            (oi_id,),
        )
        # 12b. Combined multi-field UPDATE after SOLD (including switching to FIXED_PRODUCT)
        self._assert_sql_update_blocked(
            "UPDATE order_items SET product_id = ?, product_type_snapshot = 'FIXED_PRODUCT', denomination_id = NULL, quantity = 2 WHERE id = ?",
            (self.fixed_prod.id, oi_id),
        )

    def test_sg_p2_001_followup_quantity_guards_reserved_sold_and_multi_row(self):
        """Scenarios 5-8, 11-12 & multi-row edge case: quantity cannot drop below allocated RESERVED/SOLD count."""
        self.inv_repo.import_items(
            self.voucher_prod.id,
            ["Q-100-1", "Q-100-2", "Q-100-3", "Q-100-4"],
            InventoryType.VOUCHER_CODE,
            self.denom_100.id,
        )
        # Order has 2 sibling rows of denom_100 (qty=2 + qty=2 = 4) and 1 unreserved row of denom_1000 (qty=2)
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=4 * 30000 + 2 * 300000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 2,
                    "unit_price_minor": 30000,
                },
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 2,
                    "unit_price_minor": 30000,
                },
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_1000.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 2,
                    "unit_price_minor": 300000,
                },
            ],
        )
        oi_100_a, oi_100_b, oi_1000 = [i.id for i in self.order_repo.get_order_items(order.id)]

        # Reserve 3 units of denom_100 (0 units of denom_1000 reserved)
        self.inv_repo.reserve_items_for_order(order.id, self.voucher_prod.id, 3, self.denom_100.id)

        # 5. Reducing single-stmt multi-row below 3 (1 + 1 = 2 < 3) must fail atomically across both rows
        self._assert_sql_update_blocked(
            "UPDATE order_items SET quantity = 1 WHERE id IN (?, ?)",
            (oi_100_a, oi_100_b),
        )

        # 7. Reducing oi_100_a from 2 -> 1 is ALLOWED because 1 + 2 = 3 >= 3 RESERVED
        self.conn.execute("UPDATE order_items SET quantity = 1 WHERE id = ?", (oi_100_a,))
        self.conn.commit()

        # Now oi_100_a=1, oi_100_b=2 (sum=3). Reducing oi_100_b to 1 (1+1=2 < 3) MUST be blocked
        self._assert_sql_update_blocked(
            "UPDATE order_items SET quantity = 1 WHERE id = ?",
            (oi_100_b,),
        )

        # 8. Increasing oi_100_a back from 1 -> 2 is ALLOWED
        self.conn.execute("UPDATE order_items SET quantity = 2 WHERE id = ?", (oi_100_a,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT quantity FROM order_items WHERE id = ?", (oi_100_a,)).fetchone()["quantity"],
            2,
        )

        # 11. Independent unreserved row (denom_1000) can change denomination_id (e.g. to denom_100? Wait: same product_id)
        # or reduce quantity from 2 -> 1 freely without false block
        self.conn.execute("UPDATE order_items SET quantity = 1 WHERE id = ?", (oi_1000,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT quantity FROM order_items WHERE id = ?", (oi_1000,)).fetchone()["quantity"],
            1,
        )
        # Unreserved row can also change product_id/denomination_id when no inventory is linked to (order, prod, denom_1000)
        self.conn.execute(
            "UPDATE order_items SET product_id = ?, denomination_id = ? WHERE id = ?",
            (self.other_voucher.id, self.other_denom.id, oi_1000),
        )
        self.conn.commit()
        row_1000 = self.conn.execute(
            "SELECT product_id, denomination_id FROM order_items WHERE id = ?", (oi_1000,)
        ).fetchone()
        self.assertEqual((row_1000["product_id"], row_1000["denomination_id"]), (self.other_voucher.id, self.other_denom.id))

        # Restore oi_1000 to match denom_1000 and reserve remaining items to test SOLD quantity guard
        self.conn.execute(
            "UPDATE order_items SET product_id = ?, denomination_id = ?, quantity = 1 WHERE id = ?",
            (self.voucher_prod.id, self.denom_1000.id, oi_1000),
        )
        self.conn.commit()
        # Reserve 1 more denom_100 (total 4) and remove extra denom_1000 item? Wait: instead finalize a clean SOLD order
        order_sold = self.order_repo.create_order(
            user_id=self.user_2.id,
            total_amount_minor=60000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "denomination_id": self.denom_100.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 2,
                    "unit_price_minor": 30000,
                }
            ],
        )
        # Import 2 more codes for order_sold
        self.inv_repo.import_items(
            self.voucher_prod.id,
            ["Q-100-5", "Q-100-6"],
            InventoryType.VOUCHER_CODE,
            self.denom_100.id,
        )
        self.inv_repo.reserve_items_for_order(order_sold.id, self.voucher_prod.id, 2, self.denom_100.id)
        self.order_repo.transition_order_status(order_sold.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order_sold.id, OrderStatus.PAID)
        self.assertEqual(self.inv_repo.finalize_order_sale(order_sold.id), 2)
        oi_sold_id = self.order_repo.get_order_items(order_sold.id)[0].id

        # 6. Reducing quantity below SOLD count (2 -> 1) MUST be blocked
        self._assert_sql_update_blocked(
            "UPDATE order_items SET quantity = 1 WHERE id = ?",
            (oi_sold_id,),
        )
        # Increasing quantity after SOLD (2 -> 3) is allowed if needed
        self.conn.execute("UPDATE order_items SET quantity = 3 WHERE id = ?", (oi_sold_id,))
        self.conn.commit()
        # Reducing back down to allowed boundary (3 -> 2, == 2 SOLD) succeeds
        self.conn.execute("UPDATE order_items SET quantity = 2 WHERE id = ?", (oi_sold_id,))
        self.conn.commit()

    def test_sg_p2_001_followup_fixed_product_null_denomination_and_product_type_change_guard(self):
        """Scenarios 9 & 10: FIXED_PRODUCT (denomination_id IS NULL) update guards & product_type change blocked when order_items exist without inventory."""
        # Create a FIXED_PRODUCT order BEFORE importing any inventory
        order_f1 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=100000,
            currency="RUB",
            items=[
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 2,
                    "unit_price_minor": 50000,
                }
            ],
        )
        order_f2 = self.order_repo.create_order(
            user_id=self.user_2.id,
            total_amount_minor=50000,
            currency="RUB",
            items=[
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 50000,
                }
            ],
        )

        # 9. Even with 0 inventory_items and 0 voucher_denominations, changing product_type is BLOCKED because order_items reference it
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM inventory_items WHERE product_id = ?", (self.fixed_prod.id,)).fetchone()[0],
            0,
        )
        self._assert_sql_update_blocked(
            "UPDATE products SET product_type = 'VOUCHER' WHERE id = ?",
            (self.fixed_prod.id,),
        )

        # Also verify an unused product with 0 order_items, 0 inventory, 0 denominations CAN still change type
        unused_prod = self.cat_repo.create_fixed_product(
            display_name="Unused Fixed",
            price_rub_kopecks=10000,
            delivery_method=DeliveryMethod.CODE,
            supply_method="POOL",
            approval_status=ApprovalStatus.DRAFT,
            status=ProductStatus.DRAFT,
        )
        self.conn.execute("UPDATE products SET product_type = 'VOUCHER' WHERE id = ?", (unused_prod.id,))
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT product_type FROM products WHERE id = ?", (unused_prod.id,)).fetchone()["product_type"],
            "VOUCHER",
        )

        # 10. FIXED_PRODUCT with denomination_id IS NULL: reserve 2 units on order_f1
        self.inv_repo.import_items(
            self.fixed_prod.id, ["FIX-NULL-1", "FIX-NULL-2"], InventoryType.ACTIVATION_DATA
        )
        self.inv_repo.reserve_items_for_order(order_f1.id, self.fixed_prod.id, 2)
        oi_f1 = self.order_repo.get_order_items(order_f1.id)[0].id

        # Cannot reduce quantity below 2 RESERVED (NULL denomination matching works)
        self._assert_sql_update_blocked(
            "UPDATE order_items SET quantity = 1 WHERE id = ?",
            (oi_f1,),
        )
        # Cannot change order_id or product_id or set non-NULL denomination_id
        self._assert_sql_update_blocked(
            "UPDATE order_items SET order_id = ? WHERE id = ?",
            (order_f2.id, oi_f1),
        )
        self._assert_sql_update_blocked(
            "UPDATE order_items SET product_id = ?, product_type_snapshot = 'VOUCHER', denomination_id = ? WHERE id = ?",
            (self.voucher_prod.id, self.denom_100.id, oi_f1),
        )
        # Increasing quantity (2 -> 3) and reducing back (3 -> 2) works on FIXED_PRODUCT
        self.conn.execute("UPDATE order_items SET quantity = 3 WHERE id = ?", (oi_f1,))
        self.conn.execute("UPDATE order_items SET quantity = 2 WHERE id = ?", (oi_f1,))
        self.conn.commit()

    def test_sg_p2_001_followup_migration_006_upgrade_from_005_and_preflight_abort(self):
        """Migration 006 upgrades an existing 001..005 DB cleanly and aborts pre-flight if legacy qty < allocated."""
        # Case A: Clean upgrade from already-applied 001..005 DB
        db_up = os.path.join(self.temp_dir.name, "upgrade_005_to_006.db")
        c = create_connection(db_up)
        try:
            migs_005 = self._copy_migrations_up_to(5, "up_to_005")
            self.assertEqual(run_migrations(c, migrations_dir=migs_005), [1, 2, 3, 4, 5])

            u_repo = UserRepository(c)
            cat_repo = CatalogRepository(c)
            inv_repo = InventoryRepository(c)
            ord_repo = OrderRepository(c)
            u, _ = u_repo.get_or_create_user("upg_u", "upg_u")
            vp = cat_repo.create_voucher_product("V Up", "TR", "TRY", ProductStatus.APPROVED)
            d1 = cat_repo.add_voucher_denomination(vp.id, "TR", "TRY", 10000, 30000)
            d2 = cat_repo.add_voucher_denomination(vp.id, "TR", "TRY", 20000, 60000)
            inv_repo.import_items(vp.id, ["UPG-1"], InventoryType.VOUCHER_CODE, d1.id)
            order = ord_repo.create_order(
                u.id,
                30000,
                "RUB",
                [
                    {
                        "product_id": vp.id,
                        "denomination_id": d1.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 1,
                        "unit_price_minor": 30000,
                    }
                ],
            )
            inv_repo.reserve_items_for_order(order.id, vp.id, 1, d1.id)

            # Now upgrade to 006 and 007
            self.assertEqual(run_migrations(c), [6, 7])
            self.assertEqual(get_applied_migrations(c), [1, 2, 3, 4, 5, 6, 7])
            # Idempotent re-run
            self.assertEqual(run_migrations(c), [])

            # Upgraded DB now blocks direct SQL denomination change after RESERVED
            oi_id = ord_repo.get_order_items(order.id)[0].id
            self._assert_sql_update_blocked(
                "UPDATE order_items SET denomination_id = ? WHERE id = ?",
                (d2.id, oi_id),
                conn=c,
            )
        finally:
            c.close()

        # Case B: Pre-existing DB at 005 with invalid state (2 RESERVED items, order_items.quantity = 1) -> 006 aborts safely
        db_bad = os.path.join(self.temp_dir.name, "abort_006_bad_qty.db")
        c2 = create_connection(db_bad)
        try:
            migs_005_b = self._copy_migrations_up_to(5, "bad_005")
            self.assertEqual(run_migrations(c2, migrations_dir=migs_005_b), [1, 2, 3, 4, 5])
            u_repo = UserRepository(c2)
            cat_repo = CatalogRepository(c2)
            inv_repo = InventoryRepository(c2)
            ord_repo = OrderRepository(c2)
            u, _ = u_repo.get_or_create_user("bad_u", "bad_u")
            vp = cat_repo.create_voucher_product("V Bad", "TR", "TRY", ProductStatus.APPROVED)
            d1 = cat_repo.add_voucher_denomination(vp.id, "TR", "TRY", 10000, 30000)
            inv_repo.import_items(vp.id, ["BAD-1", "BAD-2"], InventoryType.VOUCHER_CODE, d1.id)
            order = ord_repo.create_order(
                u.id,
                60000,
                "RUB",
                [
                    {
                        "product_id": vp.id,
                        "denomination_id": d1.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 2,
                        "unit_price_minor": 30000,
                    }
                ],
            )
            inv_repo.reserve_items_for_order(order.id, vp.id, 2, d1.id)
            # Simulate an older pre-006 DB that lacked the quantity check in trg_order_items_validate_update:
            c2.execute("DROP TRIGGER IF EXISTS trg_order_items_validate_update")
            c2.execute("UPDATE order_items SET quantity = 1 WHERE order_id = ?", (order.id,))
            c2.commit()

            with self.assertRaises(RuntimeError):
                run_migrations(c2)
            self.assertEqual(get_applied_migrations(c2), [1, 2, 3, 4, 5])
            self.assertEqual(
                c2.execute("SELECT quantity FROM order_items WHERE order_id = ?", (order.id,)).fetchone()["quantity"],
                1,
            )

            # Fix inconsistency -> 006 and 007 apply cleanly and restore trg_order_items_validate_update
            c2.execute("UPDATE order_items SET quantity = 2 WHERE order_id = ?", (order.id,))
            c2.commit()
            self.assertEqual(run_migrations(c2), [6, 7])
            self.assertEqual(get_applied_migrations(c2), [1, 2, 3, 4, 5, 6, 7])
            self._assert_sql_update_blocked(
                "UPDATE order_items SET quantity = 1 WHERE order_id = ?",
                (order.id,),
                conn=c2,
            )
        finally:
            c2.close()

    def test_sg_p2_001_db_update_guards_active_at_003_004_and_005(self):
        """Guards are enforced immediately at migrations 003 (product_type), 004, and 005 (order_items UPDATE)."""
        # 1. At migration 003: changing product_type with order_items (even 0 inventory) is blocked
        db_003 = os.path.join(self.temp_dir.name, "guard_at_003.db")
        c3 = create_connection(db_003)
        try:
            migs_003 = self._copy_migrations_up_to(3, "g003")
            self.assertEqual(run_migrations(c3, migrations_dir=migs_003), [1, 2, 3])
            u_id = c3.execute("INSERT INTO users (external_id, username, status) VALUES ('g3', 'g3', 'ACTIVE')").lastrowid
            p_id = c3.execute(
                "INSERT INTO products (product_type, display_name, status) VALUES ('FIXED_PRODUCT', 'G3', 'APPROVED')"
            ).lastrowid
            o_id = c3.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PENDING_PAYMENT', 50000, 'RUB')",
                (u_id,),
            ).lastrowid
            c3.execute(
                "INSERT INTO order_items (order_id, product_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                "VALUES (?, ?, 'FIXED_PRODUCT', 1, 50000, 'RUB')",
                (o_id, p_id),
            )
            c3.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                c3.execute("UPDATE products SET product_type = 'VOUCHER' WHERE id = ?", (p_id,))
            if c3.in_transaction:
                c3.rollback()
            self.assertEqual(
                c3.execute("SELECT product_type FROM products WHERE id = ?", (p_id,)).fetchone()["product_type"],
                "FIXED_PRODUCT",
            )
        finally:
            c3.close()

        # 2. At migrations 004 and 005: direct SQL UPDATE on order_items is blocked once RESERVED/SOLD exists
        for max_ver in (4, 5):
            with self.subTest(max_migration=max_ver):
                db_path = os.path.join(self.temp_dir.name, f"guard_at_{max_ver:03d}.db")
                cx = create_connection(db_path)
                try:
                    migs_dir = self._copy_migrations_up_to(max_ver, f"g{max_ver:03d}")
                    self.assertEqual(run_migrations(cx, migrations_dir=migs_dir), list(range(1, max_ver + 1)))

                    u_repo = UserRepository(cx)
                    cat_repo = CatalogRepository(cx)
                    inv_repo = InventoryRepository(cx)
                    ord_repo = OrderRepository(cx)

                    u1, _ = u_repo.get_or_create_user(f"g{max_ver}_1", f"g{max_ver}_1")
                    u2, _ = u_repo.get_or_create_user(f"g{max_ver}_2", f"g{max_ver}_2")
                    vp1 = cat_repo.create_voucher_product("VP1", "TR", "TRY", ProductStatus.APPROVED)
                    d1 = cat_repo.add_voucher_denomination(vp1.id, "TR", "TRY", 10000, 30000)
                    d2 = cat_repo.add_voucher_denomination(vp1.id, "TR", "TRY", 20000, 60000)
                    vp2 = cat_repo.create_voucher_product("VP2", "US", "USD", ProductStatus.APPROVED)
                    d_us = cat_repo.add_voucher_denomination(vp2.id, "US", "USD", 1000, 95000)

                    inv_repo.import_items(vp1.id, [f"G{max_ver}-1", f"G{max_ver}-2"], InventoryType.VOUCHER_CODE, d1.id)
                    o1 = ord_repo.create_order(
                        u1.id,
                        60000,
                        "RUB",
                        [
                            {
                                "product_id": vp1.id,
                                "denomination_id": d1.id,
                                "product_type_snapshot": "VOUCHER",
                                "quantity": 2,
                                "unit_price_minor": 30000,
                            }
                        ],
                    )
                    o2 = ord_repo.create_order(
                        u2.id,
                        30000,
                        "RUB",
                        [
                            {
                                "product_id": vp1.id,
                                "denomination_id": d1.id,
                                "product_type_snapshot": "VOUCHER",
                                "quantity": 1,
                                "unit_price_minor": 30000,
                            }
                        ],
                    )
                    inv_repo.reserve_items_for_order(o1.id, vp1.id, 2, d1.id)
                    oi_id = ord_repo.get_order_items(o1.id)[0].id

                    self._assert_sql_update_blocked("UPDATE order_items SET denomination_id = ? WHERE id = ?", (d2.id, oi_id), conn=cx)
                    self._assert_sql_update_blocked("UPDATE order_items SET product_id = ?, denomination_id = ? WHERE id = ?", (vp2.id, d_us.id, oi_id), conn=cx)
                    self._assert_sql_update_blocked("UPDATE order_items SET order_id = ? WHERE id = ?", (o2.id, oi_id), conn=cx)
                    self._assert_sql_update_blocked("UPDATE order_items SET quantity = 1 WHERE id = ?", (oi_id,), conn=cx)
                    self._assert_sql_update_blocked("UPDATE products SET product_type = 'FIXED_PRODUCT' WHERE id = ?", (vp1.id,), conn=cx)
                finally:
                    cx.close()


if __name__ == "__main__":
    unittest.main()
