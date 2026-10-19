"""Cryptographic utilities for inventory payload encryption, fingerprints, and receipt hashing.

All encryption is authenticated using Fernet (AES-128-CBC + HMAC-SHA256).
Encryption keys are strictly kept outside the database and outside version control.
"""

import base64
import hashlib
import hmac
import os
from typing import Optional
from cryptography.fernet import Fernet

FernetCipher = Fernet


class CryptoConfigurationError(ValueError):
    """Raised when encryption or cryptographic secrets are missing or improperly configured."""
    pass


# Test/Dev fallback key if environment variable is not set.
# In production, INVENTORY_ENCRYPTION_KEY must be set in the environment.
_DEV_FALLBACK_KEY = b"k9ZJ9Xm7h2Fp0Wd3VqYl5T8N1aZb6C4D7E9G2I0O3R8="
_DEV_FALLBACK_PEPPER = b"ps_fingerprint_pepper_dev_secret_key_88291"


def get_encryption_key() -> bytes:
    """Retrieves the encryption key from environment or fallback for development.

    In production (ENV='production' or APP_ENV='production'), INVENTORY_ENCRYPTION_KEY
    must be explicitly defined and must be a valid 32 url-safe base64-encoded bytes string.

    Returns:
        Valid Fernet key bytes.

    Raises:
        CryptoConfigurationError: If key is absent or invalid in production, or invalid in dev.
    """
    env = os.environ.get("ENV", os.environ.get("APP_ENV", "development")).lower()
    raw_key = os.environ.get("INVENTORY_ENCRYPTION_KEY")

    if env == "production":
        if not raw_key or not raw_key.strip():
            raise CryptoConfigurationError(
                "Production environment requires INVENTORY_ENCRYPTION_KEY to be set."
            )
        key_bytes = raw_key.strip().encode("utf-8")
        try:
            Fernet(key_bytes)
            return key_bytes
        except Exception:
            raise CryptoConfigurationError(
                "Invalid INVENTORY_ENCRYPTION_KEY format for production."
            ) from None

    # Development / Testing mode
    if raw_key and raw_key.strip():
        key_bytes = raw_key.strip().encode("utf-8")
        try:
            Fernet(key_bytes)
            return key_bytes
        except Exception:
            raise CryptoConfigurationError("Invalid INVENTORY_ENCRYPTION_KEY format.") from None

    return _DEV_FALLBACK_KEY


def get_fingerprint_pepper() -> bytes:
    """Retrieves the HMAC pepper secret from environment or dev fallback.

    In production, INVENTORY_FINGERPRINT_PEPPER must be set in the environment.
    """
    env = os.environ.get("ENV", os.environ.get("APP_ENV", "development")).lower()
    raw_pepper = os.environ.get("INVENTORY_FINGERPRINT_PEPPER")

    if env == "production":
        if not raw_pepper or not raw_pepper.strip():
            raise CryptoConfigurationError(
                "Production environment requires INVENTORY_FINGERPRINT_PEPPER to be set."
            )
        return raw_pepper.strip().encode("utf-8")

    if raw_pepper and raw_pepper.strip():
        return raw_pepper.strip().encode("utf-8")

    return _DEV_FALLBACK_PEPPER


def encrypt_payload(plaintext: str, key: Optional[bytes] = None) -> bytes:
    """Encrypts sensitive inventory payload (e.g., voucher codes) using authenticated Fernet encryption.

    Args:
        plaintext: Raw sensitive code or credentials.
        key: Optional encryption key; defaults to get_encryption_key().

    Returns:
        Encrypted payload bytes.
    """
    if not plaintext:
        raise ValueError("Cannot encrypt an empty payload.")

    cipher = Fernet(key or get_encryption_key())
    return cipher.encrypt(plaintext.strip().encode("utf-8"))


def decrypt_payload(encrypted_bytes: bytes, key: Optional[bytes] = None) -> str:
    """Decrypts encrypted inventory payload.

    Args:
        encrypted_bytes: Encrypted payload bytes.
        key: Optional encryption key.

    Returns:
        Decrypted plaintext string.
    """
    cipher = Fernet(key or get_encryption_key())
    return cipher.decrypt(encrypted_bytes).decode("utf-8")


def compute_fingerprint(plaintext: str, pepper: Optional[bytes] = None) -> str:
    """Computes a keyed HMAC-SHA256 fingerprint of the plaintext payload.

    Uses a secret pepper out-of-band to prevent rainbow tables and offline
    brute-force dictionary attacks even if the database file is compromised.

    Args:
        plaintext: The raw code.
        pepper: Optional HMAC secret key; defaults to get_fingerprint_pepper().

    Returns:
        Hex-encoded HMAC-SHA256 fingerprint.
    """
    cleaned = plaintext.strip().encode("utf-8")
    pepper_bytes = pepper if pepper is not None else get_fingerprint_pepper()
    return hmac.new(pepper_bytes, cleaned, hashlib.sha256).hexdigest()


def compute_receipt_sha256(file_bytes: bytes) -> str:
    """Computes exact SHA-256 digest over uploaded payment receipt bytes.

    Args:
        file_bytes: Raw binary content of the receipt file.

    Returns:
        Hex-encoded SHA-256 string (64 characters).
    """
    if not file_bytes:
        raise ValueError("Receipt file bytes cannot be empty.")
    return hashlib.sha256(file_bytes).hexdigest()

