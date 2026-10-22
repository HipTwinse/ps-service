"""Unit tests for orders, historical price snapshots, and lifecycle state machine transitions."""

import os
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

    def test_real_concurrency_approval_revocation_during_create_order(self):
        """Real concurrency test: create_order vs concurrent supply approval revocation."""
        import threading
        results = {"order_created": 0, "rejected_or_locked": 0, "errors": []}

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

        def worker_create_order():
            t_conn = create_connection(self.db_path, timeout_seconds=0.2)
            t_repo = OrderRepository(t_conn)
            try:
                t_repo.create_order(
                    user_id=self.user.id,
                    total_amount_minor=14900,
                    currency="RUB",
                    items=items,
                )
                results["order_created"] += 1
            except ValidationError:
                results["rejected_or_locked"] += 1
            except Exception as e:
                if "locked" in str(e).lower():
                    results["rejected_or_locked"] += 1
                else:
                    results["errors"].append(f"Unexpected create_order error: {e}")
            finally:
                t_conn.close()

        def worker_revoke_approval():
            t_conn = create_connection(self.db_path, timeout_seconds=0.2)
            try:
                t_conn.execute(
                    "UPDATE fixed_product_details SET approval_status = 'DRAFT' WHERE product_id = ?",
                    (self.fixed_prod.id,),
                )
                t_conn.commit()
            except Exception as e:
                if "locked" not in str(e).lower():
                    results["errors"].append(f"Unexpected revoke error: {e}")
            finally:
                t_conn.close()

        t1 = threading.Thread(target=worker_create_order)
        t2 = threading.Thread(target=worker_revoke_approval)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results["errors"], [])

        # Consistency verification: if approval was revoked, check that any created order was created BEFORE revocation
        check_conn = create_connection(self.db_path)
        cur = check_conn.cursor()
        cur.execute("SELECT approval_status FROM fixed_product_details WHERE product_id = ?", (self.fixed_prod.id,))
        final_approval = cur.fetchone()["approval_status"]

        cur.execute("SELECT COUNT(*) FROM orders")
        order_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM order_items")
        item_count = cur.fetchone()[0]

        if order_count == 1:
            self.assertEqual(item_count, 1)
        else:
            self.assertEqual(order_count, 0)
            self.assertEqual(item_count, 0)

        # Restore approval for tearDown
        check_conn.execute("UPDATE fixed_product_details SET approval_status = 'APPROVED' WHERE product_id = ?", (self.fixed_prod.id,))
        check_conn.commit()
        check_conn.close()

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
