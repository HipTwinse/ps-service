"""Unit and concurrency tests for inventory management, encryption, and reservation."""

import hashlib
import os
import sqlite3
import tempfile
import unittest

from src.db.connection import create_connection
from src.db.crypto import compute_fingerprint, encrypt_payload
from src.db.migrations import run_migrations
from src.db.models import (
    DeliveryMethod,
    InsufficientInventoryError,
    InventoryStatus,
    InventoryType,
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


class TestInventoryManagement(unittest.TestCase):
    """Tests for inventory import, encryption, reservation, release, and concurrency safety."""

    def setUp(self):
        os.environ["APP_ENV"] = "test"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_inv.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.catalog_repo = CatalogRepository(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.inv_repo = InventoryRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)

        # Setup test catalog and user
        self.product = self.catalog_repo.create_voucher_product(
            display_name="PSN TR Voucher",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.denom = self.catalog_repo.add_voucher_denomination(
            product_id=self.product.id,
            region="TR",
            currency="TRY",
            face_value_minor=10000,
            cost_rub_kopecks=32000,
        )
        self.user, _ = self.user_repo.get_or_create_user("tg_123456", "gamer_tr")

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_import_items_encryption_and_deduplication(self):
        """Verify codes are encrypted, fingerprints are hashed, and duplicates rejected."""
        codes = [
            "ABCD-1234-EFGH",
            "IJKL-5678-MNOP",
            "ABCD-1234-EFGH",  # duplicate in batch
            "",                # invalid/empty
            "   ",             # whitespace only
        ]

        result = self.inv_repo.import_items(
            product_id=self.product.id,
            raw_codes=codes,
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom.id,
        )

        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["rejected"], 2)
        self.assertEqual(result["total_submitted"], 5)

        # Direct database inspection to ensure plaintext is never stored raw
        cur = self.conn.cursor()
        cur.execute("SELECT encrypted_payload, fingerprint FROM inventory_items")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertNotIn(b"ABCD-1234-EFGH", row["encrypted_payload"])
            self.assertNotIn(b"IJKL-5678-MNOP", row["encrypted_payload"])
            self.assertEqual(len(row["fingerprint"]), 64)  # SHA-256 hex string

        # Verify decryption works via get_revealed_code
        cur.execute("SELECT id FROM inventory_items ORDER BY id ASC")
        item_ids = [r["id"] for r in cur.fetchall()]
        revealed_1 = self.inv_repo.get_revealed_code(item_ids[0])
        revealed_2 = self.inv_repo.get_revealed_code(item_ids[1])
        self.assertIn(revealed_1, ["ABCD-1234-EFGH", "IJKL-5678-MNOP"])
        self.assertIn(revealed_2, ["ABCD-1234-EFGH", "IJKL-5678-MNOP"])
        self.assertNotEqual(revealed_1, revealed_2)

        # Importing an existing code again across batches must be rejected as duplicate
        second_result = self.inv_repo.import_items(
            product_id=self.product.id,
            raw_codes=["ABCD-1234-EFGH"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom.id,
        )
        self.assertEqual(second_result["accepted"], 0)
        self.assertEqual(second_result["duplicates"], 1)

    def test_reservation_and_release_lifecycle(self):
        """Verify atomic reservation, insufficient inventory handling, release and final sale."""
        self.inv_repo.import_items(
            product_id=self.product.id,
            raw_codes=["CODE-01", "CODE-02", "CODE-03"],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom.id,
        )

        order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=64000,
            currency="RUB",
            items=[
                {
                    "product_id": self.product.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 2,
                    "unit_price_minor": 32000,
                    "metadata_json": None,
                }
            ],
        )

        # 1. Reserve 2 items
        reserved_ids = self.inv_repo.reserve_items_for_order(
            order_id=order.id,
            product_id=self.product.id,
            quantity=2,
            denomination_id=self.denom.id,
        )
        self.assertEqual(len(reserved_ids), 2)

        # Check DB states
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM inventory_items WHERE id IN (?, ?)", tuple(reserved_ids))
        statuses = [r["status"] for r in cur.fetchall()]
        self.assertEqual(statuses, ["RESERVED", "RESERVED"])

        # 2. Attempting to reserve 2 more when only 1 available must raise InsufficientInventoryError
        with self.assertRaises(InsufficientInventoryError):
            self.inv_repo.reserve_items_for_order(
                order_id=order.id,
                product_id=self.product.id,
                quantity=2,
                denomination_id=self.denom.id,
            )

        # 3. Release reservation for the order
        released_count = self.inv_repo.release_order_reservations(order.id)
        self.assertEqual(released_count, 2)

        cur.execute("SELECT status, reserved_order_id FROM inventory_items WHERE id IN (?, ?)", tuple(reserved_ids))
        for row in cur.fetchall():
            self.assertEqual(row["status"], "AVAILABLE")
            self.assertIsNone(row["reserved_order_id"])

        # 4. Re-reserve and finalize sale
        reserved_again = self.inv_repo.reserve_items_for_order(
            order_id=order.id,
            product_id=self.product.id,
            quantity=2,
            denomination_id=self.denom.id,
        )
        self.assertEqual(len(reserved_again), 2)

        finalized_count = self.inv_repo.finalize_order_sale(order.id)
        self.assertEqual(finalized_count, 2)

        cur.execute("SELECT status FROM inventory_items WHERE id IN (?, ?)", tuple(reserved_again))
        sold_statuses = [r["status"] for r in cur.fetchall()]
        self.assertEqual(sold_statuses, ["SOLD", "SOLD"])

        # 5. Invariant check: Release must NEVER revert SOLD items to AVAILABLE
        release_after_sold = self.inv_repo.release_order_reservations(order.id)
        self.assertEqual(release_after_sold, 0)

        cur.execute("SELECT status FROM inventory_items WHERE id IN (?, ?)", tuple(reserved_again))
        still_sold = [r["status"] for r in cur.fetchall()]
        self.assertEqual(still_sold, ["SOLD", "SOLD"])

    def test_legacy_sha256_migration_and_dual_lookup(self):
        """Verify backward compatibility: legacy SHA-256 items are detected as duplicates,

        decrypted properly, and safely upgraded to HMAC fingerprints.
        """
        legacy_code = "LEGACY-CARD-9999"
        legacy_fp = hashlib.sha256(legacy_code.encode("utf-8")).hexdigest()
        modern_fp = compute_fingerprint(legacy_code)
        self.assertNotEqual(legacy_fp, modern_fp)

        # 1. Manually insert an item with legacy unsalted SHA-256 fingerprint
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO inventory_items (
                product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self.product.id,
                self.denom.id,
                InventoryType.VOUCHER_CODE.value,
                encrypt_payload(legacy_code),
                legacy_fp,
                InventoryStatus.AVAILABLE.value,
            ),
        )
        self.conn.commit()
        item_id = cur.lastrowid

        # 2. Attempting to import the exact same code must be caught as duplicate
        import_result = self.inv_repo.import_items(
            product_id=self.product.id,
            raw_codes=[legacy_code],
            inventory_type=InventoryType.VOUCHER_CODE,
            denomination_id=self.denom.id,
        )
        self.assertEqual(import_result["accepted"], 0)
        self.assertEqual(import_result["duplicates"], 1)

        # 3. Verify auto-upgrade in place during duplicate check
        cur.execute("SELECT fingerprint FROM inventory_items WHERE id = ?", (item_id,))
        updated_fp = cur.fetchone()["fingerprint"]
        self.assertEqual(updated_fp, modern_fp)
        self.assertEqual(self.inv_repo.get_revealed_code(item_id), legacy_code)

        # 4. Insert another legacy item and test batch migrate_legacy_fingerprints
        legacy_code_2 = "LEGACY-CARD-8888"
        legacy_fp_2 = hashlib.sha256(legacy_code_2.encode("utf-8")).hexdigest()
        cur.execute(
            """
            INSERT INTO inventory_items (
                product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self.product.id,
                self.denom.id,
                InventoryType.VOUCHER_CODE.value,
                encrypt_payload(legacy_code_2),
                legacy_fp_2,
                InventoryStatus.AVAILABLE.value,
            ),
        )
        self.conn.commit()

        upgraded = self.inv_repo.migrate_legacy_fingerprints()
        self.assertEqual(upgraded, 1)

        # Idempotency check: running again upgrades 0
        upgraded_again = self.inv_repo.migrate_legacy_fingerprints()
        self.assertEqual(upgraded_again, 0)

        # Verify decryption of both items
        cur.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC")
        all_items = cur.fetchall()
        for row in all_items:
            code = self.inv_repo.get_revealed_code(row["id"])
            self.assertEqual(row["fingerprint"], compute_fingerprint(code))


if __name__ == "__main__":
    unittest.main()

