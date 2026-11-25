-- Migration 005: Prevent orphaned RESERVED/SOLD inventory on order delete (SG-P2-002) and enforce inventory_items <-> order_items (product_id, denomination_id) match (SG-P2-001)

-- 1. Pre-flight validation of existing rows (aborts atomically without mutating or deleting legacy data)
CREATE TEMP TABLE _mig005_orphaned_reserved_or_sold_inventory (
    ok INTEGER NOT NULL CONSTRAINT chk_005_existing_orphaned_reserved_or_sold_inventory CHECK (ok = 1)
);
INSERT INTO _mig005_orphaned_reserved_or_sold_inventory (ok)
SELECT 0 FROM inventory_items
WHERE status IN ('RESERVED', 'SOLD') AND reserved_order_id IS NULL
LIMIT 1;
DROP TABLE _mig005_orphaned_reserved_or_sold_inventory;

CREATE TEMP TABLE _mig005_available_inventory_with_order (
    ok INTEGER NOT NULL CONSTRAINT chk_005_existing_available_inventory_with_order CHECK (ok = 1)
);
INSERT INTO _mig005_available_inventory_with_order (ok)
SELECT 0 FROM inventory_items
WHERE status = 'AVAILABLE' AND reserved_order_id IS NOT NULL
LIMIT 1;
DROP TABLE _mig005_available_inventory_with_order;

CREATE TEMP TABLE _mig005_inventory_order_item_mismatch (
    ok INTEGER NOT NULL CONSTRAINT chk_005_existing_inventory_order_item_mismatch CHECK (ok = 1)
);
INSERT INTO _mig005_inventory_order_item_mismatch (ok)
SELECT 0 FROM inventory_items i
WHERE i.status IN ('RESERVED', 'SOLD')
  AND NOT EXISTS (
      SELECT 1 FROM order_items oi
      WHERE oi.order_id = i.reserved_order_id
        AND oi.product_id = i.product_id
        AND (
            (oi.denomination_id IS NULL AND i.denomination_id IS NULL)
            OR oi.denomination_id = i.denomination_id
        )
  )
LIMIT 1;
DROP TABLE _mig005_inventory_order_item_mismatch;

-- 2. Rebuild inventory_items with ON DELETE RESTRICT on reserved_order_id and status/order CHECK constraint
CREATE TABLE inventory_items_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    denomination_id INTEGER REFERENCES voucher_denominations(id) ON DELETE RESTRICT,
    inventory_type TEXT NOT NULL CHECK (inventory_type IN ('VOUCHER_CODE', 'FIXED_CODE', 'ACTIVATION_DATA')),
    encrypted_payload BLOB NOT NULL,
    fingerprint TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'AVAILABLE' CHECK (
        status IN ('AVAILABLE', 'RESERVED', 'SOLD', 'QUARANTINED', 'DISABLED')
    ),
    reserved_order_id INTEGER REFERENCES orders(id) ON DELETE RESTRICT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (status IN ('RESERVED', 'SOLD') AND reserved_order_id IS NOT NULL)
        OR (status = 'AVAILABLE' AND reserved_order_id IS NULL)
        OR (status IN ('QUARANTINED', 'DISABLED'))
    )
);

INSERT INTO inventory_items_new (
    id, product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id, created_at, updated_at
)
SELECT
    id, product_id, denomination_id, inventory_type, encrypted_payload, fingerprint, status, reserved_order_id, created_at, updated_at
FROM inventory_items;

DROP TRIGGER IF EXISTS trg_voucher_denominations_prevent_reparent;
DROP TRIGGER IF EXISTS trg_products_prevent_type_change_with_inventory;
DROP TRIGGER IF EXISTS trg_order_items_prevent_delete_with_inventory;
DROP TRIGGER IF EXISTS trg_order_items_validate_update;

DROP TABLE inventory_items;

ALTER TABLE inventory_items_new RENAME TO inventory_items;

CREATE INDEX IF NOT EXISTS idx_inventory_lookup ON inventory_items(product_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_denom_lookup ON inventory_items(denomination_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_reserved_order ON inventory_items(reserved_order_id);

-- Recreate cross-table triggers referencing inventory_items
CREATE TRIGGER IF NOT EXISTS trg_voucher_denominations_prevent_reparent
BEFORE UPDATE OF product_id ON voucher_denominations
WHEN OLD.product_id != NEW.product_id
 AND EXISTS (SELECT 1 FROM inventory_items WHERE denomination_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'Cannot reassign voucher denomination product_id while inventory_items reference it');
END;

CREATE TRIGGER IF NOT EXISTS trg_products_prevent_type_change_with_inventory
BEFORE UPDATE OF product_type ON products
WHEN OLD.product_type != NEW.product_type
 AND (
     EXISTS (SELECT 1 FROM inventory_items WHERE product_id = NEW.id)
     OR EXISTS (SELECT 1 FROM voucher_denominations WHERE product_id = NEW.id)
     OR EXISTS (SELECT 1 FROM order_items WHERE product_id = NEW.id)
 )
BEGIN
    SELECT RAISE(ABORT, 'Cannot change product_type while inventory_items, voucher_denominations, or order_items reference product');
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_prevent_delete_with_inventory
BEFORE DELETE ON order_items
WHEN EXISTS (
    SELECT 1 FROM inventory_items i
    WHERE i.reserved_order_id = OLD.order_id
      AND i.product_id = OLD.product_id
      AND (
          (OLD.denomination_id IS NULL AND i.denomination_id IS NULL)
          OR i.denomination_id = OLD.denomination_id
      )
      AND i.status IN ('RESERVED', 'SOLD')
)
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete order_item while RESERVED or SOLD inventory references it');
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_validate_update
BEFORE UPDATE OF order_id, product_id, product_type_snapshot, denomination_id, quantity ON order_items
BEGIN
    SELECT RAISE(ABORT, 'order_items.product_type_snapshot does not match products.product_type')
    WHERE (SELECT product_type FROM products WHERE id = NEW.product_id) != NEW.product_type_snapshot;

    SELECT RAISE(ABORT, 'denomination_id is required for VOUCHER order_items')
    WHERE NEW.denomination_id IS NULL
      AND (SELECT product_type FROM products WHERE id = NEW.product_id) = 'VOUCHER';

    SELECT RAISE(ABORT, 'denomination_id is only allowed for VOUCHER order_items')
    WHERE NEW.denomination_id IS NOT NULL
      AND (SELECT product_type FROM products WHERE id = NEW.product_id) != 'VOUCHER';

    SELECT RAISE(ABORT, 'order_items.denomination_id does not belong to product_id')
    WHERE NEW.denomination_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM voucher_denominations WHERE id = NEW.denomination_id)
      AND (SELECT product_id FROM voucher_denominations WHERE id = NEW.denomination_id) != NEW.product_id;

    SELECT RAISE(ABORT, 'Cannot change order_id, product_id, product_type_snapshot, or denomination_id of order_item while RESERVED or SOLD inventory is linked')
    WHERE (
        OLD.order_id != NEW.order_id
        OR OLD.product_id != NEW.product_id
        OR OLD.product_type_snapshot != NEW.product_type_snapshot
        OR OLD.denomination_id IS NOT NEW.denomination_id
    )
    AND EXISTS (
        SELECT 1 FROM inventory_items i
        WHERE i.reserved_order_id = OLD.order_id
          AND i.product_id = OLD.product_id
          AND (
              (OLD.denomination_id IS NULL AND i.denomination_id IS NULL)
              OR i.denomination_id = OLD.denomination_id
          )
          AND i.status IN ('RESERVED', 'SOLD')
    );

    SELECT RAISE(ABORT, 'Cannot reduce order_items.quantity below allocated RESERVED/SOLD inventory count')
    WHERE (
        COALESCE((
            SELECT SUM(oi.quantity)
            FROM order_items oi
            WHERE oi.order_id = OLD.order_id
              AND oi.product_id = OLD.product_id
              AND (
                  (OLD.denomination_id IS NULL AND oi.denomination_id IS NULL)
                  OR oi.denomination_id = OLD.denomination_id
              )
              AND oi.id != OLD.id
        ), 0) + NEW.quantity
    ) < (
        SELECT COUNT(*)
        FROM inventory_items i
        WHERE i.reserved_order_id = OLD.order_id
          AND i.product_id = OLD.product_id
          AND (
              (OLD.denomination_id IS NULL AND i.denomination_id IS NULL)
              OR i.denomination_id = OLD.denomination_id
          )
          AND i.status IN ('RESERVED', 'SOLD')
    );
END;

-- 3. Recreate inventory_items validation triggers (preserving 003 checks + adding order_items (product_id, denomination_id) match)
CREATE TRIGGER IF NOT EXISTS trg_inventory_items_validate_insert
BEFORE INSERT ON inventory_items
BEGIN
    SELECT RAISE(ABORT, 'Incompatible inventory_type for VOUCHER product')
    WHERE (SELECT product_type FROM products WHERE id = NEW.product_id) = 'VOUCHER'
      AND NEW.inventory_type != 'VOUCHER_CODE';

    SELECT RAISE(ABORT, 'Incompatible inventory_type for FIXED_PRODUCT product')
    WHERE (SELECT product_type FROM products WHERE id = NEW.product_id) = 'FIXED_PRODUCT'
      AND NEW.inventory_type NOT IN ('FIXED_CODE', 'ACTIVATION_DATA');

    SELECT RAISE(ABORT, 'denomination_id is required for VOUCHER products')
    WHERE NEW.denomination_id IS NULL
      AND (SELECT product_type FROM products WHERE id = NEW.product_id) = 'VOUCHER';

    SELECT RAISE(ABORT, 'denomination_id is only allowed for VOUCHER products')
    WHERE NEW.denomination_id IS NOT NULL
      AND (SELECT product_type FROM products WHERE id = NEW.product_id) != 'VOUCHER';

    SELECT RAISE(ABORT, 'denomination_id does not belong to product_id')
    WHERE NEW.denomination_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM voucher_denominations WHERE id = NEW.denomination_id)
      AND (SELECT product_id FROM voucher_denominations WHERE id = NEW.denomination_id) != NEW.product_id;

    SELECT RAISE(ABORT, 'Inventory reservation/sale must match an order_item (product_id, denomination_id)')
    WHERE NEW.status IN ('RESERVED', 'SOLD')
      AND NOT EXISTS (
          SELECT 1 FROM order_items oi
          WHERE oi.order_id = NEW.reserved_order_id
            AND oi.product_id = NEW.product_id
            AND (
                (oi.denomination_id IS NULL AND NEW.denomination_id IS NULL)
                OR oi.denomination_id = NEW.denomination_id
            )
      );
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_items_validate_update
BEFORE UPDATE OF product_id, denomination_id, inventory_type, status, reserved_order_id ON inventory_items
BEGIN
    SELECT RAISE(ABORT, 'Incompatible inventory_type for VOUCHER product')
    WHERE (SELECT product_type FROM products WHERE id = NEW.product_id) = 'VOUCHER'
      AND NEW.inventory_type != 'VOUCHER_CODE';

    SELECT RAISE(ABORT, 'Incompatible inventory_type for FIXED_PRODUCT product')
    WHERE (SELECT product_type FROM products WHERE id = NEW.product_id) = 'FIXED_PRODUCT'
      AND NEW.inventory_type NOT IN ('FIXED_CODE', 'ACTIVATION_DATA');

    SELECT RAISE(ABORT, 'denomination_id is required for VOUCHER products')
    WHERE NEW.denomination_id IS NULL
      AND (SELECT product_type FROM products WHERE id = NEW.product_id) = 'VOUCHER';

    SELECT RAISE(ABORT, 'denomination_id is only allowed for VOUCHER products')
    WHERE NEW.denomination_id IS NOT NULL
      AND (SELECT product_type FROM products WHERE id = NEW.product_id) != 'VOUCHER';

    SELECT RAISE(ABORT, 'denomination_id does not belong to product_id')
    WHERE NEW.denomination_id IS NOT NULL
      AND EXISTS (SELECT 1 FROM voucher_denominations WHERE id = NEW.denomination_id)
      AND (SELECT product_id FROM voucher_denominations WHERE id = NEW.denomination_id) != NEW.product_id;

    SELECT RAISE(ABORT, 'Inventory reservation/sale must match an order_item (product_id, denomination_id)')
    WHERE NEW.status IN ('RESERVED', 'SOLD')
      AND NOT EXISTS (
          SELECT 1 FROM order_items oi
          WHERE oi.order_id = NEW.reserved_order_id
            AND oi.product_id = NEW.product_id
            AND (
                (oi.denomination_id IS NULL AND NEW.denomination_id IS NULL)
                OR oi.denomination_id = NEW.denomination_id
            )
      );
END;

-- 4. Guard against order deletion while any inventory_items reference the order
CREATE TRIGGER IF NOT EXISTS trg_orders_prevent_delete_with_inventory
BEFORE DELETE ON orders
WHEN EXISTS (SELECT 1 FROM inventory_items WHERE reserved_order_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete order while inventory_items reference it');
END;
