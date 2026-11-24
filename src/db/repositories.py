"""Repository layer for catalog, users, inventory, orders, and receipts."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.db.connection import transaction
from src.db.crypto import (
    canonicalize_code,
    compute_fingerprint,
    compute_receipt_sha256,
    decrypt_payload,
    encrypt_payload,
)
from src.db.models import (
    ALLOWED_ORDER_TRANSITIONS,
    FINALIZABLE_ORDER_STATUSES,
    RELEASABLE_ORDER_STATUSES,
    RESERVABLE_ORDER_STATUSES,
    REVEALABLE_ORDER_STATUSES,
    ApprovalStatus,
    ConflictError,
    DeliveryMethod,
    DuplicateReceiptError,
    FixedProductDetails,
    InsufficientInventoryError,
    InventoryItem,
    InventoryStatus,
    InventoryType,
    InvalidStateTransitionError,
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
    UserStatus,
    ValidationError,
    VoucherDenomination,
    VoucherDetails,
    validate_inventory_type_for_product,
)


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
        if region not in ("TR", "US"):
            raise ValidationError(f"Invalid region '{region}'. Must be 'TR' or 'US'.")
        if currency not in ("TRY", "USD"):
            raise ValidationError(f"Invalid currency '{currency}'. Must be 'TRY' or 'USD'.")
        if not display_name or not display_name.strip():
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

        try:
            with transaction(self.conn, mode="IMMEDIATE"):
                product = self.get_product(product_id)
                if product.product_type != ProductType.VOUCHER:
                    raise ValidationError(f"Product {product_id} is not of type VOUCHER.")

                cur = self.conn.cursor()
                cur.execute(
                    """
                    INSERT INTO voucher_denominations (
                        product_id, region, currency, face_value_minor, cost_rub_kopecks, is_active
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        region.strip().upper(),
                        currency.strip().upper(),
                        face_value_minor,
                        cost_rub_kopecks,
                        1 if is_active else 0,
                    ),
                )
                denom_id = cur.lastrowid
            return self.get_denomination(denom_id)
        except sqlite3.IntegrityError as e:
            raise ConflictError(
                f"Denomination for {region} {currency} {face_value_minor} already exists: {str(e)}"
            )

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
                (ProductType.FIXED_PRODUCT.value, display_name, status.value),
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
                    supply_method,
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

    def update_product_status(self, product_id: int, new_status: ProductStatus) -> None:
        product_id = _validate_int(product_id, "product_id")
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
        with transaction(self.conn, mode="IMMEDIATE"):
            self.conn.execute(
                """
                UPDATE fixed_product_details
                SET approval_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE product_id = ?
                """,
                (approval_status.value, product_id),
            )
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
        if not new_supply_method or not new_supply_method.strip():
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
        if not external_id or not external_id.strip():
            raise ValidationError("external_id cannot be empty.")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT * FROM users WHERE external_id = ?", (external_id.strip(),))
            row = cur.fetchone()
            if row:
                # Update username if changed
                if username and row["username"] != username:
                    cur.execute(
                        "UPDATE users SET username = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (username, row["id"]),
                    )
                user = User(
                    id=row["id"],
                    external_id=row["external_id"],
                    username=username if username else row["username"],
                    status=UserStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                return user, False

            # Create fresh user
            cur.execute(
                "INSERT INTO users (external_id, username, status) VALUES (?, ?, ?)",
                (external_id.strip(), username, UserStatus.ACTIVE.value),
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
        with transaction(self.conn, mode="IMMEDIATE"):
            self.conn.execute(
                "UPDATE users SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status.value, user_id),
            )


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
        if not new_pepper or not isinstance(new_pepper, bytes):
            raise ValueError("new_pepper must be non-empty bytes.")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute("SELECT id, encrypted_payload, fingerprint FROM inventory_items")
            rows = cur.fetchall()
            rotated = 0
            for row in rows:
                raw_code = decrypt_payload(row["encrypted_payload"])
                clean_code = canonicalize_code(raw_code)
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

        Invariants (full-order fulfillment model):
        - Order must exist and be in FINALIZABLE_ORDER_STATUSES (PAID or FULFILLING).
        - Rejects duplicate finalization if any order items are already marked SOLD.
        - Requires exact match between every ordered (product_id, denomination_id, quantity) in order_items
          and currently RESERVED inventory items for this order; incomplete or mismatched
          reservations are rejected atomically with no partial transition.
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
                SELECT product_id, denomination_id, SUM(quantity) AS ordered_qty
                FROM order_items
                WHERE order_id = ?
                GROUP BY product_id, denomination_id
                """,
                (order_id,),
            )
            expected_by_key = {
                (row["product_id"], row["denomination_id"]): row["ordered_qty"]
                for row in cur.fetchall()
            }
            if not expected_by_key:
                raise ValidationError(
                    f"Order {order_id} has no order items to finalize."
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

    def get_revealed_code(
        self,
        item_id: int,
        *,
        expected_order_id: Optional[int] = None,
        expected_user_id: Optional[int] = None,
    ) -> str:
        """Reveals the decrypted code for a SOLD inventory item in a verified order/user context.

        Architectural decision:
        Context-free decryption via get_revealed_code() is explicitly disallowed.
        Both `expected_order_id` and `expected_user_id` MUST be supplied together.
        Internal maintenance/crypto checks (such as key/pepper rotation) use decrypt_payload()
        directly rather than exposing an unauthenticated bypass on the repository.

        Safety invariants enforced here:
        - Requires both `expected_order_id` and `expected_user_id` (strictly positive ints).
        - Reads inventory item and linked order in ONE atomic query (no WAL snapshot skew).
        - Rejects QUARANTINED, DISABLED, AVAILABLE, and RESERVED items (must be SOLD).
        - Verifies item -> order binding, order -> user ownership, and order status in REVEALABLE_ORDER_STATUSES.
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
                o.status AS order_status
            FROM inventory_items i
            LEFT JOIN orders o ON o.id = i.reserved_order_id
            WHERE i.id = ?
            """,
            (item_id,),
        )
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Inventory item {item_id} not found.")

        item_status = InventoryStatus(row["status"])
        if item_status in (InventoryStatus.QUARANTINED, InventoryStatus.DISABLED):
            raise ValidationError(
                f"Inventory item {item_id} is in '{item_status.value}' state and cannot be revealed."
            )

        linked_order_id = row["reserved_order_id"]
        if linked_order_id is None:
            raise ValidationError("Inventory item is not assigned to any order.")
        if linked_order_id != expected_order_id:
            raise ValidationError("Inventory item is not assigned to the specified order.")
        if item_status != InventoryStatus.SOLD:
            raise ValidationError(
                f"Inventory item must be SOLD before fulfillment reveal (current status: '{item_status.value}')."
            )
        if row["order_status"] is None or row["order_user_id"] is None:
            raise NotFoundError(f"Order {linked_order_id} not found.")
        if row["order_user_id"] != expected_user_id:
            raise ValidationError("Order does not belong to the specified user.")

        order_status = OrderStatus(row["order_status"])
        if order_status not in REVEALABLE_ORDER_STATUSES:
            raise InvalidStateTransitionError(
                f"Cannot reveal inventory code for order {linked_order_id} in status '{order_status.value}'."
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
    ) -> Order:
        """Creates an order and preserves historical price snapshots on all items.

        Atomic and transaction-safe:
        - Acquires an IMMEDIATE transaction lock (BEGIN IMMEDIATE) prior to reading user,
          product status, and approval status to eliminate race conditions between validation and insertion.
        - Strictly verifies that total_amount_minor matches sum of (quantity * unit_price_minor).
        - Verifies that currency is consistent across order and all item snapshots.
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

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            # 1. Verify user exists and is not blocked
            cur.execute("SELECT id, status FROM users WHERE id = ?", (user_id,))
            user_row = cur.fetchone()
            if not user_row:
                raise NotFoundError(f"User {user_id} not found.")
            if user_row["status"] == UserStatus.BLOCKED.value:
                raise ValidationError("Cannot create order for blocked user.")

            # 2. Validate all products, product types, denominations, supply approvals, and catalog prices under active lock
            validated_items = []
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
                        "SELECT price_rub_kopecks, approval_status FROM fixed_product_details WHERE product_id = ?",
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

                validated_items.append((prod_id, denom_id, prod_type, qty, unit_price, meta_json))

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
    ) -> Order:
        """Transitions order status enforcing strict state machine transitions and audit logging."""
        order_id = _validate_int(order_id, "order_id")

        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            order = self.get_order(order_id)
            current_status = expected_current_status if expected_current_status is not None else order.status

            allowed_targets = ALLOWED_ORDER_TRANSITIONS.get(current_status, set())
            if target_status not in allowed_targets:
                raise InvalidStateTransitionError(
                    f"Cannot transition order {order_id} from {current_status.value} to {target_status.value}."
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

            details = json.dumps({"notes": notes, "from_status": current_status.value, "to_status": target_status.value}) if notes else None
            cur.execute(
                """
                INSERT INTO audit_events (
                    event_type, entity_type, entity_id, details_json
                ) VALUES (?, ?, ?, ?)
                """,
                ("ORDER_STATUS_TRANSITION", "ORDER", order_id, details),
            )

        return self.get_order(order_id)

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

    MAX_RECEIPT_SIZE_BYTES = 10 * 1024 * 1024  # 10MB limit

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
        """Submits a receipt file atomically with strict order ownership and deduplication checks.

        Verifies order ownership (orders.user_id == user_id), canonicalizes provider_name,
        enforces SHA-256 and (provider_name, provider_tx_id) deduplication.
        Does NOT automatically mark the order as paid.
        """
        order_id = _validate_int(order_id, "order_id")
        user_id = _validate_int(user_id, "user_id")

        if not file_bytes:
            raise ValidationError("Receipt file cannot be empty.")
        if len(file_bytes) > self.MAX_RECEIPT_SIZE_BYTES:
            raise ValidationError(
                f"Receipt file size ({len(file_bytes)} bytes) exceeds 10MB limit."
            )

        norm_provider = provider_name.strip().upper() if provider_name and provider_name.strip() else "MANUAL"
        file_sha256 = compute_receipt_sha256(file_bytes)
        norm_tx_id = provider_tx_id.strip() if provider_tx_id and provider_tx_id.strip() else None

        try:
            with transaction(self.conn, mode="IMMEDIATE"):
                cur = self.conn.cursor()
                # 1. Verify user exists, order exists & belongs to user_id (without leaking existence/owner details),
                # and order status permits receipt submission.
                cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
                if not cur.fetchone():
                    raise NotFoundError(f"User {user_id} not found.")

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
                        len(file_bytes),
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
        with transaction(self.conn, mode="IMMEDIATE"):
            cur = self.conn.cursor()
            cur.execute(
                """
                UPDATE payment_receipts
                SET review_status = ?, reviewer_notes = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (review_status.value, reviewer_notes, receipt_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError(f"Payment receipt {receipt_id} not found.")
        return self.get_receipt(receipt_id)

    review_receipt = update_review_status

    def get_receipt(self, receipt_id: int) -> PaymentReceipt:
        receipt_id = _validate_int(receipt_id, "receipt_id")
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Payment receipt {receipt_id} not found.")
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
