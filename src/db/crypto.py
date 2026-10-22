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


# Test fallback key for designated test/development environments only.
# In production, INVENTORY_ENCRYPTION_KEY and FINGERPRINT_PEPPER must be explicitly configured.
_DEV_FALLBACK_KEY = b"k9ZJ9Xm7h2Fp0Wd3VqYl5T8N1aZb6C4D7E9G2I0O3R8="
_DEV_FALLBACK_PEPPER = b"ps_fingerprint_pepper_dev_secret_key_88291"

_PRODUCTION_ENVS = {"production", "prod"}
_ALLOWED_DEV_ENVS = {"test", "testing", "dev", "development"}


def is_dev_or_test_environment() -> bool:
    """Checks whether the execution context is an explicitly permitted dev/test environment.

    Security Policy:
    1. Production designations ('production', 'prod') strictly and unconditionally
       forbid fallback secrets under all circumstances.
    2. Fallback secrets are ONLY permitted in explicitly designated dev/test environments
       (ENV or APP_ENV in {'test', 'testing', 'dev', 'development'}).
    3. Undefined, staging, or unknown environments NEVER permit fallback secrets,
       even if ALLOW_DEV_FALLBACK_KEY is set.
    """
    env = os.environ.get("ENV", "").strip().lower()
    app_env = os.environ.get("APP_ENV", "").strip().lower()

    # Any production designation strictly and unconditionally overrides everything
    if env in _PRODUCTION_ENVS or app_env in _PRODUCTION_ENVS:
        return False

    # Fallback is strictly restricted to explicitly designated test/dev environments
    return app_env in _ALLOWED_DEV_ENVS or env in _ALLOWED_DEV_ENVS


def canonicalize_code(code: str) -> str:
    """Canonicalizes a raw code string for deterministic hashing, validation, and storage.

    Strips leading and trailing whitespace, carriage returns, and newlines.
    """
    if not code:
        return ""
    return code.strip()


def get_encryption_key() -> bytes:
    """Retrieves the encryption key from environment.

    In production, INVENTORY_ENCRYPTION_KEY must be explicitly set and be a valid Fernet key.
    Fallback test key is only permitted in explicitly designated dev/test environments.

    Returns:
        Valid Fernet key bytes.

    Raises:
        CryptoConfigurationError: If key is absent or invalid in production, or invalid in test/dev.
    """
    raw_key = os.environ.get("INVENTORY_ENCRYPTION_KEY")

    if raw_key and raw_key.strip():
        key_bytes = raw_key.strip().encode("utf-8")
        try:
            Fernet(key_bytes)
            return key_bytes
        except Exception:
            raise CryptoConfigurationError(
                "Invalid INVENTORY_ENCRYPTION_KEY: Must be a valid 32 url-safe base64-encoded key."
            ) from None

    # Fallback only allowed in explicit dev/test environment
    if is_dev_or_test_environment():
        return _DEV_FALLBACK_KEY

    raise CryptoConfigurationError(
        "Production environment requires INVENTORY_ENCRYPTION_KEY to be set."
    )


def get_fingerprint_pepper() -> bytes:
    """Retrieves the HMAC pepper secret from environment.

    In production, FINGERPRINT_PEPPER (or INVENTORY_FINGERPRINT_PEPPER) must be set.
    Fallback test pepper is only permitted in explicitly designated dev/test environments.

    Returns:
        Pepper bytes.

    Raises:
        CryptoConfigurationError: If pepper is absent in production.
    """
    raw_pepper = os.environ.get("FINGERPRINT_PEPPER") or os.environ.get("INVENTORY_FINGERPRINT_PEPPER")

    if raw_pepper and raw_pepper.strip():
        return raw_pepper.strip().encode("utf-8")

    if is_dev_or_test_environment():
        return _DEV_FALLBACK_PEPPER

    raise CryptoConfigurationError(
        "Production environment requires FINGERPRINT_PEPPER to be set."
    )


def encrypt_payload(plaintext: str, key: Optional[bytes] = None) -> bytes:
    """Encrypts sensitive inventory payload (e.g., voucher codes) using authenticated Fernet encryption.

    Args:
        plaintext: Raw sensitive code or credentials.
        key: Optional encryption key; defaults to get_encryption_key().

    Returns:
        Encrypted payload bytes.
    """
    cleaned = canonicalize_code(plaintext)
    if not cleaned:
        raise ValueError("Cannot encrypt an empty payload.")

    cipher = Fernet(key or get_encryption_key())
    return cipher.encrypt(cleaned.encode("utf-8"))


def decrypt_payload(encrypted_bytes: bytes, key: Optional[bytes] = None) -> str:
    """Decrypts encrypted inventory payload.

    Args:
        encrypted_bytes: Encrypted payload bytes.
        key: Optional encryption key.

    Returns:
        Decrypted canonical plaintext string.
    """
    cipher = Fernet(key or get_encryption_key())
    return cipher.decrypt(encrypted_bytes).decode("utf-8")


def compute_fingerprint(plaintext: str, pepper: Optional[bytes] = None) -> str:
    """Computes a keyed HMAC-SHA256 fingerprint of the canonicalized plaintext payload.

    Uses a secret pepper out-of-band to prevent rainbow tables and offline
    brute-force dictionary attacks even if the database file is compromised.

    Args:
        plaintext: The raw code.
        pepper: Optional HMAC secret key; defaults to get_fingerprint_pepper().

    Returns:
        Hex-encoded HMAC-SHA256 fingerprint.
    """
    cleaned = canonicalize_code(plaintext).encode("utf-8")
    if not cleaned:
        raise ValueError("Cannot compute fingerprint of an empty payload.")
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
