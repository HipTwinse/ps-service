"""Regression tests for Security Gate Phase 2 audit remediation findings."""

import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.connection import create_connection, transaction
from src.db.migrations import (
    discover_migration_files,
    ensure_migration_table,
    get_applied_migrations,
    run_migrations,
)
from src.db.models import (
    ALLOWED_INVENTORY_TYPES_BY_PRODUCT_TYPE,
    ApprovalStatus,
    ConflictError,
    DeliveryMethod,
    DuplicateReceiptError,
    InsufficientInventoryError,
    InvalidStateTransitionError,
    InventoryStatus,
    InventoryType,
    NotFoundError,
    OrderStatus,
    ProductStatus,
    ProductType,
    UserStatus,
    ValidationError,
    validate_inventory_type_for_product,
)
from src.db.repositories import (
    CatalogRepository,
    InventoryRepository,
    OrderRepository,
    ReceiptRepository,
    UserRepository,
)


class TestPhase2AuditRemediation(unittest.TestCase):
    """Comprehensive regression tests for P1/P2 audit findings."""

    def setUp(self):
        os.environ["APP_ENV"] = "test"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_audit_remed.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.catalog_repo = CatalogRepository(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.inv_repo = InventoryRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)
        self.receipt_repo = ReceiptRepository(self.conn)

        self.user_a, _ = self.user_repo.get_or_create_user("tg_user_a", "user_a")
        self.user_b, _ = self.user_repo.get_or_create_user("tg_user_b", "user_b")

        self.voucher_a = self.catalog_repo.create_voucher_product(
            display_name="PSN TR Card",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.denom_a = self.catalog_repo.add_voucher_denomination(
            product_id=self.voucher_a.id,
            region="TR",
            currency="TRY",
            face_value_minor=25000,
            cost_rub_kopecks=80000,
        )

        self.voucher_b = self.catalog_repo.create_voucher_product(
            display_name="PSN US Card",
            region="US",
            currency="USD",
            status=ProductStatus.APPROVED,
        )
        self.denom_b = self.catalog_repo.add_voucher_denomination(
            product_id=self.voucher_b.id,
            region="US",
            currency="USD",
            face_value_minor=1000,
            cost_rub_kopecks=95000,
        )

        self.fixed_prod = self.catalog_repo.create_fixed_product(
            display_name="PS Plus Deluxe",
            price_rub_kopecks=450000,
            delivery_method=DeliveryMethod.OFFICIAL_ACTIVATION,
            supply_method="VERIFIED_SUPPLIER",
            duration_days=365,
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.APPROVED,
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _create_voucher_order(self, user_id: int, qty: int = 1):
        return self.order_repo.create_order(
            user_id=user_id,
            total_amount_minor=80000 * qty,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_a.id,
                    "denomination_id": self.denom_a.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": qty,
                    "unit_price_minor": 80000,
                }
            ],
        )

    # ------------------------------------------------------------------
    # Section 3: Receipt order ownership checks
    # ------------------------------------------------------------------

    def test_submit_receipt_ownership_valid_user_a(self):
        """User A successfully attaches a receipt to User A's own order."""
        order_a = self._create_voucher_order(self.user_a.id)
        receipt = self.receipt_repo.submit_receipt(
            order_id=order_a.id,
            user_id=self.user_a.id,
            file_bytes=b"receipt_user_a_001",
            provider_tx_id="SBP-A-001",
            provider_name="SBER",
        )
        self.assertEqual(receipt.order_id, order_a.id)
        self.assertEqual(receipt.user_id, self.user_a.id)
        self.assertEqual(self.order_repo.get_order(order_a.id).status, OrderStatus.PENDING_PAYMENT)

    def test_submit_receipt_rejects_foreign_order_without_leaking_details(self):
        """User B cannot attach a receipt to User A's order and DB gets zero rows."""
        order_a = self._create_voucher_order(self.user_a.id)
        payload = b"user_b_attack_receipt"

        with self.assertRaises(ValidationError) as cm:
            self.receipt_repo.submit_receipt(
                order_id=order_a.id,
                user_id=self.user_b.id,
                file_bytes=payload,
                provider_tx_id="TX-FOREIGN-1",
                provider_name="SBER",
            )

        msg = str(cm.exception)
        self.assertIn("does not belong", msg.lower())
        # Must not disclose real owner's user_id, order total, or status
        self.assertNotIn(str(self.user_a.id), msg)
        self.assertNotIn("80000", msg)
        self.assertNotIn("PENDING_PAYMENT", msg)

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM payment_receipts")
        self.assertEqual(cur.fetchone()[0], 0)
        self.assertEqual(self.order_repo.get_order(order_a.id).status, OrderStatus.PENDING_PAYMENT)

        # User A can still legitimately submit a receipt even with same payload/tx_id after B's rejection
        rec_a = self.receipt_repo.submit_receipt(
            order_id=order_a.id,
            user_id=self.user_a.id,
            file_bytes=payload,
            provider_tx_id="TX-FOREIGN-1",
            provider_name="SBER",
        )
        self.assertEqual(rec_a.user_id, self.user_a.id)

        # Missing order_id yields the exact same error type/message as foreign order_id (no existence oracle)
        with self.assertRaises(ValidationError) as cm_missing:
            self.receipt_repo.submit_receipt(
                order_id=999999,
                user_id=self.user_b.id,
                file_bytes=b"missing_order_receipt",
            )
        self.assertEqual(str(cm_missing.exception), msg)

        # DuplicateReceiptError must not leak another user's order_id
        order_b = self._create_voucher_order(self.user_b.id)
        with self.assertRaises(DuplicateReceiptError) as cm_dup:
            self.receipt_repo.submit_receipt(
                order_id=order_b.id,
                user_id=self.user_b.id,
                file_bytes=payload,
                provider_tx_id="TX-B-NEW",
                provider_name="SBER",
            )
        self.assertNotIn(str(order_a.id), str(cm_dup.exception))

        # Cannot submit receipt to CANCELLED or PAID order
        self.order_repo.transition_order_status(order_b.id, OrderStatus.CANCELLED)
        with self.assertRaises(InvalidStateTransitionError):
            self.receipt_repo.submit_receipt(
                order_id=order_b.id,
                user_id=self.user_b.id,
                file_bytes=b"cancelled_order_receipt",
            )

    def test_submit_receipt_db_trigger_blocks_mismatched_user(self):
        """Direct SQL insert into payment_receipts with wrong user_id is blocked by trigger."""
        order_a = self._create_voucher_order(self.user_a.id)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                """
                INSERT INTO payment_receipts (
                    order_id, user_id, file_sha256, file_size_bytes, provider_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (order_a.id, self.user_b.id, "a" * 64, 128, "MANUAL"),
            )
        if self.conn.in_transaction:
            self.conn.rollback()

    def test_submit_receipt_concurrent_owner_vs_attacker(self):
        """Concurrent receipt submission by owner A and attacker B preserves integrity."""
        order_a = self._create_voucher_order(self.user_a.id)
        barrier = threading.Barrier(2)
        results = {"a_ok": False, "b_rejected": False, "errors": []}
        shared_bytes = b"shared_concurrent_receipt_bytes"

        def run_a():
            c = create_connection(self.db_path, timeout_seconds=5.0)
            repo = ReceiptRepository(c)
            try:
                barrier.wait(timeout=5.0)
                rec = repo.submit_receipt(
                    order_id=order_a.id,
                    user_id=self.user_a.id,
                    file_bytes=shared_bytes,
                    provider_tx_id="CONC-TX-1",
                    provider_name="TINKOFF",
                )
                if rec.user_id == self.user_a.id:
                    results["a_ok"] = True
            except Exception as e:
                results["errors"].append(f"User A error: {e}")
            finally:
                c.close()

        def run_b():
            c = create_connection(self.db_path, timeout_seconds=5.0)
            repo = ReceiptRepository(c)
            try:
                barrier.wait(timeout=5.0)
                repo.submit_receipt(
                    order_id=order_a.id,
                    user_id=self.user_b.id,
                    file_bytes=shared_bytes,
                    provider_tx_id="CONC-TX-1",
                    provider_name="TINKOFF",
                )
                results["errors"].append("User B unexpectedly succeeded")
            except ValidationError:
                results["b_rejected"] = True
            except Exception as e:
                results["errors"].append(f"User B unexpected error: {e}")
            finally:
                c.close()

        t1 = threading.Thread(target=run_a)
        t2 = threading.Thread(target=run_b)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(results["errors"], [])
        self.assertTrue(results["a_ok"])
        self.assertTrue(results["b_rejected"])

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*), MIN(user_id) FROM payment_receipts WHERE order_id = ?", (order_a.id,))
        cnt, owner = cur.fetchone()
        self.assertEqual(cnt, 1)
        self.assertEqual(owner, self.user_a.id)

    # ------------------------------------------------------------------
    # Section 4 & 5: Denomination ownership & ProductType/InventoryType matrix
    # ------------------------------------------------------------------

    def test_import_items_denomination_ownership_and_atomicity(self):
        """Denomination must belong to product_id; errors leave zero partial imports."""
        # 1. Valid multi-item batch for own denomination
        res = self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["TR-VALID-1", "TR-VALID-2", "TR-VALID-3"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )
        self.assertEqual(res["accepted"], 3)

        # 2. Denomination belonging to voucher_b rejected when importing for voucher_a
        with self.assertRaises(ValidationError) as cm:
            self.inv_repo.import_items(
                product_id=self.voucher_a.id,
                raw_codes=["CROSS-CODE-1", "CROSS-CODE-2"],
                inventory_type=InventoryType.VOUCHER_CODE,
                denomination_id=self.denom_b.id,
            )
        self.assertIn("does not belong to product", str(cm.exception))

        # 3. Non-existent denomination rejected
        with self.assertRaises(NotFoundError):
            self.inv_repo.import_items(
                product_id=self.voucher_a.id,
                raw_codes=["MISSING-DENOM-1"],
                inventory_type=InventoryType.VOUCHER_CODE,
                denomination_id=999999,
            )

        # 4. Fixed product with voucher denomination rejected
        with self.assertRaises(ValidationError):
            self.inv_repo.import_items(
                product_id=self.fixed_prod.id,
                raw_codes=["FIXED-WITH-DENOM"],
                inventory_type=InventoryType.FIXED_CODE,
                denomination_id=self.denom_a.id,
            )

        # 4b. VOUCHER product without denomination_id rejected in import_items and reserve_items_for_order
        with self.assertRaises(ValidationError):
            self.inv_repo.import_items(
                product_id=self.voucher_a.id,
                raw_codes=["VOUCHER-NO-DENOM"],
                inventory_type=InventoryType.VOUCHER_CODE,
                denomination_id=None,
            )
        order_v = self._create_voucher_order(self.user_a.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.reserve_items_for_order(
                order_id=order_v.id,
                product_id=self.voucher_a.id,
                quantity=1,
                denomination_id=None,
            )

        # 5. Mid-batch failure rolls back entire batch (zero partial items)
        call_count = {"n": 0}
        from src.db.crypto import encrypt_payload as real_encrypt

        def failing_encrypt(code_str, *args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("Simulated mid-batch encryption failure")
            return real_encrypt(code_str, *args, **kwargs)

        with patch("src.db.repositories.encrypt_payload", side_effect=failing_encrypt):
            with self.assertRaises(RuntimeError):
                self.inv_repo.import_items(
                    product_id=self.voucher_a.id,
                    raw_codes=["BATCH-ROLLBACK-1", "BATCH-ROLLBACK-2", "BATCH-ROLLBACK-3"],
                    inventory_type=InventoryType.VOUCHER_CODE,
                    denomination_id=self.denom_a.id,
                )

        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM inventory_items")
        self.assertEqual(cur.fetchone()[0], 3)  # Only the initial 3 valid items exist
        self.assertFalse(self.conn.in_transaction)

    def test_product_type_and_inventory_type_compatibility_matrix(self):
        """Every allowed and disallowed ProductType <-> InventoryType pair is enforced."""
        # Direct centralized validator checks across all 6 combinations
        self.assertEqual(
            ALLOWED_INVENTORY_TYPES_BY_PRODUCT_TYPE[ProductType.VOUCHER],
            {InventoryType.VOUCHER_CODE},
        )
        self.assertEqual(
            ALLOWED_INVENTORY_TYPES_BY_PRODUCT_TYPE[ProductType.FIXED_PRODUCT],
            {InventoryType.FIXED_CODE, InventoryType.ACTIVATION_DATA},
        )

        # Valid combinations in import_items
        r_v = self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["MAT-VOUCHER-1"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )
        self.assertEqual(r_v["accepted"], 1)

        r_fc = self.inv_repo.import_items(
            product_id=self.fixed_prod.id,
            raw_codes=["MAT-FIXED-CODE-1"],
            inventory_type=InventoryType.FIXED_CODE,
        )
        self.assertEqual(r_fc["accepted"], 1)

        r_ad = self.inv_repo.import_items(
            product_id=self.fixed_prod.id,
            raw_codes=["login:pass:2fa"],
            inventory_type=InventoryType.ACTIVATION_DATA,
        )
        self.assertEqual(r_ad["accepted"], 1)

        # Disallowed combinations in import_items
        invalid_pairs = [
            (self.voucher_a.id, self.denom_a.id, InventoryType.FIXED_CODE),
            (self.voucher_a.id, self.denom_a.id, InventoryType.ACTIVATION_DATA),
            (self.fixed_prod.id, None, InventoryType.VOUCHER_CODE),
        ]
        for pid, did, inv_type in invalid_pairs:
            with self.assertRaises(ValidationError):
                self.inv_repo.import_items(
                    product_id=pid,
                    raw_codes=["SHOULD-NOT-IMPORT"],
                    inventory_type=inv_type,
                    denomination_id=did,
                )

        # Invalid enum values rejected by centralized validator
        with self.assertRaises(ValidationError):
            validate_inventory_type_for_product("VOUCHER", InventoryType.VOUCHER_CODE)  # type: ignore
        with self.assertRaises(ValidationError):
            validate_inventory_type_for_product(ProductType.VOUCHER, "VOUCHER_CODE")  # type: ignore

        # SQLite trigger enforcement against raw SQL bypass
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, ?, ?, ?, ?, 'AVAILABLE')
                """,
                (self.fixed_prod.id, None, InventoryType.VOUCHER_CODE.value, b"cipher", "f" * 64),
            )
        if self.conn.in_transaction:
            self.conn.rollback()

        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute(
                """
                INSERT INTO inventory_items (
                    product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                ) VALUES (?, ?, ?, ?, ?, 'AVAILABLE')
                """,
                (self.voucher_a.id, self.denom_b.id, InventoryType.VOUCHER_CODE.value, b"cipher", "e" * 64),
            )
        if self.conn.in_transaction:
            self.conn.rollback()

    # ------------------------------------------------------------------
    # Section 6: Unified transaction ownership model
    # ------------------------------------------------------------------

    def test_unified_transaction_ownership_across_repositories_and_migrations(self):
        """All repo write methods and migration runners reject active caller transactions without committing."""
        self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["TX-CODE-1", "TX-CODE-2"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )
        order = self._create_voucher_order(self.user_a.id, qty=1)

        # Start caller-owned transaction with uncommitted user
        self.conn.execute(
            "INSERT INTO users (external_id, username, status) VALUES ('uncommitted_caller', 'caller', 'ACTIVE')"
        )
        self.assertTrue(self.conn.in_transaction)

        calls = [
            lambda: self.inv_repo.import_items(
                self.voucher_a.id, ["TX-BLOCKED"], InventoryType.VOUCHER_CODE, self.denom_a.id
            ),
            lambda: self.inv_repo.reserve_items_for_order(order.id, self.voucher_a.id, 1, self.denom_a.id),
            lambda: self.inv_repo.release_order_reservations(order.id),
            lambda: self.inv_repo.finalize_order_sale(order.id),
            lambda: self.receipt_repo.submit_receipt(order.id, self.user_a.id, b"receipt_tx_test"),
            lambda: self.catalog_repo.update_product_status(self.voucher_a.id, ProductStatus.APPROVED),
            lambda: self.user_repo.update_user_status(self.user_a.id, UserStatus.ACTIVE),
            lambda: ensure_migration_table(self.conn),
            lambda: run_migrations(self.conn),
            lambda: transaction(self.conn).__enter__(),
        ]

        for fn in calls:
            with self.assertRaises(RuntimeError):
                fn()
            # Caller's transaction must remain active and uncommitted
            self.assertTrue(self.conn.in_transaction)

        # Pure read via get_applied_migrations on existing table does not break or commit caller tx
        versions = get_applied_migrations(self.conn)
        self.assertIn(3, versions)
        self.assertTrue(self.conn.in_transaction)

        # Rollback caller tx and confirm uncommitted_caller was never committed
        self.conn.rollback()
        self.assertFalse(self.conn.in_transaction)
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE external_id = 'uncommitted_caller'")
        self.assertIsNone(cur.fetchone())

        # Sequential operations on the same connection succeed cleanly afterward
        reserved = self.inv_repo.reserve_items_for_order(order.id, self.voucher_a.id, 1, self.denom_a.id)
        self.assertEqual(len(reserved), 1)
        self.assertFalse(self.conn.in_transaction)
        released = self.inv_repo.release_order_reservations(order.id)
        self.assertEqual(released, 1)
        self.assertFalse(self.conn.in_transaction)

    # ------------------------------------------------------------------
    # Section 7: Alignment between OrderStatus and Inventory operations
    # ------------------------------------------------------------------

    def test_inventory_operations_aligned_with_order_statuses(self):
        """Verify reserve, release, and finalize respect OrderStatus invariants and never double-sell."""
        self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["ORD-INV-1", "ORD-INV-2", "ORD-INV-3"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )

        order_a = self._create_voucher_order(self.user_a.id, qty=1)
        order_b = self._create_voucher_order(self.user_b.id, qty=1)

        # Reserve 1 item for order_a and 1 item for order_b while PENDING_PAYMENT
        res_a = self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        res_b = self.inv_repo.reserve_items_for_order(order_b.id, self.voucher_a.id, 1, self.denom_a.id)

        # 1. Finalize rejected on PENDING_PAYMENT and PAYMENT_REVIEW
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.finalize_order_sale(order_a.id)

        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAYMENT_REVIEW)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.finalize_order_sale(order_a.id)

        # 2. Transition order_a to PAID -> release forbidden, finalize allowed
        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAID)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.release_order_reservations(order_a.id)

        sold_count = self.inv_repo.finalize_order_sale(order_a.id)
        self.assertEqual(sold_count, 1)

        # 3. Double finalization on same PAID order rejected with ConflictError
        with self.assertRaises(ConflictError):
            self.inv_repo.finalize_order_sale(order_a.id)

        # 4. Complete order_a -> reserve & finalize rejected on COMPLETED
        self.order_repo.transition_order_status(order_a.id, OrderStatus.FULFILLING)
        self.order_repo.transition_order_status(order_a.id, OrderStatus.COMPLETED)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.finalize_order_sale(order_a.id)

        # 5. Releasing order_b reservations does NOT touch order_a SOLD item; releasing order_a after refund/cancel returns 0
        self.order_repo.transition_order_status(order_a.id, OrderStatus.REFUND_REVIEW)
        self.order_repo.transition_order_status(order_a.id, OrderStatus.CANCELLED)
        self.assertEqual(self.inv_repo.release_order_reservations(order_a.id), 0)

        # Verify order_a's item is still SOLD, and order_b's item is still RESERVED
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM inventory_items WHERE id = ?", (res_a[0],))
        self.assertEqual(cur.fetchone()["status"], InventoryStatus.SOLD.value)
        cur.execute("SELECT status FROM inventory_items WHERE id = ?", (res_b[0],))
        self.assertEqual(cur.fetchone()["status"], InventoryStatus.RESERVED.value)

        # Releasing order_b (PENDING_PAYMENT) frees ONLY order_b's item
        self.assertEqual(self.inv_repo.release_order_reservations(order_b.id), 1)
        cur.execute("SELECT status FROM inventory_items WHERE id = ?", (res_b[0],))
        self.assertEqual(cur.fetchone()["status"], InventoryStatus.AVAILABLE.value)

        # Order CANCELLED cannot reserve items
        self.order_repo.transition_order_status(order_b.id, OrderStatus.CANCELLED)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.reserve_items_for_order(order_b.id, self.voucher_a.id, 1, self.denom_a.id)

    def test_reserve_items_mid_operation_error_rolls_back_atomically(self):
        """Error during multi-unit reservation rolls back partial updates cleanly."""
        self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["RB-RES-1", "RB-RES-2"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )
        order = self._create_voucher_order(self.user_a.id, qty=2)

        # Inject trigger failure only on the 2nd item reservation
        self.conn.execute(
            """
            CREATE TRIGGER fail_second_reserve BEFORE UPDATE ON inventory_items
            WHEN NEW.status = 'RESERVED'
             AND (SELECT COUNT(*) FROM inventory_items WHERE reserved_order_id = NEW.reserved_order_id AND status = 'RESERVED') >= 1
            BEGIN
                SELECT RAISE(ABORT, 'Simulated mid-reservation failure');
            END;
            """
        )
        self.conn.commit()

        with self.assertRaises(sqlite3.DatabaseError):
            self.inv_repo.reserve_items_for_order(order.id, self.voucher_a.id, 2, self.denom_a.id)

        self.assertFalse(self.conn.in_transaction)
        cur = self.conn.cursor()
        cur.execute("SELECT status, reserved_order_id FROM inventory_items")
        for row in cur.fetchall():
            self.assertEqual(row["status"], InventoryStatus.AVAILABLE.value)
            self.assertIsNone(row["reserved_order_id"])

    def test_concurrent_finalize_vs_release_on_same_reservation(self):
        """Concurrent finalize_order_sale vs release/cancel serializes safely via BEGIN IMMEDIATE."""
        self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["CONC-SALE-1"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )
        order = self._create_voucher_order(self.user_a.id, qty=1)
        item_ids = self.inv_repo.reserve_items_for_order(order.id, self.voucher_a.id, 1, self.denom_a.id)
        self.order_repo.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order.id, OrderStatus.PAID)

        barrier = threading.Barrier(2)
        outcomes = {"finalized": False, "rejected_release": False, "errors": []}

        def worker_finalize():
            c = create_connection(self.db_path, timeout_seconds=5.0)
            repo = InventoryRepository(c)
            try:
                barrier.wait(timeout=5.0)
                cnt = repo.finalize_order_sale(order.id)
                if cnt == 1:
                    outcomes["finalized"] = True
            except Exception as e:
                outcomes["errors"].append(f"finalize error: {e}")
            finally:
                c.close()

        def worker_release():
            c = create_connection(self.db_path, timeout_seconds=5.0)
            repo = InventoryRepository(c)
            try:
                barrier.wait(timeout=5.0)
                repo.release_order_reservations(order.id)
                outcomes["errors"].append("release unexpectedly succeeded on PAID order")
            except InvalidStateTransitionError:
                outcomes["rejected_release"] = True
            except Exception as e:
                outcomes["errors"].append(f"release error: {e}")
            finally:
                c.close()

        t1 = threading.Thread(target=worker_finalize)
        t2 = threading.Thread(target=worker_release)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(outcomes["errors"], [])
        self.assertTrue(outcomes["finalized"])
        self.assertTrue(outcomes["rejected_release"])

        cur = self.conn.cursor()
        cur.execute("SELECT status FROM inventory_items WHERE id = ?", (item_ids[0],))
        self.assertEqual(cur.fetchone()["status"], InventoryStatus.SOLD.value)

    # ------------------------------------------------------------------
    # Section 8: get_revealed_code contract in order context & status guard
    # ------------------------------------------------------------------

    def test_get_revealed_code_order_context_and_status_guards(self):
        """get_revealed_code blocks QUARANTINED/DISABLED and validates order/user context when supplied."""
        self.inv_repo.import_items(
            product_id=self.voucher_a.id,
            raw_codes=["SECRET-CODE-A", "BAD-QUARANTINE-CODE"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom_a.id,
        )
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM inventory_items ORDER BY id ASC")
        good_id, bad_id = [r["id"] for r in cur.fetchall()]

        # Quarantined item cannot be revealed
        self.conn.execute("UPDATE inventory_items SET status = 'QUARANTINED' WHERE id = ?", (bad_id,))
        self.conn.commit()
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(bad_id)

        # Unassigned item rejected when expected_order_id or expected_user_id is specified
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(good_id, expected_order_id=1)

        order_a = self._create_voucher_order(self.user_a.id, qty=1)
        self.inv_repo.reserve_items_for_order(order_a.id, self.voucher_a.id, 1, self.denom_a.id)

        # Cannot reveal in order context while item is only RESERVED (even after order becomes PAID!)
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(good_id, expected_order_id=order_a.id, expected_user_id=self.user_a.id)

        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAYMENT_REVIEW)
        self.order_repo.transition_order_status(order_a.id, OrderStatus.PAID)
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(good_id, expected_order_id=order_a.id, expected_user_id=self.user_a.id)

        # Finalize sale -> item becomes SOLD -> reveal succeeds for owner A & order A, fails for user B or wrong order
        self.inv_repo.finalize_order_sale(order_a.id)

        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(good_id, expected_user_id=self.user_b.id)
        with self.assertRaises(ValidationError):
            self.inv_repo.get_revealed_code(good_id, expected_order_id=order_a.id + 100)

        code = self.inv_repo.get_revealed_code(
            good_id,
            expected_order_id=order_a.id,
            expected_user_id=self.user_a.id,
        )
        self.assertEqual(code, "SECRET-CODE-A")

        # Once order enters REFUND_REVIEW / CANCELLED, reveal in order context is blocked again
        self.order_repo.transition_order_status(order_a.id, OrderStatus.REFUND_REVIEW)
        with self.assertRaises(InvalidStateTransitionError):
            self.inv_repo.get_revealed_code(
                good_id,
                expected_order_id=order_a.id,
                expected_user_id=self.user_a.id,
            )

    # ------------------------------------------------------------------
    # Section 10: Migration uniqueness, checksum verification, and legacy upgrade
    # ------------------------------------------------------------------

    def test_duplicate_migration_versions_and_checksum_verification(self):
        """Duplicate version files raise ValueError; modified applied migration raises RuntimeError."""
        mig_dir = Path(self.temp_dir.name) / "custom_migrations"
        mig_dir.mkdir(parents=True, exist_ok=True)
        local_db = os.path.join(self.temp_dir.name, "mig_check.db")
        c = create_connection(local_db)
        try:
            m1 = mig_dir / "001_init.sql"
            m1.write_text("CREATE TABLE t1 (id INTEGER PRIMARY KEY);", encoding="utf-8")
            applied = run_migrations(c, migrations_dir=mig_dir)
            self.assertEqual(applied, [1])

            # Tamper with already-applied migration 001 -> must fail checksum check
            m1.write_text("CREATE TABLE t1 (id INTEGER PRIMARY KEY, extra TEXT);", encoding="utf-8")
            with self.assertRaises(RuntimeError) as cm_chk:
                run_migrations(c, migrations_dir=mig_dir)
            self.assertIn("checksum mismatch", str(cm_chk.exception))

            # Duplicate version files -> discover_migration_files / run_migrations raises ValueError
            m1.write_text("CREATE TABLE t1 (id INTEGER PRIMARY KEY);", encoding="utf-8")
            (mig_dir / "002_first.sql").write_text("CREATE TABLE t2a (id INT);", encoding="utf-8")
            (mig_dir / "002_second.sql").write_text("CREATE TABLE t2b (id INT);", encoding="utf-8")
            with self.assertRaises(ValueError) as cm_dup:
                discover_migration_files(mig_dir)
            self.assertIn("Duplicate migration version 2", str(cm_dup.exception))
            with self.assertRaises(ValueError):
                run_migrations(c, migrations_dir=mig_dir)
        finally:
            c.close()

    def test_legacy_database_without_checksum_column_upgrades_cleanly_with_existing_data(self):
        """Existing DB created with 001+002 (no checksum column) upgrades to 003 preserving valid data."""
        legacy_db = os.path.join(self.temp_dir.name, "legacy.db")
        c = create_connection(legacy_db)
        try:
            # Manually apply 001 and 002 without checksum column (simulating legacy schema_migrations)
            m001 = Path("src/migrations/001_initial_schema.sql").read_text(encoding="utf-8")
            m002 = Path("src/migrations/002_provider_scoped_receipts.sql").read_text(encoding="utf-8")
            c.executescript(
                f"BEGIN IMMEDIATE;\n{m001}\n{m002}\n"
                "INSERT INTO schema_migrations (version, name) VALUES (1, 'initial_schema');\n"
                "INSERT INTO schema_migrations (version, name) VALUES (2, 'provider_scoped_receipts');\n"
                "COMMIT;\n"
            )

            # Seed existing valid catalog, inventory, fixed-product order (001 schema), and receipt data
            u_repo = UserRepository(c)
            cat_repo = CatalogRepository(c)
            inv_repo = InventoryRepository(c)
            rec_repo = ReceiptRepository(c)

            user, _ = u_repo.get_or_create_user("legacy_u1", "legacy_user")
            v_prod = cat_repo.create_voucher_product("Legacy PSN", "TR", "TRY", ProductStatus.APPROVED)
            denom = cat_repo.add_voucher_denomination(v_prod.id, "TR", "TRY", 25000, 80000)
            inv_repo.import_items(v_prod.id, ["LEG-CODE-1"], InventoryType.VOUCHER_CODE, denom.id)

            f_prod = cat_repo.create_fixed_product(
                display_name="Legacy Fixed",
                price_rub_kopecks=80000,
                delivery_method=DeliveryMethod.MANUAL_REVIEW,
                supply_method="MANUAL_POOL",
                approval_status=ApprovalStatus.APPROVED,
                status=ProductStatus.APPROVED,
            )
            cur = c.cursor()
            cur.execute(
                "INSERT INTO orders (user_id, status, total_amount_minor, currency) VALUES (?, 'PENDING_PAYMENT', 80000, 'RUB')",
                (user.id,),
            )
            legacy_order_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, product_type_snapshot, quantity, unit_price_minor, currency
                ) VALUES (?, ?, 'FIXED_PRODUCT', 1, 80000, 'RUB')
                """,
                (legacy_order_id, f_prod.id),
            )
            c.commit()
            rec_repo.submit_receipt(legacy_order_id, user.id, b"legacy_receipt_bytes", provider_tx_id="LEG-TX-1")

            # Upgrade using run_migrations() -> applies 003..007 cleanly and adds checksum column
            newly_applied = run_migrations(c)
            self.assertEqual(newly_applied, [3, 4, 5, 6, 7])

            # Re-running is idempotent
            self.assertEqual(run_migrations(c), [])

            # Legacy valid data remains intact
            self.assertEqual(len(get_applied_migrations(c)), 7)
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM inventory_items")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT COUNT(*) FROM payment_receipts")
            self.assertEqual(cur.fetchone()[0], 1)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
