"""Unit tests for payment receipt deduplication, SHA-256 hashing, and review workflow."""

import os
import tempfile
import unittest

from src.db.connection import create_connection
from src.db.crypto import compute_receipt_sha256
from src.db.migrations import run_migrations
from src.db.models import (
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


class TestReceiptDeduplicationAndWorkflow(unittest.TestCase):
    """Tests for SHA-256 receipt hashing, transaction deduplication, and decoupled review."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_receipts.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.catalog_repo = CatalogRepository(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)
        self.receipt_repo = ReceiptRepository(self.conn)

        self.user, _ = self.user_repo.get_or_create_user("tg_receipt_tester", "receipt_user")
        self.prod = self.catalog_repo.create_voucher_product(
            display_name="PSN TR 500",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.order1 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=160000,
            currency="RUB",
            items=[
                {
                    "product_id": self.prod.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 160000,
                    "metadata_json": None,
                }
            ],
        )
        self.order2 = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=160000,
            currency="RUB",
            items=[
                {
                    "product_id": self.prod.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 160000,
                    "metadata_json": None,
                }
            ],
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_receipt_submission_and_hash_deduplication(self):
        """Verify SHA-256 hash calculation, pending review status, and rejection of exact duplicate."""
        dummy_receipt_pdf = b"%PDF-1.4 Mock Receipt for Order 1 SBP Transfer"
        expected_sha = compute_receipt_sha256(dummy_receipt_pdf)

        receipt = self.receipt_repo.submit_receipt(
            order_id=self.order1.id,
            user_id=self.user.id,
            file_bytes=dummy_receipt_pdf,
            provider_tx_id="SBP-TX-998811",
        )

        self.assertEqual(receipt.file_sha256, expected_sha)
        self.assertEqual(receipt.review_status, ReceiptReviewStatus.PENDING_REVIEW)
        self.assertEqual(receipt.provider_tx_id, "SBP-TX-998811")
        self.assertEqual(receipt.file_size_bytes, len(dummy_receipt_pdf))

        # Order status must NOT automatically become PAID (decoupled architecture)
        order1_refetched = self.order_repo.get_order(self.order1.id)
        self.assertEqual(order1_refetched.status, OrderStatus.PENDING_PAYMENT)

        # Submitting the EXACT same file for another order must be rejected
        with self.assertRaises(DuplicateReceiptError):
            self.receipt_repo.submit_receipt(
                order_id=self.order2.id,
                user_id=self.user.id,
                file_bytes=dummy_receipt_pdf,
                provider_tx_id="SBP-TX-998812",
            )

        # Submitting a DIFFERENT file but SAME provider transaction ID must also be rejected
        different_pdf = b"%PDF-1.4 Different Mock Receipt bytes"
        with self.assertRaises(DuplicateReceiptError):
            self.receipt_repo.submit_receipt(
                order_id=self.order2.id,
                user_id=self.user.id,
                file_bytes=different_pdf,
                provider_tx_id="SBP-TX-998811",  # duplicate tx_id
            )

    def test_validation_size_and_empty_payload(self):
        """Verify empty bytes and oversized files are rejected."""
        with self.assertRaises(ValidationError):
            self.receipt_repo.submit_receipt(
                order_id=self.order1.id,
                user_id=self.user.id,
                file_bytes=b"",
            )

        huge_file = b"X" * (11 * 1024 * 1024)  # 11MB
        with self.assertRaises(ValidationError):
            self.receipt_repo.submit_receipt(
                order_id=self.order1.id,
                user_id=self.user.id,
                file_bytes=huge_file,
            )

    def test_receipt_review_workflow(self):
        """Verify operator approval and rejection updates."""
        receipt_bytes = b"Receipt file 12345"
        receipt = self.receipt_repo.submit_receipt(
            order_id=self.order1.id,
            user_id=self.user.id,
            file_bytes=receipt_bytes,
        )

        # Approve review
        updated = self.receipt_repo.update_review_status(
            receipt_id=receipt.id,
            review_status=ReceiptReviewStatus.ACCEPTED,
            reviewer_notes="Verified via bank statement 2026-10-18",
        )
        self.assertEqual(updated.review_status, ReceiptReviewStatus.ACCEPTED)
        self.assertEqual(updated.reviewer_notes, "Verified via bank statement 2026-10-18")
        self.assertIsNotNone(updated.reviewed_at)


if __name__ == "__main__":
    unittest.main()
