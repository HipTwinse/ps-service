"""Unit tests for cryptographic functions, production key safety, and HMAC fingerprints."""

import os
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet

from src.db.crypto import (
    CryptoConfigurationError,
    _DEV_FALLBACK_KEY,
    compute_fingerprint,
    compute_receipt_sha256,
    decrypt_payload,
    encrypt_payload,
    get_encryption_key,
    get_fingerprint_pepper,
)


class TestCryptoSecurityAndConfiguration(unittest.TestCase):
    """Tests for production key isolation, secret leak prevention, and HMAC fingerprints."""

    def setUp(self):
        self.original_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_production_missing_key_raises_error(self):
        """In production environment, missing encryption key must raise CryptoConfigurationError."""
        os.environ["ENV"] = "production"
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)

        with self.assertRaises(CryptoConfigurationError) as cm:
            get_encryption_key()

        self.assertIn("INVENTORY_ENCRYPTION_KEY to be set", str(cm.exception))

    def test_production_invalid_key_raises_error_without_leaking_key(self):
        """In production environment, corrupted key must raise error without leaking secret string."""
        os.environ["ENV"] = "production"
        fake_secret_key = "my_super_secret_corrupted_key_value!!!"
        os.environ["INVENTORY_ENCRYPTION_KEY"] = fake_secret_key

        with self.assertRaises(CryptoConfigurationError) as cm:
            get_encryption_key()

        # Secret must never be in exception message
        self.assertNotIn(fake_secret_key, str(cm.exception))
        self.assertIn("Invalid INVENTORY_ENCRYPTION_KEY", str(cm.exception))

    def test_production_valid_key_encrypts_and_decrypts(self):
        """In production environment, valid key functions correctly."""
        os.environ["ENV"] = "production"
        valid_prod_key = Fernet.generate_key().decode("utf-8")
        os.environ["INVENTORY_ENCRYPTION_KEY"] = valid_prod_key
        os.environ["INVENTORY_FINGERPRINT_PEPPER"] = "prod_pepper_secret_9988"

        raw_text = "PSN-CARD-PROD-9988"
        encrypted = encrypt_payload(raw_text)
        decrypted = decrypt_payload(encrypted)
        self.assertEqual(decrypted, raw_text)

    def test_dev_fallback_compatibility(self):
        """In development environment, missing key falls back safely for local development."""
        os.environ["ENV"] = "development"
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)

        key = get_encryption_key()
        self.assertEqual(key, _DEV_FALLBACK_KEY)

        raw_text = "PSN-DEV-1234"
        encrypted = encrypt_payload(raw_text)
        self.assertEqual(decrypt_payload(encrypted), raw_text)

    def test_hmac_fingerprint_deterministic_and_keyed(self):
        """HMAC fingerprint must be deterministic for same pepper, but different for different peppers."""
        code = "ABCD-1234-EFGH"
        pepper_a = b"pepper_alpha_0001"
        pepper_b = b"pepper_beta_00002"

        fp1 = compute_fingerprint(code, pepper=pepper_a)
        fp2 = compute_fingerprint(code, pepper=pepper_a)
        fp_other = compute_fingerprint(code, pepper=pepper_b)

        self.assertEqual(fp1, fp2)
        self.assertNotEqual(fp1, fp_other)
        self.assertEqual(len(fp1), 64)  # Hex representation of SHA-256


if __name__ == "__main__":
    unittest.main()
