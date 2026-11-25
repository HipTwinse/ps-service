-- Migration 007: Phase 3 Stage 3.1 DB-level integrity guards and persistence primitives
-- Forward-only migration covering SG-P2-004, SG-P2-006, OD-03, OD-04, OD-06, AR-P3-001,
-- AR-P3-004, AR-P3-005, AR-P3-006, AR-P3-007.
-- Note: Pre-flight checks execute atomically inside run_migrations() under BEGIN IMMEDIATE
-- prior to executing this DDL script.

-- 1. Indexes for single-accepted receipt, TTL order queries, and order/inventory lookups
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_single_accepted_per_order
ON payment_receipts(order_id)
WHERE review_status = 'ACCEPTED';

CREATE INDEX IF NOT EXISTS idx_orders_status_created_at
ON orders(status, created_at);

CREATE INDEX IF NOT EXISTS idx_inventory_order_status
ON inventory_items(reserved_order_id, status);

-- 2. Product detail and voucher denomination consistency guards (SG-P2-006, AR-P3-004)
CREATE TRIGGER IF NOT EXISTS trg_voucher_details_validate_insert
BEFORE INSERT ON voucher_details
BEGIN
    SELECT RAISE(ABORT, 'voucher_details can only reference VOUCHER products')
    WHERE NOT EXISTS (
        SELECT 1 FROM products p WHERE p.id = NEW.product_id AND p.product_type = 'VOUCHER'
    );

    SELECT RAISE(ABORT, 'Invalid region/currency pair for voucher_details')
    WHERE NOT ((NEW.region = 'TR' AND NEW.currency = 'TRY') OR (NEW.region = 'US' AND NEW.currency = 'USD'));
END;

CREATE TRIGGER IF NOT EXISTS trg_voucher_details_validate_update
BEFORE UPDATE OF product_id, region, currency ON voucher_details
BEGIN
    SELECT RAISE(ABORT, 'voucher_details can only reference VOUCHER products')
    WHERE NOT EXISTS (
        SELECT 1 FROM products p WHERE p.id = NEW.product_id AND p.product_type = 'VOUCHER'
    );

    SELECT RAISE(ABORT, 'Invalid region/currency pair for voucher_details')
    WHERE NOT ((NEW.region = 'TR' AND NEW.currency = 'TRY') OR (NEW.region = 'US' AND NEW.currency = 'USD'));

    SELECT RAISE(ABORT, 'Cannot modify voucher_details while voucher_denominations reference product')
    WHERE (OLD.product_id != NEW.product_id OR OLD.region != NEW.region OR OLD.currency != NEW.currency)
      AND EXISTS (SELECT 1 FROM voucher_denominations vd WHERE vd.product_id = OLD.product_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_fixed_product_details_validate_insert
BEFORE INSERT ON fixed_product_details
BEGIN
    SELECT RAISE(ABORT, 'fixed_product_details can only reference FIXED_PRODUCT products')
    WHERE NOT EXISTS (
        SELECT 1 FROM products p WHERE p.id = NEW.product_id AND p.product_type = 'FIXED_PRODUCT'
    );

    SELECT RAISE(ABORT, 'fixed_product_details.supply_method cannot be empty')
    WHERE TRIM(NEW.supply_method) = '';
END;

CREATE TRIGGER IF NOT EXISTS trg_fixed_product_details_validate_update
BEFORE UPDATE OF product_id, supply_method ON fixed_product_details
BEGIN
    SELECT RAISE(ABORT, 'fixed_product_details can only reference FIXED_PRODUCT products')
    WHERE NOT EXISTS (
        SELECT 1 FROM products p WHERE p.id = NEW.product_id AND p.product_type = 'FIXED_PRODUCT'
    );

    SELECT RAISE(ABORT, 'fixed_product_details.supply_method cannot be empty')
    WHERE TRIM(NEW.supply_method) = '';
END;

CREATE TRIGGER IF NOT EXISTS trg_voucher_denominations_validate_insert
BEFORE INSERT ON voucher_denominations
BEGIN
    SELECT RAISE(ABORT, 'voucher_denominations can only reference VOUCHER products')
    WHERE NOT EXISTS (
        SELECT 1 FROM products p WHERE p.id = NEW.product_id AND p.product_type = 'VOUCHER'
    );

    SELECT RAISE(ABORT, 'VOUCHER product is missing voucher_details row')
    WHERE NOT EXISTS (
        SELECT 1 FROM voucher_details vd WHERE vd.product_id = NEW.product_id
    );

    SELECT RAISE(ABORT, 'Invalid region/currency pair for voucher_denominations')
    WHERE NOT ((NEW.region = 'TR' AND NEW.currency = 'TRY') OR (NEW.region = 'US' AND NEW.currency = 'USD'));

    SELECT RAISE(ABORT, 'voucher_denominations region/currency must match voucher_details')
    WHERE EXISTS (
        SELECT 1 FROM voucher_details vd
        WHERE vd.product_id = NEW.product_id
          AND (vd.region != NEW.region OR vd.currency != NEW.currency)
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_voucher_denominations_validate_update
BEFORE UPDATE OF product_id, region, currency ON voucher_denominations
BEGIN
    SELECT RAISE(ABORT, 'voucher_denominations can only reference VOUCHER products')
    WHERE NOT EXISTS (
        SELECT 1 FROM products p WHERE p.id = NEW.product_id AND p.product_type = 'VOUCHER'
    );

    SELECT RAISE(ABORT, 'VOUCHER product is missing voucher_details row')
    WHERE NOT EXISTS (
        SELECT 1 FROM voucher_details vd WHERE vd.product_id = NEW.product_id
    );

    SELECT RAISE(ABORT, 'Invalid region/currency pair for voucher_denominations')
    WHERE NOT ((NEW.region = 'TR' AND NEW.currency = 'TRY') OR (NEW.region = 'US' AND NEW.currency = 'USD'));

    SELECT RAISE(ABORT, 'voucher_denominations region/currency must match voucher_details')
    WHERE EXISTS (
        SELECT 1 FROM voucher_details vd
        WHERE vd.product_id = NEW.product_id
          AND (vd.region != NEW.region OR vd.currency != NEW.currency)
    );

    SELECT RAISE(ABORT, 'Cannot change voucher_denominations region/currency while referenced by inventory_items or order_items')
    WHERE (OLD.region != NEW.region OR OLD.currency != NEW.currency)
      AND (
          EXISTS (SELECT 1 FROM inventory_items i WHERE i.denomination_id = OLD.id)
          OR EXISTS (SELECT 1 FROM order_items oi WHERE oi.denomination_id = OLD.id)
      );
END;

-- 3. Order item detail requirements, mixed-order guard (OD-04), and terminal freeze
CREATE TRIGGER IF NOT EXISTS trg_order_items_require_product_details_insert
BEFORE INSERT ON order_items
BEGIN
    SELECT RAISE(ABORT, 'VOUCHER order_item requires corresponding voucher_details row')
    WHERE NEW.product_type_snapshot = 'VOUCHER'
      AND NOT EXISTS (SELECT 1 FROM voucher_details vd WHERE vd.product_id = NEW.product_id);

    SELECT RAISE(ABORT, 'FIXED_PRODUCT order_item requires corresponding fixed_product_details row')
    WHERE NEW.product_type_snapshot = 'FIXED_PRODUCT'
      AND NOT EXISTS (SELECT 1 FROM fixed_product_details fp WHERE fp.product_id = NEW.product_id);
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_forbid_mixed_manual_and_inventory_insert
BEFORE INSERT ON order_items
BEGIN
    SELECT RAISE(ABORT, 'Cannot mix inventory-backed and MANUAL_REVIEW items in the same order (OD-04)')
    WHERE (
        (
            NEW.product_type_snapshot = 'VOUCHER'
            OR EXISTS (
                SELECT 1 FROM fixed_product_details fp
                WHERE fp.product_id = NEW.product_id
                  AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION')
            )
        )
        AND EXISTS (
            SELECT 1 FROM order_items oi
            JOIN fixed_product_details fp2 ON fp2.product_id = oi.product_id
            WHERE oi.order_id = NEW.order_id
              AND oi.product_type_snapshot = 'FIXED_PRODUCT'
              AND fp2.delivery_method = 'MANUAL_REVIEW'
        )
    )
    OR (
        NEW.product_type_snapshot = 'FIXED_PRODUCT'
        AND EXISTS (
            SELECT 1 FROM fixed_product_details fp
            WHERE fp.product_id = NEW.product_id
              AND fp.delivery_method = 'MANUAL_REVIEW'
        )
        AND EXISTS (
            SELECT 1 FROM order_items oi
            LEFT JOIN fixed_product_details fp2 ON fp2.product_id = oi.product_id
            WHERE oi.order_id = NEW.order_id
              AND (
                  oi.product_type_snapshot = 'VOUCHER'
                  OR fp2.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION')
              )
        )
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_forbid_mixed_manual_and_inventory_update
BEFORE UPDATE OF order_id, product_id, product_type_snapshot ON order_items
BEGIN
    SELECT RAISE(ABORT, 'VOUCHER order_item requires corresponding voucher_details row')
    WHERE NEW.product_type_snapshot = 'VOUCHER'
      AND NOT EXISTS (SELECT 1 FROM voucher_details vd WHERE vd.product_id = NEW.product_id);

    SELECT RAISE(ABORT, 'FIXED_PRODUCT order_item requires corresponding fixed_product_details row')
    WHERE NEW.product_type_snapshot = 'FIXED_PRODUCT'
      AND NOT EXISTS (SELECT 1 FROM fixed_product_details fp WHERE fp.product_id = NEW.product_id);

    SELECT RAISE(ABORT, 'Cannot mix inventory-backed and MANUAL_REVIEW items in the same order (OD-04)')
    WHERE (
        (
            NEW.product_type_snapshot = 'VOUCHER'
            OR EXISTS (
                SELECT 1 FROM fixed_product_details fp
                WHERE fp.product_id = NEW.product_id
                  AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION')
            )
        )
        AND EXISTS (
            SELECT 1 FROM order_items oi
            JOIN fixed_product_details fp2 ON fp2.product_id = oi.product_id
            WHERE oi.order_id = NEW.order_id
              AND oi.id != OLD.id
              AND oi.product_type_snapshot = 'FIXED_PRODUCT'
              AND fp2.delivery_method = 'MANUAL_REVIEW'
        )
    )
    OR (
        NEW.product_type_snapshot = 'FIXED_PRODUCT'
        AND EXISTS (
            SELECT 1 FROM fixed_product_details fp
            WHERE fp.product_id = NEW.product_id
              AND fp.delivery_method = 'MANUAL_REVIEW'
        )
        AND EXISTS (
            SELECT 1 FROM order_items oi
            LEFT JOIN fixed_product_details fp2 ON fp2.product_id = oi.product_id
            WHERE oi.order_id = NEW.order_id
              AND oi.id != OLD.id
              AND (
                  oi.product_type_snapshot = 'VOUCHER'
                  OR fp2.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION')
              )
        )
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_freeze_terminal_insert
BEFORE INSERT ON order_items
WHEN EXISTS (SELECT 1 FROM orders o WHERE o.id = NEW.order_id AND o.status IN ('COMPLETED', 'CANCELLED'))
BEGIN
    SELECT RAISE(ABORT, 'Cannot insert order_items for COMPLETED or CANCELLED order');
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_freeze_terminal_update
BEFORE UPDATE ON order_items
WHEN EXISTS (SELECT 1 FROM orders o WHERE o.id = OLD.order_id AND o.status IN ('COMPLETED', 'CANCELLED'))
BEGIN
    SELECT RAISE(ABORT, 'Cannot modify order_items for COMPLETED or CANCELLED order');
END;

CREATE TRIGGER IF NOT EXISTS trg_order_items_freeze_terminal_delete
BEFORE DELETE ON order_items
WHEN EXISTS (SELECT 1 FROM orders o WHERE o.id = OLD.order_id AND o.status IN ('COMPLETED', 'CANCELLED'))
BEGIN
    SELECT RAISE(ABORT, 'Cannot delete order_items for COMPLETED or CANCELLED order');
END;

-- 4. Inventory transition, allocation cap, quarantine/disabled, and SOLD terminal guards
--    (SG-P2-006, OD-06, AR-P3-007)
CREATE TRIGGER IF NOT EXISTS trg_inventory_items_prevent_direct_sold_insert
BEFORE INSERT ON inventory_items
WHEN NEW.status = 'SOLD'
BEGIN
    SELECT RAISE(ABORT, 'Cannot insert inventory item directly in SOLD status');
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_items_prevent_invalid_quarantine_insert
BEFORE INSERT ON inventory_items
WHEN NEW.status IN ('QUARANTINED', 'DISABLED') AND NEW.reserved_order_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Cannot insert QUARANTINED or DISABLED inventory with non-NULL reserved_order_id');
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_items_validate_status_transition
BEFORE UPDATE OF status, reserved_order_id, product_id, denomination_id, inventory_type, encrypted_payload ON inventory_items
BEGIN
    SELECT RAISE(ABORT, 'SOLD inventory status and reserved_order_id are terminal and immutable (OD-06)')
    WHERE OLD.status = 'SOLD'
      AND (
          NEW.status != 'SOLD'
          OR NEW.reserved_order_id IS NOT OLD.reserved_order_id
          OR NEW.product_id != OLD.product_id
          OR NEW.denomination_id IS NOT OLD.denomination_id
          OR NEW.inventory_type != OLD.inventory_type
          OR NEW.encrypted_payload != OLD.encrypted_payload
      );

    SELECT RAISE(ABORT, 'Cannot transition inventory directly from AVAILABLE to SOLD without RESERVED')
    WHERE OLD.status = 'AVAILABLE' AND NEW.status = 'SOLD';

    SELECT RAISE(ABORT, 'Cannot reassign RESERVED inventory item directly to another order')
    WHERE OLD.status = 'RESERVED'
      AND NEW.status = 'RESERVED'
      AND NEW.reserved_order_id IS NOT OLD.reserved_order_id;

    SELECT RAISE(ABORT, 'Cannot set reserved_order_id when quarantining or disabling AVAILABLE inventory')
    WHERE OLD.status = 'AVAILABLE'
      AND NEW.status IN ('QUARANTINED', 'DISABLED')
      AND NEW.reserved_order_id IS NOT NULL;

    SELECT RAISE(ABORT, 'Quarantining/disabling RESERVED inventory must release reserved_order_id unless preserving forensic link on CANCELLED/REFUND_REVIEW order')
    WHERE OLD.status = 'RESERVED'
      AND NEW.status IN ('QUARANTINED', 'DISABLED')
      AND NEW.reserved_order_id IS NOT NULL
      AND (
          NEW.reserved_order_id != OLD.reserved_order_id
          OR NOT EXISTS (
              SELECT 1 FROM orders o
              WHERE o.id = NEW.reserved_order_id
                AND o.status IN ('CANCELLED', 'REFUND_REVIEW')
          )
      );

    SELECT RAISE(ABORT, 'Cannot transition QUARANTINED or DISABLED inventory directly to RESERVED or SOLD')
    WHERE OLD.status IN ('QUARANTINED', 'DISABLED')
      AND NEW.status IN ('RESERVED', 'SOLD');

    SELECT RAISE(ABORT, 'Cannot restore QUARANTINED or DISABLED inventory to AVAILABLE while forensic reserved_order_id is set')
    WHERE OLD.status IN ('QUARANTINED', 'DISABLED')
      AND NEW.status = 'AVAILABLE'
      AND OLD.reserved_order_id IS NOT NULL;

    SELECT RAISE(ABORT, 'Cannot attach or reassign reserved_order_id on QUARANTINED or DISABLED inventory')
    WHERE OLD.status IN ('QUARANTINED', 'DISABLED')
      AND NEW.status IN ('QUARANTINED', 'DISABLED')
      AND NEW.reserved_order_id IS NOT NULL
      AND NEW.reserved_order_id IS NOT OLD.reserved_order_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_items_enforce_allocation_cap_insert
BEFORE INSERT ON inventory_items
WHEN NEW.status IN ('RESERVED', 'SOLD')
BEGIN
    SELECT RAISE(ABORT, 'MANUAL_REVIEW products cannot hold allocated inventory')
    WHERE EXISTS (
        SELECT 1 FROM fixed_product_details fp
        WHERE fp.product_id = NEW.product_id AND fp.delivery_method = 'MANUAL_REVIEW'
    );

    SELECT RAISE(ABORT, 'Order status does not permit inventory reservation')
    WHERE NEW.status = 'RESERVED'
      AND NOT EXISTS (
          SELECT 1 FROM orders o
          WHERE o.id = NEW.reserved_order_id
            AND o.status IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW', 'PAID', 'FULFILLING')
      );

    SELECT RAISE(ABORT, 'Allocated inventory count exceeds order_items quantity')
    WHERE (
        SELECT COUNT(*)
        FROM inventory_items i
        WHERE i.reserved_order_id = NEW.reserved_order_id
          AND i.product_id = NEW.product_id
          AND ((i.denomination_id IS NULL AND NEW.denomination_id IS NULL) OR i.denomination_id = NEW.denomination_id)
          AND i.status IN ('RESERVED', 'SOLD')
    ) + 1 > COALESCE((
        SELECT SUM(oi.quantity)
        FROM order_items oi
        WHERE oi.order_id = NEW.reserved_order_id
          AND oi.product_id = NEW.product_id
          AND ((oi.denomination_id IS NULL AND NEW.denomination_id IS NULL) OR oi.denomination_id = NEW.denomination_id)
    ), 0);
END;

CREATE TRIGGER IF NOT EXISTS trg_inventory_items_enforce_allocation_cap_update
BEFORE UPDATE OF product_id, denomination_id, status, reserved_order_id ON inventory_items
WHEN NEW.status IN ('RESERVED', 'SOLD')
BEGIN
    SELECT RAISE(ABORT, 'MANUAL_REVIEW products cannot hold allocated inventory')
    WHERE EXISTS (
        SELECT 1 FROM fixed_product_details fp
        WHERE fp.product_id = NEW.product_id AND fp.delivery_method = 'MANUAL_REVIEW'
    );

    SELECT RAISE(ABORT, 'Order status does not permit inventory reservation')
    WHERE NEW.status = 'RESERVED'
      AND NOT EXISTS (
          SELECT 1 FROM orders o
          WHERE o.id = NEW.reserved_order_id
            AND o.status IN ('PENDING_PAYMENT', 'PAYMENT_REVIEW', 'PAID', 'FULFILLING')
      );

    SELECT RAISE(ABORT, 'Order status does not permit finalizing inventory to SOLD')
    WHERE NEW.status = 'SOLD'
      AND NOT EXISTS (
          SELECT 1 FROM orders o
          WHERE o.id = NEW.reserved_order_id
            AND o.status IN ('PAID', 'FULFILLING')
      );

    SELECT RAISE(ABORT, 'Allocated inventory count exceeds order_items quantity')
    WHERE (
        SELECT COUNT(*)
        FROM inventory_items i
        WHERE i.reserved_order_id = NEW.reserved_order_id
          AND i.product_id = NEW.product_id
          AND ((i.denomination_id IS NULL AND NEW.denomination_id IS NULL) OR i.denomination_id = NEW.denomination_id)
          AND i.status IN ('RESERVED', 'SOLD')
          AND i.id != OLD.id
    ) + 1 > COALESCE((
        SELECT SUM(oi.quantity)
        FROM order_items oi
        WHERE oi.order_id = NEW.reserved_order_id
          AND oi.product_id = NEW.product_id
          AND ((oi.denomination_id IS NULL AND NEW.denomination_id IS NULL) OR oi.denomination_id = NEW.denomination_id)
    ), 0);
END;

-- 5. Order lifecycle, payment proof, paid_at, and completion/cancellation guards
--    (SG-P2-004, AR-P3-004, AR-P3-005)
-- Legacy compatibility backfill: older 001..006 schemas allowed NULL paid_at on PAID/FULFILLING/COMPLETED
UPDATE orders
SET paid_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
WHERE status IN ('PAID', 'FULFILLING', 'COMPLETED')
  AND paid_at IS NULL;

CREATE TRIGGER IF NOT EXISTS trg_orders_validate_insert_paid_at
BEFORE INSERT ON orders
WHEN NEW.status IN ('PAID', 'FULFILLING', 'COMPLETED') AND NEW.paid_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'paid_at cannot be NULL for PAID, FULFILLING, or COMPLETED orders');
END;

CREATE TRIGGER IF NOT EXISTS trg_orders_validate_status_and_payment_update
BEFORE UPDATE OF status, paid_at ON orders
BEGIN
    SELECT RAISE(ABORT, 'Illegal order status transition')
    WHERE OLD.status != NEW.status
      AND NOT (
          (OLD.status = 'PENDING_PAYMENT' AND NEW.status IN ('PAYMENT_REVIEW', 'CANCELLED'))
          OR (OLD.status = 'PAYMENT_REVIEW' AND NEW.status IN ('PAID', 'PENDING_PAYMENT', 'CANCELLED'))
          OR (OLD.status = 'PAID' AND NEW.status IN ('FULFILLING', 'REFUND_REVIEW'))
          OR (OLD.status = 'FULFILLING' AND NEW.status IN ('COMPLETED', 'REFUND_REVIEW'))
          OR (OLD.status = 'COMPLETED' AND NEW.status = 'REFUND_REVIEW')
          OR (OLD.status = 'REFUND_REVIEW' AND NEW.status = 'CANCELLED')
      );

    SELECT RAISE(ABORT, 'paid_at cannot be NULL when order is PAID, FULFILLING, or COMPLETED')
    WHERE NEW.status IN ('PAID', 'FULFILLING', 'COMPLETED')
      AND NEW.paid_at IS NULL;

    SELECT RAISE(ABORT, 'PAYMENT_REVIEW -> PAID requires at least one ACCEPTED receipt when receipts exist')
    WHERE OLD.status = 'PAYMENT_REVIEW'
      AND NEW.status = 'PAID'
      AND EXISTS (SELECT 1 FROM payment_receipts pr WHERE pr.order_id = NEW.id)
      AND NOT EXISTS (
          SELECT 1 FROM payment_receipts pr
          WHERE pr.order_id = NEW.id AND pr.review_status = 'ACCEPTED'
      );

    SELECT RAISE(ABORT, 'Cannot complete order while RESERVED inventory items remain')
    WHERE NEW.status = 'COMPLETED'
      AND EXISTS (
          SELECT 1 FROM inventory_items i
          WHERE i.reserved_order_id = NEW.id AND i.status = 'RESERVED'
      );

    SELECT RAISE(ABORT, 'Cannot complete order without order_items')
    WHERE NEW.status = 'COMPLETED'
      AND NOT EXISTS (
          SELECT 1 FROM order_items oi WHERE oi.order_id = NEW.id
      );

    SELECT RAISE(ABORT, 'Cannot complete order with missing product details for order_items')
    WHERE NEW.status = 'COMPLETED'
      AND EXISTS (
          SELECT 1 FROM order_items oi
          LEFT JOIN voucher_details vd ON vd.product_id = oi.product_id
          LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
          WHERE oi.order_id = NEW.id
            AND (
                (oi.product_type_snapshot = 'VOUCHER' AND vd.product_id IS NULL)
                OR (oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.product_id IS NULL)
            )
      );

    SELECT RAISE(ABORT, 'Cannot complete order: SOLD inventory quantity does not match ordered quantity')
    WHERE NEW.status = 'COMPLETED'
      AND EXISTS (
          SELECT 1
          FROM order_items oi
          LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
          WHERE oi.order_id = NEW.id
            AND (
                oi.product_type_snapshot = 'VOUCHER'
                OR (oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION'))
            )
          GROUP BY oi.product_id, oi.denomination_id
          HAVING SUM(oi.quantity) != (
              SELECT COUNT(*)
              FROM inventory_items i
              WHERE i.reserved_order_id = NEW.id
                AND i.status = 'SOLD'
                AND i.product_id = oi.product_id
                AND ((i.denomination_id IS NULL AND oi.denomination_id IS NULL) OR i.denomination_id = oi.denomination_id)
          )
      );

    SELECT RAISE(ABORT, 'Cannot complete order: stray or non-inventory SOLD inventory present')
    WHERE NEW.status = 'COMPLETED'
      AND EXISTS (
          SELECT 1
          FROM inventory_items i
          WHERE i.reserved_order_id = NEW.id
            AND i.status = 'SOLD'
            AND NOT EXISTS (
                SELECT 1
                FROM order_items oi
                LEFT JOIN fixed_product_details fp ON fp.product_id = oi.product_id
                WHERE oi.order_id = NEW.id
                  AND oi.product_id = i.product_id
                  AND ((oi.denomination_id IS NULL AND i.denomination_id IS NULL) OR oi.denomination_id = i.denomination_id)
                  AND (
                      oi.product_type_snapshot = 'VOUCHER'
                      OR (oi.product_type_snapshot = 'FIXED_PRODUCT' AND fp.delivery_method IN ('CODE', 'OFFICIAL_ACTIVATION'))
                  )
            )
      );

    SELECT RAISE(ABORT, 'Cannot cancel order while RESERVED inventory items remain')
    WHERE NEW.status = 'CANCELLED'
      AND EXISTS (
          SELECT 1 FROM inventory_items i
          WHERE i.reserved_order_id = NEW.id AND i.status = 'RESERVED'
      );
END;

-- 6. Receipt terminal review guards (AR-P3-005)
CREATE TRIGGER IF NOT EXISTS trg_payment_receipts_validate_insert_status
BEFORE INSERT ON payment_receipts
WHEN NEW.review_status != 'PENDING_REVIEW'
BEGIN
    SELECT RAISE(ABORT, 'New payment_receipts must start in PENDING_REVIEW status');
END;

CREATE TRIGGER IF NOT EXISTS trg_payment_receipts_validate_review_transition
BEFORE UPDATE OF review_status ON payment_receipts
WHEN OLD.review_status IN ('ACCEPTED', 'REJECTED', 'DUPLICATE')
  AND NEW.review_status != OLD.review_status
BEGIN
    SELECT RAISE(ABORT, 'Cannot change terminal payment_receipts review_status');
END;

-- 7. Append-only audit_events protection
CREATE TRIGGER IF NOT EXISTS trg_audit_events_prevent_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only and cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_audit_events_prevent_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only and cannot be deleted');
END;

-- 8. Delivery attempts persistence table & append-only lease protection (AR-P3-001)
CREATE TABLE IF NOT EXISTS order_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    channel TEXT NOT NULL DEFAULT 'TELEGRAM' CHECK (TRIM(channel) != ''),
    delivery_kind TEXT NOT NULL CHECK (delivery_kind IN ('INITIAL', 'REDELIVERY', 'OPERATOR_RESEND')),
    status TEXT NOT NULL CHECK (status IN ('DISPATCHING', 'SENT', 'FAILED', 'UNCERTAIN')),
    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
    claim_token TEXT UNIQUE NOT NULL CHECK (TRIM(claim_token) != ''),
    claimed_by TEXT NOT NULL CHECK (TRIM(claimed_by) != ''),
    lease_expires_at TIMESTAMP NOT NULL,
    external_message_id TEXT,
    error_code TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_order_deliveries_single_active_dispatch
ON order_deliveries(order_id)
WHERE status = 'DISPATCHING';

CREATE UNIQUE INDEX IF NOT EXISTS idx_order_deliveries_order_attempt
ON order_deliveries(order_id, attempt_no);

CREATE INDEX IF NOT EXISTS idx_order_deliveries_status_lease
ON order_deliveries(status, lease_expires_at);

CREATE TRIGGER IF NOT EXISTS trg_order_deliveries_prevent_delete
BEFORE DELETE ON order_deliveries
BEGIN
    SELECT RAISE(ABORT, 'order_deliveries history is append-only and cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS trg_order_deliveries_protect_immutable_fields
BEFORE UPDATE ON order_deliveries
BEGIN
    SELECT RAISE(ABORT, 'Immutable order_deliveries identity/lease fields cannot be modified')
    WHERE NEW.id != OLD.id
       OR NEW.order_id != OLD.order_id
       OR NEW.channel != OLD.channel
       OR NEW.delivery_kind != OLD.delivery_kind
       OR NEW.attempt_no != OLD.attempt_no
       OR NEW.claim_token != OLD.claim_token
       OR NEW.claimed_by != OLD.claimed_by
       OR NEW.lease_expires_at IS NOT OLD.lease_expires_at
       OR NEW.created_at IS NOT OLD.created_at;

    SELECT RAISE(ABORT, 'Illegal order_deliveries status transition')
    WHERE OLD.status != NEW.status
      AND NOT (
          (OLD.status = 'DISPATCHING' AND NEW.status IN ('SENT', 'FAILED', 'UNCERTAIN'))
          OR (OLD.status = 'UNCERTAIN' AND NEW.status = 'SENT')
      );

    SELECT RAISE(ABORT, 'SENT delivery record requires non-NULL delivered_at')
    WHERE NEW.status = 'SENT' AND NEW.delivered_at IS NULL;

    SELECT RAISE(ABORT, 'SENT delivery record metadata cannot be altered')
    WHERE OLD.status = 'SENT'
      AND (
          NEW.status != 'SENT'
          OR NEW.external_message_id IS NOT OLD.external_message_id
          OR NEW.delivered_at IS NOT OLD.delivered_at
          OR NEW.error_code IS NOT OLD.error_code
      );

    SELECT RAISE(ABORT, 'FAILED delivery record metadata cannot be altered')
    WHERE OLD.status = 'FAILED'
      AND (
          NEW.status != 'FAILED'
          OR NEW.external_message_id IS NOT OLD.external_message_id
          OR NEW.delivered_at IS NOT OLD.delivered_at
          OR NEW.error_code IS NOT OLD.error_code
      );
END;

-- 9. Idempotency persistence table (AR-P3-006)
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL CHECK (TRIM(scope) != ''),
    actor_key TEXT NOT NULL CHECK (TRIM(actor_key) != ''),
    idempotency_key TEXT NOT NULL CHECK (TRIM(idempotency_key) != ''),
    request_hash TEXT NOT NULL CHECK (LENGTH(request_hash) = 64),
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED')),
    owner_token TEXT NOT NULL CHECK (TRIM(owner_token) != ''),
    response_entity_type TEXT,
    response_entity_id INTEGER,
    stale_after TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (scope, actor_key, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_expires_at
ON idempotency_keys(expires_at);
