"""Repository layer for catalog, users, inventory, orders, and receipts."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.db.crypto import (
    canonicalize_code,
    compute_fingerprint,
    compute_receipt_sha256,
    decrypt_payload,
    encrypt_payload,
)
from src.db.models import (
    ALLOWED_ORDER_TRANSITIONS,
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
)


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
        self.conn.commit()
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
        if face_value_minor <= 0:
            raise ValidationError("face_value_minor must be strictly positive.")
        if cost_rub_kopecks < 0:
            raise ValidationError("cost_rub_kopecks cannot be negative.")

        product = self.get_product(product_id)
        if product.product_type != ProductType.VOUCHER:
            raise ValidationError(f"Product {product_id} is not of type VOUCHER.")

        cur = self.conn.cursor()
        try:
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
            self.conn.commit()
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
        if price_rub_kopecks <= 0:
            raise ValidationError("price_rub_kopecks must be strictly positive.")
        if duration_days is not None and duration_days <= 0:
            raise ValidationError("duration_days must be strictly positive if provided.")
        if status == ProductStatus.APPROVED and approval_status != ApprovalStatus.APPROVED:
            raise ValidationError(
                "Cannot create active FIXED_PRODUCT without approved supply method (approval_status must be APPROVED)."
            )

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
        self.conn.commit()
        return self.get_product(product_id)

    def get_product(self, product_id: int) -> Product:
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
        self.conn.commit()

    def update_fixed_product_supply_method(
        self, product_id: int, new_supply_method: str
    ) -> None:
        """Updates the supply method of a fixed product.

        SECURITY INVARIANT:
        Modifying the supply method immediately revokes any previous approval.
        Resets approval_status to DRAFT and resets products.status to DRAFT.
        """
        if not new_supply_method or not new_supply_method.strip():
            raise ValidationError("supply_method cannot be empty.")

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
        self.conn.commit()


class UserRepository:
    """Manages internal user identities and platform bindings."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_or_create_user(self, external_id: str, username: Optional[str] = None) -> Tuple[User, bool]:
        if not external_id or not external_id.strip():
            raise ValidationError("external_id cannot be empty.")

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
            self.conn.commit()
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
        self.conn.commit()
        return user, True

    def get_user(self, user_id: int) -> User:
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
        self.conn.execute(
            "UPDATE users SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status.value, user_id),
        )
        self.conn.commit()


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
        """Safely imports a batch of inventory codes.

        Canonicalizes codes, encrypts sensitive payloads, computes unique fingerprints,
        and rejects duplicates. Does NOT log any plaintext codes.
        """
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM products WHERE id = ?", (product_id,))
        if not cur.fetchone():
            raise NotFoundError(f"Product {product_id} not found.")

        if denomination_id is not None:
            cur.execute("SELECT id FROM voucher_denominations WHERE id = ?", (denomination_id,))
            if not cur.fetchone():
                raise NotFoundError(f"Voucher denomination {denomination_id} not found.")

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
            except sqlite3.IntegrityError:
                duplicates += 1

        self.conn.commit()
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
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE;")

        cur = self.conn.cursor()
        try:
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
            self.conn.commit()
            return upgraded
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def rotate_fingerprint_pepper(self, new_pepper: bytes, old_pepper: Optional[bytes] = None) -> int:
        """Rotates inventory HMAC fingerprints when FINGERPRINT_PEPPER is rotated.

        OPERATIONAL PROCEDURE FOR PEPPER ROTATION:
        1. Acquisition & Concurrency Pause: rotate_fingerprint_pepper automatically executes
           under an IMMEDIATE transaction lock (BEGIN IMMEDIATE) to pause and block concurrent
           inventory imports and writes.
        2. Recalculation: Decrypts each canonical inventory item payload and recalculates
           fingerprints using `new_pepper`.
        3. Atomic Commitment: All fingerprint updates are committed in a single transaction.
           On ANY error during rotation, the entire transaction is rolled back immediately,
           leaving all fingerprints in their consistent pre-rotation state.
        4. Secret Switching: After this method returns successfully, update the application's
           environment variable / secret store `FINGERPRINT_PEPPER` to `new_pepper` before resuming
           new import operations.

        Args:
            new_pepper: New HMAC secret key bytes.
            old_pepper: Optional previous pepper for validation.

        Returns:
            Number of rotated inventory items.
        """
        if not new_pepper or not isinstance(new_pepper, bytes):
            raise ValueError("new_pepper must be non-empty bytes.")

        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE;")

        cur = self.conn.cursor()
        try:
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
            self.conn.commit()
            return rotated
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def reserve_items_for_order(
        self,
        order_id: int,
        product_id: int,
        quantity: int,
        denomination_id: Optional[int] = None,
    ) -> List[int]:
        """Atomically reserves available inventory units for an order.

        Uses conditional updates to guarantee concurrency safety.
        """
        if quantity <= 0:
            raise ValidationError("Quantity to reserve must be strictly positive.")

        cur = self.conn.cursor()
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
                WHERE product_id = ? AND status = 'AVAILABLE'
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
            if reserved_ids:
                placeholders = ",".join("?" for _ in reserved_ids)
                cur.execute(
                    f"""
                    UPDATE inventory_items
                    SET status = 'AVAILABLE', reserved_order_id = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    tuple(reserved_ids),
                )
                self.conn.commit()
            raise InsufficientInventoryError(
                f"Concurrency conflict: requested {quantity} units, successfully locked {len(reserved_ids)}."
            )

        self.conn.commit()
        return reserved_ids

    def release_order_reservations(self, order_id: int) -> int:
        """Releases reserved inventory items back to AVAILABLE status if an order is cancelled or expires.

        Invariant: NEVER reverts SOLD items to AVAILABLE.
        """
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE inventory_items
            SET status = 'AVAILABLE', reserved_order_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE reserved_order_id = ? AND status = 'RESERVED'
            """,
            (order_id,),
        )
        count = cur.rowcount
        self.conn.commit()
        return count

    def finalize_order_sale(self, order_id: int) -> int:
        """Transitions reserved inventory items to permanently SOLD state upon successful order completion."""
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE inventory_items
            SET status = 'SOLD', updated_at = CURRENT_TIMESTAMP
            WHERE reserved_order_id = ? AND status = 'RESERVED'
            """,
            (order_id,),
        )
        count = cur.rowcount
        self.conn.commit()
        return count

    def get_revealed_code(self, item_id: int) -> str:
        """Reveals the decrypted code for an inventory item (for authorized fulfillment only)."""
        cur = self.conn.cursor()
        cur.execute("SELECT encrypted_payload FROM inventory_items WHERE id = ?", (item_id,))
        row = cur.fetchone()
        if not row:
            raise NotFoundError(f"Inventory item {item_id} not found.")
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
        if total_amount_minor <= 0:
            raise ValidationError("total_amount_minor must be strictly positive.")
        if not items:
            raise ValidationError("Order must contain at least one order item.")

        currency_norm = currency.strip().upper() if currency else ""
        if not currency_norm:
            raise ValidationError("Order currency must not be empty.")

        # Verify mathematical consistency between total_amount_minor and item prices/quantities/currency
        computed_total = 0
        for item in items:
            qty = item.get("quantity")
            unit_price = item.get("unit_price_minor")
            item_curr = item.get("currency")

            if not isinstance(qty, int) or qty <= 0:
                raise ValidationError("Item quantity must be a strictly positive integer.")
            if not isinstance(unit_price, int) or unit_price <= 0:
                raise ValidationError("Unit price must be a strictly positive integer.")
            if item_curr is not None and item_curr.strip().upper() != currency_norm:
                raise ValidationError(
                    f"Item currency '{item_curr}' does not match order currency '{currency_norm}'."
                )

            computed_total += qty * unit_price

        if total_amount_minor != computed_total:
            raise ValidationError(
                f"Order total_amount_minor ({total_amount_minor}) does not match sum of items ({computed_total})."
            )

        # Acquire write lock immediately to prevent concurrent status/approval revocation during order creation
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE;")

        cur = self.conn.cursor()
        try:
            # 1. Verify user exists and is not blocked
            cur.execute("SELECT id, status FROM users WHERE id = ?", (user_id,))
            user_row = cur.fetchone()
            if not user_row:
                raise NotFoundError(f"User {user_id} not found.")
            if user_row["status"] == UserStatus.BLOCKED.value:
                raise ValidationError("Cannot create order for blocked user.")

            # 2. Validate all products and supply approvals under active lock
            validated_items = []
            for item in items:
                prod_id = item["product_id"]
                qty = item["quantity"]
                unit_price = item["unit_price_minor"]
                prod_type = item["product_type_snapshot"]
                meta_json = item.get("metadata_json")

                # Validate product exists and is approved for sale
                cur.execute("SELECT id, status, product_type FROM products WHERE id = ?", (prod_id,))
                prod_row = cur.fetchone()
                if not prod_row:
                    raise NotFoundError(f"Product {prod_id} not found.")
                if prod_row["status"] != ProductStatus.APPROVED.value:
                    raise ValidationError(f"Product '{prod_id}' is not approved for sale.")

                # Defense-in-depth: For FIXED_PRODUCT, verify approval_status directly
                if prod_row["product_type"] == ProductType.FIXED_PRODUCT.value:
                    cur.execute(
                        "SELECT approval_status FROM fixed_product_details WHERE product_id = ?",
                        (prod_id,),
                    )
                    fp_row = cur.fetchone()
                    if not fp_row or fp_row["approval_status"] != ApprovalStatus.APPROVED.value:
                        raise ValidationError(
                            f"Fixed product '{prod_id}' does not have an approved supply method."
                        )

                validated_items.append((prod_id, prod_type, qty, unit_price, meta_json))

            # 3. Insert order and order items
            cur.execute(
                """
                INSERT INTO orders (user_id, status, total_amount_minor, currency)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, OrderStatus.PENDING_PAYMENT.value, total_amount_minor, currency_norm),
            )
            order_id = cur.lastrowid

            for prod_id, prod_type, qty, unit_price, meta_json in validated_items:
                cur.execute(
                    """
                    INSERT INTO order_items (
                        order_id, product_id, product_type_snapshot, quantity, unit_price_minor, currency, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (order_id, prod_id, prod_type, qty, unit_price, currency_norm, meta_json),
                )

            self.conn.commit()
            return self.get_order(order_id)
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def transition_order_status(
        self,
        order_id: int,
        target_status: OrderStatus,
        notes: Optional[str] = None,
    ) -> Order:
        """Transitions order status enforcing strict state machine transitions and audit logging."""
        order = self.get_order(order_id)
        current_status = order.status

        allowed_targets = ALLOWED_ORDER_TRANSITIONS.get(current_status, set())
        if target_status not in allowed_targets:
            raise InvalidStateTransitionError(
                f"Cannot transition order {order_id} from {current_status.value} to {target_status.value}."
            )

        cur = self.conn.cursor()
        if target_status == OrderStatus.PAID:
            cur.execute(
                """
                UPDATE orders
                SET status = ?, paid_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target_status.value, order_id),
            )
        else:
            cur.execute(
                """
                UPDATE orders
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target_status.value, order_id),
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
        self.conn.commit()
        return self.get_order(order_id)

    def get_order(self, order_id: int) -> Order:
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
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id ASC", (order_id,))
        return [
            OrderItem(
                id=row["id"],
                order_id=row["order_id"],
                product_id=row["product_id"],
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
        """Submits a receipt file with strict SHA-256 deduplication and provider-scoped tx deduplication.

        Canonicalizes provider_name, preserves binary receipt hashes, and scopes provider transaction IDs.
        Does NOT automatically mark the order as paid.
        """
        if not file_bytes:
            raise ValidationError("Receipt file cannot be empty.")
        if len(file_bytes) > self.MAX_RECEIPT_SIZE_BYTES:
            raise ValidationError(
                f"Receipt file size ({len(file_bytes)} bytes) exceeds 10MB limit."
            )

        norm_provider = provider_name.strip().upper() if provider_name and provider_name.strip() else "MANUAL"
        file_sha256 = compute_receipt_sha256(file_bytes)
        norm_tx_id = provider_tx_id.strip() if provider_tx_id and provider_tx_id.strip() else None

        cur = self.conn.cursor()
        # Verify order and user exist
        cur.execute("SELECT id FROM orders WHERE id = ?", (order_id,))
        if not cur.fetchone():
            raise NotFoundError(f"Order {order_id} not found.")

        cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if not cur.fetchone():
            raise NotFoundError(f"User {user_id} not found.")

        # Check existing receipt hash
        cur.execute("SELECT id, order_id FROM payment_receipts WHERE file_sha256 = ?", (file_sha256,))
        existing = cur.fetchone()
        if existing:
            raise DuplicateReceiptError(
                f"Duplicate receipt: exact file already uploaded for order {existing['order_id']}."
            )

        # Check provider transaction ID scoped to provider_name if present
        if norm_tx_id:
            cur.execute(
                "SELECT id, order_id FROM payment_receipts WHERE provider_name = ? AND provider_tx_id = ?",
                (norm_provider, norm_tx_id),
            )
            existing_tx = cur.fetchone()
            if existing_tx:
                raise DuplicateReceiptError(
                    f"Duplicate payment identifier: provider '{norm_provider}' tx '{norm_tx_id}' already attached to order {existing_tx['order_id']}."
                )

        try:
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
            self.conn.commit()
            return self.get_receipt(receipt_id)
        except sqlite3.IntegrityError as e:
            self.conn.rollback()
            raise DuplicateReceiptError(f"Database constraint violation on receipt submission: {str(e)}") from e
        except Exception:
            self.conn.rollback()
            raise

    def update_review_status(
        self,
        receipt_id: int,
        review_status: ReceiptReviewStatus,
        reviewer_notes: Optional[str] = None,
    ) -> PaymentReceipt:
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
        self.conn.commit()
        return self.get_receipt(receipt_id)

    review_receipt = update_review_status

    def get_receipt(self, receipt_id: int) -> PaymentReceipt:
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
