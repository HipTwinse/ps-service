"""Repository layer for catalog, users, inventory, orders, receipts, audit, deliveries, and idempotency."""

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.db.config import DEFAULT_OPERATIONAL_CONFIG
from src.db.connection import transaction
from src.db.crypto import (
    canonicalize_code,
    compute_fingerprint,
    compute_receipt_sha256,
    decrypt_payload,
    encrypt_payload,
    validate_fingerprint_pepper,
)
from src.db.models import (
    ALLOWED_ORDER_TRANSITIONS,
    FINALIZABLE_ORDER_STATUSES,
    RELEASABLE_ORDER_STATUSES,
    RESERVABLE_ORDER_STATUSES,
    REVEALABLE_ORDER_STATUSES,
    ApprovalStatus,
    AuditEvent,
    ConflictError,
    DeliveryAttempt,
    DeliveryClaim,
    DeliveryCooldownError,
    DeliveryInProgressError,
    DeliveryKind,
    DeliveryLeaseExpiredError,
    DeliveryMethod,
    DeliveryStatus,
    DuplicateReceiptError,
    FixedProductDetails,
    IdempotencyClaimResult,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyRecord,
    IdempotencyStatus,
    InsufficientInventoryError,
    InvalidStateTransitionError,
    InventoryItem,
    InventoryStatus,
    InventoryType,
    NotFoundError,
    Order,
    OrderItem,
    OrderStatus,
    PaymentReceipt,
    Product,
    ProductStatus,
    ProductType,
    ReceiptReviewStatus,
    User,
    UserBlockedError,
    UserStatus,
    ValidationError,
    VoucherDenomination,
    VoucherDetails,
    is_inventory_backed_item,
    validate_inventory_type_for_product,
)

_FORBIDDEN_AUDIT_DETAIL_KEYS = frozenset(
    {
        "plaintext",
        "code",
        "codes",
        "raw_code",
        "raw_codes",
        "encrypted_payload",
        "file_bytes",
        "encryption_key",
        "pepper",
        "old_pepper",
        "new_pepper",
        "claim_token",
        "owner_token",
        "secret",
        "password",
    }
)


def _ensure_utc(dt: datetime) -> datetime:
    """Normalizes a naive or aware datetime into timezone-aware UTC."""
    if not isinstance(dt, datetime):
        raise ValidationError(f"Timestamp must be a datetime instance (got {type(dt).__name__}).")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_utc_ts(dt: datetime) -> str:
    """Formats a naive or timezone-aware datetime into SQLite UTC timestamp text."""
    return _ensure_utc(dt).strftime("%Y-%m-%d %H:%M:%S")


def _parse_utc_ts(raw: Any) -> datetime:
    """Parses SQLite timestamp strings or datetimes into timezone-aware UTC datetime."""
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError(f"Invalid UTC timestamp value: {raw!r}")
    text = raw.strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            return _ensure_utc(datetime.fromisoformat(normalized))
        except ValueError as e:
            raise ValidationError(f"Invalid UTC timestamp format: {raw!r}") from e


def _parse_optional_utc_ts(raw: Any) -> Optional[datetime]:
    """Parses an optional SQLite timestamp into timezone-aware UTC datetime or None."""
    if raw is None:
        return None
    return _parse_utc_ts(raw)


def _validate_positive_float(val: Any, name: str) -> float:
    """Strictly validates a finite positive int/float, rejecting booleans."""
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValidationError(f"{name} must be a positive number (got {type(val).__name__}: {val!r}).")
    res = float(val)
    if not (0.0 < res < float("inf")):
        raise ValidationError(f"{name} must be a finite positive number (got {val!r}).")
    return res


def _validate_audit_details(details: Optional[Dict[str, Any]]) -> None:
    """Rejects accidental secret/plaintext keys inside audit details recursively."""
    if details is None:
        return
    if not isinstance(details, dict):
        raise ValidationError("Audit details must be a dict or None.")
    stack: List[Any] = [details]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for k, v in current.items():
                if isinstance(k, str) and k.strip().lower() in _FORBIDDEN_AUDIT_DETAIL_KEYS:
                    raise ValidationError(f"Sensitive key '{k}' is forbidden in audit_events.")
                if isinstance(v, (dict, list, tuple)):
                    stack.append(v)
        elif isinstance(current, (list, tuple)):
            for item in current:
                if isinstance(item, (dict, list, tuple)):
                    stack.append(item)


def _validate_int(val: Any, name: str, allow_zero: bool = False, min_value: Optional[int] = None) -> int:
    """Strictly validates an integer value.

    Rejects booleans (type(True) is bool), floats, None, strings, and out-of-range values.
    """
    if type(val) is not int:
        raise ValidationError(f"{name} must be an integer (got {type(val).__name__}: {val!r}).")
    if min_value is not None:
        if val < min_value:
            raise ValidationError(f"{name} must be >= {min_value} (got {val}).")
    elif allow_zero:
        if val < 0:
            raise ValidationError(f"{name} cannot be negative (got {val}).")
    else:
        if val <= 0:
            raise ValidationError(f"{name} must be strictly positive (got {val}).")
    return val


_validate_int_minor = _validate_int


class CatalogRepository:
    """Manages the polymorphic product catalog and denominations."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_voucher_product(
        self,
        display_name: str,
        region: str,
        currency: str,
        status: ProductStatus = ProductStatus.DRAFT,
    ) -> Product:
        if not isinstance(status, ProductStatus):
            raise ValidationError(f"Invalid product status: {status!r}")
        if not isinstance(region, str) or region not in ("TR", "US"):
            raise ValidationError(f"Invalid region '{region}'. Must be 'TR' or 'US'.")
        if not isinstance(currency, str) or currency not in ("TRY", "USD"):
            raise ValidationError(f"Invalid currency '{currency}'. Must be 'TRY' or 'USD'.")
        if (region, currency) not in (("TR", "TRY"), ("US", "USD")):
            raise ValidationError(f"Invalid region/currency pairing: ({region}, {currency}).")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationError("display_name cannot be empty.")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO products (product_type, display_name, status)
                VALUES (?, ?, ?)
                """,
                (ProductType.VOUCHER.value, display_name.strip(), status.value),
            )
            product_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO voucher_details (product_id, region, currency)
                VALUES (?, ?, ?)
                """,
                (product_id, region.strip().upper(), currency.strip().upper()),
            )
            return self.get_product(product_id)

    def add_voucher_denomination(
        self,
        product_id: int,
        region: str,
        currency: str,
        face_value_minor: int,
        cost_rub_kopecks: int,
        is_active: bool = True,
    ) -> VoucherDenomination:
        product_id = _validate_int(product_id, "product_id")
        face_value_minor = _validate_int(face_value_minor, "face_value_minor")
        cost_rub_kopecks = _validate_int(cost_rub_kopecks, "cost_rub_kopecks", allow_zero=True)
        if type(is_active) is not bool:
            raise ValidationError(f"is_active must be a boolean (got {type(is_active).__name__}: {is_active!r}).")
        if not isinstance(region, str) or not region.strip():
            raise ValidationError("region must be a non-empty string.")
        if not isinstance(currency, str) or not currency.strip():
            raise ValidationError("currency must be a non-empty string.")

        region_norm = region.strip().upper()
        currency_norm = currency.strip().upper()
        if region_norm not in ("TR", "US"):
            raise ValidationError(f"Invalid region '{region}'. Must be 'TR' or 'US'.")
        if currency_norm not in ("TRY", "USD"):
            raise ValidationError(f"Invalid currency '{currency}'. Must be 'TRY' or 'USD'.")
        if (region_norm, currency_norm) not in (("TR", "TRY"), ("US", "USD")):
            raise ValidationError(f"Invalid region/currency pairing: ({region_norm}, {currency_norm}).")

        try:
            with transaction(self.conn, mode="IMMEDIATE"):
                product = self.get_product(product_id)
                if product.product_type != ProductType.VOUCHER:
                    raise ValidationError(f"Product {product_id} is not of type VOUCHER.")

                cur = self.conn.cursor()
                cur.execute(
                    "SELECT region, currency FROM voucher_details WHERE product_id = ?",
                    (product_id,),
                )
                vd_row = cur.fetchone()
                if not vd_row:
                    raise ValidationError(f"VOUCHER product {product_id} is missing voucher_details.")
                if vd_row["region"] != region_norm or vd_row["currency"] != currency_norm:
                    raise ValidationError(
                        f"Denomination ({region_norm}, {currency_norm}) does not match product {product_id} "
                        f"voucher_details ({vd_row['region']}, {vd_row['currency']})."
                    )

                cur.execute(
                    """
                    INSERT INTO voucher_denominations (
                        product_id, region, currency, face_value_minor, cost_rub_kopecks, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        region_norm,
                        currency_norm,
                        face_value_minor,
                        cost_rub_kopecks,
                        1 if is_active else 0,
                    ),
                )
                denom_id = cur.lastrowid
                return self.get_denomination(denom_id)
        except sqlite3.IntegrityError as e:
            err_msg = str(e)
            if "UNIQUE constraint failed" in err_msg:
                raise ConflictError(
                    f"Denomination for {region_norm} {currency_norm} {face_value_minor} already exists: {err_msg}"
                ) from e
            raise ValidationError(f"Denomination constraint violation: {err_msg}") from e

    def create_fixed_product(
        self,
        display_name: str,
        price_rub_kopecks: int,
        delivery_method: DeliveryMethod,
        supply_method: str = "PENDING_APPROVAL",
        duration_days: Optional[int] = None,
        approval_status: ApprovalStatus = ApprovalStatus.DRAFT,
        status: ProductStatus = ProductStatus.DRAFT,
    ) -> Product:
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValidationError("display_name cannot be empty.")
        if not isinstance(supply_method, str) or not supply_method.strip():
            raise ValidationError("supply_method cannot be empty.")
        if not isinstance(delivery_method, DeliveryMethod):
            raise ValidationError(f"Invalid delivery_method: {delivery_method!r}")
        if not isinstance(approval_status, ApprovalStatus):
            raise ValidationError(f"Invalid approval_status: {approval_status!r}")
        if not isinstance(status, ProductStatus):
            raise ValidationError(f"Invalid product status: {status!r}")

        price_rub_kopecks = _validate_int(price_rub_kopecks, "price_rub_kopecks")
        if duration_days is not None:
            duration_days = _validate_int(duration_days, "duration_days")
        if status == ProductStatus.APPROVED and approval_status != ApprovalStatus.APPROVED:
            raise ValidationError(
                "Cannot create active FIXED_PRODUCT without approved supply method (approval_status must be APPROVED)."
            )

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO products (product_type, display_name, status)
                VALUES (?, ?, ?)
                """,
                (ProductType.FIXED_PRODUCT.value, display_name.strip(), status.value),
            )
            product_id = cur.lastrowid
            cur.execute(
                """
                INSERT INTO fixed_product_details (
                    product_id, price_rub_kopecks, delivery_method, supply_method, approval_status, duration_days
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    product_id,
                    price_rub_kopecks,
                    delivery_method.value,
                    supply_method.strip(),
                    approval_status.value,
                    duration_days,
                ),
            )
            return self.get_product(product_id)

    def get_product(self, product_id: int) -> Product:
        product_id = _validate_int(product_id, "product_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Product with id {product_id} not found.")
        return Product(
            id=row["id"],
            product_type=ProductType(row["product_type"]),
            display_name=row["display_name"],
            status=ProductStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_denomination(self, denomination_id: int) -> VoucherDenomination:
        denomination_id = _validate_int(denomination_id, "denomination_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM voucher_denominations WHERE id = ?", (denomination_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Voucher denomination {denomination_id} not found.")
        return VoucherDenomination(
            id=row["id"],
            product_id=row["product_id"],
            region=row["region"],
            currency=row["currency"],
            face_value_minor=row["face_value_minor"],
            cost_rub_kopecks=row["cost_rub_kopecks"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_active_denominations(self, region: str, currency: str) -> List[VoucherDenomination]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM voucher_denominations
            WHERE region = ? AND currency = ? AND is_active = 1
            ORDER BY face_value_minor DESC
            """,
            (region, currency),
        )
        return [
            VoucherDenomination(
                id=row["id"],
                product_id=row["product_id"],
                region=row["region"],
                currency=row["currency"],
                face_value_minor=row["face_value_minor"],
                cost_rub_kopecks=row["cost_rub_kopecks"],
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in cur.fetchall()
        ]

    def get_approved_active_denominations(self, region: str, currency: str) -> List[VoucherDenomination]:
        """Returns active voucher denominations whose parent VOUCHER product is APPROVED."""
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT vd.*
            FROM voucher_denominations vd
            JOIN products p ON p.id = vd.product_id
            WHERE vd.region = ?
              AND vd.currency = ?
              AND vd.is_active = 1
              AND p.product_type = 'VOUCHER'
              AND p.status = 'APPROVED'
            ORDER BY vd.face_value_minor DESC
            """,
            (region, currency),
        )
        return [
            VoucherDenomination(
                id=row["id"],
                product_id=row["product_id"],
                region=row["region"],
                currency=row["currency"],
                face_value_minor=row["face_value_minor"],
                cost_rub_kopecks=row["cost_rub_kopecks"],
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in cur.fetchall()
        ]

    def update_product_status(self, product_id: int, new_status: ProductStatus) -> None:
        product_id = _validate_int(product_id, "product_id")
        if not isinstance(new_status, ProductStatus):
            raise ValidationError(f"Invalid product status: {new_status!r}")
        with transaction(self.conn, mode="IMMEDIATE"):
            # Check approval before activating fixed-price products
            product = self.get_product(product_id)
            if product.product_type == ProductType.FIXED_PRODUCT and new_status == ProductStatus.APPROVED:
                cur = self.conn.cursor()
                cur.execute("SELECT approval_status FROM fixed_product_details WHERE product_id = ?", (product_id,))
                row = cur.fetchone()
                if not row or row["approval_status"] != ApprovalStatus.APPROVED.value:
                    raise ValidationError("Cannot activate fixed-price product without approved supply method.")

            self.conn.execute(
                "UPDATE products SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status.value, product_id),
            )

    def update_fixed_product_approval(
        self, product_id: int, approval_status: ApprovalStatus
    ) -> None:
        product_id = _validate_int(product_id, "product_id")
        if not isinstance(approval_status, ApprovalStatus):
            raise ValidationError(f"Invalid approval_status: {approval_status!r}")
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.execute(
                """
                UPDATE fixed_product_details
                SET approval_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?
                """,
                (approval_status.value, product_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Fixed product {product_id} not found.")
            # If approval is revoked, ensure product status is demoted to DRAFT
            if approval_status != ApprovalStatus.APPROVED:
                self.conn.execute(
                    "UPDATE products SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = ?",
                    (ProductStatus.DRAFT.value, product_id, ProductStatus.APPROVED.value),
                )

    def update_fixed_product_supply_method(
        self, product_id: int, new_supply_method: str
    ) -> None:
        """Updates the supply method of a fixed product.

        SECURITY INVARIANT:
        Modifying the supply method immediately revokes any previous approval.
        Resets approval_status to DRAFT and resets products.status to DRAFT.
        """
        product_id = _validate_int(product_id, "product_id")
        if not isinstance(new_supply_method, str) or not new_supply_method.strip():
            raise ValidationError("supply_method cannot be empty.")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT product_id FROM fixed_product_details WHERE product_id = ?", (product_id,))
            if not cur.fetchone():
                raise NotFoundError(f"Fixed product {product_id} not found.")

            # Update supply method and demote approval to DRAFT
            cur.execute(
                """
                UPDATE fixed_product_details
                SET supply_method = ?, approval_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?
                """,
                (new_supply_method.strip(), ApprovalStatus.DRAFT.value, product_id),
            )
            # Demote product status to DRAFT so it cannot be sold without re-approval
            cur.execute(
                "UPDATE products SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ProductStatus.DRAFT.value, product_id),
            )


class UserRepository:
    """Manages internal user identities and platform bindings."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_or_create_user(self, external_id: str, username: Optional[str] = None) -> Tuple[User, bool]:
        if not isinstance(external_id, str) or not external_id.strip():
            raise ValidationError("external_id cannot be empty.")
        if username is not None and not isinstance(username, str):
            raise ValidationError("username must be a string or None.")
        clean_ext = external_id.strip()

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM users WHERE external_id = ?", (clean_ext,))
            row = cur.fetchone()
            if row:
                if username and row["username"] != username:
                    cur.execute(
                        "UPDATE users SET username = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (username, row["id"]),
                    )
                    # AR-P3-008: Re-read inside the same transaction so updated_at and all fields are consistent
                    cur.execute("SELECT * FROM users WHERE id = ?", (row["id"],))
                    row = cur.fetchone()
                user = User(
                    id=row["id"],
                    external_id=row["external_id"],
                    username=row["username"],
                    status=UserStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                return user, False

            # Create fresh user
            cur.execute(
                "INSERT INTO users (external_id, username, status) VALUES (?, ?, ?)",
                (clean_ext, username, UserStatus.ACTIVE.value),
            )
            user_id = cur.lastrowid
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            new_row = cur.fetchone()
            user = User(
                id=new_row["id"],
                external_id=new_row["external_id"],
                username=new_row["username"],
                status=UserStatus(new_row["status"]),
                created_at=new_row["created_at"],
                updated_at=new_row["updated_at"],
            )
            return user, True

    def get_user(self, user_id: int) -> User:
        user_id = _validate_int(user_id, "user_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"User {user_id} not found.")
        return User(
            id=row["id"],
            external_id=row["external_id"],
            username=row["username"],
            status=UserStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def update_user_status(self, user_id: int, status: UserStatus) -> None:
        user_id = _validate_int(user_id, "user_id")
        if not isinstance(status, UserStatus):
            raise ValidationError(f"Invalid user status: {status!r}")
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.execute(
                "UPDATE users SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status.value, user_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"User {user_id} not found.")


class InventoryRepository:
    """Manages encrypted inventory items, imports, and atomic reservations."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def import_items(
        self,
        product_id: int,
        raw_codes: Sequence[str],
        inventory_type: InventoryType,
        denomination_id: Optional[int] = None,
    ) -> Dict[str, int]:
        """Safely imports a batch of inventory codes atomically.

        Canonicalizes codes, enforces ProductType <-> InventoryType compatibility matrix,
        verifies denomination ownership (denomination.product_id == product_id),
        encrypts sensitive payloads, computes unique fingerprints, and rejects duplicates.
        Any validation or runtime failure rolls back the entire batch (no partial imports).
        Does NOT log any plaintext codes.
        """
        product_id = _validate_int(product_id, "product_id")
        if denomination_id is not None:
            denomination_id = _validate_int(denomination_id, "denomination_id")
        if not isinstance(inventory_type, InventoryType):
            raise ValidationError(f"Invalid inventory_type: {inventory_type!r}")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, product_type FROM products WHERE id = ?", (product_id,))
            prod_row = cur.fetchone()
            if not prod_row:
                raise NotFoundError(f"Product {product_id} not found.")

            product_type = ProductType(prod_row["product_type"])
            validate_inventory_type_for_product(product_type, inventory_type)

            if product_type == ProductType.VOUCHER and denomination_id is None:
                raise ValidationError(
                    f"Voucher product {product_id} requires a valid denomination_id."
                )

            if denomination_id is not None:
                if product_type != ProductType.VOUCHER:
                    raise ValidationError(
                        f"denomination_id cannot be specified for product {product_id} of type '{product_type.value}'."
                    )
                cur.execute(
                    "SELECT id, product_id FROM voucher_denominations WHERE id = ?",
                    (denomination_id,),
                )
                denom_row = cur.fetchone()
                if not denom_row:
                    raise NotFoundError(f"Voucher denomination {denomination_id} not found.")
                if denom_row["product_id"] != product_id:
                    raise ValidationError(
                        f"Voucher denomination {denomination_id} does not belong to product {product_id}."
                    )

            accepted = 0
            duplicates = 0
            rejected = 0

            for raw_code in raw_codes:
                code_str = canonicalize_code(raw_code)
                if not code_str:
                    rejected += 1
                    continue

                fp = compute_fingerprint(code_str)
                legacy_fp = hashlib.sha256(code_str.encode("utf-8")).hexdigest()

                # Check both modern HMAC fingerprint and legacy SHA-256 fingerprint
                cur.execute(
                    "SELECT id, fingerprint FROM inventory_items WHERE fingerprint = ? OR fingerprint = ?",
                    (fp, legacy_fp),
                )
                existing_row = cur.fetchone()
                if existing_row:
                    duplicates += 1
                    # Auto-upgrade legacy fingerprint to HMAC if needed
                    if existing_row["fingerprint"] == legacy_fp and fp != legacy_fp:
                        cur.execute(
                            "UPDATE inventory_items SET fingerprint = ? WHERE id = ?",
                            (fp, existing_row["id"]),
                        )
                    continue

                encrypted = encrypt_payload(code_str)
                try:
                    cur.execute(
                        """
                        INSERT INTO inventory_items (
                            product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            product_id,
                            denomination_id,
                            inventory_type.value,
                            encrypted,
                            fp,
                            InventoryStatus.AVAILABLE.value,
                        ),
                    )
                    accepted += 1
                except sqlite3.IntegrityError as e:
                    if "fingerprint" in str(e).lower():
                        duplicates += 1
                    else:
                        raise

        return {
            "accepted": accepted,
            "duplicates": duplicates,
            "rejected": rejected,
            "total_submitted": len(raw_codes),
        }

    def migrate_legacy_fingerprints(self, pepper: Optional[bytes] = None) -> int:
        """Scans and upgrades any existing items with legacy unsalted SHA-256 fingerprints to HMAC-SHA256.

        Canonicalizes raw codes, preserves all encrypted payloads, keeps records intact, and executes atomically.
        """
        if pepper is not None:
            validate_fingerprint_pepper(pepper, label="pepper")
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, encrypted_payload, fingerprint FROM inventory_items")
            rows = cur.fetchall()
            upgraded = 0
            for row in rows:
                raw_code = decrypt_payload(row["encrypted_payload"])
                clean_code = canonicalize_code(raw_code)
                legacy_sha = hashlib.sha256(clean_code.encode("utf-8")).hexdigest()
                new_hmac = compute_fingerprint(clean_code, pepper=pepper)
                if row["fingerprint"] == legacy_sha and row["fingerprint"] != new_hmac:
                    cur.execute(
                        "UPDATE inventory_items SET fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (new_hmac, row["id"]),
                    )
                    upgraded += 1
            return upgraded

    def rotate_fingerprint_pepper(self, new_pepper: bytes, old_pepper: Optional[bytes] = None) -> int:
        """Rotates inventory HMAC fingerprints when FINGERPRINT_PEPPER is rotated.

        RUNBOOK: OPERATIONAL PROCEDURE FOR PEPPER ROTATION
        ===================================================
        Phase 1: Operational Shutdown & Writer Quiesce (MANDATORY)
          All background workers, bots, payment listeners, and inventory import tasks MUST be
          stopped prior to initiating rotation.
          NOTE: BEGIN IMMEDIATE holds an exclusive write lock during transaction execution,
          but once the transaction commits, locks are released. Without shutting down writers,
          a writer could insert a code using the old pepper before application instances are updated.

        Phase 2: Database Recalculation (Atomic)
          rotate_fingerprint_pepper(new_pepper) executes under BEGIN IMMEDIATE.
          It decrypts each payload using the persistent encryption key, computes the new
          HMAC-SHA256 fingerprint with new_pepper, and updates the database.
          If any error occurs mid-rotation, the entire transaction is rolled back immediately;
          all fingerprints remain consistent under old_pepper.

        Phase 3: Control Duplicate Verification (Canary Check)
          Before restarting writers, verify that re-checking a known canary code with new_pepper
          matches the updated database fingerprints (e.g. via canary duplicate lookup).

        Phase 4: Secret Switching & Multi-Instance Rollout
          Update the FINGERPRINT_PEPPER environment variable / secret across all application instances.

        Phase 5: Recovery Runbook (Failure Scenarios)
          - Failure during Phase 2 (DB rotation error):
            Transaction rolled back automatically. No DB changes persisted. Continue with old_pepper.
          - Failure during Phase 4 (DB committed new pepper, but instance crashes or fails rollout):
            Because payloads are encrypted under the invariant Fernet key, no data is lost.
            If an instance still runs with old_pepper, duplicate lookups will not find matches.
            Remediation: complete rollout of new_pepper to all instances.
            If emergency abort is required: invoke rotate_fingerprint_pepper(old_pepper) to roll back DB.

        Phase 6: Writer Resumption
          Restart application workers and resume inventory import traffic.

        Args:
            new_pepper: New HMAC secret key bytes.
            old_pepper: Optional previous pepper for validation.

        Returns:
            Number of rotated inventory items.
        """
        validate_fingerprint_pepper(new_pepper, label="new_pepper")
        if old_pepper is not None:
            validate_fingerprint_pepper(old_pepper, label="old_pepper")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, encrypted_payload, fingerprint FROM inventory_items")
            rows = cur.fetchall()
            rotated = 0
            for row in rows:
                raw_code = decrypt_payload(row["encrypted_payload"])
                clean_code = canonicalize_code(raw_code)
                if old_pepper is not None:
                    expected_old = compute_fingerprint(clean_code, pepper=old_pepper)
                    if row["fingerprint"] != expected_old:
                        raise ValidationError(
                            f"Fingerprint mismatch against old_pepper for inventory item {row['id']}."
                        )
                new_hmac = compute_fingerprint(clean_code, pepper=new_pepper)
                if row["fingerprint"] != new_hmac:
                    cur.execute(
                        "UPDATE inventory_items SET fingerprint = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (new_hmac, row["id"]),
                    )
                    rotated += 1
            return rotated

    def reserve_items_for_order(
        self,
        order_id: int,
        product_id: int,
        quantity: int,
        denomination_id: Optional[int] = None,
    ) -> List[int]:
        """Atomically reserves available inventory units for an order within an exclusive transaction.

        Verifies order existence and allowable order status (RESERVABLE_ORDER_STATUSES),
        validates product and denomination ownership, and rolls back completely if insufficient stock.
        """
        order_id = _validate_int(order_id, "order_id", min_value=1)
        product_id = _validate_int(product_id, "product_id", min_value=1)
        quantity = _validate_int(quantity, "quantity", min_value=1)
        if denomination_id is not None:
            denomination_id = _validate_int(denomination_id, "denomination_id", min_value=1)

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, status FROM orders WHERE id = ?", (order_id,))
            order_row = cur.fetchone()
            if not order_row:
                raise NotFoundError(f"Order {order_id} not found.")

            order_status = OrderStatus(order_row["status"])
            if order_status not in RESERVABLE_ORDER_STATUSES:
                raise InvalidStateTransitionError(
                    f"Cannot reserve inventory for order {order_id} in status '{order_status.value}'."
                )

            cur.execute("SELECT id, product_type FROM products WHERE id = ?", (product_id,))
            prod_row = cur.fetchone()
            if not prod_row:
                raise NotFoundError(f"Product {product_id} not found.")

            product_type = ProductType(prod_row["product_type"])
            if product_type == ProductType.FIXED_PRODUCT:
                cur.execute(
                    "SELECT delivery_method FROM fixed_product_details WHERE product_id = ?",
                    (product_id,),
                )
                fp_row = cur.fetchone()
                if fp_row is not None and fp_row["delivery_method"] == DeliveryMethod.MANUAL_REVIEW.value:
                    raise ValidationError(
                        f"Cannot reserve inventory for MANUAL_REVIEW fixed product {product_id}."
                    )
            if product_type == ProductType.VOUCHER and denomination_id is None:
                raise ValidationError(
                    f"Reserving inventory for VOUCHER product {product_id} requires a valid denomination_id."
                )

            if denomination_id is not None:
                if product_type != ProductType.VOUCHER:
                    raise ValidationError(
                        f"denomination_id cannot be used with non-VOUCHER product {product_id}."
                    )
                cur.execute("SELECT id, product_id FROM voucher_denominations WHERE id = ?", (denomination_id,))
                denom_row = cur.fetchone()
                if not denom_row:
                    raise NotFoundError(f"Voucher denomination {denomination_id} not found.")
                if denom_row["product_id"] != product_id:
                    raise ValidationError(
                        f"Voucher denomination {denomination_id} does not belong to product {product_id}."
                    )

            if denomination_id is not None:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(quantity), 0) AS ordered_qty
                    FROM order_items
                    WHERE order_id = ? AND product_id = ? AND denomination_id = ?
                    """,
                    (order_id, product_id, denomination_id),
                )
            else:
                cur.execute(
                    """
                    SELECT COALESCE(SUM(quantity), 0) AS ordered_qty
                    FROM order_items
                    WHERE order_id = ? AND product_id = ? AND denomination_id IS NULL
                    """,
                    (order_id, product_id),
                )
            ordered_qty = cur.fetchone()["ordered_qty"]
            if ordered_qty <= 0:
                raise ValidationError(
                    f"Product {product_id} (denomination {denomination_id}) is not present in order {order_id}."
                )

            if denomination_id is not None:
                cur.execute(
                    """
                    SELECT COUNT(*) AS allocated_qty
                    FROM inventory_items
                    WHERE reserved_order_id = ? AND product_id = ? AND denomination_id = ?
                      AND status IN ('RESERVED', 'SOLD')
                    """,
                    (order_id, product_id, denomination_id),
                )
            else:
                cur.execute(
                    """
                    SELECT COUNT(*) AS allocated_qty
                    FROM inventory_items
                    WHERE reserved_order_id = ? AND product_id = ? AND denomination_id IS NULL
                      AND status IN ('RESERVED', 'SOLD')
                    """,
                    (order_id, product_id),
                )
            allocated_qty = cur.fetchone()["allocated_qty"]
            if allocated_qty + quantity > ordered_qty:
                raise ValidationError(
                    f"Cannot reserve {quantity} units of product {product_id} (denomination {denomination_id}) "
                    f"for order {order_id}: ordered {ordered_qty}, already reserved/sold {allocated_qty}."
                )

            if denomination_id is not None:
                cur.execute(
                    """
                    SELECT id FROM inventory_items
                    WHERE product_id = ? AND denomination_id = ? AND status = 'AVAILABLE'
                    ORDER BY id ASC LIMIT ?
                    """,
                    (product_id, denomination_id, quantity),
                )
            else:
                cur.execute(
                    """
                    SELECT id FROM inventory_items
                    WHERE product_id = ? AND denomination_id IS NULL AND status = 'AVAILABLE'
                    ORDER BY id ASC LIMIT ?
                    """,
                    (product_id, quantity),
                )

            candidates = [row["id"] for row in cur.fetchall()]
            if len(candidates) < quantity:
                raise InsufficientInventoryError(
                    f"Requested {quantity} units for product {product_id}, but only {len(candidates)} available."
                )

            reserved_ids: List[int] = []
            for item_id in candidates:
                cur.execute(
                    """
                    UPDATE inventory_items
                    SET status = 'RESERVED', reserved_order_id = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = 'AVAILABLE'
                    """,
                    (order_id, item_id),
                )
                if cur.rowcount == 1:
                    reserved_ids.append(item_id)

            if len(reserved_ids) < quantity:
                raise InsufficientInventoryError(
                    f"Concurrency conflict: requested {quantity} units, successfully locked {len(reserved_ids)}."
                )

            return reserved_ids

    def release_order_reservations(self, order_id: int) -> int:
        """Releases reserved inventory items back to AVAILABLE status when permitted by order status.

        Invariants:
        - Checks order exists and order.status is in RELEASABLE_ORDER_STATUSES
          (PENDING_PAYMENT, PAYMENT_REVIEW, CANCELLED, REFUND_REVIEW).
        - NEVER reverts SOLD items to AVAILABLE.
        - NEVER modifies reservations belonging to any other order.
        """
        order_id = _validate_int(order_id, "order_id")
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, status FROM orders WHERE id = ?", (order_id,))
            order_row = cur.fetchone()
            if not order_row:
                raise NotFoundError(f"Order {order_id} not found.")

            order_status = OrderStatus(order_row["status"])
            if order_status not in RELEASABLE_ORDER_STATUSES:
                raise InvalidStateTransitionError(
                    f"Cannot release inventory reservations for order {order_id} in status '{order_status.value}'."
                )

            cur.execute(
                """
                UPDATE inventory_items
                SET status = 'AVAILABLE', reserved_order_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE reserved_order_id = ? AND status = 'RESERVED'
                """,
                (order_id,),
            )
            return cur.rowcount

    def finalize_order_sale(self, order_id: int) -> int:
        """Transitions reserved inventory items to permanently SOLD state upon valid paid/fulfilling order status.

        Invariants (full-order fulfillment model, OD-03, OD-04, AR-P3-004):
        - Order must exist and be in FINALIZABLE_ORDER_STATUSES (PAID or FULFILLING).
        - Rejects duplicate finalization if any order items are already marked SOLD.
        - Requires exact match between every inventory-backed (product_id, denomination_id, quantity)
          in order_items and currently RESERVED inventory items for this order; forbids mixed manual/inventory
          orders and rejects non-inventory-only orders with ValidationError.
        """
        order_id = _validate_int(order_id, "order_id", min_value=1)
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, status FROM orders WHERE id = ?", (order_id,))
            order_row = cur.fetchone()
            if not order_row:
                raise NotFoundError(f"Order {order_id} not found.")

            order_status = OrderStatus(order_row["status"])
            if order_status not in FINALIZABLE_ORDER_STATUSES:
                raise InvalidStateTransitionError(
                    f"Cannot finalize inventory sale for order {order_id} in status '{order_status.value}'. "
                    f"Order must be PAID or FULFILLING."
                )

            cur.execute(
                "SELECT 1 FROM inventory_items WHERE reserved_order_id = ? AND status = 'SOLD' LIMIT 1",
                (order_id,),
            )
            if cur.fetchone():
                raise ConflictError(
                    f"Inventory items for order {order_id} have already been finalized (SOLD)."
                )

            cur.execute(
                """
                SELECT
                    oi.product_id,
                    oi.denomination_id,
                    oi.product_type_snapshot,
                    oi.quantity,
                    fp.delivery_method AS fixed_delivery_method
                FROM order_items oi
                LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
                WHERE oi.order_id = ?
                """,
                (order_id,),
            )
            item_rows = cur.fetchall()
            if not item_rows:
                raise ValidationError(
                    f"Order {order_id} has no order items to finalize."
                )

            expected_by_key: Dict[Tuple[int, Optional[int]], int] = {}
            has_manual = False
            for r in item_rows:
                p_type = ProductType(r["product_type_snapshot"])
                d_method = (
                    DeliveryMethod(r["fixed_delivery_method"])
                    if r["fixed_delivery_method"] is not None
                    else None
                )
                if p_type == ProductType.FIXED_PRODUCT and d_method is None:
                    raise ValidationError(
                        f"FIXED_PRODUCT {r['product_id']} in order {order_id} is missing fixed_product_details."
                    )
                if is_inventory_backed_item(p_type, d_method):
                    key = (r["product_id"], r["denomination_id"])
                    expected_by_key[key] = expected_by_key.get(key, 0) + r["quantity"]
                else:
                    has_manual = True

            if has_manual and expected_by_key:
                raise ValidationError(
                    f"Order {order_id} mixes inventory-backed and MANUAL_REVIEW items (OD-04 violation)."
                )
            if not expected_by_key:
                raise ValidationError(
                    f"Order {order_id} has no inventory-backed items to finalize."
                )

            cur.execute(
                """
                SELECT product_id, denomination_id, COUNT(*) AS reserved_qty
                FROM inventory_items
                WHERE reserved_order_id = ? AND status = 'RESERVED'
                GROUP BY product_id, denomination_id
                """,
                (order_id,),
            )
            reserved_by_key = {
                (row["product_id"], row["denomination_id"]): row["reserved_qty"]
                for row in cur.fetchall()
            }

            if reserved_by_key != expected_by_key:
                raise ValidationError(
                    f"Cannot finalize order {order_id}: reserved inventory {reserved_by_key} "
                    f"does not match ordered items {expected_by_key}."
                )

            cur.execute(
                """
                UPDATE inventory_items
                SET status = 'SOLD', updated_at = CURRENT_TIMESTAMP
                WHERE reserved_order_id = ? AND status = 'RESERVED'
                """,
                (order_id,),
            )
            count = cur.rowcount
            expected_total = sum(expected_by_key.values())
            if count != expected_total:
                raise ConflictError(
                    f"Unexpected row count finalizing order {order_id}: expected {expected_total}, updated {count}."
                )
            return count

    def get_item(self, item_id: int) -> InventoryItem:
        item_id = _validate_int(item_id, "item_id", min_value=1)
        row = self.conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            raise NotFoundError(f"Inventory item {item_id} not found.")
        return InventoryItem(
            id=row["id"],
            product_id=row["product_id"],
            denomination_id=row["denomination_id"],
            inventory_type=InventoryType(row["inventory_type"]),
            fingerprint=row["fingerprint"],
            status=InventoryStatus(row["status"]),
            reserved_order_id=row["reserved_order_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _transition_to_quarantined_or_disabled(
        self,
        item_id: int,
        target_status: InventoryStatus,
        *,
        preserve_forensic_link: bool = False,
    ) -> InventoryItem:
        item_id = _validate_int(item_id, "item_id", min_value=1)
        if target_status not in (InventoryStatus.QUARANTINED, InventoryStatus.DISABLED):
            raise ValidationError(f"Unsupported quarantine/disable target status: {target_status!r}")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT i.id, i.status, i.reserved_order_id, o.status AS order_status
                FROM inventory_items i
                LEFT JOIN orders o ON o.id = i.reserved_order_id
                WHERE i.id = ?
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                raise NotFoundError(f"Inventory item {item_id} not found.")

            current_status = InventoryStatus(row["status"])
            if current_status == InventoryStatus.SOLD:
                raise InvalidStateTransitionError(
                    f"SOLD inventory item {item_id} is terminal and cannot transition to {target_status.value}."
                )

            current_order_id = row["reserved_order_id"]
            if current_status == InventoryStatus.AVAILABLE:
                if preserve_forensic_link:
                    raise ValidationError("Cannot preserve forensic order link on AVAILABLE inventory item.")
                new_order_id = None
            elif current_status == InventoryStatus.RESERVED:
                if preserve_forensic_link:
                    if row["order_status"] not in (OrderStatus.CANCELLED.value, OrderStatus.REFUND_REVIEW.value):
                        raise InvalidStateTransitionError(
                            "Forensic order link can only be preserved when linked order is CANCELLED or REFUND_REVIEW."
                        )
                    new_order_id = current_order_id
                else:
                    new_order_id = None
            else:
                # Already QUARANTINED or DISABLED
                new_order_id = current_order_id if preserve_forensic_link else (None if not preserve_forensic_link and current_order_id is None else current_order_id)
                if not preserve_forensic_link and current_order_id is not None:
                    raise InvalidStateTransitionError(
                        "Forensic link is still attached; call clear_forensic_order_link() explicitly."
                    )

            cur.execute(
                """
                UPDATE inventory_items
                SET status = ?, reserved_order_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target_status.value, new_order_id, item_id),
            )
            return self.get_item(item_id)

    def quarantine_item(self, item_id: int, *, preserve_forensic_link: bool = False) -> InventoryItem:
        return self._transition_to_quarantined_or_disabled(
            item_id, InventoryStatus.QUARANTINED, preserve_forensic_link=preserve_forensic_link
        )

    def disable_item(self, item_id: int, *, preserve_forensic_link: bool = False) -> InventoryItem:
        return self._transition_to_quarantined_or_disabled(
            item_id, InventoryStatus.DISABLED, preserve_forensic_link=preserve_forensic_link
        )

    def clear_forensic_order_link(self, item_id: int) -> InventoryItem:
        """Explicitly clears reserved_order_id on a QUARANTINED or DISABLED inventory item (AR-P3-007)."""
        item_id = _validate_int(item_id, "item_id", min_value=1)
        with transaction(self.conn, mode="IMMEDIATE"):
            item = self.get_item(item_id)
            if item.status == InventoryStatus.SOLD:
                raise InvalidStateTransitionError("Cannot clear order link on terminal SOLD inventory item.")
            if item.status not in (InventoryStatus.QUARANTINED, InventoryStatus.DISABLED):
                raise InvalidStateTransitionError(
                    f"clear_forensic_order_link requires QUARANTINED or DISABLED item (got {item.status.value})."
                )
            self.conn.execute(
                "UPDATE inventory_items SET reserved_order_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (item_id,),
            )
            return self.get_item(item_id)

    def restore_to_available(self, item_id: int) -> InventoryItem:
        """Restores a QUARANTINED or DISABLED item to AVAILABLE ONLY after forensic link is cleared (AR-P3-007)."""
        item_id = _validate_int(item_id, "item_id", min_value=1)
        with transaction(self.conn, mode="IMMEDIATE"):
            item = self.get_item(item_id)
            if item.status == InventoryStatus.SOLD:
                raise InvalidStateTransitionError("Cannot restore terminal SOLD inventory item to AVAILABLE.")
            if item.status not in (InventoryStatus.QUARANTINED, InventoryStatus.DISABLED):
                raise InvalidStateTransitionError(
                    f"restore_to_available requires QUARANTINED or DISABLED item (got {item.status.value})."
                )
            if item.reserved_order_id is not None:
                raise InvalidStateTransitionError(
                    f"Cannot restore item {item_id} to AVAILABLE while forensic reserved_order_id={item.reserved_order_id} is set."
                )
            self.conn.execute(
                "UPDATE inventory_items SET status = 'AVAILABLE', reserved_order_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (item_id,),
            )
            return self.get_item(item_id)

    def get_revealed_code(
        self,
        item_id: int,
        *,
        expected_order_id: Optional[int] = None,
        expected_user_id: Optional[int] = None,
    ) -> str:
        """Reveals the decrypted code for a SOLD inventory item in a verified ACTIVE user/order context.

        Safety invariants enforced atomically in one query (SG-P2-013, OD-01):
        - Requires both `expected_order_id` and `expected_user_id` (strictly positive ints).
        - Joins orders AND users atomically to block BLOCKED users server-side (UserBlockedError).
        - Uses neutral non-oracle error descriptions across status/ownership checks.
        """
        item_id = _validate_int(item_id, "item_id", min_value=1)
        if expected_order_id is None or expected_user_id is None:
            raise ValidationError(
                "get_revealed_code requires both expected_order_id and expected_user_id."
            )
        expected_order_id = _validate_int(expected_order_id, "expected_order_id", min_value=1)
        expected_user_id = _validate_int(expected_user_id, "expected_user_id", min_value=1)

        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT
                i.id,
                i.status,
                i.reserved_order_id,
                i.encrypted_payload,
                o.user_id AS order_user_id,
                o.status AS order_status,
                u.status AS user_status
            FROM inventory_items i
            LEFT JOIN orders o ON o.id = i.reserved_order_id
            LEFT JOIN users u ON u.id = o.user_id
            WHERE i.id = ?
            """,
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Inventory item {item_id} not found.")

        item_status = InventoryStatus(row["status"])
        if item_status in (InventoryStatus.QUARANTINED, InventoryStatus.DISABLED):
            raise ValidationError("Requested digital inventory item is unavailable for reveal.")

        linked_order_id = row["reserved_order_id"]
        if linked_order_id is None or linked_order_id != expected_order_id or item_status != InventoryStatus.SOLD:
            raise ValidationError("Requested digital inventory item is unavailable for the specified order.")
        if row["order_status"] is None or row["order_user_id"] is None or row["user_status"] is None:
            raise NotFoundError(f"Order {linked_order_id} not found.")
        if row["order_user_id"] != expected_user_id:
            raise ValidationError("Requested digital inventory item is unavailable for the specified order.")
        if row["user_status"] == UserStatus.BLOCKED.value:
            raise UserBlockedError("User is blocked from accessing revealed codes.")

        order_status = OrderStatus(row["order_status"])
        if order_status not in REVEALABLE_ORDER_STATUSES:
            raise InvalidStateTransitionError(
                f"Cannot reveal inventory code for order {linked_order_id} in current state."
            )

        return decrypt_payload(row["encrypted_payload"])


class OrderRepository:
    """Manages order creation, price snapshots, and lifecycle state transitions."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_order(
        self,
        user_id: int,
        total_amount_minor: int,
        currency: str,
        items: Sequence[Dict[str, Any]],
        *,
        enforce_open_order_limit: bool = False,
        max_open_orders: Optional[int] = None,
    ) -> Order:
        """Creates an order and preserves historical price snapshots on all items.

        Atomic and transaction-safe:
        - Acquires an IMMEDIATE transaction lock (BEGIN IMMEDIATE) prior to reading user,
          product status, and approval status to eliminate race conditions between validation and insertion.
        - Strictly verifies that total_amount_minor matches sum of (quantity * unit_price_minor).
        - Verifies that currency is consistent across order and all item snapshots.
        - Rejects mixing inventory-backed and MANUAL_REVIEW items (OD-04).
        - Completely rolls back on any error without leaving orphan records.
        """
        user_id = _validate_int(user_id, "user_id", min_value=1)
        total_amount_minor = _validate_int(total_amount_minor, "total_amount_minor", min_value=1)
        if not items:
            raise ValidationError("Order must contain at least one order item.")

        if not isinstance(currency, str) or not currency.strip():
            raise ValidationError("Order currency must not be empty.")
        currency_norm = currency.strip().upper()
        if currency_norm not in ("RUB", "TRY", "USD"):
            raise ValidationError(f"Unsupported order currency '{currency_norm}'.")

        # Verify mathematical consistency between total_amount_minor and item prices/quantities/currency
        computed_total = 0
        for item in items:
            qty = _validate_int(item.get("quantity"), "Item quantity", min_value=1)
            unit_price = _validate_int(item.get("unit_price_minor"), "Unit price", min_value=0)
            item_curr = item.get("currency")

            if item_curr is not None:
                if not isinstance(item_curr, str):
                    raise ValidationError("Item currency must be a string.")
                if item_curr.strip().upper() != currency_norm:
                    raise ValidationError(
                        f"Item currency '{item_curr}' does not match order currency '{currency_norm}'."
                    )

            if item.get("denomination_id") is not None:
                _validate_int(item.get("denomination_id"), "denomination_id", min_value=1)

            computed_total += qty * unit_price

        if total_amount_minor != computed_total:
            raise ValidationError(
                f"Order total_amount_minor ({total_amount_minor}) does not match sum of items ({computed_total})."
            )

        limit_val = (
            _validate_int(max_open_orders, "max_open_orders", min_value=1)
            if max_open_orders is not None
            else DEFAULT_OPERATIONAL_CONFIG.max_open_pending_payment_orders_per_user
        )

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            # 1. Verify user exists and is not blocked (OD-01)
            cur.execute("SELECT id, status FROM users WHERE id = ?", (user_id,))
            user_row = cur.fetchone()
            if not user_row:
                raise NotFoundError(f"User {user_id} not found.")
            if user_row["status"] == UserStatus.BLOCKED.value:
                raise UserBlockedError("Cannot create order for blocked user.")

            if enforce_open_order_limit:
                cur.execute(
                    "SELECT COUNT(*) AS open_cnt FROM orders WHERE user_id = ? AND status = ?",
                    (user_id, OrderStatus.PENDING_PAYMENT.value),
                )
                open_cnt = int(cur.fetchone()["open_cnt"])
                if open_cnt >= limit_val:
                    raise ValidationError(
                        f"Open PENDING_PAYMENT order limit reached ({open_cnt} >= {limit_val})."
                    )

            # 2. Validate all products, product types, denominations, supply approvals, and catalog prices under active lock
            validated_items = []
            saw_inventory_backed = False
            saw_manual_review = False
            for item in items:
                prod_id = _validate_int(item.get("product_id"), "Product id", min_value=1)
                qty = _validate_int(item.get("quantity"), "Item quantity", min_value=1)
                unit_price = _validate_int(item.get("unit_price_minor"), "Unit price", min_value=0)
                prod_type = item.get("product_type_snapshot")
                denom_id = item.get("denomination_id")
                if denom_id is not None:
                    denom_id = _validate_int(denom_id, "denomination_id", min_value=1)
                meta_json = item.get("metadata_json")

                # Validate product exists and is approved for sale
                cur.execute("SELECT id, status, product_type FROM products WHERE id = ?", (prod_id,))
                prod_row = cur.fetchone()
                if not prod_row:
                    raise NotFoundError(f"Product {prod_id} not found.")
                if prod_row["status"] != ProductStatus.APPROVED.value:
                    raise ValidationError(f"Product '{prod_id}' is not approved for sale.")

                # Validate product_type_snapshot matches actual product type
                if prod_type != prod_row["product_type"]:
                    raise ValidationError(
                        f"Item product_type_snapshot '{prod_type}' does not match product {prod_id} type '{prod_row['product_type']}'."
                    )

                if prod_row["product_type"] == ProductType.FIXED_PRODUCT.value:
                    if denom_id is not None:
                        raise ValidationError(
                            f"denomination_id must not be specified for FIXED_PRODUCT {prod_id}."
                        )
                    cur.execute(
                        "SELECT price_rub_kopecks, delivery_method, approval_status FROM fixed_product_details WHERE product_id = ?",
                        (prod_id,),
                    )
                    fp_row = cur.fetchone()
                    if not fp_row or fp_row["approval_status"] != ApprovalStatus.APPROVED.value:
                        raise ValidationError(
                            f"Fixed product '{prod_id}' does not have an approved supply method."
                        )
                    if currency_norm != "RUB":
                        raise ValidationError(
                            f"Order currency for FIXED_PRODUCT must be 'RUB' (got '{currency_norm}')."
                        )
                    if unit_price != fp_row["price_rub_kopecks"]:
                        raise ValidationError(
                            f"Unit price {unit_price} does not match catalog price {fp_row['price_rub_kopecks']} "
                            f"for FIXED_PRODUCT {prod_id}."
                        )
                    if is_inventory_backed_item(ProductType.FIXED_PRODUCT, DeliveryMethod(fp_row["delivery_method"])):
                        saw_inventory_backed = True
                    else:
                        saw_manual_review = True
                elif prod_row["product_type"] == ProductType.VOUCHER.value:
                    if denom_id is None:
                        raise ValidationError(
                            f"VOUCHER order item for product {prod_id} requires a valid denomination_id."
                        )
                    cur.execute(
                        """
                        SELECT id, product_id, cost_rub_kopecks, is_active
                        FROM voucher_denominations
                        WHERE id = ?
                        """,
                        (denom_id,),
                    )
                    denom_row = cur.fetchone()
                    if not denom_row:
                        raise NotFoundError(f"Voucher denomination {denom_id} not found.")
                    if denom_row["product_id"] != prod_id:
                        raise ValidationError(
                            f"Voucher denomination {denom_id} does not belong to product {prod_id}."
                        )
                    if denom_row["is_active"] != 1:
                        raise ValidationError(
                            f"Voucher denomination {denom_id} is inactive."
                        )
                    if currency_norm != "RUB":
                        raise ValidationError(
                            f"Order currency for VOUCHER must be 'RUB' (got '{currency_norm}')."
                        )
                    if unit_price != denom_row["cost_rub_kopecks"]:
                        raise ValidationError(
                            f"Unit price {unit_price} does not match catalog price {denom_row['cost_rub_kopecks']} "
                            f"for denomination {denom_id}."
                        )
                    saw_inventory_backed = True

                validated_items.append((prod_id, denom_id, prod_type, qty, unit_price, meta_json))

            if saw_inventory_backed and saw_manual_review:
                raise ValidationError(
                    "Cannot mix inventory-backed and MANUAL_REVIEW items in the same order (OD-04)."
                )

            # 3. Insert order and order items
            cur.execute(
                """
                INSERT INTO orders (user_id, status, total_amount_minor, currency)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, OrderStatus.PENDING_PAYMENT.value, total_amount_minor, currency_norm),
            )
            order_id = cur.lastrowid

            for prod_id, denom_id, prod_type, qty, unit_price, meta_json in validated_items:
                cur.execute(
                    """
                    INSERT INTO order_items (
                        order_id, product_id, denomination_id, product_type_snapshot, quantity, unit_price_minor, currency, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, prod_id, denom_id, prod_type, qty, unit_price, currency_norm, meta_json),
                )

            return self.get_order(order_id)

    def transition_order_status(
        self,
        order_id: int,
        target_status: OrderStatus,
        notes: Optional[str] = None,
        expected_current_status: Optional[OrderStatus] = None,
        *,
        actor_type: Optional[str] = None,
        actor_id: Optional[int] = None,
        actor_external_id: Optional[str] = None,
    ) -> Order:
        """Transitions order status enforcing strict state machine, inventory/payment invariants, and audit logging."""
        order_id = _validate_int(order_id, "order_id")
        if not isinstance(target_status, OrderStatus):
            raise ValidationError(f"Invalid target_status: {target_status!r}")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            order = self.get_order(order_id)
            current_status = expected_current_status if expected_current_status is not None else order.status

            allowed_targets = ALLOWED_ORDER_TRANSITIONS.get(current_status, set())
            if target_status not in allowed_targets:
                raise InvalidStateTransitionError(
                    f"Cannot transition order {order_id} from {current_status.value} to {target_status.value}."
                )

            # AR-P3-005: If moving PAYMENT_REVIEW -> PAID and receipts exist, require at least one ACCEPTED receipt
            if current_status == OrderStatus.PAYMENT_REVIEW and target_status == OrderStatus.PAID:
                cur.execute(
                    "SELECT COUNT(*) AS total_cnt, SUM(CASE WHEN review_status = 'ACCEPTED' THEN 1 ELSE 0 END) AS acc_cnt "
                    "FROM payment_receipts WHERE order_id = ?",
                    (order_id,),
                )
                rcpt_stats = cur.fetchone()
                if rcpt_stats["total_cnt"] > 0 and (rcpt_stats["acc_cnt"] or 0) == 0:
                    raise InvalidStateTransitionError(
                        f"Cannot mark order {order_id} PAID: payment receipts exist without an ACCEPTED receipt."
                    )

            # SG-P2-004 & AR-P3-004: Strict inventory completion invariants before FULFILLING -> COMPLETED
            if target_status == OrderStatus.COMPLETED:
                cur.execute(
                    "SELECT COUNT(*) AS res_cnt FROM inventory_items WHERE reserved_order_id = ? AND status = 'RESERVED'",
                    (order_id,),
                )
                if cur.fetchone()["res_cnt"] > 0:
                    raise InvalidStateTransitionError(
                        f"Cannot complete order {order_id} while RESERVED inventory items remain."
                    )

                cur.execute(
                    """
                    SELECT
                        oi.product_id,
                        oi.denomination_id,
                        oi.product_type_snapshot,
                        oi.quantity,
                        fp.delivery_method AS fixed_delivery_method
                    FROM order_items oi
                    LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
                    WHERE oi.order_id = ?
                    """,
                    (order_id,),
                )
                oi_rows = cur.fetchall()
                if not oi_rows:
                    raise ValidationError(f"Cannot complete order {order_id} with no order items.")

                expected_inv_by_key: Dict[Tuple[int, Optional[int]], int] = {}
                for r in oi_rows:
                    p_type = ProductType(r["product_type_snapshot"])
                    d_method = (
                        DeliveryMethod(r["fixed_delivery_method"])
                        if r["fixed_delivery_method"] is not None
                        else None
                    )
                    if is_inventory_backed_item(p_type, d_method):
                        key = (r["product_id"], r["denomination_id"])
                        expected_inv_by_key[key] = expected_inv_by_key.get(key, 0) + r["quantity"]

                cur.execute(
                    """
                    SELECT product_id, denomination_id, COUNT(*) AS sold_qty
                    FROM inventory_items
                    WHERE reserved_order_id = ? AND status = 'SOLD'
                    GROUP BY product_id, denomination_id
                    """,
                    (order_id,),
                )
                sold_by_key = {
                    (r["product_id"], r["denomination_id"]): r["sold_qty"]
                    for r in cur.fetchall()
                }
                if sold_by_key != expected_inv_by_key:
                    raise ValidationError(
                        f"Cannot complete order {order_id}: SOLD inventory {sold_by_key} "
                        f"does not match expected inventory-backed order items {expected_inv_by_key}."
                    )

            # SG-P2-004: Cannot transition to CANCELLED while RESERVED items remain
            if target_status == OrderStatus.CANCELLED:
                cur.execute(
                    "SELECT COUNT(*) AS res_cnt FROM inventory_items WHERE reserved_order_id = ? AND status = 'RESERVED'",
                    (order_id,),
                )
                if cur.fetchone()["res_cnt"] > 0:
                    raise InvalidStateTransitionError(
                        f"Cannot cancel order {order_id} while RESERVED inventory items remain; release reservations first."
                    )

            if target_status == OrderStatus.PAID:
                cur.execute(
                    """
                    UPDATE orders
                    SET status = ?, paid_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = ?
                    """,
                    (target_status.value, order_id, current_status.value),
                )
            else:
                cur.execute(
                    """
                    UPDATE orders
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND status = ?
                    """,
                    (target_status.value, order_id, current_status.value),
                )

            if cur.rowcount == 0:
                raise ConflictError(
                    f"Concurrent state modification on order {order_id}: order is no longer in expected state '{current_status.value}'."
                )

            # SG-P2-015: Always preserve transition metadata in audit_events even when notes is None
            details_dict: Dict[str, Any] = {
                "from_status": current_status.value,
                "to_status": target_status.value,
                "notes": notes,
            }
            if actor_type is not None:
                details_dict["actor_type"] = actor_type
            if actor_id is not None:
                details_dict["actor_id"] = actor_id
            if actor_external_id is not None:
                details_dict["actor_external_id"] = actor_external_id

            details = json.dumps(details_dict, sort_keys=True)
            cur.execute(
                """
                INSERT INTO audit_events (
                    event_type, entity_type, entity_id, details_json
                ) VALUES (?, ?, ?, ?)
                """,
                ("ORDER_STATUS_TRANSITION", "ORDER", order_id, details),
            )

            return self.get_order(order_id)

    def cancel_order_atomically(
        self,
        order_id: int,
        *,
        actor_type: str = "OPERATOR",
        notes: Optional[str] = None,
        actor_id: Optional[int] = None,
        actor_external_id: Optional[str] = None,
    ) -> Order:
        """Atomically releases RESERVED inventory, rejects PENDING_REVIEW receipts, and transitions to CANCELLED.

        Enforces OD-02, OD-05, OD-06, and SG-P2-004:
        - USER cannot self-cancel while order is in PAYMENT_REVIEW (OD-05).
        - Pending receipts in PAYMENT_REVIEW are automatically marked REJECTED in the same transaction (OD-05).
        - RESERVED inventory returns to AVAILABLE; SOLD inventory remains immutable (OD-06).

        Audit contract (SG-P31-005):
        - `ORDER_STATUS_TRANSITION` (emitted inside `transition_order_status()`) is the primary canonical
          lifecycle transition event.
        - `ORDER_CANCELLED_ATOMIC` is the mandatory cancellation side-effect summary event recording
          `released_inventory_count` and `rejected_pending_receipts`.
        - Stage 3.2 `OrderService.cancel_order()` MUST delegate to this method and MUST NOT write a
          third duplicate cancellation audit event.
        """
        order_id = _validate_int(order_id, "order_id", min_value=1)
        actor_norm = actor_type.strip().upper() if isinstance(actor_type, str) else ""
        if actor_norm not in ("USER", "OPERATOR", "SYSTEM"):
            raise ValidationError(f"Invalid actor_type: {actor_type!r}")

        with transaction(self.conn, mode="IMMEDIATE"):
            order = self.get_order(order_id)
            if order.status not in (
                OrderStatus.PENDING_PAYMENT,
                OrderStatus.PAYMENT_REVIEW,
                OrderStatus.REFUND_REVIEW,
            ):
                raise InvalidStateTransitionError(
                    f"Cannot cancel order {order_id} in status '{order.status.value}'."
                )
            if actor_norm == "USER" and order.status == OrderStatus.PAYMENT_REVIEW:
                raise InvalidStateTransitionError(
                    f"User cannot self-cancel order {order_id} while in PAYMENT_REVIEW."
                )
            if actor_norm == "USER" and order.status == OrderStatus.REFUND_REVIEW:
                raise InvalidStateTransitionError(
                    f"User cannot cancel order {order_id} in REFUND_REVIEW."
                )
            if actor_norm == "SYSTEM" and order.status != OrderStatus.PENDING_PAYMENT:
                raise InvalidStateTransitionError(
                    f"SYSTEM can only expire/cancel PENDING_PAYMENT orders (got {order.status.value})."
                )

            inv_repo = InventoryRepository(self.conn)
            released_count = inv_repo.release_order_reservations(order_id)

            rcpt_repo = ReceiptRepository(self.conn)
            rejected_receipts = rcpt_repo.reject_pending_receipts_for_order(
                order_id,
                reviewer_notes=notes or "Order cancelled",
            )

            updated_order = self.transition_order_status(
                order_id,
                OrderStatus.CANCELLED,
                notes=notes,
                expected_current_status=order.status,
                actor_type=actor_norm,
                actor_id=actor_id,
                actor_external_id=actor_external_id,
            )

            audit_repo = AuditRepository(self.conn)
            audit_repo.record_event(
                "ORDER_CANCELLED_ATOMIC",
                "ORDER",
                order_id,
                actor_type=actor_norm,
                actor_id=actor_id,
                actor_external_id=actor_external_id,
                details={
                    "from_status": order.status.value,
                    "released_inventory_count": released_count,
                    "rejected_pending_receipts": rejected_receipts,
                    "notes": notes,
                },
            )
            return updated_order

    def count_open_pending_orders(self, user_id: int) -> int:
        user_id = _validate_int(user_id, "user_id", min_value=1)
        row = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM orders WHERE user_id = ? AND status = ?",
            (user_id, OrderStatus.PENDING_PAYMENT.value),
        ).fetchone()
        return int(row["cnt"])

    def list_expired_pending_orders(
        self,
        ttl_seconds: Optional[int] = None,
        *,
        limit: int = 50,
        now_utc: Optional[datetime] = None,
    ) -> List[Order]:
        ttl_val = (
            _validate_int(ttl_seconds, "ttl_seconds", min_value=1)
            if ttl_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.pending_payment_ttl_seconds
        )
        limit_val = _validate_int(limit, "limit", min_value=1)
        ref_now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        cutoff = _format_utc_ts(ref_now - timedelta(seconds=ttl_val))
        rows = self.conn.execute(
            """
            SELECT * FROM orders
            WHERE status = ? AND created_at <= ?
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (OrderStatus.PENDING_PAYMENT.value, cutoff, limit_val),
        ).fetchall()
        return [
            Order(
                id=row["id"],
                user_id=row["user_id"],
                status=OrderStatus(row["status"]),
                total_amount_minor=row["total_amount_minor"],
                currency=row["currency"],
                paid_at=row["paid_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get_order(self, order_id: int) -> Order:
        order_id = _validate_int(order_id, "order_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Order {order_id} not found.")

        return Order(
            id=row["id"],
            user_id=row["user_id"],
            status=OrderStatus(row["status"]),
            total_amount_minor=row["total_amount_minor"],
            currency=row["currency"],
            paid_at=row["paid_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_order_items(self, order_id: int) -> List[OrderItem]:
        order_id = _validate_int(order_id, "order_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC", (order_id,))
        return [
            OrderItem(
                id=row["id"],
                order_id=row["order_id"],
                product_id=row["product_id"],
                denomination_id=row["denomination_id"],
                product_type_snapshot=ProductType(row["product_type_snapshot"]),
                quantity=row["quantity"],
                unit_price_minor=row["unit_price_minor"],
                currency=row["currency"],
                metadata_json=row["metadata_json"],
                created_at=row["created_at"],
            )
            for row in cur.fetchall()
        ]


class ReceiptRepository:
    """Manages payment receipt upload, deduplication, and manual operator review."""

    MAX_RECEIPT_SIZE_BYTES = DEFAULT_OPERATIONAL_CONFIG.max_receipt_size_bytes

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def submit_receipt(
        self,
        order_id: int,
        user_id: int,
        file_bytes: bytes,
        provider_tx_id: Optional[str] = None,
        provider_name: str = "MANUAL",
    ) -> PaymentReceipt:
        """Submits a receipt file atomically with user status, ownership, and deduplication checks.

        Verifies user is not BLOCKED (OD-02), verifies order ownership (orders.user_id == user_id),
        canonicalizes provider_name, enforces SHA-256 and (provider_name, provider_tx_id) deduplication.
        Does NOT automatically mark the order as paid.
        """
        order_id = _validate_int(order_id, "order_id")
        user_id = _validate_int(user_id, "user_id")

        if not isinstance(provider_name, str):
            raise ValidationError("provider_name must be a string.")
        if provider_tx_id is not None and not isinstance(provider_tx_id, str):
            raise ValidationError("provider_tx_id must be a string or None.")

        if not isinstance(file_bytes, (bytes, bytearray)) or not file_bytes:
            raise ValidationError("Receipt file cannot be empty.")
        raw_bytes = bytes(file_bytes)
        if len(raw_bytes) > self.MAX_RECEIPT_SIZE_BYTES:
            raise ValidationError(
                f"Receipt file size ({len(raw_bytes)} bytes) exceeds 10MB limit."
            )

        norm_provider = provider_name.strip().upper() if provider_name.strip() else "MANUAL"
        file_sha256 = compute_receipt_sha256(raw_bytes)
        norm_tx_id = provider_tx_id.strip() if provider_tx_id and provider_tx_id.strip() else None

        try:
            with transaction(self.conn, mode="IMMEDIATE"):
                cur = self.conn.cursor()
                # 1. Verify user exists and is not BLOCKED (OD-02), order belongs to user, and status allows receipt
                cur.execute("SELECT id, status FROM users WHERE id = ?", (user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    raise NotFoundError(f"User {user_id} not found.")
                if user_row["status"] == UserStatus.BLOCKED.value:
                    raise UserBlockedError("Cannot submit payment receipt for blocked user.")

                cur.execute("SELECT id, user_id, status FROM orders WHERE id = ?", (order_id,))
                order_row = cur.fetchone()
                if not order_row or order_row["user_id"] != user_id:
                    raise ValidationError("Order does not exist or does not belong to the specified user.")

                if order_row["status"] not in (
                    OrderStatus.PENDING_PAYMENT.value,
                    OrderStatus.PAYMENT_REVIEW.value,
                ):
                    raise InvalidStateTransitionError(
                        f"Cannot submit receipt for order in status '{order_row['status']}'."
                    )

                # 2. Check existing receipt hash (without disclosing another order's ID)
                cur.execute("SELECT id FROM payment_receipts WHERE file_sha256 = ?", (file_sha256,))
                if cur.fetchone():
                    raise DuplicateReceiptError(
                        "Duplicate receipt: exact receipt file has already been uploaded."
                    )

                # 3. Check provider transaction ID scoped to provider_name (without disclosing other order ID)
                if norm_tx_id:
                    cur.execute(
                        "SELECT id FROM payment_receipts WHERE provider_name = ? AND provider_tx_id = ?",
                        (norm_provider, norm_tx_id),
                    )
                    if cur.fetchone():
                        raise DuplicateReceiptError(
                            f"Duplicate payment identifier: transaction for provider '{norm_provider}' has already been submitted."
                        )

                cur.execute(
                    """
                    INSERT INTO payment_receipts (
                        order_id, user_id, file_sha256, file_size_bytes, provider_name, provider_tx_id, review_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        user_id,
                        file_sha256,
                        len(raw_bytes),
                        norm_provider,
                        norm_tx_id,
                        ReceiptReviewStatus.PENDING_REVIEW.value,
                    ),
                )
                receipt_id = cur.lastrowid
                return self.get_receipt(receipt_id)
        except sqlite3.IntegrityError as e:
            if "orders.user_id" in str(e):
                raise ValidationError("Order does not belong to the specified user.") from e
            raise DuplicateReceiptError(f"Database constraint violation on receipt submission: {str(e)}") from e

    def update_review_status(
        self,
        receipt_id: int,
        review_status: ReceiptReviewStatus,
        reviewer_notes: Optional[str] = None,
    ) -> PaymentReceipt:
        receipt_id = _validate_int(receipt_id, "receipt_id")
        if not isinstance(review_status, ReceiptReviewStatus):
            raise ValidationError(f"Invalid review_status: {review_status!r}")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, order_id, review_status FROM payment_receipts WHERE id = ?", (receipt_id,))
            existing = cur.fetchone()
            if not existing:
                raise NotFoundError(f"Payment receipt {receipt_id} not found.")

            current_review = ReceiptReviewStatus(existing["review_status"])
            if current_review != ReceiptReviewStatus.PENDING_REVIEW and review_status != current_review:
                raise InvalidStateTransitionError(
                    f"Cannot change terminal receipt review status from '{current_review.value}' to '{review_status.value}'."
                )

            if review_status == ReceiptReviewStatus.ACCEPTED and current_review != ReceiptReviewStatus.ACCEPTED:
                cur.execute(
                    "SELECT id FROM payment_receipts WHERE order_id = ? AND review_status = 'ACCEPTED' AND id != ? LIMIT 1",
                    (existing["order_id"], receipt_id),
                )
                if cur.fetchone():
                    raise ConflictError(
                        f"Order {existing['order_id']} already has an ACCEPTED payment receipt."
                    )

            cur.execute(
                """
                UPDATE payment_receipts
                SET review_status = ?, reviewer_notes = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (review_status.value, reviewer_notes, receipt_id),
            )
            return self.get_receipt(receipt_id)

    review_receipt = update_review_status

    def reject_pending_receipts_for_order(
        self,
        order_id: int,
        reviewer_notes: Optional[str] = None,
    ) -> int:
        """Atomically marks all PENDING_REVIEW receipts for an order as REJECTED (OD-05)."""
        order_id = _validate_int(order_id, "order_id")
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.execute(
                """
                UPDATE payment_receipts
                SET review_status = ?, reviewer_notes = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE order_id = ? AND review_status = ?
                """,
                (
                    ReceiptReviewStatus.REJECTED.value,
                    reviewer_notes,
                    order_id,
                    ReceiptReviewStatus.PENDING_REVIEW.value,
                ),
            )
            return cur.rowcount

    def list_receipts_for_order(self, order_id: int) -> List[PaymentReceipt]:
        order_id = _validate_int(order_id, "order_id")
        rows = self.conn.execute(
            "SELECT * FROM payment_receipts WHERE order_id = ? ORDER BY id ASC",
            (order_id,),
        ).fetchall()
        return [self._row_to_receipt(row) for row in rows]

    @staticmethod
    def _row_to_receipt(row: sqlite3.Row) -> PaymentReceipt:
        return PaymentReceipt(
            id=row["id"],
            order_id=row["order_id"],
            user_id=row["user_id"],
            file_sha256=row["file_sha256"],
            file_size_bytes=row["file_size_bytes"],
            provider_name=row["provider_name"] if "provider_name" in row.keys() else "LEGACY_UNSPECIFIED",
            provider_tx_id=row["provider_tx_id"],
            review_status=ReceiptReviewStatus(row["review_status"]),
            reviewer_notes=row["reviewer_notes"],
            submitted_at=row["submitted_at"],
            reviewed_at=row["reviewed_at"],
        )

    def get_receipt(self, receipt_id: int) -> PaymentReceipt:
        receipt_id = _validate_int(receipt_id, "receipt_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Payment receipt {receipt_id} not found.")
        return self._row_to_receipt(row)


class AuditRepository:
    """Append-only structured audit event repository (forbids secrets/plaintext in details)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: int,
        *,
        details: Optional[Dict[str, Any]] = None,
        actor_type: Optional[str] = None,
        actor_id: Optional[int] = None,
        actor_external_id: Optional[str] = None,
    ) -> AuditEvent:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValidationError("event_type must be a non-empty string.")
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise ValidationError("entity_type must be a non-empty string.")
        entity_id = _validate_int(entity_id, "entity_id", min_value=1)
        _validate_audit_details(details)

        payload: Dict[str, Any] = dict(details) if details is not None else {}
        if actor_type is not None:
            payload["actor_type"] = actor_type
        if actor_id is not None:
            payload["actor_id"] = actor_id
        if actor_external_id is not None:
            payload["actor_external_id"] = actor_external_id

        details_json = json.dumps(payload, sort_keys=True) if payload else None
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO audit_events (event_type, entity_type, entity_id, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (event_type.strip().upper(), entity_type.strip().upper(), entity_id, details_json),
            )
            event_id = cur.lastrowid
            return self.get_event(event_id)

    def get_event(self, event_id: int) -> AuditEvent:
        event_id = _validate_int(event_id, "event_id", min_value=1)
        row = self.conn.execute("SELECT * FROM audit_events WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise NotFoundError(f"Audit event {event_id} not found.")
        return AuditEvent(
            id=row["id"],
            event_type=row["event_type"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            details_json=row["details_json"],
            created_at=row["created_at"],
        )

    def list_events(
        self,
        *,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditEvent]:
        limit = _validate_int(limit, "limit", min_value=1)
        clauses: List[str] = []
        params: List[Any] = []
        if entity_type is not None:
            clauses.append("entity_type = ?")
            params.append(entity_type.strip().upper())
        if entity_id is not None:
            clauses.append("entity_id = ?")
            params.append(_validate_int(entity_id, "entity_id", min_value=1))
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type.strip().upper())

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM audit_events {where_sql} ORDER BY id ASC LIMIT ?",
            tuple(params),
        ).fetchall()
        return [
            AuditEvent(
                id=row["id"],
                event_type=row["event_type"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                details_json=row["details_json"],
                created_at=row["created_at"],
            )
            for row in rows
        ]


class DeliveryRepository:
    """Persistence primitives for append-only delivery attempts and lease-guarded outcomes (AR-P3-001).

    Models honest at-least-once delivery semantics:
    - Each delivery attempt creates a distinct immutable-identity row (`delivery_id`, `attempt_no`, `claim_token`).
    - Only one unexpired `DISPATCHING` attempt can exist per order at a time.
    - Late `SENT` outcomes for an expired/reclaimed `UNCERTAIN` attempt are recorded on that old attempt
      with `DELIVERY_LATE_SENT_AFTER_RECLAIM` audit event without mutating any newer attempt or `SOLD` inventory.
    - Never stores plaintext digital codes in `order_deliveries` or `audit_events` (OD-07).
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _row_to_attempt(row: sqlite3.Row) -> DeliveryAttempt:
        return DeliveryAttempt(
            id=row["id"],
            order_id=row["order_id"],
            channel=row["channel"],
            delivery_kind=DeliveryKind(row["delivery_kind"]),
            status=DeliveryStatus(row["status"]),
            attempt_no=row["attempt_no"],
            claim_token=row["claim_token"],
            claimed_by=row["claimed_by"],
            lease_expires_at=_parse_utc_ts(row["lease_expires_at"]),
            external_message_id=row["external_message_id"],
            error_code=row["error_code"],
            created_at=_parse_utc_ts(row["created_at"]),
            updated_at=_parse_utc_ts(row["updated_at"]),
            delivered_at=_parse_optional_utc_ts(row["delivered_at"]),
        )

    def get_delivery(self, delivery_id: int) -> DeliveryAttempt:
        delivery_id = _validate_int(delivery_id, "delivery_id", min_value=1)
        row = self.conn.execute("SELECT * FROM order_deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if not row:
            raise NotFoundError(f"Delivery attempt {delivery_id} not found.")
        return self._row_to_attempt(row)

    def list_deliveries_for_order(self, order_id: int) -> List[DeliveryAttempt]:
        order_id = _validate_int(order_id, "order_id", min_value=1)
        rows = self.conn.execute(
            "SELECT * FROM order_deliveries WHERE order_id = ? ORDER BY attempt_no ASC, id ASC",
            (order_id,),
        ).fetchall()
        return [self._row_to_attempt(r) for r in rows]

    def claim_delivery(
        self,
        order_id: int,
        *,
        claimed_by: str,
        delivery_kind: DeliveryKind = DeliveryKind.INITIAL,
        channel: str = "TELEGRAM",
        lease_seconds: Optional[int] = None,
        cooldown_seconds: Optional[int] = None,
        min_send_budget_seconds: Optional[float] = None,
        http_timeout_seconds: Optional[float] = None,
        safety_margin_seconds: Optional[float] = None,
        now_utc: Optional[datetime] = None,
        now_monotonic: Optional[float] = None,
    ) -> DeliveryClaim:
        """Atomically reaps expired leases on the order and claims a new DISPATCHING delivery attempt."""
        order_id = _validate_int(order_id, "order_id", min_value=1)
        if not isinstance(claimed_by, str) or not claimed_by.strip():
            raise ValidationError("claimed_by must be a non-empty string.")
        if not isinstance(delivery_kind, DeliveryKind):
            raise ValidationError(f"Invalid delivery_kind: {delivery_kind!r}")
        if not isinstance(channel, str) or not channel.strip():
            raise ValidationError("channel must be a non-empty string.")

        lease_sec = (
            _validate_int(lease_seconds, "lease_seconds", min_value=1)
            if lease_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.delivery_lease_seconds
        )
        cooldown_sec = (
            _validate_int(cooldown_seconds, "cooldown_seconds", allow_zero=True, min_value=0)
            if cooldown_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.redelivery_cooldown_seconds
        )
        min_budget = _validate_positive_float(
            min_send_budget_seconds
            if min_send_budget_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.delivery_min_send_budget_seconds,
            "min_send_budget_seconds",
        )
        http_timeout = _validate_positive_float(
            http_timeout_seconds
            if http_timeout_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.delivery_http_timeout_seconds,
            "http_timeout_seconds",
        )
        safety_margin = _validate_positive_float(
            safety_margin_seconds
            if safety_margin_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.delivery_safety_margin_seconds,
            "safety_margin_seconds",
        )
        if min_budget < http_timeout + safety_margin:
            raise ValidationError(
                "min_send_budget_seconds must be >= http_timeout_seconds + safety_margin_seconds."
            )
        if min_budget >= float(lease_sec):
            raise ValidationError("min_send_budget_seconds must be strictly less than lease_seconds.")
        if (http_timeout + safety_margin) >= float(lease_sec):
            raise ValidationError(
                "http_timeout_seconds + safety_margin_seconds must be strictly less than lease_seconds."
            )

        ref_now = _ensure_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
        now_str = _format_utc_ts(ref_now)
        lease_expires_str = _format_utc_ts(ref_now + timedelta(seconds=lease_sec))
        base_mono = (
            time.monotonic()
            if now_monotonic is None
            else _validate_positive_float(now_monotonic, "now_monotonic")
            if isinstance(now_monotonic, (int, float)) and not isinstance(now_monotonic, bool) and now_monotonic > 0
            else (0.0 if now_monotonic == 0 and not isinstance(now_monotonic, bool) else _validate_positive_float(now_monotonic, "now_monotonic"))
        )
        claim_token = secrets.token_hex(16)
        audit_repo = AuditRepository(self.conn)

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT o.id, o.user_id, o.status AS order_status, u.status AS user_status
                FROM orders o
                JOIN users u ON u.id = o.user_id
                WHERE o.id = ?
                """,
                (order_id,),
            )
            order_row = cur.fetchone()
            if not order_row:
                raise NotFoundError(f"Order {order_id} not found.")
            if order_row["user_status"] == UserStatus.BLOCKED.value:
                raise UserBlockedError("Cannot claim digital delivery for a blocked user.")
            if OrderStatus(order_row["order_status"]) not in REVEALABLE_ORDER_STATUSES:
                raise InvalidStateTransitionError(
                    f"Order {order_id} status '{order_row['order_status']}' is not eligible for delivery."
                )

            # Reap expired DISPATCHING attempts for this order first
            cur.execute(
                """
                SELECT id, attempt_no FROM order_deliveries
                WHERE order_id = ? AND status = 'DISPATCHING' AND lease_expires_at <= ?
                """,
                (order_id, now_str),
            )
            expired_rows = cur.fetchall()
            for exp in expired_rows:
                cur.execute(
                    """
                    UPDATE order_deliveries
                    SET status = 'UNCERTAIN', error_code = 'LEASE_EXPIRED', updated_at = ?
                    WHERE id = ? AND status = 'DISPATCHING'
                    """,
                    (now_str, exp["id"]),
                )
                audit_repo.record_event(
                    "DELIVERY_LEASE_EXPIRED",
                    "DELIVERY",
                    exp["id"],
                    details={"order_id": order_id, "attempt_no": exp["attempt_no"]},
                )

            # Reject if an unexpired DISPATCHING lease is still active
            cur.execute(
                "SELECT id, attempt_no, lease_expires_at FROM order_deliveries WHERE order_id = ? AND status = 'DISPATCHING' LIMIT 1",
                (order_id,),
            )
            active_row = cur.fetchone()
            if active_row:
                raise DeliveryInProgressError(
                    f"Order {order_id} already has active delivery attempt {active_row['attempt_no']} "
                    f"until {active_row['lease_expires_at']}."
                )

            if delivery_kind == DeliveryKind.INITIAL:
                cur.execute(
                    "SELECT id FROM order_deliveries WHERE order_id = ? AND status = 'SENT' LIMIT 1",
                    (order_id,),
                )
                if cur.fetchone():
                    raise ConflictError(
                        f"Order {order_id} already has a SENT initial delivery; use REDELIVERY or OPERATOR_RESEND."
                    )
            else:
                if cooldown_sec > 0:
                    cutoff_str = _format_utc_ts(ref_now - timedelta(seconds=cooldown_sec))
                    cur.execute(
                        """
                        SELECT id, created_at FROM order_deliveries
                        WHERE order_id = ? AND COALESCE(delivered_at, created_at) > ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (order_id, cutoff_str),
                    )
                    recent = cur.fetchone()
                    if recent:
                        raise DeliveryCooldownError(
                            f"Redelivery cooldown ({cooldown_sec}s) active for order {order_id}."
                        )

            cur.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS max_att FROM order_deliveries WHERE order_id = ?",
                (order_id,),
            )
            attempt_no = int(cur.fetchone()["max_att"]) + 1

            cur.execute(
                """
                INSERT INTO order_deliveries (
                    order_id, channel, delivery_kind, status, attempt_no,
                    claim_token, claimed_by, lease_expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    channel.strip().upper(),
                    delivery_kind.value,
                    DeliveryStatus.DISPATCHING.value,
                    attempt_no,
                    claim_token,
                    claimed_by.strip(),
                    lease_expires_str,
                    now_str,
                    now_str,
                ),
            )
            delivery_id = cur.lastrowid

            audit_repo.record_event(
                "DELIVERY_CLAIMED",
                "DELIVERY",
                delivery_id,
                details={
                    "order_id": order_id,
                    "attempt_no": attempt_no,
                    "delivery_kind": delivery_kind.value,
                    "channel": channel.strip().upper(),
                    "claimed_by": claimed_by.strip(),
                    "lease_expires_at": lease_expires_str,
                },
            )

            return DeliveryClaim(
                delivery_id=delivery_id,
                order_id=order_id,
                target_user_id=order_row["user_id"],
                attempt_no=attempt_no,
                claim_token=claim_token,
                delivery_kind=delivery_kind,
                lease_expires_at=_parse_utc_ts(lease_expires_str),
                lease_monotonic_deadline=base_mono + float(lease_sec),
                min_send_budget_seconds=min_budget,
                http_timeout_seconds=http_timeout,
                safety_margin_seconds=safety_margin,
            )

    def check_pre_send_lease_budget(
        self,
        claim: DeliveryClaim,
        *,
        now_monotonic: Optional[float] = None,
        now_utc: Optional[datetime] = None,
    ) -> float:
        """Verifies live DB claim ownership/status and monotonic+UTC lease budget before external HTTP send (SG-P31-001).

        Guarantees:
        - Reads the live `order_deliveries` row by `(delivery_id, claim_token)` inside `BEGIN IMMEDIATE`.
        - Refuses send if the row is missing (`claim_token` stale) or no longer `DISPATCHING`
          (records `DELIVERY_PRE_SEND_REJECTED_STALE_STATE` without altering existing state).
        - Requires both UTC `lease_expires_at` and monotonic deadline to provide at least
          `max(min_send_budget_seconds, http_timeout_seconds + safety_margin_seconds)` remaining.
        - Conditionally transitions `DISPATCHING -> UNCERTAIN` (`LEASE_EXPIRED_BEFORE_SEND`) ONLY when
          still owned by `(delivery_id, claim_token)` and still in `DISPATCHING`.
        - Does NOT claim exactly-once delivery; only reduces duplicate-send probability within leases.
        """
        if now_monotonic is not None and (
            isinstance(now_monotonic, bool)
            or not isinstance(now_monotonic, (int, float))
            or not (-float("inf") < float(now_monotonic) < float("inf"))
        ):
            raise ValidationError(f"now_monotonic must be a finite number (got {now_monotonic!r}).")

        current_mono = time.monotonic() if now_monotonic is None else float(now_monotonic)
        ref_now = _ensure_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
        now_str = _format_utc_ts(ref_now)
        rejected_message: Optional[str] = None
        bounded_timeout: Optional[float] = None

        with transaction(self.conn, mode="IMMEDIATE"):
            row = self.conn.execute(
                "SELECT * FROM order_deliveries WHERE id = ? AND claim_token = ?",
                (claim.delivery_id, claim.claim_token),
            ).fetchone()
            if not row:
                rejected_message = (
                    f"Delivery claim {claim.delivery_id} does not match any active record for the supplied claim_token."
                )
            elif row["status"] != DeliveryStatus.DISPATCHING.value:
                AuditRepository(self.conn).record_event(
                    "DELIVERY_PRE_SEND_REJECTED_STALE_STATE",
                    "DELIVERY",
                    claim.delivery_id,
                    details={
                        "order_id": row["order_id"],
                        "attempt_no": row["attempt_no"],
                        "current_status": row["status"],
                    },
                )
                rejected_message = (
                    f"Delivery claim {claim.delivery_id} is no longer DISPATCHING (status='{row['status']}')."
                )
            else:
                db_expires_at = _parse_utc_ts(row["lease_expires_at"])
                utc_remaining = (db_expires_at - ref_now).total_seconds()
                mono_remaining = claim.lease_monotonic_deadline - current_mono
                effective_remaining = min(mono_remaining, utc_remaining)
                required_budget = max(
                    claim.min_send_budget_seconds,
                    claim.http_timeout_seconds + claim.safety_margin_seconds,
                )

                if utc_remaining <= 0 or mono_remaining <= 0 or effective_remaining < required_budget:
                    cur = self.conn.execute(
                        """
                        UPDATE order_deliveries
                        SET status = 'UNCERTAIN', error_code = 'LEASE_EXPIRED_BEFORE_SEND', updated_at = ?
                        WHERE id = ? AND claim_token = ? AND status = 'DISPATCHING'
                        """,
                        (now_str, claim.delivery_id, claim.claim_token),
                    )
                    AuditRepository(self.conn).record_event(
                        "LEASE_EXPIRED_BEFORE_SEND",
                        "DELIVERY",
                        claim.delivery_id,
                        details={
                            "order_id": claim.order_id,
                            "attempt_no": claim.attempt_no,
                            "mono_remaining_seconds": round(max(mono_remaining, 0.0), 3),
                            "utc_remaining_seconds": round(max(utc_remaining, 0.0), 3),
                            "effective_remaining_seconds": round(max(effective_remaining, 0.0), 3),
                            "required_budget_seconds": round(required_budget, 3),
                            "updated_status": cur.rowcount == 1,
                        },
                    )
                    rejected_message = (
                        f"Delivery lease {claim.delivery_id} lacks sufficient budget before external send "
                        f"(effective_remaining={effective_remaining:.3f}s < required={required_budget:.3f}s)."
                    )
                else:
                    bounded_timeout = min(
                        claim.http_timeout_seconds,
                        effective_remaining - claim.safety_margin_seconds,
                    )

        if rejected_message is not None:
            raise DeliveryLeaseExpiredError(rejected_message)
        assert bounded_timeout is not None
        return bounded_timeout

    def record_outcome(
        self,
        delivery_id: int,
        claim_token: str,
        outcome: DeliveryStatus,
        *,
        external_message_id: Optional[str] = None,
        error_code: Optional[str] = None,
        now_utc: Optional[datetime] = None,
    ) -> DeliveryAttempt:
        """Records delivery attempt outcome by (delivery_id, claim_token) without touching newer attempts.

        - DISPATCHING -> SENT / FAILED / UNCERTAIN.
        - UNCERTAIN -> SENT (late confirmation after reclaim): updates ONLY this old attempt,
          records DELIVERY_LATE_SENT_AFTER_RECLAIM audit event, never mutates newer attempts or their tokens.
        - UNCERTAIN -> FAILED / UNCERTAIN: does not clobber newer attempts, records DELIVERY_STALE_OUTCOME_IGNORED.
        """
        delivery_id = _validate_int(delivery_id, "delivery_id", min_value=1)
        if not isinstance(claim_token, str) or not claim_token.strip():
            raise ValidationError("claim_token must be non-empty.")
        if outcome not in (DeliveryStatus.SENT, DeliveryStatus.FAILED, DeliveryStatus.UNCERTAIN):
            raise ValidationError(f"Outcome status must be SENT, FAILED, or UNCERTAIN (got {outcome!r}).")

        now_str = _format_utc_ts(now_utc if now_utc is not None else datetime.now(timezone.utc))
        norm_msg_id = external_message_id.strip() if isinstance(external_message_id, str) and external_message_id.strip() else None
        norm_err = error_code.strip() if isinstance(error_code, str) and error_code.strip() else None
        audit_repo = AuditRepository(self.conn)

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM order_deliveries WHERE id = ? AND claim_token = ?",
                (delivery_id, claim_token.strip()),
            )
            row = cur.fetchone()
            if not row:
                raise NotFoundError(f"Delivery attempt {delivery_id} with provided claim_token not found.")

            current_status = DeliveryStatus(row["status"])
            order_id = row["order_id"]

            if current_status == DeliveryStatus.DISPATCHING:
                delivered_at = now_str if outcome == DeliveryStatus.SENT else None
                persisted_error = None if outcome == DeliveryStatus.SENT else norm_err
                cur.execute(
                    """
                    UPDATE order_deliveries
                    SET status = ?, external_message_id = ?, error_code = ?, delivered_at = ?, updated_at = ?
                    WHERE id = ? AND claim_token = ? AND status = 'DISPATCHING'
                    """,
                    (outcome.value, norm_msg_id, persisted_error, delivered_at, now_str, delivery_id, claim_token.strip()),
                )
                if cur.rowcount == 0:
                    raise ConflictError(f"Concurrent update on delivery attempt {delivery_id}.")
                audit_repo.record_event(
                    f"DELIVERY_{outcome.value}",
                    "DELIVERY",
                    delivery_id,
                    details={
                        "order_id": order_id,
                        "attempt_no": row["attempt_no"],
                        "external_message_id": norm_msg_id,
                        "error_code": persisted_error,
                    },
                )
                return self.get_delivery(delivery_id)

            if current_status == DeliveryStatus.UNCERTAIN:
                if outcome == DeliveryStatus.SENT:
                    cur.execute(
                        """
                        UPDATE order_deliveries
                        SET status = 'SENT', external_message_id = ?, error_code = NULL, delivered_at = ?, updated_at = ?
                        WHERE id = ? AND claim_token = ? AND status = 'UNCERTAIN'
                        """,
                        (norm_msg_id, now_str, now_str, delivery_id, claim_token.strip()),
                    )
                    if cur.rowcount == 0:
                        raise ConflictError(f"Concurrent update recording late SENT on delivery {delivery_id}.")
                    audit_repo.record_event(
                        "DELIVERY_LATE_SENT_AFTER_RECLAIM",
                        "DELIVERY",
                        delivery_id,
                        details={
                            "order_id": order_id,
                            "attempt_no": row["attempt_no"],
                            "previous_error_code": row["error_code"],
                            "external_message_id": norm_msg_id,
                        },
                    )
                    return self.get_delivery(delivery_id)

                audit_repo.record_event(
                    "DELIVERY_STALE_OUTCOME_IGNORED",
                    "DELIVERY",
                    delivery_id,
                    details={
                        "order_id": order_id,
                        "attempt_no": row["attempt_no"],
                        "ignored_outcome": outcome.value,
                        "error_code": norm_err,
                    },
                )
                return self.get_delivery(delivery_id)

            if current_status == DeliveryStatus.SENT:
                if outcome == DeliveryStatus.SENT and row["external_message_id"] == norm_msg_id:
                    return self._row_to_attempt(row)
                raise InvalidStateTransitionError(
                    f"Delivery attempt {delivery_id} is already terminal SENT."
                )

            raise InvalidStateTransitionError(
                f"Delivery attempt {delivery_id} is already terminal FAILED."
            )

    def reap_expired_leases(self, *, limit: int = 50, now_utc: Optional[datetime] = None) -> int:
        """Transitions expired DISPATCHING leases across orders to UNCERTAIN without erasing attempts."""
        limit = _validate_int(limit, "limit", min_value=1)
        now_str = _format_utc_ts(now_utc if now_utc is not None else datetime.now(timezone.utc))
        audit_repo = AuditRepository(self.conn)

        with transaction(self.conn, mode="IMMEDIATE"):
            rows = self.conn.execute(
                """
                SELECT id, order_id, attempt_no
                FROM order_deliveries
                WHERE status = 'DISPATCHING' AND lease_expires_at <= ?
                ORDER BY lease_expires_at ASC, id ASC
                LIMIT ?
                """,
                (now_str, limit),
            ).fetchall()
            reaped = 0
            for r in rows:
                cur = self.conn.execute(
                    """
                    UPDATE order_deliveries
                    SET status = 'UNCERTAIN', error_code = 'LEASE_EXPIRED', updated_at = ?
                    WHERE id = ? AND status = 'DISPATCHING'
                    """,
                    (now_str, r["id"]),
                )
                if cur.rowcount == 1:
                    reaped += 1
                    audit_repo.record_event(
                        "DELIVERY_LEASE_EXPIRED",
                        "DELIVERY",
                        r["id"],
                        details={"order_id": r["order_id"], "attempt_no": r["attempt_no"]},
                    )
            return reaped


class IdempotencyRepository:
    """SQLite persistence primitives for idempotent operation claiming, replay, stale reclaim, and retention (AR-P3-006)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> IdempotencyRecord:
        return IdempotencyRecord(
            id=row["id"],
            scope=row["scope"],
            actor_key=row["actor_key"],
            idempotency_key=row["idempotency_key"],
            request_hash=row["request_hash"],
            status=IdempotencyStatus(row["status"]),
            owner_token=row["owner_token"],
            response_entity_type=row["response_entity_type"],
            response_entity_id=row["response_entity_id"],
            stale_after=_parse_utc_ts(row["stale_after"]),
            expires_at=_parse_utc_ts(row["expires_at"]),
            created_at=_parse_utc_ts(row["created_at"]),
            updated_at=_parse_utc_ts(row["updated_at"]),
        )

    @staticmethod
    def _validate_key_parts(scope: str, actor_key: str, idempotency_key: str) -> Tuple[str, str, str]:
        if not isinstance(scope, str) or not scope.strip():
            raise ValidationError("idempotency scope must be a non-empty string.")
        if not isinstance(actor_key, str) or not actor_key.strip():
            raise ValidationError("idempotency actor_key must be a non-empty string.")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationError("idempotency_key must be a non-empty string.")
        return scope.strip(), actor_key.strip(), idempotency_key.strip()

    def get_record(self, scope: str, actor_key: str, idempotency_key: str) -> IdempotencyRecord:
        s, a, k = self._validate_key_parts(scope, actor_key, idempotency_key)
        row = self.conn.execute(
            "SELECT * FROM idempotency_keys WHERE scope = ? AND actor_key = ? AND idempotency_key = ?",
            (s, a, k),
        ).fetchone()
        if not row:
            raise NotFoundError(f"Idempotency key not found for ({s}, {a}, {k}).")
        return self._row_to_record(row)

    def claim(
        self,
        scope: str,
        actor_key: str,
        idempotency_key: str,
        request_hash: str,
        *,
        ttl_seconds: Optional[int] = None,
        stale_after_seconds: Optional[int] = None,
        now_utc: Optional[datetime] = None,
    ) -> IdempotencyClaimResult:
        """Atomically claims an idempotency key, replays COMPLETED outcomes, or reclaims stale IN_PROGRESS locks."""
        s, a, k = self._validate_key_parts(scope, actor_key, idempotency_key)
        if not isinstance(request_hash, str):
            raise ValidationError("request_hash must be a 64-character hex digest.")
        req_hash = request_hash.strip().lower()
        if len(req_hash) != 64 or not all(c in "0123456789abcdef" for c in req_hash):
            raise ValidationError("request_hash must be a 64-character hex digest.")

        ttl_sec = (
            _validate_int(ttl_seconds, "ttl_seconds", min_value=1)
            if ttl_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.idempotency_ttl_seconds
        )
        stale_sec = (
            _validate_int(stale_after_seconds, "stale_after_seconds", min_value=1)
            if stale_after_seconds is not None
            else DEFAULT_OPERATIONAL_CONFIG.idempotency_in_progress_stale_seconds
        )
        if stale_sec > ttl_sec:
            raise ValidationError("stale_after_seconds cannot exceed ttl_seconds.")

        ref_now = now_utc if now_utc is not None else datetime.now(timezone.utc)
        now_str = _format_utc_ts(ref_now)
        stale_str = _format_utc_ts(ref_now + timedelta(seconds=stale_sec))
        expires_str = _format_utc_ts(ref_now + timedelta(seconds=ttl_sec))
        new_owner_token = secrets.token_hex(16)

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                "SELECT * FROM idempotency_keys WHERE scope = ? AND actor_key = ? AND idempotency_key = ?",
                (s, a, k),
            )
            existing = cur.fetchone()

            if existing is not None and existing["expires_at"] <= now_str:
                # Expired key may be evicted and claimed freshly even with a new payload
                cur.execute("DELETE FROM idempotency_keys WHERE id = ?", (existing["id"],))
                existing = None

            if existing is None:
                cur.execute(
                    """
                    INSERT INTO idempotency_keys (
                        scope, actor_key, idempotency_key, request_hash, status,
                        owner_token, stale_after, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        s,
                        a,
                        k,
                        req_hash,
                        IdempotencyStatus.IN_PROGRESS.value,
                        new_owner_token,
                        stale_str,
                        expires_str,
                        now_str,
                        now_str,
                    ),
                )
                rec = self.get_record(s, a, k)
                return IdempotencyClaimResult(
                    outcome="CLAIMED",
                    record=rec,
                    owner_token=new_owner_token,
                    reclaimed=False,
                )

            if existing["request_hash"] != req_hash:
                raise IdempotencyConflictError(
                    f"Idempotency key '{k}' in scope '{s}' was already used with a different request payload."
                )

            existing_status = IdempotencyStatus(existing["status"])
            if existing_status == IdempotencyStatus.COMPLETED:
                return IdempotencyClaimResult(
                    outcome="REPLAY",
                    record=self._row_to_record(existing),
                    owner_token=existing["owner_token"],
                    reclaimed=False,
                )

            # IN_PROGRESS
            if existing["stale_after"] > now_str:
                raise IdempotencyInProgressError(
                    f"Operation for idempotency key '{k}' in scope '{s}' is currently in progress."
                )

            # Stale IN_PROGRESS lock reclaim guarded by old owner_token
            cur.execute(
                """
                UPDATE idempotency_keys
                SET owner_token = ?, stale_after = ?, expires_at = ?, updated_at = ?
                WHERE id = ? AND owner_token = ? AND status = 'IN_PROGRESS'
                """,
                (new_owner_token, stale_str, expires_str, now_str, existing["id"], existing["owner_token"]),
            )
            if cur.rowcount == 0:
                raise IdempotencyInProgressError(
                    f"Concurrent reclaim on idempotency key '{k}' in scope '{s}'."
                )

            AuditRepository(self.conn).record_event(
                "IDEMPOTENCY_IN_PROGRESS_RECLAIMED",
                "IDEMPOTENCY",
                existing["id"],
                details={"scope": s, "idempotency_key": k},
            )
            rec = self.get_record(s, a, k)
            return IdempotencyClaimResult(
                outcome="CLAIMED",
                record=rec,
                owner_token=new_owner_token,
                reclaimed=True,
            )

    def mark_completed(
        self,
        scope: str,
        actor_key: str,
        idempotency_key: str,
        owner_token: str,
        *,
        response_entity_type: str,
        response_entity_id: int,
        now_utc: Optional[datetime] = None,
    ) -> IdempotencyRecord:
        """Marks an IN_PROGRESS idempotency record COMPLETED with mandatory validated response metadata (SG-P31-003)."""
        s, a, k = self._validate_key_parts(scope, actor_key, idempotency_key)
        if not isinstance(owner_token, str) or not owner_token.strip():
            raise ValidationError("owner_token must be non-empty.")
        if not isinstance(response_entity_type, str) or not response_entity_type.strip():
            raise ValidationError("response_entity_type must be a non-empty string.")
        norm_entity_type = response_entity_type.strip()
        validated_entity_id = _validate_int(response_entity_id, "response_entity_id", min_value=1)
        now_str = _format_utc_ts(now_utc if now_utc is not None else datetime.now(timezone.utc))

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.execute(
                """
                UPDATE idempotency_keys
                SET status = 'COMPLETED', response_entity_type = ?, response_entity_id = ?, updated_at = ?
                WHERE scope = ? AND actor_key = ? AND idempotency_key = ? AND owner_token = ? AND status = 'IN_PROGRESS'
                """,
                (norm_entity_type, validated_entity_id, now_str, s, a, k, owner_token.strip()),
            )
            if cur.rowcount == 0:
                raise ConflictError(
                    f"Cannot complete idempotency key '{k}' in scope '{s}': lock missing, already completed, or reclaimed."
                )
            return self.get_record(s, a, k)

    def release_in_progress(
        self,
        scope: str,
        actor_key: str,
        idempotency_key: str,
        owner_token: str,
    ) -> bool:
        s, a, k = self._validate_key_parts(scope, actor_key, idempotency_key)
        if not isinstance(owner_token, str) or not owner_token.strip():
            raise ValidationError("owner_token must be non-empty.")
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.execute(
                """
                DELETE FROM idempotency_keys
                WHERE scope = ? AND actor_key = ? AND idempotency_key = ? AND owner_token = ? AND status = 'IN_PROGRESS'
                """,
                (s, a, k, owner_token.strip()),
            )
            return cur.rowcount == 1

    def cleanup_expired(self, *, limit: int = 500, now_utc: Optional[datetime] = None) -> int:
        limit = _validate_int(limit, "limit", min_value=1)
        now_str = _format_utc_ts(now_utc if now_utc is not None else datetime.now(timezone.utc))
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.execute(
                """
                DELETE FROM idempotency_keys
                WHERE id IN (
                    SELECT id FROM idempotency_keys
                    WHERE expires_at <= ?
                    ORDER BY expires_at ASC, id ASC
                    LIMIT ?
                )
                """,
                (now_str, limit),
            )
            return cur.rowcount
