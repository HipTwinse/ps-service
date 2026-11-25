-- Migration 003: Validate existing rows and enforce inventory type compatibility, denomination ownership, and receipt order ownership at DB level

-- 1. Pre-flight validation of existing rows (aborts atomically without modifying or deleting legacy data)
CREATE TEMP TABLE _mig003_voucher_type_guard (
    ok INTEGER NOT NULL CONSTRAINT chk_003_existing_voucher_inventory_type CHECK (ok = 1)
);
INSERT INTO _mig003_voucher_type_guard (ok)
SELECT 0 FROM inventory_items i
JOIN products p ON p.id = i.product_id
WHERE p.product_type = 'VOUCHER' AND i.inventory_type != 'VOUCHER_CODE'
LIMIT 1;
DROP TABLE _mig003_voucher_type_guard;

CREATE TEMP TABLE _mig003_fixed_type_guard (
    ok INTEGER NOT NULL CONSTRAINT chk_003_existing_fixed_inventory_type CHECK (ok = 1)
);
INSERT INTO _mig003_fixed_type_guard (ok)
SELECT 0 FROM inventory_items i
JOIN products p ON p.id = i.product_id
WHERE p.product_type = 'FIXED_PRODUCT' AND i.inventory_type NOT IN ('FIXED_CODE', 'ACTIVATION_DATA')
LIMIT 1;
DROP TABLE _mig003_fixed_type_guard;

CREATE TEMP TABLE _mig003_voucher_denom_req_guard (
    ok INTEGER NOT NULL CONSTRAINT chk_003_existing_voucher_missing_denomination CHECK (ok = 1)
);
INSERT INTO _mig003_voucher_denom_req_guard (ok)
SELECT 0 FROM inventory_items i
JOIN products p ON p.id = i.product_id
WHERE p.product_type = 'VOUCHER' AND i.denomination_id IS NULL
LIMIT 1;
DROP TABLE _mig003_voucher_denom_req_guard;

CREATE TEMP TABLE _mig003_non_voucher_denom_guard (
    ok INTEGER NOT NULL CONSTRAINT chk_003_existing_non_voucher_has_denomination CHECK (ok = 1)
);
INSERT INTO _mig003_non_voucher_denom_guard (ok)
SELECT 0 FROM inventory_items i
JOIN products p ON p.id = i.product_id
WHERE p.product_type != 'VOUCHER' AND i.denomination_id IS NOT NULL
LIMIT 1;
DROP TABLE _mig003_non_voucher_denom_guard;

CREATE TEMP TABLE _mig003_denom_owner_guard (
    ok INTEGER NOT NULL CONSTRAINT chk_003_existing_denomination_product_mismatch CHECK (ok = 1)
);
INSERT INTO _mig003_denom_owner_guard (ok)
SELECT 0 FROM inventory_items i
JOIN voucher_denominations vd ON vd.id = i.denomination_id
WHERE i.denomination_id IS NOT NULL AND vd.product_id != i.product_id
LIMIT 1;
DROP TABLE _mig003_denom_owner_guard;

CREATE TEMP TABLE _mig003_receipt_owner_guard (
    ok INTEGER NOT NULL CONSTRAINT chk_003_existing_receipt_order_owner_mismatch CHECK (ok = 1)
);
INSERT INTO _mig003_receipt_owner_guard (ok)
SELECT 0 FROM payment_receipts r
JOIN orders o ON o.id = r.order_id
WHERE o.user_id != r.user_id
LIMIT 1;
DROP TABLE _mig003_receipt_owner_guard;

-- 2. Triggers enforcing invariants on future INSERT/UPDATE operations
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
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_items_validate_update
BEFORE UPDATE OF product_id, denomination_id, inventory_type ON inventory_items
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
END;

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

CREATE TRIGGER IF NOT EXISTS trg_payment_receipts_validate_owner_insert
BEFORE INSERT ON payment_receipts
WHEN EXISTS (SELECT 1 FROM orders WHERE id = NEW.order_id)
 AND (SELECT user_id FROM orders WHERE id = NEW.order_id) != NEW.user_id
BEGIN
    SELECT RAISE(ABORT, 'Receipt user_id must match orders.user_id');
END;

CREATE TRIGGER IF NOT EXISTS trg_payment_receipts_validate_owner_update
BEFORE UPDATE OF order_id, user_id ON payment_receipts
WHEN EXISTS (SELECT 1 FROM orders WHERE id = NEW.order_id)
 AND (SELECT user_id FROM orders WHERE id = NEW.order_id) != NEW.user_id
BEGIN
    SELECT RAISE(ABORT, 'Receipt user_id must match orders.user_id');
END;

CREATE TRIGGER IF NOT EXISTS trg_orders_prevent_reparent_with_receipts
BEFORE UPDATE OF user_id ON orders
WHEN OLD.user_id != NEW.user_id
 AND EXISTS (SELECT 1 FROM payment_receipts WHERE order_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'Cannot reassign order user_id while payment_receipts reference order');
END;
