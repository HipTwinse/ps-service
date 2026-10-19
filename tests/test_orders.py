"""Unit tests for orders, historical price snapshots, and lifecycle state machine transitions."""

import os
import tempfile
import unittest

from src.db.connection import create_connection
from src.db.migrations import run_migrations
from src.db.models import (
    ApprovalStatus,
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


if __name__ == "__main__":
    unittest.main()
