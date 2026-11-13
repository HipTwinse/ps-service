"""Tests for provider-scoped receipts, catalog supply method revocation, and atomic order creation."""

import os
import sqlite3
import tempfile
import unittest

from src.db.connection import create_connection
from src.db.migrations import run_migrations
from src.db.models import (
    ApprovalStatus,
    DeliveryMethod,
    DuplicateReceiptError,
    OrderStatus,
    ProductStatus,
    ReceiptReviewStatus,
    ValidationError,
)
from src.db.repositories import (
    CatalogRepository,
    OrderRepository,
    ReceiptRepository,
    UserRepository,
)


class TestReceiptsAndCatalogRemediation(unittest.TestCase):
    """P1 remediation tests for provider-scoped receipts and catalog approval invariants."""

    def setUp(self):
        os.environ["APP_ENV"] = "test"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_remed.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.catalog_repo = CatalogRepository(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)
        self.receipt_repo = ReceiptRepository(self.conn)

        self.user, _ = self.user_repo.get_or_create_user("tg_user_1", "gamer_1")

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_canonical_provider_name_and_scoped_tx_deduplication(self):
        """Provider name is canonicalized to uppercase stripped string and scoped correctly."""
        voucher_prod = self.catalog_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
        order1 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=1000,
            currency="RUB",
            items=[{"product_id": voucher_prod.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 1000}],
        )
        order2 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=1000,
            currency="RUB",
            items=[{"product_id": voucher_prod.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 1000}],
        )

        # Submit receipt with whitespace and lower-case provider
        rec1 = self.receipt_repo.submit_receipt(
            order_id=order1.id,
            user_id=self.user.id,
            file_bytes=b"Receipt file A",
            provider_name="  sber  ",
            provider_tx_id="TX_COMMON_123",
        )
        self.assertEqual(rec1.provider_name, "SBER")

        # Submitting duplicate under different provider (TINKOFF) succeeds
        rec2 = self.receipt_repo.submit_receipt(
            order_id=order2.id,
            user_id=self.user.id,
            file_bytes=b"Receipt file B",
            provider_name="tinkoff",
            provider_tx_id="TX_COMMON_123",
        )
        self.assertEqual(rec2.provider_name, "TINKOFF")

        # Submitting duplicate under same provider (even with lower-case or spaces) must be rejected
        with self.assertRaises(DuplicateReceiptError):
            self.receipt_repo.submit_receipt(
                order_id=order2.id,
                user_id=self.user.id,
                file_bytes=b"Receipt file C",
                provider_name="SBER",
                provider_tx_id="TX_COMMON_123",
            )

    def test_modifying_supply_method_revokes_approval_and_product_status(self):
        """Updating supply method immediately demotes approval_status and products.status to DRAFT."""
        prod = self.catalog_repo.create_fixed_product(
            display_name="PS Plus 12M",
            price_rub_kopecks=500000,
            delivery_method=DeliveryMethod.OFFICIAL_ACTIVATION,
            supply_method="MANUAL_SELLER_A",
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.APPROVED,
        )
        self.assertEqual(self.catalog_repo.get_product(prod.id).status, ProductStatus.APPROVED)

        # Modify supply method to new vendor
        self.catalog_repo.update_fixed_product_supply_method(prod.id, "AUTOMATED_API_VENDOR_B")

        # Must be revoked to DRAFT
        updated_prod = self.catalog_repo.get_product(prod.id)
        self.assertEqual(updated_prod.status, ProductStatus.DRAFT)

        cur = self.conn.cursor()
        cur.execute("SELECT approval_status, supply_method FROM fixed_product_details WHERE product_id = ?", (prod.id,))
        details = cur.fetchone()
        self.assertEqual(details["approval_status"], "DRAFT")
        self.assertEqual(details["supply_method"], "AUTOMATED_API_VENDOR_B")

    def test_atomic_order_creation_rejects_unapproved_item_without_orphan_records(self):
        """If any item in an order is unapproved, the entire order is rejected and NO records created."""
        valid_prod = self.catalog_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
        unapproved_prod = self.catalog_repo.create_fixed_product(
            display_name="Unapproved Sub",
            price_rub_kopecks=100000,
            delivery_method=DeliveryMethod.OFFICIAL_ACTIVATION,
            supply_method="UNTESTED",
            approval_status=ApprovalStatus.DRAFT,
            status=ProductStatus.DRAFT,
        )

        items = [
            {"product_id": valid_prod.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 1000},
            {"product_id": unapproved_prod.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 100000},
        ]

        with self.assertRaises(ValidationError) as cm:
            self.order_repo.create_order(
                user_id=self.user.id,
                total_amount_minor=101000,
                currency="RUB",
                items=items,
            )
        self.assertIn("not approved for sale", str(cm.exception))

        # Invariant: NO orphan records created in orders or order_items
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT COUNT(*) FROM order_items")
        self.assertEqual(cur.fetchone()[0], 0)

    def test_update_product_status_commits_transaction(self):
        """update_product_status must commit its transaction and not leave connection in open transaction."""
        prod = self.catalog_repo.create_fixed_product(
            display_name="PS Plus Essential",
            price_rub_kopecks=250000,
            delivery_method=DeliveryMethod.OFFICIAL_ACTIVATION,
            supply_method="APPROVED_SUPPLIER",
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.DRAFT,
        )

        self.catalog_repo.update_product_status(prod.id, ProductStatus.APPROVED)

        # Invariant: connection must NOT have an open uncommitted transaction
        self.assertFalse(self.conn.in_transaction)

        # Subsequent transactional operations (such as create_order) must succeed without transaction conflict
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=250000,
            currency="RUB",
            items=[{"product_id": prod.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 250000}],
        )
        self.assertIsNotNone(order)

    def test_strict_typing_catalog_repository(self):
        """CatalogRepository methods must enforce strict typing (reject bool, float, str, None, and non-positive values)."""
        # 1. get_product(product_id)
        for bad_id in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.catalog_repo.get_product(bad_id)

        # 2. update_product_status(product_id, ...)
        for bad_id in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.catalog_repo.update_product_status(bad_id, ProductStatus.APPROVED)

        # 3. create_fixed_product(price_rub_kopecks, duration_days)
        for bad_price in (True, False, 100.5, "100", None, 0, -50):
            with self.assertRaises(ValidationError):
                self.catalog_repo.create_fixed_product(
                    display_name="Test Prod",
                    price_rub_kopecks=bad_price,
                    delivery_method=DeliveryMethod.MANUAL_REVIEW,
                    supply_method="VENDOR_A",
                )

        for bad_days in (True, False, 10.5, "10", 0, -5):
            with self.assertRaises(ValidationError):
                self.catalog_repo.create_fixed_product(
                    display_name="Test Prod",
                    price_rub_kopecks=1000,
                    delivery_method=DeliveryMethod.MANUAL_REVIEW,
                    supply_method="VENDOR_A",
                    duration_days=bad_days,
                )

        # 4. update_fixed_product_supply_method(product_id, ...)
        for bad_id in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.catalog_repo.update_fixed_product_supply_method(bad_id, "VENDOR_B")

        # 5. add_voucher_denomination(product_id, region, currency, face_value_minor, cost_rub_kopecks)
        prod = self.catalog_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)

        for bad_id in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.catalog_repo.add_voucher_denomination(bad_id, "TR", "TRY", 100, 200)

        for bad_denom in (True, False, 100.5, "100", None, 0, -10):
            with self.assertRaises(ValidationError):
                self.catalog_repo.add_voucher_denomination(prod.id, "TR", "TRY", bad_denom, 200)

        for bad_price in (True, False, 200.5, "200", None, -10):
            with self.assertRaises(ValidationError):
                self.catalog_repo.add_voucher_denomination(prod.id, "TR", "TRY", 100, bad_price)

    def test_strict_typing_receipt_repository(self):
        """ReceiptRepository methods must enforce strict typing."""
        voucher_prod = self.catalog_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=1000,
            currency="RUB",
            items=[{"product_id": voucher_prod.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 1000}],
        )

        # 1. submit_receipt(order_id, user_id, ...)
        for bad_oid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.receipt_repo.submit_receipt(
                    order_id=bad_oid,
                    user_id=self.user.id,
                    file_bytes=b"dummy",
                )

        for bad_uid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.receipt_repo.submit_receipt(
                    order_id=order.id,
                    user_id=bad_uid,
                    file_bytes=b"dummy",
                )

        receipt = self.receipt_repo.submit_receipt(
            order_id=order.id,
            user_id=self.user.id,
            file_bytes=b"valid receipt file",
            provider_name="SBER",
        )

        # 2. get_receipt(receipt_id)
        for bad_rid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.receipt_repo.get_receipt(bad_rid)

        # 3. update_review_status(receipt_id, review_status)
        for bad_rid in (True, False, 1.5, "1", None, 0, -1):
            with self.assertRaises(ValidationError):
                self.receipt_repo.update_review_status(
                    receipt_id=bad_rid,
                    review_status=ReceiptReviewStatus.ACCEPTED,
                )


if __name__ == "__main__":
    unittest.main()
