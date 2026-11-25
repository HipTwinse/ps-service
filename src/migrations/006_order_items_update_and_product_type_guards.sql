-- Migration 006: Harden order_items UPDATE guards (SG-P2-001-FOLLOWUP) and block product_type changes when order_items exist

-- 1. Pre-flight check: abort atomically without mutating data if existing RESERVED/SOLD inventory exceeds ordered quantity
CREATE TEMP TABLE _mig006_order_items_qty_below_allocated (
    ok INTEGER NOT NULL CONSTRAINT chk_006_existing_order_items_quantity_below_allocated CHECK (ok = 1)
);
INSERT INTO _mig006_order_items_qty_below_allocated (ok)
SELECT 0
FROM (
    SELECT i.reserved_order_id, i.product_id, i.denomination_id, COUNT(*) AS allocated_qty
    FROM inventory_items i
    WHERE i.status IN ('RESERVED', 'SOLD')
    GROUP BY i.reserved_order_id, i.product_id, i.denomination_id
) alloc
WHERE alloc.allocated_qty > COALESCE((
    SELECT SUM(oi.quantity)
    FROM order_items oi
    WHERE oi.order_id = alloc.reserved_order_id
      AND oi.product_id = alloc.product_id
      AND (
          (oi.denomination_id IS NULL AND alloc.denomination_id IS NULL)
          OR oi.denomination_id = alloc.denomination_id
      )
), 0)
LIMIT 1;
DROP TABLE _mig006_order_items_qty_below_allocated;

-- 2. Replace trg_order_items_validate_update to cover order_id, product_id, product_type_snapshot, denomination_id, quantity
DROP TRIGGER IF EXISTS trg_order_items_validate_update;

CREATE TRIGGER trg_order_items_validate_update
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

-- 3. Replace trg_products_prevent_type_change_with_inventory to also guard existing order_items
DROP TRIGGER IF EXISTS trg_products_prevent_type_change_with_inventory;

CREATE TRIGGER trg_products_prevent_type_change_with_inventory
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
