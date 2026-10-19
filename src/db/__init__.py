"""Database package for PlayStation Digital Vouchers & Products Service.

Provides connection management, cryptographic functions, schema migrations,
domain models, and transactional repositories.
"""

from src.db.connection import (
    backup_database,
    create_connection,
    get_connection_context,
    restore_database,
)
from src.db.crypto import (
    FernetCipher,
    compute_fingerprint,
    compute_receipt_sha256,
    decrypt_payload,
    encrypt_payload,
)
from src.db.migrations import (
    get_applied_migrations,
    run_migrations,
)
from src.db.models import (
    AuditEvent,
    ConflictError,
    DeliveryMethod,
    FixedProductDetails,
    InsufficientInventoryError,
    InvalidStateTransitionError,
    InventoryItem,
    InventoryStatus,
    InventoryType,
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
from src.db.repositories import (
    CatalogRepository,
    InventoryRepository,
    OrderRepository,
    ReceiptRepository,
    UserRepository,
)

__all__ = [
    # Connection & migrations
    "create_connection",
    "get_connection_context",
    "backup_database",
    "restore_database",
    "run_migrations",
    "get_applied_migrations",
    # Crypto
    "FernetCipher",
    "compute_fingerprint",
    "compute_receipt_sha256",
    # Enums & Models
    "ProductType",
    "ProductStatus",
    "DeliveryMethod",
    "InventoryType",
    "InventoryStatus",
    "OrderStatus",
    "ReceiptReviewStatus",
    "UserStatus",
    "User",
    "Product",
    "VoucherDetails",
    "VoucherDenomination",
    "FixedProductDetails",
    "Order",
    "OrderItem",
    "InventoryItem",
    "PaymentReceipt",
    "AuditEvent",
    # Exceptions
    "ConflictError",
    "ValidationError",
    "InsufficientInventoryError",
    "InvalidStateTransitionError",
    # Repositories
    "CatalogRepository",
    "UserRepository",
    "InventoryRepository",
    "OrderRepository",
    "ReceiptRepository",
]
