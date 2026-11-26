"""Cryptographic utilities for inventory payload encryption, fingerprints, and receipt hashing.

All encryption is authenticated using Fernet (AES-128-CBC + HMAC-SHA256).
Encryption keys are strictly kept outside the database and outside version control.
"""

import hashlib
import hmac
import os
from typing import Optional
from cryptography.fernet import Fernet

from src.db.config import DEFAULT_OPERATIONAL_CONFIG

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
    1. At least one environment variable (ENV or APP_ENV) must be explicitly set and non-empty.
    2. All explicitly specified environment variables (ENV and/or APP_ENV) must strictly belong to _ALLOWED_DEV_ENVS.
    3. If any specified environment variable is outside _ALLOWED_DEV_ENVS (e.g. production, staging, unknown),
       fallback secrets are unconditionally forbidden.
    4. If neither variable is set or both are empty, fallback secrets are forbidden.
    """
    raw_env = os.environ.get("ENV")
    raw_app_env = os.environ.get("APP_ENV")

    specified_envs = []
    if raw_env is not None and raw_env.strip():
        specified_envs.append(raw_env.strip().lower())
    if raw_app_env is not None and raw_app_env.strip():
        specified_envs.append(raw_app_env.strip().lower())

    if not specified_envs:
        return False

    return all(e in _ALLOWED_DEV_ENVS for e in specified_envs)


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


def validate_fingerprint_pepper(pepper: bytes, label: str = "Explicit fingerprint pepper") -> bytes:
    """Validates fingerprint pepper bytes against type and minimum length policy (SG-P2-012, SG-P31-006)."""
    if not isinstance(pepper, bytes) or len(pepper) == 0:
        raise CryptoConfigurationError(f"{label} must be non-empty bytes.")
    min_len = DEFAULT_OPERATIONAL_CONFIG.min_fingerprint_pepper_bytes
    if len(pepper) < min_len:
        raise CryptoConfigurationError(
            f"{label} is too short ({len(pepper)} bytes; minimum {min_len} bytes required)."
        )
    return pepper


def get_fingerprint_pepper() -> bytes:
    """Retrieves the HMAC pepper secret from environment.

    In production, FINGERPRINT_PEPPER (or INVENTORY_FINGERPRINT_PEPPER) must be set
    and must meet the minimum length requirement (SG-P2-012).
    Fallback test pepper is only permitted in explicitly designated dev/test environments.

    Returns:
        Pepper bytes.

    Raises:
        CryptoConfigurationError: If pepper is absent in production or shorter than minimum length.
    """
    raw_pepper: Optional[str] = None
    for env_var in ("FINGERPRINT_PEPPER", "INVENTORY_FINGERPRINT_PEPPER"):
        candidate = os.environ.get(env_var)
        if candidate is not None and candidate.strip():
            raw_pepper = candidate.strip()
            break

    if raw_pepper is not None:
        pepper_bytes = raw_pepper.encode("utf-8")
        return validate_fingerprint_pepper(pepper_bytes, label="Configured fingerprint pepper")

    if is_dev_or_test_environment():
        return _DEV_FALLBACK_PEPPER

    raise CryptoConfigurationError(
        "Production environment requires FINGERPRINT_PEPPER to be set."
    )


def _resolve_cipher(key: Optional[bytes]) -> Fernet:
    """Resolves Fernet cipher from explicit key or configured environment key (SG-P2-012)."""
    if key is None:
        return Fernet(get_encryption_key())
    if not isinstance(key, bytes) or not key:
        raise CryptoConfigurationError("Explicit encryption key must be non-empty bytes.")
    try:
        return Fernet(key)
    except Exception:
        raise CryptoConfigurationError(
            "Invalid explicit encryption key: Must be a valid 32 url-safe base64-encoded Fernet key."
        ) from None


def encrypt_payload(plaintext: str, key: Optional[bytes] = None) -> bytes:
    """Encrypts sensitive inventory payload (e.g., voucher codes) using authenticated Fernet encryption.

    Args:
        plaintext: Raw sensitive code or credentials.
        key: Optional encryption key; defaults to get_encryption_key() ONLY when key is None.

    Returns:
        Encrypted payload bytes.
    """
    cleaned = canonicalize_code(plaintext)
    if not cleaned:
        raise ValueError("Cannot encrypt an empty payload.")

    cipher = _resolve_cipher(key)
    return cipher.encrypt(cleaned.encode("utf-8"))


def decrypt_payload(encrypted_bytes: bytes, key: Optional[bytes] = None) -> str:
    """Decrypts encrypted inventory payload.

    Args:
        encrypted_bytes: Encrypted payload bytes.
        key: Optional encryption key; defaults to get_encryption_key() ONLY when key is None.

    Returns:
        Decrypted canonical plaintext string.
    """
    cipher = _resolve_cipher(key)
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
    if pepper is None:
        pepper_bytes = get_fingerprint_pepper()
    else:
        pepper_bytes = validate_fingerprint_pepper(pepper, label="Explicit fingerprint pepper")
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
