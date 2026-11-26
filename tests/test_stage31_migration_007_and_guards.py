"""Tests for Migration 007 upgrade, AR-P3-004 pre-flight atomic rollback, and direct SQL bypass guards."""

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.connection import create_connection
from src.db.crypto import encrypt_payload
from src.db.migrations import MIGRATIONS_DIR, MigrationPreflightError, get_applied_migrations, run_migrations
from src.db.models import (
    ApprovalStatus,
    DeliveryMethod,
    InventoryType,
    OrderStatus,
    ProductStatus,
    ReceiptReviewStatus,
)
from src.db.repositories import CatalogRepository, InventoryRepository, OrderRepository, ReceiptRepository, UserRepository


BASELINE_001_006_CHECKSUMS = {
    1: ("001_initial_schema.sql", "1039037a63b203b270be65d0325b4950814b60d946ccea9d3a80237ea5fa517d"),
    2: ("002_provider_scoped_receipts.sql", "7ea9dd850ef69789d1943568948dfa3f0a94d4926c5cefac5cb3d1e92338c417"),
    3: ("003_inventory_and_integrity_guards.sql", "f81494473a244566503f59ed21f6a0f3abe2de0117eb65bb205c9f4f78b764a6"),
    4: ("004_order_item_denomination.sql", "beac060c3b3fa6f7f8ba8f9b5359d096d93a4d089a8c74627cfef1127c450f5a"),
    5: ("005_inventory_order_delete_restrict.sql", "1c157e36c888897e5626ab19816659aca7c710fc2ea93539626a277a72b94f72"),
    6: ("006_order_items_update_and_product_type_guards.sql", "9ac4528903fd9e00128791710ea346f58935e65c229cddc70e95949cd8d7c92e"),
}


class TestStage31Migration007AndGuards(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_env = os.environ.get("ENV")
        os.environ["ENV"] = "test"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mig_006_dir = Path(self.temp_dir.name) / "mig_006"
        self.mig_006_dir.mkdir(parents=True, exist_ok=True)
        for _ver, (fname, _) in BASELINE_001_006_CHECKSUMS.items():
            shutil.copy2(MIGRATIONS_DIR / fname, self.mig_006_dir / fname)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        if self._prev_env is None:
            os.environ.pop("ENV", None)
        else:
            os.environ["ENV"] = self._prev_env

    def _new_006_conn(self, name: str) -> sqlite3.Connection:
        db_path = os.path.join(self.temp_dir.name, name)
        conn = create_connection(db_path)
        applied = run_migrations(conn, migrations_dir=self.mig_006_dir)
        self.assertEqual(applied, [1, 2, 3, 4, 5, 6])
        return conn

    def _new_007_conn(self, name: str) -> sqlite3.Connection:
        db_path = os.path.join(self.temp_dir.name, name)
        conn = create_connection(db_path)
        applied = run_migrations(conn)
        self.assertEqual(applied, [1, 2, 3, 4, 5, 6, 7])
        return conn

    def _assert_sql_integrity_error(self, conn: sqlite3.Connection, sql: str, params=()) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(sql, params)
        if conn.in_transaction:
            conn.rollback()

    def _assert_007_rolled_back(self, conn: sqlite3.Connection) -> None:
        self.assertFalse(conn.in_transaction)
        self.assertEqual(get_applied_migrations(conn), [1, 2, 3, 4, 5, 6])
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertNotIn("order_deliveries", tables)
        self.assertNotIn("idempotency_keys", tables)
        triggers = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        }
        self.assertNotIn("trg_inventory_items_validate_status_transition", triggers)
        self.assertNotIn("trg_order_deliveries_prevent_delete", triggers)

    # ------------------------------------------------------------------
    # Baseline Checksums, Upgrade 001..006 -> 007, and Idempotency
    # ------------------------------------------------------------------

    def test_baseline_001_006_checksums_and_007_upgrade_and_legacy_paid_at_backfill(self) -> None:
        for _ver, (fname, expected_sha) in BASELINE_001_006_CHECKSUMS.items():
            raw_bytes = (MIGRATIONS_DIR / fname).read_bytes()
            actual_sha = hashlib.sha256(raw_bytes).hexdigest()
            self.assertEqual(actual_sha, expected_sha, f"Historical checksum drifted for {fname}")

        conn = self._new_006_conn("upgrade_006_to_007.db")
        try:
            user, _ = UserRepository(conn).get_or_create_user("legacy_paid_u", username="lp")
            prod = CatalogRepository(conn).create_voucher_product("Legacy V", "TR", "TRY", status=ProductStatus.APPROVED)
            denom = CatalogRepository(conn).add_voucher_denomination(prod.id, "TR", "TRY", 1000, 5000)
            order = OrderRepository(conn).create_order(
                user.id,
                5000,
                "RUB",
                [{"product_id": prod.id, "denomination_id": denom.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            # Legacy 006 DB could hold PAID with NULL paid_at
            conn.execute("UPDATE orders SET status = 'PAID', paid_at = NULL WHERE id = ?", (order.id,))
            conn.commit()

            applied = run_migrations(conn)
            self.assertEqual(applied, [7])
            self.assertEqual(get_applied_migrations(conn), [1, 2, 3, 4, 5, 6, 7])

            row = conn.execute("SELECT paid_at FROM orders WHERE id = ?", (order.id,)).fetchone()
            self.assertIsNotNone(row["paid_at"])

            # Idempotent re-run applies nothing
            self.assertEqual(run_migrations(conn), [])
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # AR-P3-004: All 9 Migration 007 Pre-Flight Checks & Atomic Rollback
    # ------------------------------------------------------------------

    def test_ar_p3_004_preflight_voucher_missing_and_invalid_product_details(self) -> None:
        # 1a. VOUCHER product without voucher_details
        c1 = self._new_006_conn("pf_voucher_missing.db")
        try:
            c1.execute("INSERT INTO products (product_type, display_name, status) VALUES ('VOUCHER', 'Broken V', 'DRAFT')")
            c1.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c1)
            self.assertEqual(ctx.exception.migration_version, 7)
            self.assertEqual(ctx.exception.check_name, "chk_007_voucher_missing_or_malformed_details")
            self._assert_007_rolled_back(c1)
        finally:
            c1.close()

        # 1b. voucher_details attached to FIXED_PRODUCT
        c2 = self._new_006_conn("pf_voucher_details_wrong_type.db")
        try:
            fp = CatalogRepository(c2).create_fixed_product(
                "FP",
                10000,
                DeliveryMethod.CODE,
                "SUP",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            c2.execute("INSERT INTO voucher_details (product_id, region, currency) VALUES (?, 'TR', 'TRY')", (fp.id,))
            c2.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c2)
            self.assertEqual(ctx.exception.migration_version, 7)
            self.assertEqual(ctx.exception.check_name, "chk_007_voucher_details_invalid_product")
            self._assert_007_rolled_back(c2)
        finally:
            c2.close()

    def test_ar_p3_004_preflight_fixed_product_missing_and_invalid_details(self) -> None:
        # 2a. FIXED_PRODUCT missing fixed_product_details
        c1 = self._new_006_conn("pf_fp_missing.db")
        try:
            c1.execute("INSERT INTO products (product_type, display_name, status) VALUES ('FIXED_PRODUCT', 'Broken FP', 'DRAFT')")
            c1.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c1)
            self.assertEqual(ctx.exception.migration_version, 7)
            self.assertEqual(ctx.exception.check_name, "chk_007_fixed_product_missing_or_malformed_details")
            self._assert_007_rolled_back(c1)
        finally:
            c1.close()

        # 2b. FIXED_PRODUCT with whitespace supply_method
        c1b = self._new_006_conn("pf_fp_blank_supply.db")
        try:
            c1b.execute("INSERT INTO products (product_type, display_name, status) VALUES ('FIXED_PRODUCT', 'Blank Sup', 'DRAFT')")
            pid = c1b.execute("SELECT last_insert_rowid()").fetchone()[0]
            c1b.execute(
                "INSERT INTO fixed_product_details (product_id, price_rub_kopecks, delivery_method, supply_method, approval_status) "
                "VALUES (?, 1000, 'CODE', '   ', 'DRAFT')",
                (pid,),
            )
            c1b.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c1b)
            self.assertEqual(ctx.exception.check_name, "chk_007_fixed_product_missing_or_malformed_details")
            self._assert_007_rolled_back(c1b)
        finally:
            c1b.close()

        # 2c. fixed_product_details attached to VOUCHER
        c2 = self._new_006_conn("pf_fp_details_wrong_type.db")
        try:
            v = CatalogRepository(c2).create_voucher_product("V", "TR", "TRY")
            c2.execute(
                "INSERT INTO fixed_product_details (product_id, price_rub_kopecks, delivery_method, supply_method, approval_status) "
                "VALUES (?, 1000, 'CODE', 'S', 'DRAFT')",
                (v.id,),
            )
            c2.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c2)
            self.assertEqual(ctx.exception.check_name, "chk_007_fixed_details_invalid_product")
            self._assert_007_rolled_back(c2)
        finally:
            c2.close()

    def test_ar_p3_004_preflight_voucher_denom_and_orphans_and_mixed_and_cancelled(self) -> None:
        # 3. voucher_denominations bound to FIXED_PRODUCT or mismatched region/currency
        c3 = self._new_006_conn("pf_denom_bad.db")
        try:
            v = CatalogRepository(c3).create_voucher_product("V", "TR", "TRY")
            c3.execute(
                "INSERT INTO voucher_denominations (product_id, region, currency, face_value_minor, cost_rub_kopecks) "
                "VALUES (?, 'US', 'USD', 1000, 9000)",
                (v.id,),
            )
            c3.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c3)
            self.assertEqual(ctx.exception.check_name, "chk_007_voucher_denom_invalid_product_or_details")
            self._assert_007_rolled_back(c3)
        finally:
            c3.close()

        # 4a. Orphaned inventory_items via LEFT JOIN products (foreign_keys=OFF outside tx)
        c4 = self._new_006_conn("pf_orphan_inv.db")
        try:
            c4.execute("PRAGMA foreign_keys = OFF;")
            c4.execute(
                "INSERT INTO inventory_items (product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status) "
                "VALUES (999999, NULL, 'FIXED_CODE', ?, 'deadbeef_orphan_inv', 'AVAILABLE')",
                (encrypt_payload("CODE-ORPHAN-1"),),
            )
            c4.commit()
            c4.execute("PRAGMA foreign_keys = ON;")
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c4)
            self.assertEqual(ctx.exception.check_name, "chk_007_orphaned_inventory_product")
            self._assert_007_rolled_back(c4)
        finally:
            c4.close()

        # 4b. Orphaned order_items via LEFT JOIN products (foreign_keys=OFF must run outside tx!)
        c4b = self._new_006_conn("pf_orphan_oi.db")
        try:
            u, _ = UserRepository(c4b).get_or_create_user("u_orph_oi")
            c4b.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PENDING_PAYMENT', 1000, 'RUB')",
                (u.id,),
            )
            oid = c4b.execute("SELECT last_insert_rowid()").fetchone()[0]
            c4b.commit()
            c4b.execute("PRAGMA foreign_keys = OFF;")
            c4b.execute(
                "INSERT INTO order_items (order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                "VALUES (?, 888888, NULL, 'FIXED_PRODUCT', 1, 1000, 'RUB')",
                (oid,),
            )
            c4b.commit()
            c4b.execute("PRAGMA foreign_keys = ON;")
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c4b)
            self.assertEqual(ctx.exception.check_name, "chk_007_orphaned_order_item_product")
            self._assert_007_rolled_back(c4b)
        finally:
            c4b.close()

        # 5. Mixed order combining inventory-backed + MANUAL_REVIEW items
        c5 = self._new_006_conn("pf_mixed.db")
        try:
            cat = CatalogRepository(c5)
            u, _ = UserRepository(c5).get_or_create_user("u_mix")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            mr = cat.create_fixed_product(
                "MR",
                2000,
                DeliveryMethod.MANUAL_REVIEW,
                "MAN",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            c5.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PENDING_PAYMENT', 7000, 'RUB')",
                (u.id,),
            )
            oid = c5.execute("SELECT last_insert_rowid()").fetchone()[0]
            c5.execute(
                "INSERT INTO order_items (order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                "VALUES (?, ?, ?, 'VOUCHER', 1, 5000, 'RUB')",
                (oid, v.id, d.id),
            )
            c5.execute(
                "INSERT INTO order_items (order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                "VALUES (?, ?, NULL, 'FIXED_PRODUCT', 1, 2000, 'RUB')",
                (oid, mr.id),
            )
            c5.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c5)
            self.assertEqual(ctx.exception.check_name, "chk_007_mixed_inventory_and_manual_orders")
            self._assert_007_rolled_back(c5)
        finally:
            c5.close()

        # 6. CANCELLED order holding RESERVED inventory
        c6 = self._new_006_conn("pf_cancelled_res.db")
        try:
            cat = CatalogRepository(c6)
            u, _ = UserRepository(c6).get_or_create_user("u_can_res")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            InventoryRepository(c6).import_items(v.id, ["V-CAN-1"], InventoryType.VOUCHER_CODE, d.id)
            o = OrderRepository(c6).create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            InventoryRepository(c6).reserve_items_for_order(o.id, v.id, 1, d.id)
            c6.execute("UPDATE orders SET status = 'CANCELLED' WHERE id = ?", (o.id,))
            c6.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c6)
            self.assertEqual(ctx.exception.check_name, "chk_007_cancelled_order_with_reserved_inventory")
            self._assert_007_rolled_back(c6)
        finally:
            c6.close()

    def test_ar_p3_004_preflight_completed_invariants_multiple_accepted_and_quarantine(self) -> None:
        # 7a. COMPLETED order missing SOLD inventory
        c7a = self._new_006_conn("pf_comp_missing_sold.db")
        try:
            cat = CatalogRepository(c7a)
            u, _ = UserRepository(c7a).get_or_create_user("u_c7a")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            o = OrderRepository(c7a).create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            c7a.execute("UPDATE orders SET status = 'COMPLETED' WHERE id = ?", (o.id,))
            c7a.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c7a)
            self.assertEqual(ctx.exception.check_name, "chk_007_completed_order_inventory_invariant")
            self._assert_007_rolled_back(c7a)
        finally:
            c7a.close()

        # 7b. COMPLETED MANUAL_REVIEW order holding stray SOLD inventory
        c7b = self._new_006_conn("pf_comp_stray_sold.db")
        try:
            cat = CatalogRepository(c7b)
            u, _ = UserRepository(c7b).get_or_create_user("u_c7b")
            mr = cat.create_fixed_product(
                "MR",
                2000,
                DeliveryMethod.MANUAL_REVIEW,
                "MAN",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            o = OrderRepository(c7b).create_order(
                u.id, 2000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 2000}]
            )
            c7b.execute("UPDATE orders SET status = 'COMPLETED' WHERE id = ?", (o.id,))
            c7b.execute(
                "INSERT INTO inventory_items (product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id) "
                "VALUES (?, NULL, 'FIXED_CODE', ?, 'stray_sold_fp_1', 'SOLD', ?)",
                (mr.id, encrypt_payload("STRAY-1"), o.id),
            )
            c7b.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c7b)
            self.assertEqual(ctx.exception.check_name, "chk_007_completed_order_inventory_invariant")
            self._assert_007_rolled_back(c7b)
        finally:
            c7b.close()

        # 7c. COMPLETED inventory-backed order holding stray extra SOLD inventory from unordered denomination
        c7c = self._new_006_conn("pf_comp_extra_stray.db")
        try:
            cat = CatalogRepository(c7c)
            u, _ = UserRepository(c7c).get_or_create_user("u_c7c")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d1 = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            d2 = cat.add_voucher_denomination(v.id, "TR", "TRY", 2000, 10000)
            InventoryRepository(c7c).import_items(v.id, ["V-D1-1"], InventoryType.VOUCHER_CODE, d1.id)
            InventoryRepository(c7c).import_items(v.id, ["V-D2-STRAY"], InventoryType.VOUCHER_CODE, d2.id)
            o = OrderRepository(c7c).create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d1.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            InventoryRepository(c7c).reserve_items_for_order(o.id, v.id, 1, d1.id)
            c7c.execute("UPDATE orders SET status = 'COMPLETED' WHERE id = ?", (o.id,))
            c7c.execute("UPDATE inventory_items SET status = 'SOLD' WHERE reserved_order_id = ?", (o.id,))
            c7c.execute(
                "INSERT INTO order_items (order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                "VALUES (?, ?, ?, 'VOUCHER', 1, 10000, 'RUB')",
                (o.id, v.id, d2.id),
            )
            c7c.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c7c)
            self.assertEqual(ctx.exception.check_name, "chk_007_completed_order_inventory_invariant")
            self._assert_007_rolled_back(c7c)
        finally:
            c7c.close()

        # 8. Multiple ACCEPTED receipts on the same order
        c8 = self._new_006_conn("pf_multi_accepted.db")
        try:
            cat = CatalogRepository(c8)
            u, _ = UserRepository(c8).get_or_create_user("u_c8")
            mr = cat.create_fixed_product(
                "MR",
                2000,
                DeliveryMethod.MANUAL_REVIEW,
                "MAN",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            o = OrderRepository(c8).create_order(
                u.id, 2000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 2000}]
            )
            rr = ReceiptRepository(c8)
            r1 = rr.submit_receipt(o.id, u.id, b"receipt_1", provider_tx_id="tx1")
            r2 = rr.submit_receipt(o.id, u.id, b"receipt_2", provider_tx_id="tx2")
            c8.execute("UPDATE payment_receipts SET review_status = 'ACCEPTED' WHERE id IN (?, ?)", (r1.id, r2.id))
            c8.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c8)
            self.assertEqual(ctx.exception.check_name, "chk_007_multiple_accepted_receipts")
            self._assert_007_rolled_back(c8)
        finally:
            c8.close()

        # 9. QUARANTINED item with invalid reserved_order_id pointing to PENDING_PAYMENT order
        c9 = self._new_006_conn("pf_quarantine_bad_link.db")
        try:
            cat = CatalogRepository(c9)
            u, _ = UserRepository(c9).get_or_create_user("u_c9")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            o = OrderRepository(c9).create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            InventoryRepository(c9).import_items(v.id, ["Q-BAD-1"], InventoryType.VOUCHER_CODE, d.id)
            c9.execute("UPDATE inventory_items SET status = 'QUARANTINED', reserved_order_id = ?", (o.id,))
            c9.commit()
            with self.assertRaises(MigrationPreflightError) as ctx:
                run_migrations(c9)
            self.assertEqual(ctx.exception.check_name, "chk_007_quarantined_or_disabled_invalid_order_link")
            self._assert_007_rolled_back(c9)
        finally:
            c9.close()

    # ------------------------------------------------------------------
    # Direct SQL Bypass Tests on Migrated 007 Schema
    # ------------------------------------------------------------------

    def test_direct_sql_sold_immutability_and_quarantine_guards(self) -> None:
        conn = self._new_007_conn("sql_guards_inv.db")
        try:
            cat = CatalogRepository(conn)
            u, _ = UserRepository(conn).get_or_create_user("sql_u1")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d1 = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            d2 = cat.add_voucher_denomination(v.id, "TR", "TRY", 2000, 10000)
            inv = InventoryRepository(conn)
            inv.import_items(v.id, ["SOLD-SQL-1", "AVAIL-SQL-2", "RES-SQL-3"], InventoryType.VOUCHER_CODE, d1.id)

            ord_repo = OrderRepository(conn)
            o1 = ord_repo.create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d1.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            sold_ids = inv.reserve_items_for_order(o1.id, v.id, 1, d1.id)
            ord_repo.transition_order_status(o1.id, OrderStatus.PAYMENT_REVIEW)
            ord_repo.transition_order_status(o1.id, OrderStatus.PAID)
            inv.finalize_order_sale(o1.id)
            sold_id = sold_ids[0]

            avail_row = conn.execute("SELECT id FROM inventory_items WHERE status = 'AVAILABLE' ORDER BY id ASC LIMIT 1").fetchone()
            avail_id = avail_row["id"]

            # OD-06: Direct INSERT as SOLD blocked
            self._assert_sql_integrity_error(
                conn,
                "INSERT INTO inventory_items (product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id) "
                "VALUES (?, ?, 'VOUCHER_CODE', ?, 'direct_sold_ins', 'SOLD', ?)",
                (v.id, d1.id, encrypt_payload("DIRECT-SOLD"), o1.id),
            )

            # OD-06: AVAILABLE -> SOLD blocked
            self._assert_sql_integrity_error(
                conn,
                "UPDATE inventory_items SET status = 'SOLD', reserved_order_id = ? WHERE id = ?",
                (o1.id, avail_id),
            )

            # OD-06: SOLD immutable for status, reserved_order_id, product_id, denomination_id, inventory_type, encrypted_payload
            for sql, params in [
                ("UPDATE inventory_items SET status = 'AVAILABLE', reserved_order_id = NULL WHERE id = ?", (sold_id,)),
                ("UPDATE inventory_items SET status = 'QUARANTINED' WHERE id = ?", (sold_id,)),
                ("UPDATE inventory_items SET reserved_order_id = NULL WHERE id = ?", (sold_id,)),
                ("UPDATE inventory_items SET denomination_id = ? WHERE id = ?", (d2.id, sold_id)),
                ("UPDATE inventory_items SET encrypted_payload = ? WHERE id = ?", (encrypt_payload("TAMPERED"), sold_id)),
            ]:
                self._assert_sql_integrity_error(conn, sql, params)

            # Rotating fingerprint on SOLD item MUST still be permitted!
            rotated = inv.rotate_fingerprint_pepper(b"new_pepper_for_sold_rotation_32b")
            self.assertEqual(rotated, 3)

            # AR-P3-007: Direct SQL quarantine/disabled bypasses blocked
            self._assert_sql_integrity_error(
                conn,
                "INSERT INTO inventory_items (product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id) "
                "VALUES (?, ?, 'VOUCHER_CODE', ?, 'bad_q_ins', 'QUARANTINED', ?)",
                (v.id, d1.id, encrypt_payload("Q-INS"), o1.id),
            )
            self._assert_sql_integrity_error(
                conn,
                "UPDATE inventory_items SET status = 'QUARANTINED', reserved_order_id = ? WHERE id = ?",
                (o1.id, avail_id),
            )

            # Reserve another item on a PENDING_PAYMENT order and verify RESERVED -> QUARANTINED preserving link is blocked
            o2 = ord_repo.create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d1.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            res_id = inv.reserve_items_for_order(o2.id, v.id, 1, d1.id)[0]
            self._assert_sql_integrity_error(
                conn,
                "UPDATE inventory_items SET status = 'QUARANTINED' WHERE id = ?",
                (res_id,),
            )

            # Transition o2 to REFUND_REVIEW so forensic link preservation is permitted
            ord_repo.transition_order_status(o2.id, OrderStatus.PAYMENT_REVIEW)
            ord_repo.transition_order_status(o2.id, OrderStatus.PAID)
            ord_repo.transition_order_status(o2.id, OrderStatus.REFUND_REVIEW)

            q_item = inv.quarantine_item(res_id, preserve_forensic_link=True)
            self.assertEqual(q_item.reserved_order_id, o2.id)

            # Direct SQL restore QUARANTINED -> AVAILABLE while forensic link set is BLOCKED
            self._assert_sql_integrity_error(conn, "UPDATE inventory_items SET status = 'AVAILABLE' WHERE id = ?", (res_id,))
            self._assert_sql_integrity_error(conn, "UPDATE inventory_items SET status = 'RESERVED' WHERE id = ?", (res_id,))
            self._assert_sql_integrity_error(conn, "UPDATE inventory_items SET reserved_order_id = ? WHERE id = ?", (o1.id, res_id))

            # Clear forensic link then restore succeeds
            inv.clear_forensic_order_link(res_id)
            restored = inv.restore_to_available(res_id)
            self.assertEqual(restored.status.value, "AVAILABLE")
            self.assertIsNone(restored.reserved_order_id)
        finally:
            conn.close()

    def test_direct_sql_payment_order_audit_delivery_and_catalog_guards(self) -> None:
        conn = self._new_007_conn("sql_guards_orders.db")
        try:
            cat = CatalogRepository(conn)
            u, _ = UserRepository(conn).get_or_create_user("sql_u2")
            v = cat.create_voucher_product("V", "TR", "TRY", status=ProductStatus.APPROVED)
            d = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            mr = cat.create_fixed_product(
                "MR",
                3000,
                DeliveryMethod.MANUAL_REVIEW,
                "MAN",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            InventoryRepository(conn).import_items(v.id, ["V-ORD-1"], InventoryType.VOUCHER_CODE, d.id)

            ord_repo = OrderRepository(conn)
            rcpt_repo = ReceiptRepository(conn)

            # Order with unaccepted receipt cannot SQL-UPDATE from PAYMENT_REVIEW -> PAID (AR-P3-005)
            o_rcpt = ord_repo.create_order(
                u.id, 3000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 3000}]
            )
            r1 = rcpt_repo.submit_receipt(o_rcpt.id, u.id, b"r_pending_1", provider_tx_id="txp1")
            r2 = rcpt_repo.submit_receipt(o_rcpt.id, u.id, b"r_pending_2", provider_tx_id="txp2")
            ord_repo.transition_order_status(o_rcpt.id, OrderStatus.PAYMENT_REVIEW)

            self._assert_sql_integrity_error(
                conn,
                "UPDATE orders SET status = 'PAID', paid_at = CURRENT_TIMESTAMP WHERE id = ?",
                (o_rcpt.id,),
            )

            # Accept r1; second ACCEPTED blocked by unique partial index
            conn.execute("UPDATE payment_receipts SET review_status = 'ACCEPTED' WHERE id = ?", (r1.id,))
            conn.commit()
            self._assert_sql_integrity_error(
                conn,
                "UPDATE payment_receipts SET review_status = 'ACCEPTED' WHERE id = ?",
                (r2.id,),
            )
            # Terminal receipt cannot change review_status
            self._assert_sql_integrity_error(
                conn,
                "UPDATE payment_receipts SET review_status = 'REJECTED' WHERE id = ?",
                (r1.id,),
            )

            # paid_at IS NOT NULL guard on direct SQL UPDATE and INSERT
            self._assert_sql_integrity_error(
                conn,
                "UPDATE orders SET status = 'PAID', paid_at = NULL WHERE id = ?",
                (o_rcpt.id,),
            )
            self._assert_sql_integrity_error(
                conn,
                "INSERT INTO orders (user_id, status, total_amount_minor, currency, paid_at) VALUES (?, 'PAID', 1000, 'RUB', NULL)",
                (u.id,),
            )

            # Valid SQL transition with ACCEPTED receipt and non-NULL paid_at succeeds, and clearing paid_at afterward is blocked
            conn.execute("UPDATE orders SET status = 'PAID', paid_at = CURRENT_TIMESTAMP WHERE id = ?", (o_rcpt.id,))
            conn.commit()
            self._assert_sql_integrity_error(
                conn,
                "UPDATE orders SET paid_at = NULL WHERE id = ?",
                (o_rcpt.id,),
            )

            # Legacy compatibility: PAYMENT_REVIEW -> PAID with 0 receipts still allowed if paid_at is set
            o_legacy = ord_repo.create_order(
                u.id, 3000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 3000}]
            )
            ord_repo.transition_order_status(o_legacy.id, OrderStatus.PAYMENT_REVIEW)
            conn.execute("UPDATE orders SET status = 'PAID', paid_at = CURRENT_TIMESTAMP WHERE id = ?", (o_legacy.id,))
            conn.commit()

            # Mixed order SQL bypass blocked (OD-04) & voucher_denominations on FIXED_PRODUCT blocked
            o_v = ord_repo.create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
            self._assert_sql_integrity_error(
                conn,
                "INSERT INTO order_items (order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                "VALUES (?, ?, NULL, 'FIXED_PRODUCT', 1, 3000, 'RUB')",
                (o_v.id, mr.id),
            )
            self._assert_sql_integrity_error(
                conn,
                "INSERT INTO voucher_denominations (product_id, region, currency, face_value_minor, cost_rub_kopecks) "
                "VALUES (?, 'TR', 'TRY', 500, 1000)",
                (mr.id,),
            )
            # Cancelled order freezes order_items, but DELETE FROM orders ON DELETE CASCADE with 0 inventory/receipts succeeds
            ord_repo.transition_order_status(o_v.id, OrderStatus.CANCELLED)
            self._assert_sql_integrity_error(
                conn,
                "DELETE FROM order_items WHERE order_id = ?",
                (o_v.id,),
            )
            conn.execute("DELETE FROM orders WHERE id = ?", (o_v.id,))
            conn.commit()

            # audit_events append-only SQL guard
            ev_id = conn.execute("SELECT id FROM audit_events ORDER BY id ASC LIMIT 1").fetchone()["id"]
            self._assert_sql_integrity_error(conn, "UPDATE audit_events SET event_type = 'TAMPERED' WHERE id = ?", (ev_id,))
            self._assert_sql_integrity_error(conn, "DELETE FROM audit_events WHERE id = ?", (ev_id,))

            # order_deliveries direct SQL guards (AR-P3-001)
            conn.execute(
                "INSERT INTO order_deliveries (order_id, channel, delivery_kind, status, attempt_no, claim_token, claimed_by, lease_expires_at) "
                "VALUES (?, 'TELEGRAM', 'INITIAL', 'DISPATCHING', 1, 'tok-sql-1', 'w1', '2099-01-01 00:00:00')",
                (o_rcpt.id,),
            )
            deliv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()

            # Second active DISPATCHING on same order blocked by partial unique index
            self._assert_sql_integrity_error(
                conn,
                "INSERT INTO order_deliveries (order_id, channel, delivery_kind, status, attempt_no, claim_token, claimed_by, lease_expires_at) "
                "VALUES (?, 'TELEGRAM', 'REDELIVERY', 'DISPATCHING', 2, 'tok-sql-2', 'w2', '2099-01-01 00:00:00')",
                (o_rcpt.id,),
            )
            # DELETE, immutable fields, and SENT with NULL delivered_at blocked
            self._assert_sql_integrity_error(conn, "DELETE FROM order_deliveries WHERE id = ?", (deliv_id,))
            self._assert_sql_integrity_error(
                conn, "UPDATE order_deliveries SET claim_token = 'tampered' WHERE id = ?", (deliv_id,)
            )
            self._assert_sql_integrity_error(
                conn, "UPDATE order_deliveries SET status = 'SENT', delivered_at = NULL WHERE id = ?", (deliv_id,)
            )
            # Mark FAILED and confirm FAILED cannot transition or edit error_code
            conn.execute("UPDATE order_deliveries SET status = 'FAILED', error_code = 'NET_ERR' WHERE id = ?", (deliv_id,))
            conn.commit()
            self._assert_sql_integrity_error(
                conn,
                "UPDATE order_deliveries SET status = 'SENT', delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (deliv_id,),
            )
            self._assert_sql_integrity_error(
                conn,
                "UPDATE order_deliveries SET error_code = 'TAMPERED' WHERE id = ?",
                (deliv_id,),
            )
        finally:
            conn.close()

    def test_clean_install_001_to_007_and_idempotency(self) -> None:
        conn = self._new_007_conn("clean_install_007.db")
        try:
            self.assertEqual(get_applied_migrations(conn), [1, 2, 3, 4, 5, 6, 7])
            self.assertEqual(run_migrations(conn), [])
            tables = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.assertIn("order_deliveries", tables)
            self.assertIn("idempotency_keys", tables)
            triggers = {
                r["name"]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
            }
            self.assertIn("trg_order_items_freeze_non_pending_insert", triggers)
            self.assertIn("trg_order_items_freeze_non_pending_update", triggers)
            self.assertIn("trg_order_items_freeze_non_pending_delete", triggers)
        finally:
            conn.close()

    def test_sg_p31_002_order_items_frozen_outside_pending_payment_and_delivery_error_code_cleared(self) -> None:
        conn = self._new_007_conn("sg_p31_002_freeze.db")
        try:
            cat = CatalogRepository(conn)
            u, _ = UserRepository(conn).get_or_create_user("freeze_u1")
            v = cat.create_voucher_product("V Freeze", "TR", "TRY", status=ProductStatus.APPROVED)
            d = cat.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
            mr = cat.create_fixed_product(
                "MR Freeze",
                3000,
                DeliveryMethod.MANUAL_REVIEW,
                "MAN",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            inv = InventoryRepository(conn)
            inv.import_items(v.id, ["FRZ-1", "FRZ-2", "FRZ-3", "FRZ-4"], InventoryType.VOUCHER_CODE, d.id)
            ord_repo = OrderRepository(conn)
            rcpt_repo = ReceiptRepository(conn)

            pending_target = ord_repo.create_order(
                u.id, 3000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 3000}]
            )
            pending_oi_id = ord_repo.get_order_items(pending_target.id)[0].id

            # Build orders in all 6 non-PENDING_PAYMENT states
            o_pr = ord_repo.create_order(
                u.id, 3000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 3000}]
            )
            ord_repo.transition_order_status(o_pr.id, OrderStatus.PAYMENT_REVIEW)

            o_paid = ord_repo.create_order(
                u.id, 5000, "RUB", [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}]
            )
            inv.reserve_items_for_order(o_paid.id, v.id, 1, d.id)
            r_paid = rcpt_repo.submit_receipt(o_paid.id, u.id, b"r_p", provider_tx_id="tx_paid_frz")
            rcpt_repo.review_receipt(r_paid.id, ReceiptReviewStatus.ACCEPTED)
            ord_repo.transition_order_status(o_paid.id, OrderStatus.PAYMENT_REVIEW)
            ord_repo.transition_order_status(o_paid.id, OrderStatus.PAID)

            o_ful = ord_repo.create_order(
                u.id, 5000, "RUB", [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}]
            )
            inv.reserve_items_for_order(o_ful.id, v.id, 1, d.id)
            ord_repo.transition_order_status(o_ful.id, OrderStatus.PAYMENT_REVIEW)
            ord_repo.transition_order_status(o_ful.id, OrderStatus.PAID)
            ord_repo.transition_order_status(o_ful.id, OrderStatus.FULFILLING)

            o_comp = ord_repo.create_order(
                u.id, 5000, "RUB", [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}]
            )
            inv.reserve_items_for_order(o_comp.id, v.id, 1, d.id)
            ord_repo.transition_order_status(o_comp.id, OrderStatus.PAYMENT_REVIEW)
            ord_repo.transition_order_status(o_comp.id, OrderStatus.PAID)
            inv.finalize_order_sale(o_comp.id)
            ord_repo.transition_order_status(o_comp.id, OrderStatus.FULFILLING)
            ord_repo.transition_order_status(o_comp.id, OrderStatus.COMPLETED)

            o_ref = ord_repo.create_order(
                u.id, 3000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 3000}]
            )
            ord_repo.transition_order_status(o_ref.id, OrderStatus.PAYMENT_REVIEW)
            ord_repo.transition_order_status(o_ref.id, OrderStatus.PAID)
            ord_repo.transition_order_status(o_ref.id, OrderStatus.REFUND_REVIEW)

            o_can = ord_repo.create_order(
                u.id, 3000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 3000}]
            )
            ord_repo.transition_order_status(o_can.id, OrderStatus.CANCELLED)

            non_pending_cases = [
                (o_pr.id, mr.id, None, "FIXED_PRODUCT", 3000),
                (o_paid.id, v.id, d.id, "VOUCHER", 5000),
                (o_ful.id, v.id, d.id, "VOUCHER", 5000),
                (o_comp.id, v.id, d.id, "VOUCHER", 5000),
                (o_ref.id, mr.id, None, "FIXED_PRODUCT", 3000),
                (o_can.id, mr.id, None, "FIXED_PRODUCT", 3000),
            ]
            for oid, pid, did, ptype, price in non_pending_cases:
                oi_id = ord_repo.get_order_items(oid)[0].id
                # Direct INSERT blocked
                self._assert_sql_integrity_error(
                    conn,
                    "INSERT INTO order_items (order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency) "
                    "VALUES (?, ?, ?, ?, 1, ?, 'RUB')",
                    (oid, pid, did, ptype, price),
                )
                # Direct UPDATE of quantity/price blocked
                self._assert_sql_integrity_error(
                    conn,
                    "UPDATE order_items SET quantity = 9, unit_price_minor = 1 WHERE id = ?",
                    (oi_id,),
                )
                # Move non-pending item -> PENDING_PAYMENT order blocked (checks OLD.order_id)
                self._assert_sql_integrity_error(
                    conn,
                    "UPDATE order_items SET order_id = ? WHERE id = ?",
                    (pending_target.id, oi_id),
                )
                # Move PENDING_PAYMENT item -> non-pending order blocked (checks NEW.order_id!)
                self._assert_sql_integrity_error(
                    conn,
                    "UPDATE order_items SET order_id = ? WHERE id = ?",
                    (oid, pending_oi_id),
                )
                # Direct DELETE blocked
                self._assert_sql_integrity_error(
                    conn,
                    "DELETE FROM order_items WHERE id = ?",
                    (oi_id,),
                )

            # Move between two non-pending orders blocked
            self._assert_sql_integrity_error(
                conn,
                "UPDATE order_items SET order_id = ? WHERE id = ?",
                (o_ref.id, ord_repo.get_order_items(o_pr.id)[0].id),
            )

            # PENDING_PAYMENT allows modification, and ON DELETE CASCADE cleanup on CANCELLED without inventory/receipts works
            conn.execute("UPDATE order_items SET quantity = 2 WHERE id = ?", (pending_oi_id,))
            conn.commit()
            self.assertEqual(ord_repo.get_order_items(pending_target.id)[0].quantity, 2)
            conn.execute("DELETE FROM orders WHERE id = ?", (o_can.id,))
            conn.commit()

            # Direct SQL trigger guard on order_deliveries SENT error_code IS NULL
            conn.execute(
                "INSERT INTO order_deliveries (order_id, channel, delivery_kind, status, attempt_no, claim_token, claimed_by, lease_expires_at, error_code) "
                "VALUES (?, 'TELEGRAM', 'INITIAL', 'UNCERTAIN', 1, 'tok-err-1', 'w1', '2099-01-01 00:00:00', 'LEASE_EXPIRED')",
                (o_comp.id,),
            )
            d_unc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()
            # Failing to clear error_code when transitioning UNCERTAIN -> SENT is rejected by DB trigger
            self._assert_sql_integrity_error(
                conn,
                "UPDATE order_deliveries SET status = 'SENT', delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (d_unc_id,),
            )
            # Clearing error_code = NULL passes
            conn.execute(
                "UPDATE order_deliveries SET status = 'SENT', error_code = NULL, delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (d_unc_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_sg_p31_003_idempotency_keys_sql_check_enforces_completed_metadata(self) -> None:
        conn = self._new_007_conn("sg_p31_003_sql_check.db")
        try:
            h = hashlib.sha256(b"sql-check").hexdigest()
            # IN_PROGRESS without response metadata is permitted
            conn.execute(
                "INSERT INTO idempotency_keys (scope, actor_key, idempotency_key, request_hash, status, owner_token, stale_after, expires_at) "
                "VALUES ('CREATE_ORDER', 'u:1', 'k-sql', ?, 'IN_PROGRESS', 'tok-1', '2099-01-01 00:01:00', '2099-01-02 00:00:00')",
                (h,),
            )
            conn.commit()

            invalid_completed_cases = [
                (None, 10),
                ("", 10),
                ("   ", 10),
                ("ORDER", None),
                ("ORDER", 0),
                ("ORDER", -1),
                ("ORDER", "not_int"),
                ("ORDER", 1.5),
            ]
            for idx, (etype, eid) in enumerate(invalid_completed_cases):
                self._assert_sql_integrity_error(
                    conn,
                    "UPDATE idempotency_keys SET status = 'COMPLETED', response_entity_type = ?, response_entity_id = ? "
                    "WHERE scope = 'CREATE_ORDER' AND actor_key = 'u:1' AND idempotency_key = 'k-sql'",
                    (etype, eid),
                )
                self._assert_sql_integrity_error(
                    conn,
                    "INSERT INTO idempotency_keys (scope, actor_key, idempotency_key, request_hash, status, owner_token, response_entity_type, response_entity_id, stale_after, expires_at) "
                    "VALUES ('CREATE_ORDER', 'u:1', ?, ?, 'COMPLETED', 'tok-bad', ?, ?, '2099-01-01 00:01:00', '2099-01-02 00:00:00')",
                    (f"k-ins-{idx}", h, etype, eid),
                )

            # Valid COMPLETED succeeds
            conn.execute(
                "UPDATE idempotency_keys SET status = 'COMPLETED', response_entity_type = 'ORDER', response_entity_id = 42 "
                "WHERE scope = 'CREATE_ORDER' AND actor_key = 'u:1' AND idempotency_key = 'k-sql'"
            )
            conn.commit()
            row = conn.execute(
                "SELECT status, response_entity_type, response_entity_id FROM idempotency_keys WHERE idempotency_key = 'k-sql'"
            ).fetchone()
            self.assertEqual(row["status"], "COMPLETED")
            self.assertEqual(row["response_entity_type"], "ORDER")
            self.assertEqual(row["response_entity_id"], 42)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
