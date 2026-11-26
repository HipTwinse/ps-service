"""Regression tests for Security Gate Phase 2 follow-up audit findings."""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.db.connection import create_connection, transaction
from src.db.migrations import get_applied_migrations, run_migrations
from src.db.models import (
    ApprovalStatus,
    ConflictError,
    DeliveryMethod,
    InvalidStateTransitionError,
    InventoryStatus,
    InventoryType,
    OrderStatus,
    ProductStatus,
    UserStatus,
    ValidationError,
)
from src.db.repositories import (
    CatalogRepository,
    InventoryRepository,
    OrderRepository,
    UserRepository,
)


class TestPhase2RemediationFollowup(unittest.TestCase):
    """Covers all 5 blocking findings and additional concurrency/BaseException checks."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "followup.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.user_repo = UserRepository(self.conn)
        self.cat_repo = CatalogRepository(self.conn)
        self.inv_repo = InventoryRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)

        self.user_a, _ = self.user_repo.get_or_create_user("tg_a", "user_a")
        self.user_b, _ = self.user_repo.get_or_create_user("tg_b", "user_b")

        self.voucher_a = self.cat_repo.create_voucher_product(
            display_name="PSN TR",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.denom_a = self.cat_repo.add_voucher_denomination(
            product_id=self.voucher_a.id,
            region="TR",
            currency="TRY",
            face_value_minor=10000,
            cost_rub_kopecks=30000,
        )
        self.voucher_b = self.cat_repo.create_voucher_product(
            display_name="PSN US",
            region="US",
            currency="USD",
            status=ProductStatus.APPROVED,
        )
        self.denom_b = self.cat_repo.add_voucher_denomination(
            product_id=self.voucher_b.id,
            region="US",
            currency="USD",
            face_value_minor=1000,
            cost_rub_kopecks=100000,
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

    def _snapshot_state(self, order_id: int):
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
        order_status = cur.fetchone()["status"]
        cur.execute(
            "SELECT id, status, reserved_order_id FROM inventory_items ORDER BY id ASC"
        )
        inv_rows = [tuple(r) for r in cur.fetchall()]
        return order_status, inv_rows

    def _create_order_for_product(
        self,
        user_id: int,
        product_id: int,
        product_type: str,
        qty: int,
        unit_price: int = 30000,
        denomination_id: int = None,
    ):
        item = {
            "product_id": product_id,
            "product_type_snapshot": product_type,
            "quantity": qty,
            "unit_price_minor": unit_price,
        }
        if product_type == "VOUCHER":
            item["denomination_id"] = denomination_id if denomination_id is not None else self.denom_a.id
        elif denomination_id is not None:
            item["denomination_id"] = denomination_id
        return self.order_repo.create_order(
            user_id=user_id,
            total_amount_minor=qty * unit_price,
            currency="RUB",
            items=[item],
        )

    # ------------------------------------------------------------------
    # 1. P1: reserve_items_for_order quantity bounds (-1, 0, True)
    # ------------------------------------------------------------------

    def test_reserve_rejects_negative_zero_and_bool_quantity_without_mutating_db(self):
        self.inv_repo.import_items(
            self.voucher_a.id,
            ["Q-1", "Q-2", "Q-3"],
            InventoryType.VOUCHER_CODE,
            self.denom_a.id,
        )
        order = self._create_order_for_product(self.user_a.id, self.voucher_a.id, "VOUCHER", qty=2)
        before = self._snapshot_state(order.id)

        for bad_qty in (-1, 0, True):
            with self.subTest(bad_qty=bad_qty):
                with self.assertRaises(ValidationError):
                    self.inv_repo.reserve_items_for_order(
                        order_id=order.id,
                        product_id=self.voucher_a.id,
                        quantity=bad_qty,
                        denomination_id=self.denom_a.id,
                    )
                self.assertEqual(self._snapshot_state(order.id), before)

    # ------------------------------------------------------------------
    # 2. P1: reserve_items_for_order order_items & quantity cap
    # ------------------------------------------------------------------

    def test_reserve_enforces_order_items_membership_and_quantity_limits(self):
        self.inv_repo.import_items(
            self.voucher_a.id,
            ["CAP-A1", "CAP-A2", "CAP-A3", "CAP-A4"],
            InventoryType.VOUCHER_CODE,
            self.denom_a.id,
        )
        self.inv_repo.import_items(
            self.voucher_b.id,
            ["CAP-B1", "CAP-B2"],
            InventoryType.VOUCHER_CODE,
            self.denom_b.id,
        )

        # Order A only contains voucher_a with qty=2
        order_a = self._create_order_for_product(self.user_a.id, self.voucher_a.id, "VOUCHER", qty=2)
        order_b = self._create_order_for_product(
            self.user_b.id,
            self.voucher_b.id,
            "VOUCHER",
            qty=1,
            unit_price=100000,
            denomination_id=self.denom_b.id,
        )

        # 2a. Reserving product not in order_a
        before_a = self._snapshot_state(order_a.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_b.id, 1, self.denom_b.id)
        self.assertEqual(self._snapshot_state(order_a.id), before_a)

        # 2b. Reserving quantity > ordered quantity
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 3, self.denom_a.id)
        self.assertEqual(self._snapshot_state(order_a.id), before_a)

        # 2c. Reserve 1 unit -> reserve 1 more (reaches cap=2) -> 3rd attempt fails
        res_1 = self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        self.assertEqual(len(res_1), 1)
        res_2 = self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        self.assertEqual(len(res_2), 1)

        snap_after_two = self._snapshot_state(order_a.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        self.assertEqual(self._snapshot_state(order_a.id), snap_after_two)

        # 2d. Even if one item is SOLD (accounting for both RESERVED and SOLD), cannot exceed ordered qty
        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAID)
        self.conn.execute("UPDATE inventory_items SET status = 'SOLD' WHERE id = ?", (res_1[0],))
        self.conn.commit()
        snap_with_sold = self._snapshot_state(order_a.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        self.assertEqual(self._snapshot_state(order_a.id), snap_with_sold)

        # 2e. Attempting to reserve voucher_a on order_b (which only ordered voucher_b) fails
        before_b = self._snapshot_state(order_b.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(order_b.id, self.voucher_a.id, 1, self.denom_a.id)
        self.assertEqual(self._snapshot_state(order_b.id), before_b)

    # ------------------------------------------------------------------
    # 3. P2: finalize_order_sale full-order fulfillment invariant
    # ------------------------------------------------------------------

    def test_finalize_order_sale_enforces_complete_reservation_atomically(self):
        self.inv_repo.import_items(
            self.voucher_a.id,
            ["FIN-A1", "FIN-A2"],
            InventoryType.VOUCHER_CODE,
            self.denom_a.id,
        )
        self.inv_repo.import_items(
            self.fixed_prod.id,
            ["FIN-FIX1"],
            InventoryType.ACTIVATION_DATA,
        )

        # 3a. Order with 0 reservations -> rejected
        order_empty = self._create_order_for_product(self.user_a.id, self.voucher_a.id, "VOUCHER", qty=1)
        self.order_repo.transition_order_status(order_empty.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order_empty.id, OrderStatus.PAID)
        with self.assertRaises(ValidationError):
            self.inv_repo.finalize_order_sale(order_empty.id)

        # 3b. Partially reserved multi-item order (voucher_a qty=2, fixed_prod qty=1; only 1 voucher_a + 1 fixed_prod reserved)
        multi_order = self.order_repo.create_order(
            user_id=self.user_a.id,
            total_amount_minor=110000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_a.id,
                    "denomination_id": self.denom_a.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 2,
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
        self.inv_repo.reserve_items_for_order(multi_order.id, self.voucher_a.id, 1, self.denom_a.id)
        self.inv_repo.reserve_items_for_order(multi_order.id, self.fixed_prod.id, 1)
        self.order_repo.transition_order_status(multi_order.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(multi_order.id, OrderStatus.PAID)

        before_partial = self._snapshot_state(multi_order.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.finalize_order_sale(multi_order.id)
        # Atomic rollback: fixed_prod item must NOT be SOLD
        self.assertEqual(self._snapshot_state(multi_order.id), before_partial)

        # 3c. Mismatched quantity (e.g., extra reserved item beyond order_items) -> rejected atomically
        self.inv_repo.reserve_items_for_order(multi_order.id, self.voucher_a.id, 1, self.denom_a.id)
        # Now multi_order has all 3 items reserved (2 voucher_a + 1 fixed_prod) -> finalize succeeds!
        sold_count = self.inv_repo.finalize_order_sale(multi_order.id)
        self.assertEqual(sold_count, 3)

        # 3d. Re-finalization -> ConflictError
        with self.assertRaises(ConflictError):
            self.inv_repo.finalize_order_sale(multi_order.id)

    # ------------------------------------------------------------------
    # 4. P1: Migration 003 pre-flight validation of existing legacy rows
    # ------------------------------------------------------------------

    def _setup_legacy_db_at_002(self, db_name: str):
        legacy_db = os.path.join(self.temp_dir.name, db_name)
        c = create_connection(legacy_db)
        migrations_root = Path(__file__).resolve().parent.parent / "src" / "migrations"
        partial_dir = Path(self.temp_dir.name) / f"partial_migs_{db_name}"
        partial_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("001_initial_schema.sql", "002_provider_scoped_receipts.sql"):
            (partial_dir / fname).write_text(
                (migrations_root / fname).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        self.assertEqual(run_migrations(c, migrations_dir=partial_dir), [1, 2])
        return c

    def _setup_migs_dir_001_to_003(self, tag: str) -> Path:
        migrations_root = Path(__file__).resolve().parent.parent / "src" / "migrations"
        migs_dir = Path(self.temp_dir.name) / f"migs_001_003_{tag}"
        migs_dir.mkdir(parents=True, exist_ok=True)
        for fname in (
            "001_initial_schema.sql",
            "002_provider_scoped_receipts.sql",
            "003_inventory_and_integrity_guards.sql",
        ):
            (migs_dir / fname).write_text(
                (migrations_root / fname).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        return migs_dir

    def test_migration_003_aborts_on_invalid_legacy_inventory_without_mutating_data(self):
        # Case A: ProductType / InventoryType incompatibility (VOUCHER with FIXED_CODE & NULL denom)
        c = self._setup_legacy_db_at_002("legacy_type_mismatch.db")
        migs_003 = self._setup_migs_dir_001_to_003("case_a")
        try:
            cat = CatalogRepository(c)
            v_prod = cat.create_voucher_product("Legacy Bad Voucher", "TR", "TRY", ProductStatus.APPROVED)
            v_denom = cat.add_voucher_denomination(v_prod.id, "TR", "TRY", 10000, 30000)
            c.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, NULL, 'FIXED_CODE', ?, 'legacy_bad_fp_1', 'AVAILABLE')
                """,
                (v_prod.id, b"dummy_encrypted_bytes"),
            )
            c.commit()

            before_rows = [
                tuple(r)
                for r in c.execute(
                    "SELECT id, product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status "
                    "FROM inventory_items ORDER BY id ASC"
                ).fetchall()
            ]

            with self.assertRaises(RuntimeError):
                run_migrations(c, migrations_dir=migs_003)

            self.assertEqual(get_applied_migrations(c), [1, 2])
            triggers = [
                r["name"]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_inventory_items_validate_insert'"
                ).fetchall()
            ]
            self.assertEqual(triggers, [])

            after_rows = [
                tuple(r)
                for r in c.execute(
                    "SELECT id, product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status "
                    "FROM inventory_items ORDER BY id ASC"
                ).fetchall()
            ]
            self.assertEqual(after_rows, before_rows)

            # Fix legacy data -> migration 003 must now succeed
            c.execute(
                "UPDATE inventory_items SET inventory_type = 'VOUCHER_CODE', denomination_id = ? WHERE fingerprint = 'legacy_bad_fp_1'",
                (v_denom.id,),
            )
            c.commit()
            self.assertEqual(run_migrations(c, migrations_dir=migs_003), [3])
            self.assertEqual(get_applied_migrations(c), [1, 2, 3])
        finally:
            c.close()

        # Case B: denomination_id belongs to a different product_id
        c = self._setup_legacy_db_at_002("legacy_denom_mismatch.db")
        migs_003_b = self._setup_migs_dir_001_to_003("case_b")
        try:
            cat = CatalogRepository(c)
            v1 = cat.create_voucher_product("V1", "TR", "TRY", ProductStatus.APPROVED)
            d1 = cat.add_voucher_denomination(v1.id, "TR", "TRY", 10000, 30000)
            v2 = cat.create_voucher_product("V2", "US", "USD", ProductStatus.APPROVED)
            d2 = cat.add_voucher_denomination(v2.id, "US", "USD", 1000, 100000)

            c.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, ?, 'VOUCHER_CODE', ?, 'legacy_bad_denom_fp', 'AVAILABLE')
                """,
                (v1.id, d2.id, b"dummy_encrypted_bytes"),
            )
            c.commit()

            before_rows = [
                tuple(r)
                for r in c.execute(
                    "SELECT id, product_id, denomination_id, inventory_type, fingerprint FROM inventory_items ORDER BY id ASC"
                ).fetchall()
            ]
            with self.assertRaises(RuntimeError):
                run_migrations(c, migrations_dir=migs_003_b)

            self.assertEqual(get_applied_migrations(c), [1, 2])
            after_rows = [
                tuple(r)
                for r in c.execute(
                    "SELECT id, product_id, denomination_id, inventory_type, fingerprint FROM inventory_items ORDER BY id ASC"
                ).fetchall()
            ]
            self.assertEqual(after_rows, before_rows)

            # Fix legacy row -> migration 003 succeeds
            c.execute(
                "UPDATE inventory_items SET denomination_id = ? WHERE fingerprint = 'legacy_bad_denom_fp'",
                (d1.id,),
            )
            c.commit()
            self.assertEqual(run_migrations(c, migrations_dir=migs_003_b), [3])
            self.assertEqual(get_applied_migrations(c), [1, 2, 3])
        finally:
            c.close()

        # Case C: payment_receipts.user_id != orders.user_id (seeded in 001 schema)
        c = self._setup_legacy_db_at_002("legacy_receipt_owner.db")
        migs_003_c = self._setup_migs_dir_001_to_003("case_c")
        try:
            u_repo = UserRepository(c)
            cat = CatalogRepository(c)
            u1, _ = u_repo.get_or_create_user("leg_u1", "leg1")
            u2, _ = u_repo.get_or_create_user("leg_u2", "leg2")
            v1 = cat.create_voucher_product("V1", "TR", "TRY", ProductStatus.APPROVED)
            cur = c.cursor()
            cur.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PENDING_PAYMENT', 30000, 'RUB')",
                (u1.id,),
            )
            legacy_order_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, product_type_snapshot, quantity, unit_price_minor, currency
                ) VALUES (?, ?, 'VOUCHER', 1, 30000, 'RUB')
                """,
                (legacy_order_id, v1.id),
            )
            c.execute(
                """
                INSERT INTO payment_receipts (
                    order_id, user_id, file_sha256, file_size_bytes, review_status
                ) VALUES (?, ?, 'legacy_receipt_sha_1', 1024, 'PENDING_REVIEW')
                """,
                (legacy_order_id, u2.id),
            )
            c.commit()

            before_receipts = [
                tuple(r)
                for r in c.execute(
                    "SELECT id, order_id, user_id, file_sha256 FROM payment_receipts ORDER BY id ASC"
                ).fetchall()
            ]
            with self.assertRaises(RuntimeError):
                run_migrations(c, migrations_dir=migs_003_c)

            self.assertEqual(get_applied_migrations(c), [1, 2])
            after_receipts = [
                tuple(r)
                for r in c.execute(
                    "SELECT id, order_id, user_id, file_sha256 FROM payment_receipts ORDER BY id ASC"
                ).fetchall()
            ]
            self.assertEqual(after_receipts, before_receipts)

            # Fix legacy receipt owner -> migration 003 succeeds
            c.execute(
                "UPDATE payment_receipts SET user_id = ? WHERE file_sha256 = 'legacy_receipt_sha_1'",
                (u1.id,),
            )
            c.commit()
            self.assertEqual(run_migrations(c, migrations_dir=migs_003_c), [3])
            self.assertEqual(get_applied_migrations(c), [1, 2, 3])
        finally:
            c.close()

    def test_migration_003_triggers_block_insert_and_update_violations(self):
        # Verify post-003 triggers on self.conn block all 3 violation classes on INSERT and UPDATE
        self.inv_repo.import_items(
            self.voucher_a.id,
            ["TRIG-V1"],
            InventoryType.VOUCHER_CODE,
            self.denom_a.id,
        )
        self.inv_repo.import_items(
            self.fixed_prod.id,
            ["TRIG-F1"],
            InventoryType.ACTIVATION_DATA,
        )
        order_a = self._create_order_for_product(self.user_a.id, self.voucher_a.id, "VOUCHER", qty=1)
        self.conn.execute(
            """
            INSERT INTO payment_receipts (order_id, user_id, file_sha256, file_size_bytes, review_status)
            VALUES (?, ?, 'trig_rcpt_sha_1', 512, 'PENDING_REVIEW')
            """,
            (order_a.id, self.user_a.id),
        )
        self.conn.commit()

        # 1. INSERT & UPDATE: denomination_id belongs to different product_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, ?, 'VOUCHER_CODE', ?, 'trig_bad_denom_ins', 'AVAILABLE')
                """,
                (self.voucher_a.id, self.denom_b.id, b"enc"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE inventory_items SET denomination_id = ? WHERE product_id = ?",
                (self.denom_b.id, self.voucher_a.id),
            )

        # 2. INSERT & UPDATE: ProductType / InventoryType incompatibility
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, ?, 'FIXED_CODE', ?, 'trig_bad_type_ins_1', 'AVAILABLE')
                """,
                (self.voucher_a.id, self.denom_a.id, b"enc"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, NULL, 'VOUCHER_CODE', ?, 'trig_bad_type_ins_2', 'AVAILABLE')
                """,
                (self.fixed_prod.id, b"enc"),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE inventory_items SET inventory_type = 'FIXED_CODE' WHERE product_id = ?",
                (self.voucher_a.id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE inventory_items SET inventory_type = 'VOUCHER_CODE' WHERE product_id = ?",
                (self.fixed_prod.id,),
            )

        # 3. INSERT & UPDATE: payment_receipts.user_id != orders.user_id
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO payment_receipts (order_id, user_id, file_sha256, file_size_bytes, review_status)
                VALUES (?, ?, 'trig_rcpt_sha_bad', 512, 'PENDING_REVIEW')
                """,
                (order_a.id, self.user_b.id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE payment_receipts SET user_id = ? WHERE file_sha256 = 'trig_rcpt_sha_1'",
                (self.user_b.id,),
            )

    # ------------------------------------------------------------------
    # 5. P1: get_revealed_code requires full context & SOLD status
    # ------------------------------------------------------------------

    def test_get_revealed_code_strict_context_and_state_matrix(self):
        self.inv_repo.import_items(
            self.voucher_a.id,
            ["REV-CODE-1", "REV-CODE-2"],
            InventoryType.VOUCHER_CODE,
            self.denom_a.id,
        )
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM inventory_items ORDER BY id ASC")
        item_1, item_2 = [r["id"] for r in cur.fetchall()]

        # 5a. No context arguments
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(item_1)

        # 5b. Only expected_order_id
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(item_1, expected_order_id=1)

        # 5c. Only expected_user_id
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(item_1, expected_user_id=self.user_a.id)

        order_a = self._create_order_for_product(self.user_a.id, self.voucher_a.id, "VOUCHER", qty=1)
        order_b = self._create_order_for_product(self.user_b.id, self.voucher_a.id, "VOUCHER", qty=1)

        # 5d. AVAILABLE item rejected even with valid order/user
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(
                item_2,
                expected_order_id=order_a.id,
                expected_user_id=self.user_a.id,
            )

        # 5e. RESERVED item rejected (even after order is PAID)
        res_a = self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        item_a = res_a[0]
        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAID)
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(
                item_a,
                expected_order_id=order_a.id,
                expected_user_id=self.user_a.id,
            )

        # Finalize sale -> SOLD
        self.inv_repo.finalize_order_sale(order_a.id)

        # 5f. Wrong user
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(
                item_a,
                expected_order_id=order_a.id,
                expected_user_id=self.user_b.id,
            )

        # 5g. Foreign order / item not assigned to specified order
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(
                item_a,
                expected_order_id=order_b.id,
                expected_user_id=self.user_b.id,
            )

        # 5h. Valid owner + order + SOLD + PAID -> succeeds
        self.assertEqual(
            self.inv_repo.get_revealed_code(
                item_a,
                expected_order_id=order_a.id,
                expected_user_id=self.user_a.id,
            ),
            "REV-CODE-1",
        )

        # 5i. Disallowed order status (REFUND_REVIEW) -> rejected
        self.order_repo.transition_order_status(order_a.id, OrderStatus.REFUND_REVIEW)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.get_revealed_code(
                item_a,
                expected_order_id=order_a.id,
                expected_user_id=self.user_a.id,
            )

        # 5j. Invalid IDs (0, -1, True, str) rejected before decryption
        for bad_val in (0, -1, True, "1"):
            with self.subTest(bad_val=bad_val):
                with self.assertRaises(ValidationError):
                    self.inv_repo.get_revealed_code(
                        bad_val,
                        expected_order_id=order_a.id,
                        expected_user_id=self.user_a.id,
                    )
                with self.assertRaises(ValidationError):
                    self.inv_repo.get_revealed_code(
                        item_a,
                        expected_order_id=bad_val,
                        expected_user_id=self.user_a.id,
                    )
                with self.assertRaises(ValidationError):
                    self.inv_repo.get_revealed_code(
                        item_a,
                        expected_order_id=order_a.id,
                        expected_user_id=bad_val,
                    )

    # ------------------------------------------------------------------
    # 6. Additional checks: Exception/BaseException rollback & concurrent run_migrations
    # ------------------------------------------------------------------

    def test_transaction_rolls_back_on_exception_and_base_exception(self):
        class _CustomBaseAbort(BaseException):
            pass

        # 6a. Standard Exception
        with self.assertRaises(ValueError):
            with transaction(self.conn, mode="IMMEDIATE"):
                self.conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('exc_u', 'exc', 'ACTIVE')"
                )
                raise ValueError("Simulated standard exception")

        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM users WHERE external_id = 'exc_u'").fetchone()
        )

        # 6b. Direct BaseException subclass
        with self.assertRaises(_CustomBaseAbort):
            with transaction(self.conn, mode="IMMEDIATE"):
                self.conn.execute(
                    "INSERT INTO users (external_id, username, status) VALUES ('abort_u', 'abort', 'ACTIVE')"
                )
                raise _CustomBaseAbort("Simulated cancellation")

        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM users WHERE external_id = 'abort_u'").fetchone()
        )

    def test_transaction_never_rolls_back_caller_owned_transaction(self):
        self.conn.execute("BEGIN IMMEDIATE;")
        self.conn.execute(
            "INSERT INTO users (external_id, username, status) VALUES ('caller_u', 'caller', 'ACTIVE')"
        )
        self.assertTrue(self.conn.in_transaction)

        with self.assertRaises(RuntimeError):
            with transaction(self.conn, mode="IMMEDIATE"):
                pass

        # Caller's transaction must remain active and intact (neither committed nor rolled back)
        self.assertTrue(self.conn.in_transaction)
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM users WHERE external_id = 'caller_u'").fetchone()
        )
        self.conn.rollback()
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM users WHERE external_id = 'caller_u'").fetchone()
        )

    def test_concurrent_run_migrations_serializes_safely(self):
        conc_db = os.path.join(self.temp_dir.name, "concurrent_mig.db")
        init_conn = create_connection(conc_db, timeout_seconds=10.0)
        init_conn.close()

        barrier = threading.Barrier(2)
        errors = []
        results = []

        def worker():
            c = create_connection(conc_db, timeout_seconds=10.0)
            try:
                barrier.wait(timeout=5.0)
                applied = run_migrations(c)
                results.append(applied)
            except BaseException as exc:
                errors.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        combined = results[0] + results[1]
        self.assertEqual(len(combined), 7)
        self.assertEqual(sorted(combined), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(results[0], sorted(results[0]))
        self.assertEqual(results[1], sorted(results[1]))

        verify_conn = create_connection(conc_db, timeout_seconds=10.0)
        try:
            mig_rows = verify_conn.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version ASC"
            ).fetchall()
            self.assertEqual([r["version"] for r in mig_rows], [1, 2, 3, 4, 5, 6, 7])
            for r in mig_rows:
                self.assertIsNotNone(r["checksum"])
                self.assertEqual(len(r["checksum"]), 64)

            tables = {
                r["name"]
                for r in verify_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for expected_table in ("users", "products", "orders", "order_items", "inventory_items", "payment_receipts"):
                self.assertIn(expected_table, tables)

            triggers = {
                r["name"]
                for r in verify_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                ).fetchall()
            }
            self.assertIn("trg_inventory_items_validate_insert", triggers)
            self.assertIn("trg_payment_receipts_validate_owner_insert", triggers)
        finally:
            verify_conn.close()

    def test_concurrent_run_migrations_waits_on_immediate_lock_and_skips_applied(self):
        import hashlib

        conc_db = os.path.join(self.temp_dir.name, "concurrent_lock_wait.db")
        mig_dir = Path(self.temp_dir.name) / "single_mig"
        mig_dir.mkdir(parents=True, exist_ok=True)
        sql_text = "CREATE TABLE IF NOT EXISTS lock_test_tbl (id INTEGER PRIMARY KEY);\n"
        (mig_dir / "001_lock_test.sql").write_text(sql_text, encoding="utf-8")
        checksum = hashlib.sha256(sql_text.encode("utf-8")).hexdigest()

        conn1 = create_connection(conc_db, timeout_seconds=10.0)
        try:
            # Ensure schema_migrations exists before acquiring write lock on conn1
            from src.db.migrations import ensure_migration_table
            ensure_migration_table(conn1)

            conn1.execute("BEGIN IMMEDIATE;")
            conn1.execute("CREATE TABLE IF NOT EXISTS lock_test_tbl (id INTEGER PRIMARY KEY);")

            results = []
            errors = []
            entered_immediate = threading.Event()

            def runner2():
                c2 = create_connection(conc_db, timeout_seconds=10.0)

                def _trace(stmt):
                    if "BEGIN IMMEDIATE" in stmt.upper() and not entered_immediate.is_set():
                        entered_immediate.set()

                c2.set_trace_callback(_trace)
                try:
                    results.append(run_migrations(c2, migrations_dir=mig_dir))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    c2.set_trace_callback(None)
                    c2.close()

            t2 = threading.Thread(target=runner2)
            t2.start()
            # Deterministically wait until runner2 actually reaches BEGIN IMMEDIATE in SQLite
            self.assertTrue(entered_immediate.wait(timeout=5.0))
            self.assertTrue(t2.is_alive())

            conn1.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (1, 'lock_test', ?)",
                (checksum,),
            )
            conn1.commit()
            t2.join(timeout=10.0)
            self.assertFalse(t2.is_alive())

            self.assertEqual(errors, [])
            self.assertEqual(results, [[]])

            rows = conn1.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["version"], 1)
            self.assertEqual(rows[0]["checksum"], checksum)
        finally:
            conn1.close()


if __name__ == "__main__":
    unittest.main()
