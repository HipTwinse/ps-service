"""Unit tests for orders, historical price snapshots, and lifecycle state machine transitions."""

import os
import sqlite3
import tempfile
import unittest

from src.db.connection import create_connection
from src.db.migrations import run_migrations
from src.db.models import (
    ApprovalStatus,
    ConflictError,
    DeliveryMethod,
    InvalidStateTransitionError,
    NotFoundError,
    OrderStatus,
    ProductStatus,
    ProductType,
    UserStatus,
    ValidationError,
)
from src.db.repositories import (
    CatalogRepository,
    OrderRepository,
    UserRepository,
)


class TestOrderLifecycleAndSnapshots(unittest.TestCase):
    """Tests for order creation, price snapshotting, and strict state transitions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_orders.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.catalog_repo = CatalogRepository(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)

        # Create active user
        self.user, _ = self.user_repo.get_or_create_user("tg_order_user", "tester")

        # Create approved voucher product & denomination
        self.voucher_prod = self.catalog_repo.create_voucher_product(
            display_name="PSN Wallet Topup",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.denom = self.catalog_repo.add_voucher_denomination(
            product_id=self.voucher_prod.id,
            region="TR",
            currency="TRY",
            face_value_minor=10000,
            cost_rub_kopecks=32000,
        )

        # Create approved fixed product
        self.fixed_prod = self.catalog_repo.create_fixed_product(
            display_name="Gemini 1M",
            price_rub_kopecks=14900,
            delivery_method=DeliveryMethod.MANUAL_REVIEW,
            supply_method="WHOLESALE_READY_ACCOUNT",
            duration_days=30,
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.APPROVED,
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_order_creation_and_price_snapshots(self):
        """Verify order items preserve historical prices even if catalog values change."""
        items = [
            {
                "product_id": self.voucher_prod.id,
                "product_type_snapshot": "VOUCHER",
                "quantity": 2,
                "unit_price_minor": 32000,
                "metadata_json": '{"denomination_minor": 10000}',
            },
            {
                "product_id": self.fixed_prod.id,
                "product_type_snapshot": "FIXED_PRODUCT",
                "quantity": 1,
                "unit_price_minor": 14900,
                "metadata_json": '{"duration_days": 30}',
            },
        ]

        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=78900,
            currency="RUB",
            items=items,
        )

        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)
        self.assertEqual(order.total_amount_minor, 78900)
        self.assertEqual(order.currency, "RUB")

        # Verify order items
        order_items = self.order_repo.get_order_items(order.id)
        self.assertEqual(len(order_items), 2)

        v_item = [i for i in order_items if i.product_id == self.voucher_prod.id][0]
        self.assertEqual(v_item.unit_price_minor, 32000)
        self.assertEqual(v_item.quantity, 2)

        f_item = [i for i in order_items if i.product_id == self.fixed_prod.id][0]
        self.assertEqual(f_item.unit_price_minor, 14900)
        self.assertEqual(f_item.quantity, 1)

        # Mutate catalog prices: update fixed product price in DB
        self.conn.execute(
            "UPDATE fixed_product_details SET price_rub_kopecks = 25000 WHERE product_id = ?",
            (self.fixed_prod.id,),
        )

        # Re-fetch order items - snapshots must remain invariant!
        refetched_items = self.order_repo.get_order_items(order.id)
        refetched_f_item = [i for i in refetched_items if i.product_id == self.fixed_prod.id][0]
        self.assertEqual(refetched_f_item.unit_price_minor, 14900)

    def test_order_creation_blocked_for_blocked_user_or_unapproved_product(self):
        """Verify blocked user or draft product cannot create an order."""
        # Block user
        self.user_repo.update_user_status(self.user.id, UserStatus.BLOCKED)

        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=32000,
                currency="RUB",
                items=[
                    {
                        "product_id": self.voucher_prod.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 1,
                        "unit_price_minor": 32000,
                        "metadata_json": None,
                    }
                ],
            )

        # Unblock user
        self.user_repo.update_user_status(self.user.id, UserStatus.ACTIVE)

        # Create draft product
        draft_prod = self.catalog_repo.create_voucher_product(
            display_name="Draft Voucher",
            region="US",
            currency="USD",
            status=ProductStatus.DRAFT,
        )

        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=1000,
                currency="RUB",
                items=[
                    {
                        "product_id": draft_prod.id,
                        "product_type_snapshot": "VOUCHER",
                        "quantity": 1,
                        "unit_price_minor": 1000,
                        "metadata_json": None,
                    }
                ],
            )

    def test_order_creation_blocked_for_fixed_product_with_unapproved_supply(self):
        """Even if products.status is somehow APPROVED, order creation must directly reject unapproved supply."""
        # Create unapproved fixed product
        unapproved_fp = self.catalog_repo.create_fixed_product(
            display_name="Unapproved Accounts",
            price_rub_kopecks=50000,
            delivery_method=DeliveryMethod.MANUAL_REVIEW,
            approval_status=ApprovalStatus.DRAFT,
            status=ProductStatus.DRAFT,
        )

        # Directly simulate malicious/buggy DB update that bypassed catalog and set status=APPROVED
        self.conn.execute("UPDATE products SET status = 'APPROVED' WHERE id = ?", (unapproved_fp.id,))
        self.conn.commit()

        # OrderRepository MUST block this order directly!
        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=50000,
                currency="RUB",
                items=[
                    {
                        "product_id": unapproved_fp.id,
                        "product_type_snapshot": "FIXED_PRODUCT",
                        "quantity": 1,
                        "unit_price_minor": 50000,
                        "metadata_json": None,
                    }
                ],
            )
        self.assertIn("does not have an approved supply method", str(cm.exception))

        # Now approve the supply method
        self.catalog_repo.update_fixed_product_approval(unapproved_fp.id, ApprovalStatus.APPROVED)
        self.conn.commit()

        # Now ordering must succeed!
        valid_order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=50000,
            currency="RUB",
            items=[
                {
                    "product_id": unapproved_fp.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 50000,
                    "metadata_json": None,
                }
            ],
        )
        self.assertEqual(valid_order.status, OrderStatus.PENDING_PAYMENT)

    def test_order_state_machine_transitions(self):
        """Verify valid transitions flow smoothly and illegal transitions are rejected."""
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=32000,
            currency="RUB",
            items=[
                {
                    "product_id": self.voucher_prod.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 32000,
                    "metadata_json": None,
                }
            ],
        )

        # Current status: PENDING_PAYMENT
        # Illegal transition: PENDING_PAYMENT -> COMPLETED (must fail)
        with self.assertRaises(InvalidStateTransitionError):
            self.order_repo.transition_order_status(order.id, OrderStatus.COMPLETED)

        # Valid transition: PENDING_PAYMENT -> PAYMENT_REVIEW
        order = self.order_repo.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.assertEqual(order.status, OrderStatus.PAYMENT_REVIEW)

        # Valid transition: PAYMENT_REVIEW -> PAID
        order = self.order_repo.transition_order_status(order.id, OrderStatus.PAID)
        self.assertEqual(order.status, OrderStatus.PAID)
        self.assertIsNotNone(order.paid_at)

        # Valid transition: PAID -> FULFILLING
        order = self.order_repo.transition_order_status(order.id, OrderStatus.FULFILLING)
        self.assertEqual(order.status, OrderStatus.FULFILLING)

        # Valid transition: FULFILLING -> COMPLETED
        order = self.order_repo.transition_order_status(order.id, OrderStatus.COMPLETED)
        self.assertEqual(order.status, OrderStatus.COMPLETED)

        # Illegal transition: COMPLETED -> PENDING_PAYMENT
        with self.assertRaises(InvalidStateTransitionError):
            self.order_repo.transition_order_status(order.id, OrderStatus.PENDING_PAYMENT)


    def test_order_creation_rejects_amount_mismatch_with_items(self):
        """Order creation must reject total_amount_minor that does not equal sum(quantity * unit_price)."""
        items = [
            {
                "product_id": self.voucher_prod.id,
                "product_type_snapshot": "VOUCHER",
                "quantity": 2,
                "unit_price_minor": 32000,
                "metadata_json": None,
            }
        ]
        # Actual sum is 64000, passing 50000 must fail
        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=50000,
                currency="RUB",
                items=items,
            )
        self.assertIn("does not match sum of items", str(cm.exception))

    def test_order_creation_rejects_currency_mismatch_with_items(self):
        """Order creation must reject item currencies that do not match the order currency."""
        items = [
            {
                "product_id": self.voucher_prod.id,
                "product_type_snapshot": "VOUCHER",
                "quantity": 1,
                "unit_price_minor": 32000,
                "currency": "TRY",
                "metadata_json": None,
            }
        ]
        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=32000,
                currency="RUB",
                items=items,
            )
        self.assertIn("does not match order currency", str(cm.exception))

    def test_concurrent_two_connections_approval_revocation_race(self):
        """Concurrent two-connection test: approval revocation before or during order creation protects consistency."""
        if self.conn.in_transaction:
            self.conn.commit()

        conn2 = create_connection(self.db_path, timeout_seconds=0.1)
        try:
            # Scenario 1: conn2 revokes approval before order creation
            conn2.execute(
                "UPDATE fixed_product_details SET approval_status = 'DRAFT' WHERE product_id = ?",
                (self.fixed_prod.id,),
            )
            conn2.commit()

            items = [
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 14900,
                    "currency": "RUB",
                    "metadata_json": None,
                }
            ]

            with self.assertRaises(ValidationError) as cm:
                self.order_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=items,
                )
            self.assertIn("does not have an approved supply method", str(cm.exception))

            # Verify no orphan order or order_items created
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM orders")
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(*) FROM order_items")
            self.assertEqual(cur.fetchone()[0], 0)

            # Re-approve fixed product
            conn2.execute(
                "UPDATE fixed_product_details SET approval_status = 'APPROVED' WHERE product_id = ?",
                (self.fixed_prod.id,),
            )
            conn2.commit()

            # Scenario 2: conn1 begins immediate order creation; conn2 cannot modify concurrently
            self.conn.execute("BEGIN IMMEDIATE;")
            import sqlite3
            with self.assertRaises(sqlite3.OperationalError):
                conn2.execute(
                    "UPDATE fixed_product_details SET approval_status = 'DRAFT' WHERE product_id = ?",
                    (self.fixed_prod.id,),
                )
            self.conn.rollback()
        finally:
            conn2.close()

    def test_concurrent_two_connections_user_blocked_race(self):
        """Concurrent two-connection test: blocking user on conn2 prevents order creation on conn1."""
        if self.conn.in_transaction:
            self.conn.commit()

        conn2 = create_connection(self.db_path, timeout_seconds=0.1)
        try:
            conn2.execute(
                "UPDATE users SET status = 'BLOCKED' WHERE id = ?",
                (self.user.id,),
            )
            conn2.commit()

            items = [
                {
                    "product_id": self.voucher_prod.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 32000,
                    "currency": "RUB",
                    "metadata_json": None,
                }
            ]

            with self.assertRaises(ValidationError) as cm:
                self.order_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=32000,
                    currency="RUB",
                    items=items,
                )
            self.assertIn("Cannot create order for blocked user", str(cm.exception))

            # Restore user status
            conn2.execute("UPDATE users SET status = 'ACTIVE' WHERE id = ?", (self.user.id,))
            conn2.commit()
        finally:
            conn2.close()

    def test_transaction_ownership_create_order_rejected_if_in_transaction(self):
        """create_order must refuse to execute if connection already has an active uncommitted transaction."""
        self.conn.execute("INSERT INTO users (external_id, username, status) VALUES ('caller_owned', 'caller', 'ACTIVE')")
        self.assertTrue(self.conn.in_transaction)

        items = [
            {
                "product_id": self.voucher_prod.id,
                "product_type_snapshot": "VOUCHER",
                "quantity": 1,
                "unit_price_minor": 32000,
                "currency": "RUB",
                "metadata_json": None,
            }
        ]

        with self.assertRaises(RuntimeError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=32000,
                currency="RUB",
                items=items,
            )

        self.assertIn("active uncommitted transaction", str(cm.exception))

        # Caller's transaction must remain uncommitted and intact
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_transaction_ownership_transition_order_status_rejected_if_in_transaction(self):
        """transition_order_status must refuse to execute if connection already has an active uncommitted transaction."""
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=14900,
            currency="RUB",
            items=[
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 14900,
                    "currency": "RUB",
                    "metadata_json": None,
                }
            ],
        )

        # 1. Caller starts an uncommitted transaction
        self.conn.execute("INSERT INTO users (external_id, username, status) VALUES ('caller_in_tx', 'caller_tx', 'ACTIVE')")
        self.assertTrue(self.conn.in_transaction)

        # 2. Calling transition_order_status must be rejected BEFORE any SQL is executed
        with self.assertRaises(RuntimeError) as cm:
            self.order_repo.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)

        self.assertIn("active uncommitted transaction", str(cm.exception))

        # 3. Caller's uncommitted transaction MUST remain active and uncommitted
        self.assertTrue(self.conn.in_transaction)

        # 4. Caller's uncommitted data is preserved and can be committed by caller
        self.conn.commit()
        self.assertFalse(self.conn.in_transaction)

        # Verify caller's inserted user exists
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE external_id = 'caller_in_tx'")
        self.assertIsNotNone(cur.fetchone())

        # Verify order was NOT transitioned (remains PENDING_PAYMENT)
        self.assertEqual(self.order_repo.get_order(order.id).status, OrderStatus.PENDING_PAYMENT)

    def test_transaction_ownership_transition_order_status_conflict_scenario_preserves_caller_tx(self):
        """If caller is in transaction, transition_order_status rejects before ConflictError without touching caller tx."""
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=14900,
            currency="RUB",
            items=[
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 14900,
                    "currency": "RUB",
                    "metadata_json": None,
                }
            ],
        )

        # Caller starts an uncommitted transaction
        self.conn.execute("INSERT INTO users (external_id, username, status) VALUES ('caller_conflict_tx', 'caller_c', 'ACTIVE')")
        self.assertTrue(self.conn.in_transaction)

        # Attempt transition expecting obsolete state
        with self.assertRaises(RuntimeError) as cm:
            self.order_repo.transition_order_status(
                order.id,
                OrderStatus.PAID,
                expected_current_status=OrderStatus.PAYMENT_REVIEW,
            )

        self.assertIn("active uncommitted transaction", str(cm.exception))
        self.assertTrue(self.conn.in_transaction)

        # Caller rolls back their own transaction safely
        self.conn.rollback()
        self.assertFalse(self.conn.in_transaction)

        cur = self.conn.cursor()
        cur.execute("SELECT id FROM users WHERE external_id = 'caller_conflict_tx'")
        self.assertIsNone(cur.fetchone())

    def test_money_validation_rejects_bool_and_non_integers(self):
        """Money and quantity fields must reject booleans (isinstance(True, int)) and non-integers."""
        base_item = {
            "product_id": self.voucher_prod.id,
            "product_type_snapshot": "VOUCHER",
            "quantity": 1,
            "unit_price_minor": 32000,
            "currency": "RUB",
            "metadata_json": None,
        }

        # 1. total_amount_minor as bool True
        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=True,  # type is bool
                currency="RUB",
                items=[base_item],
            )
        self.assertIn("must be an integer", str(cm.exception))

        # 2. total_amount_minor as float
        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=32000.50,
                currency="RUB",
                items=[base_item],
            )
        self.assertIn("must be an integer", str(cm.exception))

        # 3. Item quantity as bool
        bad_qty_item = dict(base_item, quantity=True)
        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=32000,
                currency="RUB",
                items=[bad_qty_item],
            )

        # 4. Item unit_price_minor as bool
        bad_price_item = dict(base_item, unit_price_minor=True)
        with self.assertRaises(ValidationError):
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=32000,
                currency="RUB",
                items=[bad_price_item],
            )

    def test_product_type_snapshot_mismatch_rejected(self):
        """product_type_snapshot must match the actual product type in products table."""
        # Fixed product with snapshot claiming to be VOUCHER
        mismatched_item = {
            "product_id": self.fixed_prod.id,
            "product_type_snapshot": "VOUCHER",  # Actual is FIXED_PRODUCT
            "quantity": 1,
            "unit_price_minor": 14900,
            "currency": "RUB",
            "metadata_json": None,
        }

        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=14900,
                currency="RUB",
                items=[mismatched_item],
            )
        self.assertIn("does not match product", str(cm.exception))

    def test_strict_typing_order_repository_methods(self):
        """OrderRepository methods must enforce strict typing (reject bool, float, str, None, and non-positive values)."""
        base_item = {
            "product_id": self.fixed_prod.id,
            "product_type_snapshot": "FIXED_PRODUCT",
            "quantity": 1,
            "unit_price_minor": 14900,
            "currency": "RUB",
            "metadata_json": None,
        }

        # 1. user_id validation
        for bad_user_id in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.order_repo.create_order(
                    user_id=bad_user_id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=[base_item],
                )

        # 2. total_amount_minor validation
        for bad_total in (True, False, 14900.5, "14900", None, 0, -500):
            with self.assertRaises(ValidationError):
                self.order_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=bad_total,
                    currency="RUB",
                    items=[base_item],
                )

        # 3. item product_id validation
        for bad_pid in (True, False, 2.5, "2", None, 0, -10):
            bad_item = dict(base_item, product_id=bad_pid)
            with self.assertRaises(ValidationError):
                self.order_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=[bad_item],
                )

        # 4. item quantity validation
        for bad_qty in (True, False, 1.5, "1", None, 0, -1):
            bad_item = dict(base_item, quantity=bad_qty)
            with self.assertRaises(ValidationError):
                self.order_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=[bad_item],
                )

        # 5. item unit_price_minor validation
        for bad_price in (True, False, 14900.5, "14900", None, 0, -100):
            bad_item = dict(base_item, unit_price_minor=bad_price)
            with self.assertRaises(ValidationError):
                self.order_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=[bad_item],
                )

        # Create valid order for testing get_order, transition_order_status, list_orders_by_user
        valid_order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=14900,
            currency="RUB",
            items=[base_item],
        )

        # 6. get_order(order_id)
        for bad_oid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.order_repo.get_order(bad_oid)

        # 7. transition_order_status(order_id, ...)
        for bad_oid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.order_repo.transition_order_status(bad_oid, OrderStatus.PAYMENT_REVIEW)

        # 8. get_order_items(order_id)
        for bad_oid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.order_repo.get_order_items(bad_oid)

    def test_real_concurrency_approval_revocation_during_create_order(self):
        """Deterministic concurrency test for create_order vs supply approval revocation.

        Covers both interleaved scenarios deterministically:
        - Scenario 1: create_order acquires BEGIN IMMEDIATE and actively HOLDS the transaction lock.
          While create_order() still holds the transaction (before commit), the second thread
          starts the real SQL UPDATE that revokes approval (and blocks on SQLite write lock).
          Only after the real SQL UPDATE has started while create_order() is uncommitted is
          create_order() allowed to commit. After create_order() commits, the revocation UPDATE
          acquires the lock and finishes cleanly.
          Verified DB state: exactly 1 order, 1 order item, approval_status='DRAFT', 0 orphans.
        - Scenario 2: Revocation commits BEFORE create_order executes.
          create_order MUST fail specifically with ValidationError (unapproved supply method).
          'database is locked' is strictly forbidden from counting as a pass.
          Verified DB state: 0 orders, 0 order items, 0 orphan records.
        """
        import threading
        import time

        items = [
            {
                "product_id": self.fixed_prod.id,
                "product_type_snapshot": "FIXED_PRODUCT",
                "quantity": 1,
                "unit_price_minor": 14900,
                "currency": "RUB",
                "metadata_json": None,
            }
        ]

        # --- SCENARIO 1: create_order holds write lock while real SQL UPDATE revocation starts ---
        # Ensure product is initially APPROVED
        setup_conn = create_connection(self.db_path)
        setup_conn.execute("UPDATE fixed_product_details SET approval_status = 'APPROVED' WHERE product_id = ?", (self.fixed_prod.id,))
        setup_conn.commit()
        setup_conn.close()

        sc1_events = {
            "in_transaction": threading.Event(),
            "revoke_update_started": threading.Event(),
            "allow_commit": threading.Event(),
            "order_committed": threading.Event(),
        }
        sc1_results = {
            "order_created": False,
            "revoke_ok": False,
            "update_started_before_commit": False,
            "update_completed_after_commit": False,
            "errors": [],
        }

        def sc1_worker_create():
            conn = create_connection(self.db_path, timeout_seconds=5.0)

            def trace_hook(stmt):
                # When create_order starts inserting order items (after BEGIN IMMEDIATE and validation,
                # before COMMIT), signal that write lock is actively held and wait until allow_commit.
                if "INSERT INTO order_items" in stmt and not sc1_events["in_transaction"].is_set():
                    sc1_events["in_transaction"].set()
                    if not sc1_events["allow_commit"].wait(timeout=5.0):
                        sc1_results["errors"].append("SC1 create: timed out waiting for allow_commit signal")

            conn.set_trace_callback(trace_hook)
            repo = OrderRepository(conn)
            try:
                repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=items,
                )
                sc1_events["order_committed"].set()
                sc1_results["order_created"] = True
            except Exception as e:
                sc1_results["errors"].append(f"SC1 create_order error: {e}")
            finally:
                conn.set_trace_callback(None)
                conn.close()

        def sc1_worker_revoke():
            conn = create_connection(self.db_path, timeout_seconds=5.0)

            def revoke_trace(stmt):
                # Fires when SQLite starts stepping the real UPDATE statement (before acquiring write lock)
                if "UPDATE fixed_product_details" in stmt and not sc1_events["revoke_update_started"].is_set():
                    sc1_results["update_started_before_commit"] = not sc1_events["order_committed"].is_set()
                    sc1_events["revoke_update_started"].set()

            conn.set_trace_callback(revoke_trace)
            try:
                # 1. Wait until create_order is holding the active BEGIN IMMEDIATE transaction
                if not sc1_events["in_transaction"].wait(timeout=5.0):
                    sc1_results["errors"].append("SC1 revoke: timed out waiting for in_transaction signal")
                    return

                # 2. Execute the REAL SQL UPDATE revocation while create_order holds the write lock.
                # This will trigger revoke_trace and then block in SQLite until create_order commits.
                conn.execute(
                    "UPDATE fixed_product_details SET approval_status = 'DRAFT' WHERE product_id = ?",
                    (self.fixed_prod.id,),
                )
                sc1_results["update_completed_after_commit"] = sc1_events["order_committed"].is_set()
                conn.commit()
                sc1_results["revoke_ok"] = True
            except Exception as e:
                sc1_results["errors"].append(f"SC1 revoke error: {e}")
            finally:
                conn.set_trace_callback(None)
                conn.close()

        t1 = threading.Thread(target=sc1_worker_create, name="SC1_CreateOrder")
        t2 = threading.Thread(target=sc1_worker_revoke, name="SC1_RevokeApproval")
        t1.start()
        t2.start()

        # Wait until the real SQL UPDATE revocation has actually started while create_order holds the lock
        self.assertTrue(
            sc1_events["revoke_update_started"].wait(timeout=5.0),
            "Scenario 1: real SQL UPDATE revocation did not start in time",
        )
        # Briefly yield so the revocation thread deterministically enters SQLite busy wait on the held lock
        time.sleep(0.05)
        self.assertEqual(sc1_results["errors"], [], f"Scenario 1 errors before commit: {sc1_results['errors']}")
        self.assertFalse(sc1_events["order_committed"].is_set(), "Scenario 1: create_order committed prematurely")
        self.assertFalse(sc1_results["revoke_ok"], "Scenario 1: revoke completed before create_order committed")
        self.assertTrue(t2.is_alive(), "Scenario 1: revoke thread should be blocked waiting on create_order lock")

        # Only after the real SQL UPDATE has started (and is waiting on the lock), allow create_order to commit
        sc1_events["allow_commit"].set()

        t1.join(timeout=10.0)
        t2.join(timeout=10.0)

        # Assert threads have cleanly terminated
        self.assertFalse(t1.is_alive(), "Scenario 1: create_order thread hung or timed out")
        self.assertFalse(t2.is_alive(), "Scenario 1: revoke thread hung or timed out")

        self.assertEqual(sc1_results["errors"], [], f"Scenario 1 encountered unexpected errors: {sc1_results['errors']}")
        self.assertTrue(sc1_results["update_started_before_commit"], "Scenario 1: real SQL UPDATE must start before create_order commits")
        self.assertTrue(sc1_results["update_completed_after_commit"], "Scenario 1: real SQL UPDATE must wait until create_order commits")
        self.assertTrue(sc1_results["order_created"], "Scenario 1: order should be successfully created")
        self.assertTrue(sc1_results["revoke_ok"], "Scenario 1: revocation should succeed after order creation commits")

        # Verify DB state after Scenario 1
        verify_conn = create_connection(self.db_path)
        cur = verify_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders")
        sc1_order_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM order_items")
        sc1_item_count = cur.fetchone()[0]
        self.assertEqual(sc1_order_count, 1)
        self.assertEqual(sc1_item_count, 1)

        # Verify fixed product approval was indeed updated to DRAFT
        cur.execute("SELECT approval_status FROM fixed_product_details WHERE product_id = ?", (self.fixed_prod.id,))
        self.assertEqual(cur.fetchone()[0], "DRAFT")

        # Clean up orders before Scenario 2
        verify_conn.execute("DELETE FROM order_items")
        verify_conn.execute("DELETE FROM orders")
        verify_conn.commit()

        # --- SCENARIO 2: Revocation commits BEFORE create_order executes ---
        # Revocation commits first -> approval is DRAFT -> create_order MUST raise ValidationError specifically
        sc2_events = {
            "revoked": threading.Event(),
        }
        sc2_results = {"order_validation_error": False, "revoke_ok": False, "errors": []}

        def sc2_worker_revoke():
            conn = create_connection(self.db_path, timeout_seconds=5.0)
            try:
                conn.execute(
                    "UPDATE fixed_product_details SET approval_status = 'DRAFT' WHERE product_id = ?",
                    (self.fixed_prod.id,),
                )
                conn.commit()
                sc2_results["revoke_ok"] = True
                sc2_events["revoked"].set()
            except Exception as e:
                sc2_results["errors"].append(f"SC2 revoke error: {e}")
            finally:
                conn.close()

        def sc2_worker_create():
            if not sc2_events["revoked"].wait(timeout=5.0):
                sc2_results["errors"].append("SC2 create: timed out waiting for revoked signal")
                return

            conn = create_connection(self.db_path, timeout_seconds=5.0)
            repo = OrderRepository(conn)
            try:
                repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=items,
                )
                sc2_results["errors"].append("SC2: create_order unexpectedly succeeded after revocation")
            except ValidationError as ve:
                if "approved" in str(ve).lower():
                    sc2_results["order_validation_error"] = True
                else:
                    sc2_results["errors"].append(f"SC2: Unexpected ValidationError message: {ve}")
            except Exception as e:
                # "database is locked" is strictly forbidden from counting as a pass
                sc2_results["errors"].append(f"SC2: Unexpected create_order exception (not ValidationError): {e}")
            finally:
                conn.close()

        t3 = threading.Thread(target=sc2_worker_revoke, name="SC2_RevokeApproval")
        t4 = threading.Thread(target=sc2_worker_create, name="SC2_CreateOrder")
        t3.start()
        t4.start()
        t3.join(timeout=10.0)
        t4.join(timeout=10.0)

        # Assert threads have cleanly terminated
        self.assertFalse(t3.is_alive(), "Scenario 2: revoke thread hung or timed out")
        self.assertFalse(t4.is_alive(), "Scenario 2: create thread hung or timed out")

        self.assertEqual(sc2_results["errors"], [], f"Scenario 2 encountered unexpected errors: {sc2_results['errors']}")
        self.assertTrue(sc2_results["revoke_ok"], "Scenario 2: revocation should succeed")
        self.assertTrue(sc2_results["order_validation_error"], "Scenario 2: create_order must fail with ValidationError('not approved for sale')")

        # Verify DB state after Scenario 2: exactly 0 orders, 0 order items, no orphans
        cur.execute("SELECT COUNT(*) FROM orders")
        sc2_order_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM order_items")
        sc2_item_count = cur.fetchone()[0]
        self.assertEqual(sc2_order_count, 0)
        self.assertEqual(sc2_item_count, 0)

        # Restore approval for tearDown
        verify_conn.execute("UPDATE fixed_product_details SET approval_status = 'APPROVED' WHERE product_id = ?", (self.fixed_prod.id,))
        verify_conn.commit()
        verify_conn.close()

    def test_transition_order_status_optimistic_locking_concurrency(self):
        """Optimistic locking prevents lost updates when order status is concurrently changed."""
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=14900,
            currency="RUB",
            items=[
                {
                    "product_id": self.fixed_prod.id,
                    "product_type_snapshot": "FIXED_PRODUCT",
                    "quantity": 1,
                    "unit_price_minor": 14900,
                    "currency": "RUB",
                    "metadata_json": None,
                }
            ],
        )
        self.assertEqual(order.status, OrderStatus.PENDING_PAYMENT)

        # Transition order to PAYMENT_REVIEW
        order = self.order_repo.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW, notes="Receipt submitted")
        self.assertEqual(order.status, OrderStatus.PAYMENT_REVIEW)

        conn2 = create_connection(self.db_path)
        try:
            repo2 = OrderRepository(conn2)

            # Actor 1 confirms payment: PAYMENT_REVIEW -> PAID
            self.order_repo.transition_order_status(order.id, OrderStatus.PAID, notes="Payment confirmed")

            # Actor 2 attempts to cancel assuming order is still in PAYMENT_REVIEW
            # With optimistic locking, rowcount is 0 because status was concurrently changed to PAID -> ConflictError
            with self.assertRaises(ConflictError) as cm:
                repo2.transition_order_status(
                    order.id,
                    OrderStatus.CANCELLED,
                    notes="User cancellation",
                    expected_current_status=OrderStatus.PAYMENT_REVIEW,
                )

            self.assertIn("Concurrent state modification", str(cm.exception))

            # Verify final status remains PAID and was not overwritten by CANCELLED
            final_order = self.order_repo.get_order(order.id)
            self.assertEqual(final_order.status, OrderStatus.PAID)
        finally:
            conn2.close()


if __name__ == "__main__":
    unittest.main()
