-- Migration 004: Add denomination_id to order_items and enforce voucher denomination invariants at DB level (SG-P2-001)

-- 1. Pre-flight validation of existing rows (aborts atomically without mutating or deleting legacy data)
CREATE TEMP TABLE _mig004_legacy_voucher_order_items (
    ok INTEGER NOT NULL CONSTRAINT chk_004_existing_voucher_order_item_missing_denomination CHECK (ok = 1)
);
INSERT INTO _mig004_legacy_voucher_order_items (ok)
SELECT 0 FROM order_items oi
JOIN products p ON p.id = oi.product_id
WHERE oi.product_type_snapshot = 'VOUCHER' OR p.product_type = 'VOUCHER'
LIMIT 1;
DROP TABLE _mig004_legacy_voucher_order_items;

CREATE TEMP TABLE _mig004_order_item_type_mismatch (
    ok INTEGER NOT NULL CONSTRAINT chk_004_existing_order_item_product_type_mismatch CHECK (ok = 1)
);
INSERT INTO _mig004_order_item_type_mismatch (ok)
SELECT 0 FROM order_items oi
JOIN products p ON p.id = oi.product_id
WHERE oi.product_type_snapshot != p.product_type
LIMIT 1;
DROP TABLE _mig004_order_item_type_mismatch;

-- 2. Add denomination_id foreign key to order_items and lookup index
ALTER TABLE order_items ADD COLUMN denomination_id INTEGER REFERENCES voucher_denominations(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_order_items_order_product_denom
ON order_items(order_id, product_id, denomination_id);

-- 3. Triggers enforcing product_type_snapshot and denomination_id invariants on order_items
CREATE TRIGGER IF NOT EXISTS trg_order_items_validate_insert
BEFORE INSERT ON order_items
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

CREATE TRIGGER IF NOT EXISTS trg_voucher_denominations_prevent_reparent_with_order_items
BEFORE UPDATE OF product_id ON voucher_denominations
WHEN OLD.product_id != NEW.product_id
 AND EXISTS (SELECT 1 FROM order_items WHERE denomination_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'Cannot reassign voucher denomination product_id while order_items reference it');
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
