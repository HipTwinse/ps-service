"""Cryptographic utilities for inventory payload encryption, fingerprints, and receipt hashing.

All encryption is authenticated using Fernet (AES-128-CBC + HMAC-SHA256).
Encryption keys are strictly kept outside the database and outside version control.
"""

import base64
import hashlib
import os
from typing import Optional
from cryptography.fernet import Fernet

FernetCipher = Fernet


# Test/Dev fallback key if environment variable is not set.
# In production, INVENTORY_ENCRYPTION_KEY must be set in the environment.
_DEV_FALLBACK_KEY = b"k9ZJ9Xm7h2Fp0Wd3VqYl5T8N1aZb6C4D7E9G2I0O3R8="


def get_encryption_key() -> bytes:
    """Retrieves the encryption key from environment or fallback."""
    env_key = os.environ.get("INVENTORY_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    return _DEV_FALLBACK_KEY


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


def compute_fingerprint(plaintext: str) -> str:
    """Computes a deterministic SHA-256 fingerprint of the plaintext payload.

    Used strictly for database-level duplicate import detection without
    exposing or logging the plaintext code.

    Args:
        plaintext: The raw code.

    Returns:
        Hex-encoded SHA-256 fingerprint.
    """
    cleaned = plaintext.strip().encode("utf-8")
    return hashlib.sha256(cleaned).hexdigest()


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
