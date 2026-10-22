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

    def test_production_env_with_allow_dev_fallback_unconditionally_forbids_fallback(self):
        """ENV=production with ALLOW_DEV_FALLBACK_KEY=1 must NEVER allow fallback keys or pepper."""
        os.environ["ENV"] = "production"
        os.environ["ALLOW_DEV_FALLBACK_KEY"] = "1"
        os.environ.pop("APP_ENV", None)
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)
        os.environ.pop("INVENTORY_FINGERPRINT_PEPPER", None)

        with self.assertRaises(CryptoConfigurationError):
            get_encryption_key()

        with self.assertRaises(CryptoConfigurationError):
            get_fingerprint_pepper()

    def test_app_env_production_with_allow_dev_fallback_unconditionally_forbids_fallback(self):
        """APP_ENV=production with ALLOW_DEV_FALLBACK_KEY=1 must NEVER allow fallback keys or pepper."""
        os.environ["APP_ENV"] = "production"
        os.environ["ALLOW_DEV_FALLBACK_KEY"] = "1"
        os.environ.pop("ENV", None)
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)
        os.environ.pop("INVENTORY_FINGERPRINT_PEPPER", None)

        with self.assertRaises(CryptoConfigurationError):
            get_encryption_key()

        with self.assertRaises(CryptoConfigurationError):
            get_fingerprint_pepper()

    def test_explicitly_allowed_test_environment_permits_fallback(self):
        """Explicitly allowed test environment (APP_ENV=test or ENV=dev) permits fallback."""
        os.environ["APP_ENV"] = "test"
        os.environ.pop("ENV", None)
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)

        self.assertIsNotNone(get_encryption_key())
        self.assertIsNotNone(get_fingerprint_pepper())

    def test_staging_env_forbids_fallback_even_with_allow_dev_fallback_key(self):
        """ENV=staging with ALLOW_DEV_FALLBACK_KEY=1 must forbid fallback (not an allowed dev/test env)."""
        os.environ["ENV"] = "staging"
        os.environ["ALLOW_DEV_FALLBACK_KEY"] = "1"
        os.environ.pop("APP_ENV", None)
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)

        with self.assertRaises(CryptoConfigurationError):
            get_encryption_key()

        with self.assertRaises(CryptoConfigurationError):
            get_fingerprint_pepper()

    def test_unset_env_forbids_fallback_even_with_allow_dev_fallback_key(self):
        """Unset ENV and APP_ENV with ALLOW_DEV_FALLBACK_KEY=1 must forbid fallback."""
        os.environ.pop("ENV", None)
        os.environ.pop("APP_ENV", None)
        os.environ["ALLOW_DEV_FALLBACK_KEY"] = "1"
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)

        with self.assertRaises(CryptoConfigurationError):
            get_encryption_key()

        with self.assertRaises(CryptoConfigurationError):
            get_fingerprint_pepper()

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

    def test_rotate_fingerprint_pepper_error_mid_rotation_rolls_back_atomically(self):
        """If an error occurs mid-rotation, transaction rolls back and all fingerprints remain unchanged."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test_rot_err.db")
            conn = create_connection(db_path)
            run_migrations(conn)

            cat_repo = CatalogRepository(conn)
            prod = cat_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
            denom = cat_repo.add_voucher_denomination(prod.id, "TR", "TRY", 1000, 3000)
            inv_repo = InventoryRepository(conn)

            codes = ["ROT-ITEM-1", "ROT-ITEM-2", "ROT-ITEM-3"]
            inv_repo.import_items(prod.id, codes, InventoryType.VOUCHER_CODE, denom.id)

            cur = conn.cursor()
            cur.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC")
            original_fps = {row["id"]: row["fingerprint"] for row in cur.fetchall()}

            new_pepper = b"fresh_secret_pepper_12345"

            # Mock compute_fingerprint to fail on the second item
            real_compute = compute_fingerprint
            call_count = 0

            def faulty_compute(code, pepper=None):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("Simulated transient failure on second item")
                return real_compute(code, pepper=pepper)

            from unittest.mock import patch
            with patch("src.db.repositories.compute_fingerprint", side_effect=faulty_compute):
                with self.assertRaises(RuntimeError):
                    inv_repo.rotate_fingerprint_pepper(new_pepper=new_pepper)

            # Confirm ALL fingerprints remain in their exact pre-rotation state
            cur.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC")
            current_fps = {row["id"]: row["fingerprint"] for row in cur.fetchall()}
            self.assertEqual(current_fps, original_fps)
            conn.close()

    def test_deduplication_works_correctly_after_pepper_rotation(self):
        """After successful pepper rotation, importing identical code with new pepper is properly deduplicated."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test_rot_dedup.db")
            conn = create_connection(db_path)
            run_migrations(conn)

            cat_repo = CatalogRepository(conn)
            prod = cat_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
            denom = cat_repo.add_voucher_denomination(prod.id, "TR", "TRY", 1000, 3000)
            inv_repo = InventoryRepository(conn)

            test_code = "DEDUP-PEPPER-TEST-CODE"
            # 1. Import with initial default pepper
            r1 = inv_repo.import_items(prod.id, [test_code], InventoryType.VOUCHER_CODE, denom.id)
            self.assertEqual(r1["accepted"], 1)

            # 2. Rotate to new pepper
            new_pepper = b"brand_new_rotated_pepper_key_7777"
            inv_repo.rotate_fingerprint_pepper(new_pepper=new_pepper)

            # 3. Simulate environment secret switch to new_pepper
            os.environ["FINGERPRINT_PEPPER"] = new_pepper.decode("utf-8")

            # 4. Attempt to import the exact same code under new pepper
            r2 = inv_repo.import_items(prod.id, [test_code], InventoryType.VOUCHER_CODE, denom.id)
            self.assertEqual(r2["accepted"], 0)
            self.assertEqual(r2["duplicates"], 1)
            conn.close()

    def test_rotate_fingerprint_pepper_rejected_if_connection_in_active_transaction(self):
        """Calling rotate_fingerprint_pepper with uncommitted active transaction must raise RuntimeError."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test_rot_tx.db")
            conn = create_connection(db_path)
            run_migrations(conn)

            # Start an uncommitted transaction
            conn.execute("INSERT INTO users (external_id, username, status) VALUES ('active_caller', 'caller', 'ACTIVE')")
            self.assertTrue(conn.in_transaction)

            inv_repo = InventoryRepository(conn)
            with self.assertRaises(RuntimeError) as cm:
                inv_repo.rotate_fingerprint_pepper(b"new_pepper_123")

            self.assertIn("active uncommitted transaction", str(cm.exception))

            # Caller's transaction must remain uncommitted and intact
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            conn.close()

    def test_pepper_rotation_failure_recovery_allows_continuation_with_old_pepper(self):
        """If rotation fails and rolls back, application can safely continue imports with old pepper."""
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "test_rot_rec.db")
            conn = create_connection(db_path)
            run_migrations(conn)

            cat_repo = CatalogRepository(conn)
            prod = cat_repo.create_voucher_product("TR Card", "TR", "TRY", ProductStatus.APPROVED)
            denom = cat_repo.add_voucher_denomination(prod.id, "TR", "TRY", 1000, 3000)
            inv_repo = InventoryRepository(conn)

            # 1. Import baseline item
            inv_repo.import_items(prod.id, ["INITIAL-CODE-1"], InventoryType.VOUCHER_CODE, denom.id)

            # 2. Simulate failed rotation
            from unittest.mock import patch
            with patch("src.db.repositories.compute_fingerprint", side_effect=RuntimeError("Transient failure")):
                with self.assertRaises(RuntimeError):
                    inv_repo.rotate_fingerprint_pepper(b"aborted_pepper_secret")

            # 3. Verify recovery: system operates normally with old pepper, detecting duplicates
            dup_result = inv_repo.import_items(prod.id, ["INITIAL-CODE-1"], InventoryType.VOUCHER_CODE, denom.id)
            self.assertEqual(dup_result["accepted"], 0)
            self.assertEqual(dup_result["duplicates"], 1)

            # 4. Verify fresh imports work with old pepper
            new_result = inv_repo.import_items(prod.id, ["NEW-CODE-AFTER-FAIL"], InventoryType.VOUCHER_CODE, denom.id)
            self.assertEqual(new_result["accepted"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
