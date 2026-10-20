"""Tests for encryption keys, pepper enforcement, canonicalization, and pepper rotation."""

import hashlib
import os
import sqlite3
import tempfile
import unittest
from cryptography.fernet import Fernet

from src.db.connection import create_connection
from src.db.crypto import (
    CryptoConfigurationError,
    canonicalize_code,
    compute_fingerprint,
    decrypt_payload,
    encrypt_payload,
    get_encryption_key,
    get_fingerprint_pepper,
)
from src.db.migrations import run_migrations
from src.db.models import InventoryType, ProductStatus
from src.db.repositories import CatalogRepository, InventoryRepository


class TestCryptoRemediation(unittest.TestCase):
    """P0/P1 remediation tests for crypto configurations, canonicalization, and pepper rotation."""

    def setUp(self):
        self.orig_env = os.environ.copy()
        os.environ["APP_ENV"] = "test"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_env)

    def test_production_missing_secrets_aborts_safely(self):
        """In production environment without secrets, safe failure occurs."""
        os.environ["ENV"] = "production"
        os.environ.pop("APP_ENV", None)
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)
        os.environ.pop("INVENTORY_FINGERPRINT_PEPPER", None)

        with self.assertRaises(CryptoConfigurationError):
            get_encryption_key()

        with self.assertRaises(CryptoConfigurationError):
            get_fingerprint_pepper()

    def test_canonicalization_whitespace_and_newlines(self):
        """Canonicalization strips boundary whitespace, carriage returns, and newlines."""
        self.assertEqual(canonicalize_code("  ABCD-1234  "), "ABCD-1234")
        self.assertEqual(canonicalize_code("ABCD-1234\r\n"), "ABCD-1234")
        self.assertEqual(canonicalize_code("\t\nABCD-1234 \r\n"), "ABCD-1234")
        self.assertEqual(canonicalize_code(""), "")
        self.assertEqual(canonicalize_code(None), "")

    def test_inventory_deduplication_with_whitespace_variations(self):
        """Codes with different whitespace/newlines must resolve to same fingerprint and be deduplicated."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test_canon.db")
            conn = create_connection(db_path)
            run_migrations(conn)

            cat_repo = CatalogRepository(conn)
            prod = cat_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
            denom = cat_repo.add_voucher_denomination(prod.id, "TR", "TRY", 1000, 3000)

            inv_repo = InventoryRepository(conn)

            # First import with clean code
            r1 = inv_repo.import_items(
                product_id=prod.id,
                raw_codes=["MY-SECRET-CODE-001"],
                inventory_type=InventoryType.VOUCHER_CODE,
                denomination_id=denom.id,
            )
            self.assertEqual(r1["accepted"], 1)

            # Second import with \r\n and whitespace variations of the exact same code
            r2 = inv_repo.import_items(
                product_id=prod.id,
                raw_codes=[
                    "  MY-SECRET-CODE-001  ",
                    "MY-SECRET-CODE-001\r\n",
                    "\tMY-SECRET-CODE-001\n",
                ],
                inventory_type=InventoryType.VOUCHER_CODE,
                denomination_id=denom.id,
            )
            self.assertEqual(r2["accepted"], 0)
            self.assertEqual(r2["duplicates"], 3)

            # Decrypted payload in DB must be canonical
            cur = conn.cursor()
            cur.execute("SELECT id FROM inventory_items")
            item_id = cur.fetchone()["id"]
            self.assertEqual(inv_repo.get_revealed_code(item_id), "MY-SECRET-CODE-001")
            conn.close()

    def test_rotate_fingerprint_pepper_maintains_integrity(self):
        """Rotating pepper updates all fingerprints correctly while preserving decrypted payload."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test_rot.db")
            conn = create_connection(db_path)
            run_migrations(conn)

            cat_repo = CatalogRepository(conn)
            prod = cat_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
            denom = cat_repo.add_voucher_denomination(prod.id, "TR", "TRY", 1000, 3000)
            inv_repo = InventoryRepository(conn)

            codes = ["CARD-A-1", "CARD-B-2"]
            inv_repo.import_items(prod.id, codes, InventoryType.VOUCHER_CODE, denom.id)

            old_pepper = get_fingerprint_pepper()
            new_pepper = b"new_pepper_secret_rotation_key_9999"

            rotated_count = inv_repo.rotate_fingerprint_pepper(new_pepper=new_pepper)
            self.assertEqual(rotated_count, 2)

            # Verify all fingerprints now match new_pepper
            cur = conn.cursor()
            cur.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC")
            rows = cur.fetchall()
            for row in rows:
                code = inv_repo.get_revealed_code(row["id"])
                expected_fp = compute_fingerprint(code, pepper=new_pepper)
                self.assertEqual(row["fingerprint"], expected_fp)
                self.assertNotEqual(row["fingerprint"], compute_fingerprint(code, pepper=old_pepper))

            # Idempotency: rotating again with same new_pepper modifies 0 rows
            self.assertEqual(inv_repo.rotate_fingerprint_pepper(new_pepper=new_pepper), 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
