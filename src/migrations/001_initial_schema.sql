-- Migration 001: Initial schema for polymorphic catalog, users, orders, inventory, and receipts

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE NOT NULL,
    username TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'BLOCKED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_users_external_id ON users(external_id);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_type TEXT NOT NULL CHECK (product_type IN ('VOUCHER', 'FIXED_PRODUCT')),
    display_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT', 'APPROVED', 'DISABLED')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_products_type_status ON products(product_type, status);

CREATE TABLE IF NOT EXISTS voucher_details (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    region TEXT NOT NULL CHECK (region IN ('TR', 'US')),
    currency TEXT NOT NULL CHECK (currency IN ('TRY', 'USD')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS voucher_denominations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    region TEXT NOT NULL CHECK (region IN ('TR', 'US')),
    currency TEXT NOT NULL CHECK (currency IN ('TRY', 'USD')),
    face_value_minor INTEGER NOT NULL CHECK (face_value_minor > 0),
    cost_rub_kopecks INTEGER NOT NULL CHECK (cost_rub_kopecks >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (region, currency, face_value_minor)
);

CREATE INDEX IF NOT EXISTS idx_voucher_denoms_lookup ON voucher_denominations(region, currency, is_active);

CREATE TABLE IF NOT EXISTS fixed_product_details (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    price_rub_kopecks INTEGER NOT NULL CHECK (price_rub_kopecks > 0),
    delivery_method TEXT NOT NULL CHECK (delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION', 'MANUAL_REVIEW')),
    supply_method TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
    approval_status TEXT NOT NULL DEFAULT 'DRAFT' CHECK (approval_status IN ('DRAFT', 'APPROVED', 'SUSPENDED')),
    duration_days INTEGER CHECK (duration_days IS NULL OR duration_days > 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'PENDING_PAYMENT' CHECK (
        status IN (
            'PENDING_PAYMENT',
            'PAYMENT_REVIEW',
            'PAID',
            'FULFILLING',
            'COMPLETED',
            'CANCELLED',
            'REFUND_REVIEW'
        )
    ),
    total_amount_minor INTEGER NOT NULL CHECK (total_amount_minor > 0),
    currency TEXT NOT NULL CHECK (currency IN ('RUB', 'TRY', 'USD')),
    paid_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    product_type_snapshot TEXT NOT NULL CHECK (product_type_snapshot IN ('VOUCHER', 'FIXED_PRODUCT')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_minor INTEGER NOT NULL CHECK (unit_price_minor >= 0),
    currency TEXT NOT NULL,
    metadata_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    denomination_id INTEGER REFERENCES voucher_denominations(id) ON DELETE RESTRICT,
    inventory_type TEXT NOT NULL CHECK (inventory_type IN ('VOUCHER_CODE', 'FIXED_CODE', 'ACTIVATION_DATA')),
    encrypted_payload BLOB NOT NULL,
    fingerprint TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (
        status IN ('AVAILABLE', 'RESERVED', 'SOLD', 'QUARANTINED', 'DISABLED')
    ),
    reserved_order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_inventory_lookup ON inventory_items(product_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_denom_lookup ON inventory_items(denomination_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_reserved_order ON inventory_items(reserved_order_id);

CREATE TABLE IF NOT EXISTS payment_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    file_sha256 TEXT UNIQUE NOT NULL,
    file_size_bytes INTEGER NOT NULL CHECK (file_size_bytes > 0 AND file_size_bytes <= 10485760),
    provider_tx_id TEXT,
    review_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW' CHECK (
        review_status IN ('PENDING_REVIEW', 'ACCEPTED', 'REJECTED', 'DUPLICATE')
    ),
    reviewer_notes TEXT,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_file_sha256 ON payment_receipts(file_sha256);
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_provider_tx ON payment_receipts(provider_tx_id) WHERE provider_tx_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_receipts_order ON payment_receipts(order_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    details_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id);
