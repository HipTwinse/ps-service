"""Data models, enums, and domain exceptions for the database layer."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class ProductType(str, Enum):
    VOUCHER = "VOUCHER"
    FIXED_PRODUCT = "FIXED_PRODUCT"


class ProductStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    DISABLED = "DISABLED"


class DeliveryMethod(str, Enum):
    CODE = "CODE"
    OFFICIAL_ACTIVATION = "OFFICIAL_ACTIVATION"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class ApprovalStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUSPENDED = "SUSPENDED"


class InventoryType(str, Enum):
    VOUCHER_CODE = "VOUCHER_CODE"
    FIXED_CODE = "FIXED_CODE"
    ACTIVATION_DATA = "ACTIVATION_DATA"


class InventoryStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    SOLD = "SOLD"
    QUARANTINED = "QUARANTINED"
    DISABLED = "DISABLED"


class OrderStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAYMENT_REVIEW = "PAYMENT_REVIEW"
    PAID = "PAID"
    FULFILLING = "FULFILLING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUND_REVIEW = "REFUND_REVIEW"


class ReceiptReviewStatus(str, Enum):
    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class DeliveryStatus(str, Enum):
    DISPATCHING = "DISPATCHING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class DeliveryKind(str, Enum):
    INITIAL = "INITIAL"
    REDELIVERY = "REDELIVERY"
    OPERATOR_RESEND = "OPERATOR_RESEND"


class IdempotencyStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


# Allowed state transitions for Orders
ALLOWED_ORDER_TRANSITIONS: Dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: {OrderStatus.PAYMENT_REVIEW, OrderStatus.CANCELLED},
    OrderStatus.PAYMENT_REVIEW: {OrderStatus.PAID, OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.FULFILLING, OrderStatus.REFUND_REVIEW},
    OrderStatus.FULFILLING: {OrderStatus.COMPLETED, OrderStatus.REFUND_REVIEW},
    OrderStatus.COMPLETED: {OrderStatus.REFUND_REVIEW},
    OrderStatus.CANCELLED: set(),  # Terminal state
    OrderStatus.REFUND_REVIEW: {OrderStatus.CANCELLED},
}

# Explicit compatibility matrix between ProductType and InventoryType
ALLOWED_INVENTORY_TYPES_BY_PRODUCT_TYPE: Dict[ProductType, set[InventoryType]] = {
    ProductType.VOUCHER: {InventoryType.VOUCHER_CODE},
    ProductType.FIXED_PRODUCT: {InventoryType.FIXED_CODE, InventoryType.ACTIVATION_DATA},
}

# OrderStatus constraints for inventory operations
RESERVABLE_ORDER_STATUSES: set[OrderStatus] = {
    OrderStatus.PENDING_PAYMENT,
    OrderStatus.PAYMENT_REVIEW,
    OrderStatus.PAID,
    OrderStatus.FULFILLING,
}
RELEASABLE_ORDER_STATUSES: set[OrderStatus] = {
    OrderStatus.PENDING_PAYMENT,
    OrderStatus.PAYMENT_REVIEW,
    OrderStatus.CANCELLED,
    OrderStatus.REFUND_REVIEW,
}
FINALIZABLE_ORDER_STATUSES: set[OrderStatus] = {
    OrderStatus.PAID,
    OrderStatus.FULFILLING,
}
REVEALABLE_ORDER_STATUSES: set[OrderStatus] = {
    OrderStatus.PAID,
    OrderStatus.FULFILLING,
    OrderStatus.COMPLETED,
}


# Domain Exceptions
class DatabaseError(Exception):
    """Base exception for database operations."""
    pass


class NotFoundError(DatabaseError):
    """Entity not found."""
    pass


class ConflictError(DatabaseError):
    """Uniqueness constraint or concurrency conflict."""
    pass


class ValidationError(DatabaseError):
    """Business rule or contract violation."""
    pass


def validate_inventory_type_for_product(
    product_type: ProductType,
    inventory_type: InventoryType,
) -> None:
    """Validates that an InventoryType is permitted for a given ProductType."""
    if not isinstance(product_type, ProductType):
        raise ValidationError(f"Invalid product_type: {product_type!r}")
    if not isinstance(inventory_type, InventoryType):
        raise ValidationError(f"Invalid inventory_type: {inventory_type!r}")

    allowed = ALLOWED_INVENTORY_TYPES_BY_PRODUCT_TYPE.get(product_type, set())
    if inventory_type not in allowed:
        allowed_str = ", ".join(sorted(t.value for t in allowed))
        raise ValidationError(
            f"Inventory type '{inventory_type.value}' is incompatible with product type '{product_type.value}'. "
            f"Allowed inventory types: [{allowed_str}]."
        )


def is_inventory_backed_item(
    product_type: ProductType,
    delivery_method: Optional[DeliveryMethod] = None,
) -> bool:
    """Determines whether an order line item uses SQLite `inventory_items` (OD-03, OD-04).

    - VOUCHER -> always inventory-backed (VOUCHER_CODE).
    - FIXED_PRODUCT with DeliveryMethod.CODE or DeliveryMethod.OFFICIAL_ACTIVATION -> inventory-backed.
    - FIXED_PRODUCT with DeliveryMethod.MANUAL_REVIEW -> non-inventory.
    """
    if not isinstance(product_type, ProductType):
        raise ValidationError(f"Invalid product_type: {product_type!r}")
    if product_type == ProductType.VOUCHER:
        if delivery_method is not None:
            raise ValidationError("delivery_method must be None for VOUCHER products.")
        return True
    if not isinstance(delivery_method, DeliveryMethod):
        raise ValidationError(
            f"FIXED_PRODUCT requires a valid DeliveryMethod (got {delivery_method!r})."
        )
    return delivery_method in (DeliveryMethod.CODE, DeliveryMethod.OFFICIAL_ACTIVATION)


class InvalidStateTransitionError(DatabaseError):
    """Illegal state transition attempted."""
    pass


class InsufficientInventoryError(DatabaseError):
    """Not enough inventory available to fulfill reservation."""
    pass


class DuplicateReceiptError(ConflictError):
    """Payment receipt SHA-256 hash or provider transaction ID already exists."""
    pass


class UserBlockedError(ValidationError):
    """Operation forbidden for BLOCKED user (subclass of ValidationError for compatibility)."""
    pass


class IdempotencyConflictError(ConflictError):
    """Idempotency key reused with a different request_hash."""
    pass


class IdempotencyInProgressError(ConflictError):
    """Concurrent request with identical idempotency key is actively in progress."""
    pass


class DeliveryInProgressError(ConflictError):
    """An unexpired delivery dispatch lease is already active for the order."""
    pass


class DeliveryLeaseExpiredError(DatabaseError):
    """Delivery lease expired prior to dispatch or recording."""
    pass


class DeliveryCooldownError(ConflictError):
    """Redelivery requested before the configured cooldown elapsed."""
    pass


# Domain Record Dataclasses
@dataclass(frozen=True)
class User:
    id: int
    external_id: str
    username: Optional[str]
    status: UserStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Product:
    id: int
    product_type: ProductType
    display_name: str
    status: ProductStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VoucherDetails:
    product_id: int
    region: str
    currency: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VoucherDenomination:
    id: int
    product_id: int
    region: str
    currency: str
    face_value_minor: int
    cost_rub_kopecks: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class FixedProductDetails:
    product_id: int
    price_rub_kopecks: int
    delivery_method: DeliveryMethod
    supply_method: str
    approval_status: ApprovalStatus
    duration_days: Optional[int]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InventoryItem:
    id: int
    product_id: int
    denomination_id: Optional[int]
    inventory_type: InventoryType
    fingerprint: str
    status: InventoryStatus
    reserved_order_id: Optional[int]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Order:
    id: int
    user_id: int
    status: OrderStatus
    total_amount_minor: int
    currency: str
    paid_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OrderItem:
    id: int
    order_id: int
    product_id: int
    denomination_id: Optional[int]
    product_type_snapshot: ProductType
    quantity: int
    unit_price_minor: int
    currency: str
    metadata_json: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class PaymentReceipt:
    id: int
    order_id: int
    user_id: int
    file_sha256: str
    file_size_bytes: int
    provider_name: str
    provider_tx_id: Optional[str]
    review_status: ReceiptReviewStatus
    reviewer_notes: Optional[str]
    submitted_at: datetime
    reviewed_at: Optional[datetime]


@dataclass(frozen=True)
class AuditEvent:
    id: int
    event_type: str
    entity_type: str
    entity_id: int
    details_json: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class DeliveryAttempt:
    id: int
    order_id: int
    channel: str
    delivery_kind: DeliveryKind
    status: DeliveryStatus
    attempt_no: int
    claim_token: str
    claimed_by: str
    lease_expires_at: datetime
    external_message_id: Optional[str]
    error_code: Optional[str]
    created_at: datetime
    updated_at: datetime
    delivered_at: Optional[datetime]


@dataclass(frozen=True)
class DeliveryClaim:
    delivery_id: int
    order_id: int
    target_user_id: int
    attempt_no: int
    claim_token: str
    delivery_kind: DeliveryKind
    lease_expires_at: datetime
    lease_monotonic_deadline: float
    min_send_budget_seconds: float
    http_timeout_seconds: float
    safety_margin_seconds: float = 5.0


@dataclass(frozen=True)
class IdempotencyRecord:
    id: int
    scope: str
    actor_key: str
    idempotency_key: str
    request_hash: str
    status: IdempotencyStatus
    owner_token: str
    response_entity_type: Optional[str]
    response_entity_id: Optional[int]
    stale_after: datetime
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IdempotencyClaimResult:
    outcome: str  # 'CLAIMED' or 'REPLAY'
    record: IdempotencyRecord
    owner_token: str
    reclaimed: bool = False
