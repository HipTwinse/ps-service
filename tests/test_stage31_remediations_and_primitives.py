"""Tests for Stage 3.1 SG-P2-005..016 remediations, OD-01..09 policies, AR-P3-001 delivery, AR-P3-006 idempotency, and AR-P3-008."""

import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.db.config import DEFAULT_OPERATIONAL_CONFIG, OperationalDefaults
from src.db.connection import backup_database, create_connection, get_connection, restore_database
from src.db.crypto import (
    CryptoConfigurationError,
    compute_fingerprint,
    decrypt_payload,
    encrypt_payload,
    get_fingerprint_pepper,
)
from src.db.migrations import _split_sql_statements, run_migrations
from src.db.models import (
    ApprovalStatus,
    ConflictError,
    DeliveryCooldownError,
    DeliveryInProgressError,
    DeliveryKind,
    DeliveryLeaseExpiredError,
    DeliveryMethod,
    DeliveryStatus,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyStatus,
    InvalidStateTransitionError,
    InventoryStatus,
    InventoryType,
    NotFoundError,
    OrderStatus,
    ProductStatus,
    ProductType,
    ReceiptReviewStatus,
    UserBlockedError,
    UserStatus,
    ValidationError,
)
from src.db.repositories import (
    AuditRepository,
    CatalogRepository,
    DeliveryRepository,
    IdempotencyRepository,
    InventoryRepository,
    OrderRepository,
    ReceiptRepository,
    UserRepository,
)
from src.db.uow import UnitOfWork


class _CustomRestoreAbort(BaseException):
    """Non-Exception BaseException used for restore_database SG-P2-014 test."""


class TestStage31RemediationsAndPrimitives(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {k: os.environ.get(k) for k in ("ENV", "APP_ENV", "INVENTORY_ENCRYPTION_KEY", "FINGERPRINT_PEPPER", "INVENTORY_FINGERPRINT_PEPPER")}
        os.environ["ENV"] = "test"
        os.environ.pop("APP_ENV", None)
        os.environ.pop("INVENTORY_ENCRYPTION_KEY", None)
        os.environ.pop("FINGERPRINT_PEPPER", None)
        os.environ.pop("INVENTORY_FINGERPRINT_PEPPER", None)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stage31_remed.db")
        self.conn = create_connection(self.db_path)
        run_migrations(self.conn)

        self.catalog = CatalogRepository(self.conn)
        self.users = UserRepository(self.conn)
        self.inv = InventoryRepository(self.conn)
        self.orders = OrderRepository(self.conn)
        self.receipts = ReceiptRepository(self.conn)
        self.audit = AuditRepository(self.conn)
        self.deliveries = DeliveryRepository(self.conn)
        self.idempotency = IdempotencyRepository(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _seed_voucher_and_order(self, *, ext_id: str = "buyer_1", raw_code: str = "SECRET-PSN-VOUCHER-9911"):
        user, _ = self.users.get_or_create_user(ext_id, username=ext_id)
        prod = self.catalog.create_voucher_product("PSN 1000 TRY", "TR", "TRY", status=ProductStatus.APPROVED)
        denom = self.catalog.add_voucher_denomination(prod.id, "TR", "TRY", 100000, 350000)
        self.inv.import_items(prod.id, [raw_code], InventoryType.VOUCHER_CODE, denom.id)
        order = self.orders.create_order(
            user.id,
            350000,
            "RUB",
            [
                {
                    "product_id": prod.id,
                    "denomination_id": denom.id,
                    "product_type_snapshot": ProductType.VOUCHER.value,
                    "quantity": 1,
                    "unit_price_minor": 350000,
                }
            ],
        )
        inv_ids = self.inv.reserve_items_for_order(order.id, prod.id, 1, denom.id)
        return user, prod, denom, order, inv_ids[0]

    # ------------------------------------------------------------------
    # SG-P2-005..016 & AR-P3-008
    # ------------------------------------------------------------------

    def test_sg_p2_005_split_sql_statements_edge_cases(self) -> None:
        script = (
            "-- leading comment with ; semicolon\n"
            "CREATE TABLE a (x INT); CREATE TABLE [b;tbl] (\"c;col\" TEXT DEFAULT 'v;1''2');\n"
            "CREATE TRIGGER `t;1` AFTER INSERT ON a BEGIN INSERT INTO [b;tbl] VALUES ('inside;trigger'); END; /* trailing comment */\n"
            "-- eof comment"
        )
        stmts = _split_sql_statements(script)
        self.assertEqual(len(stmts), 3)
        self.assertIn("CREATE TABLE a (x INT);", stmts[0])
        self.assertTrue(stmts[1].startswith("CREATE TABLE [b;tbl]"))
        self.assertTrue(stmts[2].startswith("CREATE TRIGGER `t;1`"))

        mem = create_connection(":memory:")
        try:
            for stmt in stmts:
                mem.execute(stmt)
        finally:
            mem.close()

        with self.assertRaisesRegex(ValueError, "Unclosed quoted string"):
            _split_sql_statements("SELECT 'unterminated;")
        with self.assertRaisesRegex(ValueError, "Unclosed block comment"):
            _split_sql_statements("SELECT 1; /* unterminated")
        with self.assertRaisesRegex(ValueError, "Incomplete SQL statement"):
            _split_sql_statements("CREATE TABLE incomplete (x INT)")

    def test_sg_p2_008_009_010_015_016_catalog_user_order_receipt_audit_validations(self) -> None:
        # SG-P2-008: add_voucher_denomination validation & ConflictError vs ValidationError
        v = self.catalog.create_voucher_product("V TR", "TR", "TRY", status=ProductStatus.APPROVED)
        with self.assertRaises(ValidationError):
            self.catalog.add_voucher_denomination(v.id, "US", "TRY", 1000, 5000)
        with self.assertRaises(ValidationError):
            self.catalog.add_voucher_denomination(v.id, "US", "USD", 1000, 5000)
        self.catalog.add_voucher_denomination(v.id, "tr", "try", 1000, 5000)
        with self.assertRaises(ConflictError):
            self.catalog.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)

        # SG-P2-010: NotFoundError instead of silent no-op
        with self.assertRaises(NotFoundError):
            self.catalog.update_fixed_product_approval(999999, ApprovalStatus.APPROVED)
        with self.assertRaises(NotFoundError):
            self.users.update_user_status(999999, UserStatus.BLOCKED)

        # SG-P2-016: Strict type/string validation across repositories
        with self.assertRaises(ValidationError):
            self.catalog.create_voucher_product("Bad", "TR", "TRY", status="APPROVED")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            self.catalog.update_product_status(v.id, "DISABLED")  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            self.catalog.update_fixed_product_supply_method(v.id, 123)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            self.users.get_or_create_user("valid_ext", username=12345)  # type: ignore[arg-type]

        # SG-P2-009: create_order strict currency validation
        u, _ = self.users.get_or_create_user("curr_user", username="cu")
        with self.assertRaises(ValidationError):
            self.orders.create_order(
                u.id, 5000, 643, [{"product_id": v.id, "denomination_id": 1, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}]  # type: ignore[arg-type]
            )
        with self.assertRaises(ValidationError):
            self.orders.create_order(
                u.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": 1, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000, "currency": 643}],
            )

        # SG-P2-016: ReceiptRepository string validation
        mr = self.catalog.create_fixed_product(
            "MR", 2000, DeliveryMethod.MANUAL_REVIEW, "MAN", approval_status=ApprovalStatus.APPROVED, status=ProductStatus.APPROVED
        )
        o_mr = self.orders.create_order(
            u.id, 2000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 2000}]
        )
        with self.assertRaises(ValidationError):
            self.receipts.submit_receipt(o_mr.id, u.id, b"bytes", provider_name=123)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            self.receipts.submit_receipt(o_mr.id, u.id, b"bytes", provider_tx_id=123)  # type: ignore[arg-type]

        # SG-P2-015: transition_order_status preserves metadata even when notes is None
        t_order = self.orders.transition_order_status(
            o_mr.id, OrderStatus.PAYMENT_REVIEW, notes=None, actor_type="USER", actor_id=u.id
        )
        self.assertEqual(t_order.status, OrderStatus.PAYMENT_REVIEW)
        events = self.audit.list_events(entity_type="ORDER", entity_id=o_mr.id, event_type="ORDER_STATUS_TRANSITION")
        self.assertEqual(len(events), 1)
        payload = json.loads(events[0].details_json or "{}")
        self.assertEqual(payload["from_status"], "PENDING_PAYMENT")
        self.assertEqual(payload["to_status"], "PAYMENT_REVIEW")
        self.assertIsNone(payload["notes"])
        self.assertEqual(payload["actor_type"], "USER")
        self.assertEqual(payload["actor_id"], u.id)

        # AuditRepository forbidden keys check (OD-07)
        for bad_key in ("plaintext", "code", "raw_code", "claim_token", "owner_token", "encryption_key", "pepper"):
            with self.assertRaises(ValidationError):
                self.audit.record_event("BAD", "ORDER", o_mr.id, details={"nested": [{bad_key: "redacted"}]})

    def test_sg_p2_011_and_012_crypto_pepper_rotation_and_empty_key_guards(self) -> None:
        # SG-P2-012: key=b"" and pepper=b"" raise CryptoConfigurationError (no fallback)
        with self.assertRaises(CryptoConfigurationError):
            encrypt_payload("ABC-123", key=b"")
        with self.assertRaises(CryptoConfigurationError):
            decrypt_payload(b"irrelevant", key=b"")
        with self.assertRaises(CryptoConfigurationError):
            compute_fingerprint("ABC-123", pepper=b"")

        # Short FINGERPRINT_PEPPER (<16 bytes) rejected
        os.environ["FINGERPRINT_PEPPER"] = "short_15_bytes!"
        with self.assertRaises(CryptoConfigurationError):
            get_fingerprint_pepper()

        # Whitespace-only FINGERPRINT_PEPPER falls through to valid INVENTORY_FINGERPRINT_PEPPER
        os.environ["FINGERPRINT_PEPPER"] = "   \t  "
        os.environ["INVENTORY_FINGERPRINT_PEPPER"] = "valid_secondary_pepper_16_plus"
        self.assertEqual(get_fingerprint_pepper(), b"valid_secondary_pepper_16_plus")

        # Reset to dev fallback for import/rotation
        os.environ.pop("FINGERPRINT_PEPPER", None)
        os.environ.pop("INVENTORY_FINGERPRINT_PEPPER", None)

        prod = self.catalog.create_voucher_product("V Rot", "TR", "TRY", status=ProductStatus.APPROVED)
        denom = self.catalog.add_voucher_denomination(prod.id, "TR", "TRY", 500, 2000)
        self.inv.import_items(prod.id, ["ROT-CODE-1", "ROT-CODE-2"], InventoryType.VOUCHER_CODE, denom.id)

        current_pepper = get_fingerprint_pepper()
        before_fps = [r["fingerprint"] for r in self.conn.execute("SELECT fingerprint FROM inventory_items ORDER BY id").fetchall()]

        # SG-P2-011: Wrong old_pepper aborts atomically before changing any rows
        with self.assertRaises(ValidationError):
            self.inv.rotate_fingerprint_pepper(b"new_pepper_secret_key_12345", old_pepper=b"wrong_old_pepper_secret")
        after_fail_fps = [r["fingerprint"] for r in self.conn.execute("SELECT fingerprint FROM inventory_items ORDER BY id").fetchall()]
        self.assertEqual(after_fail_fps, before_fps)

        # Matching old_pepper succeeds
        rotated = self.inv.rotate_fingerprint_pepper(b"new_pepper_secret_key_12345", old_pepper=current_pepper)
        self.assertEqual(rotated, 2)

    def test_sg_p2_013_and_od_01_reveal_requires_context_blocks_blocked_user_and_neutral_errors(self) -> None:
        user, _p, _d, order, item_id = self._seed_voucher_and_order(ext_id="reveal_u1", raw_code="REVEAL-TOP-SECRET")
        other_user, _ = self.users.get_or_create_user("other_u2")
        self.orders.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.orders.transition_order_status(order.id, OrderStatus.PAID)
        self.inv.finalize_order_sale(order.id)

        # Missing context rejected
        with self.assertRaises(ValidationError):
            self.inv.get_revealed_code(item_id, expected_order_id=order.id, expected_user_id=None)
        with self.assertRaises(ValidationError):
            self.inv.get_revealed_code(item_id, expected_order_id=None, expected_user_id=user.id)

        # Neutral anti-oracle message across wrong user vs wrong order
        with self.assertRaises(ValidationError) as ctx_wrong_user:
            self.inv.get_revealed_code(item_id, expected_order_id=order.id, expected_user_id=other_user.id)
        with self.assertRaises(ValidationError) as ctx_wrong_order:
            self.inv.get_revealed_code(item_id, expected_order_id=999999, expected_user_id=user.id)
        self.assertEqual(str(ctx_wrong_user.exception), str(ctx_wrong_order.exception))
        self.assertNotIn("REVEAL-TOP-SECRET", str(ctx_wrong_user.exception))

        # Active owner succeeds
        self.assertEqual(
            self.inv.get_revealed_code(item_id, expected_order_id=order.id, expected_user_id=user.id),
            "REVEAL-TOP-SECRET",
        )

        # OD-01: BLOCKED user blocked server-side, SOLD status untouched, unblocking restores reveal
        self.users.update_user_status(user.id, UserStatus.BLOCKED)
        with self.assertRaises(UserBlockedError):
            self.inv.get_revealed_code(item_id, expected_order_id=order.id, expected_user_id=user.id)
        self.assertEqual(self.inv.get_item(item_id).status, InventoryStatus.SOLD)

        self.users.update_user_status(user.id, UserStatus.ACTIVE)
        self.assertEqual(
            self.inv.get_revealed_code(item_id, expected_order_id=order.id, expected_user_id=user.id),
            "REVEAL-TOP-SECRET",
        )

    def test_sg_p2_014_restore_base_exception_and_ar_p3_008_user_updated_at(self) -> None:
        # AR-P3-008: get_or_create_user re-reads after UPDATE username to return refreshed updated_at
        user, created = self.users.get_or_create_user("ts_user_1", username="old_nick")
        self.assertTrue(created)
        self.conn.execute("UPDATE users SET updated_at = '2000-01-01 00:00:00' WHERE id = ?", (user.id,))
        self.conn.commit()
        stale_read = self.users.get_user(user.id)
        self.assertEqual(str(stale_read.updated_at), "2000-01-01 00:00:00")

        updated_user, created2 = self.users.get_or_create_user("ts_user_1", username="new_nick")
        self.assertFalse(created2)
        self.assertEqual(updated_user.username, "new_nick")
        self.assertNotEqual(str(updated_user.updated_at), "2000-01-01 00:00:00")
        self.assertEqual(updated_user.updated_at, self.users.get_user(user.id).updated_at)

        # SG-P2-014: restore_database BaseException rollback & temp cleanup
        src_db = os.path.join(self.temp_dir.name, "src_restore.db")
        bak_file = os.path.join(self.temp_dir.name, "bak_restore.db")
        dst_db = os.path.join(self.temp_dir.name, "dst_restore.db")

        c_src = create_connection(src_db)
        run_migrations(c_src)
        UserRepository(c_src).get_or_create_user("from_backup", username="bak")
        backup_database(c_src, bak_file)
        c_src.close()

        c_dst = create_connection(dst_db)
        run_migrations(c_dst)
        UserRepository(c_dst).get_or_create_user("orig_target", username="orig")
        c_dst.close()

        with patch("src.db.connection.os.replace", side_effect=_CustomRestoreAbort("abort replace")):
            with self.assertRaises(_CustomRestoreAbort):
                restore_database(bak_file, dst_db)

        c_verify = get_connection(dst_db)
        try:
            self.assertIsNotNone(c_verify.execute("SELECT id FROM users WHERE external_id = 'orig_target'").fetchone())
            self.assertIsNone(c_verify.execute("SELECT id FROM users WHERE external_id = 'from_backup'").fetchone())
        finally:
            c_verify.close()
        leftovers = [f for f in os.listdir(self.temp_dir.name) if "restore_tmp" in f or "orig_bak" in f]
        self.assertEqual(leftovers, [])

    # ------------------------------------------------------------------
    # OD-01..08 Product Decisions & Operational Defaults
    # ------------------------------------------------------------------

    def test_od_02_03_04_05_08_policies_and_defaults(self) -> None:
        # OD-08 defaults exact checks and validation
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.max_open_pending_payment_orders_per_user, 3)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.pending_payment_ttl_seconds, 900)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.delivery_lease_seconds, 60)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.delivery_min_send_budget_seconds, 20.0)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.delivery_http_timeout_seconds, 15.0)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.delivery_safety_margin_seconds, 5.0)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.redelivery_cooldown_seconds, 60)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.idempotency_ttl_seconds, 86_400)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.idempotency_in_progress_stale_seconds, 120)
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.min_fingerprint_pepper_bytes, 16)
        with self.assertRaises(ValueError):
            OperationalDefaults(delivery_min_send_budget_seconds=60.0, delivery_lease_seconds=60)
        with self.assertRaises(ValueError):
            OperationalDefaults(delivery_min_send_budget_seconds=18.0, delivery_http_timeout_seconds=15.0, delivery_safety_margin_seconds=5.0)
        with self.assertRaises(ValueError):
            OperationalDefaults(delivery_safety_margin_seconds=0.0)
        with self.assertRaises(ValueError):
            OperationalDefaults(delivery_safety_margin_seconds=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            OperationalDefaults(idempotency_in_progress_stale_seconds=200, idempotency_ttl_seconds=100)

        # OD-03 & OD-04: OFFICIAL_ACTIVATION uses ACTIVATION_DATA and can share an order with VOUCHER,
        # while mixing with MANUAL_REVIEW is forbidden and MANUAL_REVIEW cannot reserve inventory.
        user, _ = self.users.get_or_create_user("od_buyer")
        v = self.catalog.create_voucher_product("V TR", "TR", "TRY", status=ProductStatus.APPROVED)
        d = self.catalog.add_voucher_denomination(v.id, "TR", "TRY", 1000, 5000)
        oa = self.catalog.create_fixed_product(
            "PS Plus Official",
            120000,
            DeliveryMethod.OFFICIAL_ACTIVATION,
            "SUP_OA",
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.APPROVED,
        )
        mr = self.catalog.create_fixed_product(
            "Custom Manual",
            25000,
            DeliveryMethod.MANUAL_REVIEW,
            "SUP_MR",
            approval_status=ApprovalStatus.APPROVED,
            status=ProductStatus.APPROVED,
        )
        self.inv.import_items(v.id, ["V-OA-1"], InventoryType.VOUCHER_CODE, d.id)
        self.inv.import_items(oa.id, ["ACC-EMAIL:PASS-1"], InventoryType.ACTIVATION_DATA)

        with self.assertRaisesRegex(ValidationError, "Cannot mix inventory-backed and MANUAL_REVIEW"):
            self.orders.create_order(
                user.id,
                30000,
                "RUB",
                [
                    {"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000},
                    {"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 25000},
                ],
            )

        joint_order = self.orders.create_order(
            user.id,
            125000,
            "RUB",
            [
                {"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000},
                {"product_id": oa.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 120000},
            ],
        )
        self.inv.reserve_items_for_order(joint_order.id, v.id, 1, d.id)
        self.inv.reserve_items_for_order(joint_order.id, oa.id, 1)
        rcpt_j = self.receipts.submit_receipt(joint_order.id, user.id, b"rcpt_joint", provider_tx_id="tx_j1")
        self.receipts.review_receipt(rcpt_j.id, ReceiptReviewStatus.ACCEPTED, "ok")
        self.orders.transition_order_status(joint_order.id, OrderStatus.PAYMENT_REVIEW)
        self.orders.transition_order_status(joint_order.id, OrderStatus.PAID)
        self.assertEqual(self.inv.finalize_order_sale(joint_order.id), 2)
        self.orders.transition_order_status(joint_order.id, OrderStatus.FULFILLING)
        completed = self.orders.transition_order_status(joint_order.id, OrderStatus.COMPLETED)
        self.assertEqual(completed.status, OrderStatus.COMPLETED)

        # MANUAL_REVIEW order cannot reserve inventory
        mr_order = self.orders.create_order(
            user.id, 25000, "RUB", [{"product_id": mr.id, "product_type_snapshot": "FIXED_PRODUCT", "quantity": 1, "unit_price_minor": 25000}]
        )
        with self.assertRaises(ValidationError):
            self.inv.reserve_items_for_order(mr_order.id, mr.id, 1)
        self.orders.cancel_order_atomically(mr_order.id, actor_type="USER", notes="cleanup")

        # OD-02 & OD-05 & OD-08: open order cap=3, TTL=900s, resubmit after REJECTED, and PAYMENT_REVIEW cancellation rules
        self.inv.import_items(v.id, ["V-CAP-1", "V-CAP-2", "V-CAP-3", "V-CAP-4"], InventoryType.VOUCHER_CODE, d.id)
        open_orders = []
        for idx in range(3):
            o = self.orders.create_order(
                user.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
                enforce_open_order_limit=True,
            )
            self.inv.reserve_items_for_order(o.id, v.id, 1, d.id)
            open_orders.append(o)

        with self.assertRaisesRegex(ValidationError, "limit reached"):
            self.orders.create_order(
                user.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
                enforce_open_order_limit=True,
            )

        # Backdate open_orders[0] by 901 seconds and test ReadSession.list_expired_pending_orders
        ref_now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.conn.execute(
            "UPDATE orders SET created_at = '2026-06-01 11:44:59' WHERE id = ?",
            (open_orders[0].id,),
        )
        self.conn.execute(
            "UPDATE orders SET created_at = '2026-06-01 11:55:00' WHERE id IN (?, ?)",
            (open_orders[1].id, open_orders[2].id),
        )
        self.conn.commit()
        with UnitOfWork.from_connection(self.conn).read() as rs:
            expired = rs.list_expired_pending_orders(now_utc=ref_now)
            self.assertEqual([o.id for o in expired], [open_orders[0].id])

        # OD-05: Receipt REJECTED -> order returns PAYMENT_REVIEW -> PENDING_PAYMENT -> new receipt allowed
        r_rej = self.receipts.submit_receipt(open_orders[0].id, user.id, b"bad_rcpt", provider_tx_id="tx_rej1")
        self.orders.transition_order_status(open_orders[0].id, OrderStatus.PAYMENT_REVIEW)
        self.receipts.review_receipt(r_rej.id, ReceiptReviewStatus.REJECTED, "blurry")
        self.orders.transition_order_status(open_orders[0].id, OrderStatus.PENDING_PAYMENT)
        r_retry = self.receipts.submit_receipt(open_orders[0].id, user.id, b"good_rcpt_retry", provider_tx_id="tx_rej2")
        self.orders.transition_order_status(open_orders[0].id, OrderStatus.PAYMENT_REVIEW)

        # USER cannot self-cancel order in PAYMENT_REVIEW (OD-05)
        with self.assertRaises(InvalidStateTransitionError):
            self.orders.cancel_order_atomically(open_orders[0].id, actor_type="USER", notes="user wants cancel")

        # OPERATOR cancels PAYMENT_REVIEW atomically -> releases reserves + rejects pending receipts
        cancelled = self.orders.cancel_order_atomically(open_orders[0].id, actor_type="OPERATOR", notes="operator cancel")
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
        self.assertEqual(self.receipts.get_receipt(r_retry.id).review_status, ReceiptReviewStatus.REJECTED)
        avail_after = self.conn.execute(
            "SELECT COUNT(*) FROM inventory_items WHERE product_id = ? AND status = 'AVAILABLE'",
            (v.id,),
        ).fetchone()[0]
        self.assertEqual(avail_after, 2)  # V-CAP-4 + released V-CAP-1

        # OD-01 & OD-02: Blocked user cannot create order or submit receipt, while unpaid order still cancels atomically
        self.users.update_user_status(user.id, UserStatus.BLOCKED)
        with self.assertRaises(UserBlockedError):
            self.orders.create_order(
                user.id,
                5000,
                "RUB",
                [{"product_id": v.id, "denomination_id": d.id, "product_type_snapshot": "VOUCHER", "quantity": 1, "unit_price_minor": 5000}],
            )
        with self.assertRaises(UserBlockedError):
            self.receipts.submit_receipt(open_orders[1].id, user.id, b"blocked_submit")
        cancelled_blocked = self.orders.cancel_order_atomically(open_orders[1].id, actor_type="SYSTEM", notes="blocked cleanup")
        self.assertEqual(cancelled_blocked.status, OrderStatus.CANCELLED)

    # ------------------------------------------------------------------
    # AR-P3-001: DeliveryRepository Append-Only Attempts, Leases & Late Outcomes
    # ------------------------------------------------------------------

    def test_ar_p3_001_delivery_lease_budget_reclaim_late_sent_stale_failed_and_cooldown(self) -> None:
        user, _p, _d, order, item_id = self._seed_voucher_and_order(ext_id="deliv_u1", raw_code="DELIV-SECRET-9876")

        # Cannot claim while PENDING_PAYMENT
        with self.assertRaises(InvalidStateTransitionError):
            self.deliveries.claim_delivery(order.id, claimed_by="worker-1")

        self.orders.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.orders.transition_order_status(order.id, OrderStatus.PAID)
        self.inv.finalize_order_sale(order.id)

        # Blocked user cannot claim delivery
        self.users.update_user_status(user.id, UserStatus.BLOCKED)
        with self.assertRaises(UserBlockedError):
            self.deliveries.claim_delivery(order.id, claimed_by="worker-1")
        self.users.update_user_status(user.id, UserStatus.ACTIVE)

        t0 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
        claim1 = self.deliveries.claim_delivery(
            order.id,
            claimed_by="worker-1",
            delivery_kind=DeliveryKind.INITIAL,
            lease_seconds=60,
            cooldown_seconds=60,
            min_send_budget_seconds=20.0,
            http_timeout_seconds=15.0,
            safety_margin_seconds=5.0,
            now_utc=t0,
            now_monotonic=1000.0,
        )
        self.assertEqual(claim1.attempt_no, 1)

        # Sufficient monotonic & UTC budget returns bounded HTTP timeout
        timeout_val = self.deliveries.check_pre_send_lease_budget(claim1, now_monotonic=1020.0, now_utc=t0 + timedelta(seconds=20))
        self.assertEqual(timeout_val, 15.0)

        # Active lease blocks second claim
        with self.assertRaises(DeliveryInProgressError):
            self.deliveries.claim_delivery(order.id, claimed_by="worker-2", now_utc=t0 + timedelta(seconds=30))

        # Insufficient monotonic budget (< 20.0s required for 15.0s HTTP + 5.0s margin) triggers LEASE_EXPIRED_BEFORE_SEND -> UNCERTAIN
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(
                claim1,
                now_monotonic=1042.0,  # remaining = 18.0s < 20.0s
                now_utc=t0 + timedelta(seconds=42),
            )
        attempt1 = self.deliveries.get_delivery(claim1.delivery_id)
        self.assertEqual(attempt1.status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(attempt1.error_code, "LEASE_EXPIRED_BEFORE_SEND")

        # Reclaim after lease expiry creates attempt 2 while attempt 1 stays UNCERTAIN
        claim2 = self.deliveries.claim_delivery(
            order.id,
            claimed_by="worker-2",
            delivery_kind=DeliveryKind.INITIAL,
            lease_seconds=60,
            cooldown_seconds=60,
            now_utc=t0 + timedelta(seconds=65),
            now_monotonic=2000.0,
        )
        self.assertEqual(claim2.attempt_no, 2)
        self.assertNotEqual(claim2.claim_token, claim1.claim_token)

        # Stale FAILED on UNCERTAIN attempt1 is ignored without clobbering attempt2
        res_stale = self.deliveries.record_outcome(
            claim1.delivery_id,
            claim1.claim_token,
            DeliveryStatus.FAILED,
            error_code="STALE_TIMEOUT",
            now_utc=t0 + timedelta(seconds=66),
        )
        self.assertEqual(res_stale.status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(self.deliveries.get_delivery(claim2.delivery_id).status, DeliveryStatus.DISPATCHING)
        stale_ev = self.audit.list_events(entity_type="DELIVERY", entity_id=claim1.delivery_id, event_type="DELIVERY_STALE_OUTCOME_IGNORED")
        self.assertEqual(len(stale_ev), 1)

        # Late SENT on UNCERTAIN attempt1 clears stale error_code=NULL, audits previous_error_code, leaves attempt2 untouched
        res_late = self.deliveries.record_outcome(
            claim1.delivery_id,
            claim1.claim_token,
            DeliveryStatus.SENT,
            external_message_id="tg-msg-late-1",
            now_utc=t0 + timedelta(seconds=67),
        )
        self.assertEqual(res_late.status, DeliveryStatus.SENT)
        self.assertEqual(res_late.external_message_id, "tg-msg-late-1")
        self.assertIsNone(res_late.error_code)
        self.assertIsInstance(res_late.delivered_at, datetime)
        claim2_after = self.deliveries.get_delivery(claim2.delivery_id)
        self.assertEqual(claim2_after.status, DeliveryStatus.DISPATCHING)
        self.assertEqual(claim2_after.claim_token, claim2.claim_token)
        late_ev = self.audit.list_events(entity_type="DELIVERY", entity_id=claim1.delivery_id, event_type="DELIVERY_LATE_SENT_AFTER_RECLAIM")
        self.assertEqual(len(late_ev), 1)
        late_details = json.loads(late_ev[0].details_json or "{}")
        self.assertEqual(late_details["previous_error_code"], "LEASE_EXPIRED_BEFORE_SEND")
        self.assertEqual(late_details["external_message_id"], "tg-msg-late-1")
        self.assertEqual(self.inv.get_item(item_id).status, InventoryStatus.SOLD)

        # reap_expired_leases transitions expired attempt2 -> UNCERTAIN
        reaped = self.deliveries.reap_expired_leases(now_utc=t0 + timedelta(seconds=130))
        self.assertEqual(reaped, 1)
        self.assertEqual(self.deliveries.get_delivery(claim2.delivery_id).status, DeliveryStatus.UNCERTAIN)

        # INITIAL blocked once a SENT attempt exists
        with self.assertRaises(ConflictError):
            self.deliveries.claim_delivery(
                order.id,
                claimed_by="worker-3",
                delivery_kind=DeliveryKind.INITIAL,
                now_utc=t0 + timedelta(seconds=200),
            )

        # Cooldown enforced from COALESCE(delivered_at, created_at) (attempt1 delivered at +67s, attempt2 created at +65s)
        with self.assertRaises(DeliveryCooldownError):
            self.deliveries.claim_delivery(
                order.id,
                claimed_by="worker-3",
                delivery_kind=DeliveryKind.REDELIVERY,
                cooldown_seconds=60,
                now_utc=t0 + timedelta(seconds=126),  # 59s after delivered_at=+67s, though 61s after created_at=+65s
            )

        claim3 = self.deliveries.claim_delivery(
            order.id,
            claimed_by="worker-3",
            delivery_kind=DeliveryKind.REDELIVERY,
            cooldown_seconds=60,
            now_utc=t0 + timedelta(seconds=128),
            now_monotonic=3000.0,
        )
        self.assertEqual(claim3.attempt_no, 3)

        # Ensure zero plaintext secret leaks in order_deliveries or audit_events
        dump_deliveries = str([dict(r) for r in self.conn.execute("SELECT * FROM order_deliveries").fetchall()])
        dump_audits = str([dict(r) for r in self.conn.execute("SELECT * FROM audit_events").fetchall()])
        self.assertNotIn("DELIV-SECRET-9876", dump_deliveries)
        self.assertNotIn("DELIV-SECRET-9876", dump_audits)

    def test_ar_p3_001_concurrent_claim_delivery_race(self) -> None:
        _u, _p, _d, order, _inv_id = self._seed_voucher_and_order(ext_id="deliv_race_u", raw_code="RACE-CODE-1")
        self.orders.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.orders.transition_order_status(order.id, OrderStatus.PAID)
        self.inv.finalize_order_sale(order.id)

        barrier = threading.Barrier(2)
        successes: list[int] = []
        conflicts: list[Exception] = []
        unexpected: list[Exception] = []
        lock = threading.Lock()

        def worker(worker_name: str) -> None:
            c = get_connection(self.db_path, timeout_seconds=5.0)
            try:
                barrier.wait(timeout=5.0)
                claim = DeliveryRepository(c).claim_delivery(order.id, claimed_by=worker_name)
                with lock:
                    successes.append(claim.attempt_no)
            except DeliveryInProgressError as exc:
                with lock:
                    conflicts.append(exc)
            except Exception as exc:
                with lock:
                    unexpected.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=worker, args=("race-w1",))
        t2 = threading.Thread(target=worker, args=("race-w2",))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(unexpected, [])
        self.assertEqual(successes, [1])
        self.assertEqual(len(conflicts), 1)

    # ------------------------------------------------------------------
    # AR-P3-006: IdempotencyRepository Replay, Stale Reclaim, Eviction & Race
    # ------------------------------------------------------------------

    def test_ar_p3_006_idempotency_lifecycle_replay_conflict_stale_reclaim_and_cleanup(self) -> None:
        t0 = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)
        h1 = hashlib.sha256(b"payload-1").hexdigest()
        h2 = hashlib.sha256(b"payload-2").hexdigest()

        with self.assertRaises(ValidationError):
            self.idempotency.claim("CREATE_ORDER", "u:1", "k1", "z" * 64, now_utc=t0)

        res1 = self.idempotency.claim("CREATE_ORDER", "u:1", "k1", h1, ttl_seconds=600, stale_after_seconds=60, now_utc=t0)
        self.assertEqual(res1.outcome, "CLAIMED")
        self.assertFalse(res1.reclaimed)
        self.assertEqual(res1.record.status, IdempotencyStatus.IN_PROGRESS)

        # Active IN_PROGRESS raises IdempotencyInProgressError; different hash raises IdempotencyConflictError
        with self.assertRaises(IdempotencyInProgressError):
            self.idempotency.claim("CREATE_ORDER", "u:1", "k1", h1, now_utc=t0 + timedelta(seconds=30))
        with self.assertRaises(IdempotencyConflictError):
            self.idempotency.claim("CREATE_ORDER", "u:1", "k1", h2, now_utc=t0 + timedelta(seconds=30))

        # After stale_after (60s), stale IN_PROGRESS can be reclaimed; old owner_token is rejected
        res_reclaim = self.idempotency.claim(
            "CREATE_ORDER", "u:1", "k1", h1, ttl_seconds=600, stale_after_seconds=60, now_utc=t0 + timedelta(seconds=61)
        )
        self.assertEqual(res_reclaim.outcome, "CLAIMED")
        self.assertTrue(res_reclaim.reclaimed)
        self.assertNotEqual(res_reclaim.owner_token, res1.owner_token)

        with self.assertRaises(ConflictError):
            self.idempotency.mark_completed("CREATE_ORDER", "u:1", "k1", res1.owner_token, response_entity_type="ORDER", response_entity_id=42)
        self.assertFalse(self.idempotency.release_in_progress("CREATE_ORDER", "u:1", "k1", res1.owner_token))

        completed = self.idempotency.mark_completed(
            "CREATE_ORDER", "u:1", "k1", res_reclaim.owner_token, response_entity_type="ORDER", response_entity_id=77, now_utc=t0 + timedelta(seconds=62)
        )
        self.assertEqual(completed.status, IdempotencyStatus.COMPLETED)
        self.assertEqual(completed.response_entity_id, 77)

        # Replay returns COMPLETED record; different hash raises IdempotencyConflictError
        replay = self.idempotency.claim("CREATE_ORDER", "u:1", "k1", h1, now_utc=t0 + timedelta(seconds=120))
        self.assertEqual(replay.outcome, "REPLAY")
        self.assertEqual(replay.record.response_entity_id, 77)
        with self.assertRaises(IdempotencyConflictError):
            self.idempotency.claim("CREATE_ORDER", "u:1", "k1", h2, now_utc=t0 + timedelta(seconds=120))

        # After TTL expiration, same key is evicted and can be freshly claimed even with a new request_hash
        res_evicted = self.idempotency.claim(
            "CREATE_ORDER", "u:1", "k1", h2, ttl_seconds=600, stale_after_seconds=60, now_utc=t0 + timedelta(seconds=700)
        )
        self.assertEqual(res_evicted.outcome, "CLAIMED")
        self.assertFalse(res_evicted.reclaimed)
        self.assertEqual(res_evicted.record.request_hash, h2)

        # release_in_progress cleans IN_PROGRESS by matching owner, and cleanup_expired removes expired keys
        self.assertTrue(self.idempotency.release_in_progress("CREATE_ORDER", "u:1", "k1", res_evicted.owner_token))
        self.idempotency.claim("CREATE_ORDER", "u:1", "k_exp", h1, ttl_seconds=10, stale_after_seconds=5, now_utc=t0)
        self.assertEqual(self.idempotency.cleanup_expired(now_utc=t0 + timedelta(seconds=15)), 1)

    def test_ar_p3_006_concurrent_idempotency_claim_race(self) -> None:
        h = hashlib.sha256(b"concurrent-idem").hexdigest()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        in_progress_errs: list[Exception] = []
        unexpected: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            c = get_connection(self.db_path, timeout_seconds=5.0)
            try:
                barrier.wait(timeout=5.0)
                res = IdempotencyRepository(c).claim("SUBMIT_RECEIPT", "u:9", "idem-race-1", h)
                with lock:
                    outcomes.append(res.outcome)
            except IdempotencyInProgressError as exc:
                with lock:
                    in_progress_errs.append(exc)
            except Exception as exc:
                with lock:
                    unexpected.append(exc)
            finally:
                c.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        self.assertEqual(unexpected, [])
        self.assertEqual(outcomes, ["CLAIMED"])
        self.assertEqual(len(in_progress_errs), 1)

    # ------------------------------------------------------------------
    # SG-P31-001..005 Dedicated Remediation Regression Tests
    # ------------------------------------------------------------------

    def test_sg_p31_001_pre_send_lease_budget_db_checks_margin_and_concurrent_reaper(self) -> None:
        _u, _p, _d, order, _inv_id = self._seed_voucher_and_order(ext_id="sg_p31_001_u", raw_code="SG-P31-001-CODE")
        self.orders.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.orders.transition_order_status(order.id, OrderStatus.PAID)
        self.inv.finalize_order_sale(order.id)

        t0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Inconsistent claim config rejected: min_send_budget < http_timeout + safety_margin
        with self.assertRaises(ValidationError):
            self.deliveries.claim_delivery(
                order.id,
                claimed_by="w-bad",
                lease_seconds=60,
                min_send_budget_seconds=18.0,
                http_timeout_seconds=15.0,
                safety_margin_seconds=5.0,
                now_utc=t0,
                now_monotonic=100.0,
            )

        # 1. Lease not expired vs http_timeout (18s > 15s), but insufficient for http_timeout (15s) + margin (5s) = 20s
        c_margin = self.deliveries.claim_delivery(
            order.id,
            claimed_by="w-margin",
            delivery_kind=DeliveryKind.INITIAL,
            lease_seconds=60,
            cooldown_seconds=0,
            min_send_budget_seconds=20.0,
            http_timeout_seconds=15.0,
            safety_margin_seconds=5.0,
            now_utc=t0,
            now_monotonic=1000.0,
        )
        # With sufficient remaining (22s >= 20s), returns min(15.0, 22.0 - 5.0) = 15.0
        self.assertEqual(
            self.deliveries.check_pre_send_lease_budget(
                c_margin, now_monotonic=1038.0, now_utc=t0 + timedelta(seconds=38)
            ),
            15.0,
        )
        # With 18.0s remaining (< 20.0s), transitions to UNCERTAIN (LEASE_EXPIRED_BEFORE_SEND) and rejects send
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(
                c_margin, now_monotonic=1042.0, now_utc=t0 + timedelta(seconds=42)
            )
        self.assertEqual(self.deliveries.get_delivery(c_margin.delivery_id).status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(self.deliveries.get_delivery(c_margin.delivery_id).error_code, "LEASE_EXPIRED_BEFORE_SEND")

        # 2. Record already UNCERTAIN prior to pre-send check refuses HTTP call without overwriting error_code
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(c_margin, now_monotonic=1010.0, now_utc=t0 + timedelta(seconds=10))
        self.assertEqual(self.deliveries.get_delivery(c_margin.delivery_id).error_code, "LEASE_EXPIRED_BEFORE_SEND")
        stale_ev = self.audit.list_events(
            entity_type="DELIVERY", entity_id=c_margin.delivery_id, event_type="DELIVERY_PRE_SEND_REJECTED_STALE_STATE"
        )
        self.assertEqual(len(stale_ev), 1)

        # 3. Stale/mismatched claim_token refuses HTTP call cleanly without mutating DB
        c_valid = self.deliveries.claim_delivery(
            order.id,
            claimed_by="w-valid",
            delivery_kind=DeliveryKind.INITIAL,
            lease_seconds=60,
            cooldown_seconds=0,
            min_send_budget_seconds=20.0,
            http_timeout_seconds=15.0,
            safety_margin_seconds=5.0,
            now_utc=t0 + timedelta(seconds=70),
            now_monotonic=2000.0,
        )
        fake_claim = c_valid.__class__(
            delivery_id=c_valid.delivery_id,
            order_id=c_valid.order_id,
            target_user_id=c_valid.target_user_id,
            attempt_no=c_valid.attempt_no,
            claim_token="0" * 32,
            delivery_kind=c_valid.delivery_kind,
            lease_expires_at=c_valid.lease_expires_at,
            lease_monotonic_deadline=c_valid.lease_monotonic_deadline,
            min_send_budget_seconds=c_valid.min_send_budget_seconds,
            http_timeout_seconds=c_valid.http_timeout_seconds,
            safety_margin_seconds=c_valid.safety_margin_seconds,
        )
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(
                fake_claim, now_monotonic=2005.0, now_utc=t0 + timedelta(seconds=75)
            )
        self.assertEqual(self.deliveries.get_delivery(c_valid.delivery_id).status, DeliveryStatus.DISPATCHING)

        # 4. Lease expired by UTC clock (even if monotonic clock looks early, and with non-UTC tzinfo) refuses send
        plus3_tz = timezone(timedelta(hours=3))
        utc_expired_time = (t0 + timedelta(seconds=135)).astimezone(plus3_tz)  # +65s after claim (+70s -> expires +130s)
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(c_valid, now_monotonic=2005.0, now_utc=utc_expired_time)
        self.assertEqual(self.deliveries.get_delivery(c_valid.delivery_id).error_code, "LEASE_EXPIRED_BEFORE_SEND")

        # 5. Lease expired purely by monotonic clock before UTC expiry also refuses send
        c_mono = self.deliveries.claim_delivery(
            order.id,
            claimed_by="w-mono",
            delivery_kind=DeliveryKind.INITIAL,
            lease_seconds=60,
            cooldown_seconds=0,
            now_utc=t0 + timedelta(seconds=200),
            now_monotonic=3000.0,
        )
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(
                c_mono, now_monotonic=3061.0, now_utc=t0 + timedelta(seconds=210)
            )
        self.assertEqual(self.deliveries.get_delivery(c_mono.delivery_id).error_code, "LEASE_EXPIRED_BEFORE_SEND")

        # 6. Concurrent reaper runs on a second thread BETWEEN claim_delivery and check_pre_send_lease_budget
        c_race = self.deliveries.claim_delivery(
            order.id,
            claimed_by="w-race",
            delivery_kind=DeliveryKind.INITIAL,
            lease_seconds=60,
            cooldown_seconds=0,
            now_utc=t0 + timedelta(seconds=300),
            now_monotonic=4000.0,
        )
        reaped_counts: list[int] = []
        reap_errors: list[Exception] = []
        reaper_done = threading.Event()

        def reaper_worker() -> None:
            rc = get_connection(self.db_path, timeout_seconds=5.0)
            try:
                cnt = DeliveryRepository(rc).reap_expired_leases(now_utc=t0 + timedelta(seconds=361))
                reaped_counts.append(cnt)
            except Exception as exc:
                reap_errors.append(exc)
            finally:
                rc.close()
                reaper_done.set()

        t_reap = threading.Thread(target=reaper_worker)
        t_reap.start()
        self.assertTrue(reaper_done.wait(timeout=10.0))
        t_reap.join(timeout=10.0)
        self.assertFalse(t_reap.is_alive())
        self.assertEqual(reap_errors, [])
        self.assertEqual(reaped_counts, [1])

        # Even if local monotonic clock looks healthy, live DB check rejects because reaper marked UNCERTAIN
        with self.assertRaises(DeliveryLeaseExpiredError):
            self.deliveries.check_pre_send_lease_budget(
                c_race, now_monotonic=4005.0, now_utc=t0 + timedelta(seconds=305)
            )
        after_race = self.deliveries.get_delivery(c_race.delivery_id)
        self.assertEqual(after_race.status, DeliveryStatus.UNCERTAIN)
        self.assertEqual(after_race.error_code, "LEASE_EXPIRED")

    def test_sg_p31_003_idempotency_mark_completed_validation_and_replay(self) -> None:
        t0 = datetime(2026, 9, 2, 8, 0, 0, tzinfo=timezone.utc)
        req_hash = hashlib.sha256(b"sg-p31-003-payload").hexdigest()
        claimed = self.idempotency.claim("CREATE_ORDER", "u:31", "key-31", req_hash, now_utc=t0)

        with self.assertRaises(TypeError):
            self.idempotency.mark_completed("CREATE_ORDER", "u:31", "key-31", claimed.owner_token)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            self.idempotency.mark_completed(
                "CREATE_ORDER", "u:31", "key-31", claimed.owner_token, response_entity_type="ORDER"  # type: ignore[call-arg]
            )
        with self.assertRaises(TypeError):
            self.idempotency.mark_completed(
                "CREATE_ORDER", "u:31", "key-31", claimed.owner_token, response_entity_id=10  # type: ignore[call-arg]
            )

        for bad_type, bad_id in [
            (None, 10),
            ("", 10),
            ("   ", 10),
            (123, 10),
            ("ORDER", None),
            ("ORDER", 0),
            ("ORDER", -5),
            ("ORDER", True),
            ("ORDER", False),
            ("ORDER", 1.5),
            ("ORDER", "10"),
        ]:
            with self.assertRaises(ValidationError):
                self.idempotency.mark_completed(
                    "CREATE_ORDER",
                    "u:31",
                    "key-31",
                    claimed.owner_token,
                    response_entity_type=bad_type,  # type: ignore[arg-type]
                    response_entity_id=bad_id,  # type: ignore[arg-type]
                )
            rec_still_in_progress = self.idempotency.get_record("CREATE_ORDER", "u:31", "key-31")
            self.assertEqual(rec_still_in_progress.status, IdempotencyStatus.IN_PROGRESS)
            self.assertIsNone(rec_still_in_progress.response_entity_type)
            self.assertIsNone(rec_still_in_progress.response_entity_id)

        done = self.idempotency.mark_completed(
            "CREATE_ORDER",
            "u:31",
            "key-31",
            claimed.owner_token,
            response_entity_type="ORDER",
            response_entity_id=555,
            now_utc=t0 + timedelta(seconds=2),
        )
        self.assertEqual(done.status, IdempotencyStatus.COMPLETED)
        self.assertEqual(done.response_entity_type, "ORDER")
        self.assertEqual(done.response_entity_id, 555)

        replay = self.idempotency.claim(
            "CREATE_ORDER", "u:31", "key-31", req_hash, now_utc=t0 + timedelta(seconds=10)
        )
        self.assertEqual(replay.outcome, "REPLAY")
        self.assertEqual(replay.record.response_entity_type, "ORDER")
        self.assertEqual(replay.record.response_entity_id, 555)

    def test_sg_p31_004_utc_timezone_aware_dto_timestamp_contracts(self) -> None:
        _u, _p, _d, order, _inv_id = self._seed_voucher_and_order(ext_id="sg_p31_004_u", raw_code="SG-P31-004-CODE")
        self.orders.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW)
        self.orders.transition_order_status(order.id, OrderStatus.PAID)
        self.inv.finalize_order_sale(order.id)

        plus4 = timezone(timedelta(hours=4))
        local_claim_time = datetime(2026, 9, 3, 14, 30, 0, tzinfo=plus4)  # == 10:30:00 UTC

        claim = self.deliveries.claim_delivery(
            order.id,
            claimed_by="w-utc",
            lease_seconds=60,
            now_utc=local_claim_time,
            now_monotonic=5000.0,
        )
        self.assertIsInstance(claim.lease_expires_at, datetime)
        self.assertEqual(claim.lease_expires_at.tzinfo, timezone.utc)
        self.assertEqual(claim.lease_expires_at.utcoffset(), timedelta(0))
        self.assertEqual(claim.lease_expires_at, datetime(2026, 9, 3, 10, 31, 0, tzinfo=timezone.utc))

        attempt_dispatching = self.deliveries.get_delivery(claim.delivery_id)
        for ts in (attempt_dispatching.lease_expires_at, attempt_dispatching.created_at, attempt_dispatching.updated_at):
            self.assertIsInstance(ts, datetime)
            self.assertEqual(ts.tzinfo, timezone.utc)
            self.assertEqual(ts.utcoffset(), timedelta(0))
        self.assertIsNone(attempt_dispatching.delivered_at)

        attempt_sent = self.deliveries.record_outcome(
            claim.delivery_id,
            claim.claim_token,
            DeliveryStatus.SENT,
            external_message_id="tg-utc-1",
            now_utc=local_claim_time + timedelta(seconds=5),
        )
        self.assertIsInstance(attempt_sent.delivered_at, datetime)
        assert attempt_sent.delivered_at is not None
        self.assertEqual(attempt_sent.delivered_at.tzinfo, timezone.utc)
        self.assertEqual(attempt_sent.delivered_at, datetime(2026, 9, 3, 10, 30, 5, tzinfo=timezone.utc))

        idem_res = self.idempotency.claim(
            "TEST_UTC",
            "u:utc",
            "k-utc",
            hashlib.sha256(b"utc").hexdigest(),
            ttl_seconds=300,
            stale_after_seconds=60,
            now_utc=local_claim_time,
        )
        rec = idem_res.record
        for ts in (rec.stale_after, rec.expires_at, rec.created_at, rec.updated_at):
            self.assertIsInstance(ts, datetime)
            self.assertEqual(ts.tzinfo, timezone.utc)
            self.assertEqual(ts.utcoffset(), timedelta(0))
        self.assertEqual(rec.stale_after, datetime(2026, 9, 3, 10, 31, 0, tzinfo=timezone.utc))
        self.assertEqual(rec.expires_at, datetime(2026, 9, 3, 10, 35, 0, tzinfo=timezone.utc))

    def test_sg_p31_005_cancel_order_atomically_audit_contract(self) -> None:
        user, _p, _d, order, _inv_id = self._seed_voucher_and_order(ext_id="sg_p31_005_u", raw_code="SG-P31-005-CODE")
        self.receipts.submit_receipt(order.id, user.id, b"rcpt_sg_p31_005", provider_tx_id="tx_005")
        self.orders.transition_order_status(order.id, OrderStatus.PAYMENT_REVIEW, actor_type="USER", actor_id=user.id)

        self.orders.cancel_order_atomically(
            order.id,
            actor_type="OPERATOR",
            actor_id=99,
            notes="manual operator cancel",
        )
        events = self.audit.list_events(entity_type="ORDER", entity_id=order.id)
        # Expect: initial PENDING_PAYMENT->PAYMENT_REVIEW + PAYMENT_REVIEW->CANCELLED + ORDER_CANCELLED_ATOMIC
        cancel_phase_events = events[1:]
        self.assertEqual([e.event_type for e in cancel_phase_events], ["ORDER_STATUS_TRANSITION", "ORDER_CANCELLED_ATOMIC"])
        tr_payload = json.loads(cancel_phase_events[0].details_json or "{}")
        ca_payload = json.loads(cancel_phase_events[1].details_json or "{}")
        self.assertEqual(tr_payload["from_status"], "PAYMENT_REVIEW")
        self.assertEqual(tr_payload["to_status"], "CANCELLED")
        self.assertEqual(ca_payload["released_inventory_count"], 1)
        self.assertEqual(ca_payload["rejected_pending_receipts"], 1)

    def test_sg_p31_nb_002_operational_defaults_finite_float_validation(self) -> None:
        float_fields = (
            "delivery_min_send_budget_seconds",
            "delivery_http_timeout_seconds",
            "delivery_safety_margin_seconds",
        )
        for field_name in float_fields:
            for bad_val in (
                float("nan"),
                float("inf"),
                float("-inf"),
                True,
                False,
                0,
                0.0,
                -1,
                -1.0,
                10**1000,
                -(10**1000),
                10**5000,
                -(10**5000),
            ):
                with self.subTest(field=field_name, value=type(bad_val).__name__):
                    with self.assertRaises(ValueError) as ctx:
                        OperationalDefaults(**{field_name: bad_val})  # type: ignore[arg-type]
                    self.assertIs(type(ctx.exception), ValueError)

        valid_cfg = OperationalDefaults(
            delivery_lease_seconds=90,
            delivery_min_send_budget_seconds=25.0,
            delivery_http_timeout_seconds=18.0,
            delivery_safety_margin_seconds=7.0,
        )
        self.assertEqual(valid_cfg.delivery_min_send_budget_seconds, 25.0)
        self.assertEqual(valid_cfg.delivery_http_timeout_seconds, 18.0)
        self.assertEqual(valid_cfg.delivery_safety_margin_seconds, 7.0)

        valid_int_cfg = OperationalDefaults(
            delivery_lease_seconds=90,
            delivery_min_send_budget_seconds=25,  # type: ignore[arg-type]
            delivery_http_timeout_seconds=18,  # type: ignore[arg-type]
            delivery_safety_margin_seconds=7,  # type: ignore[arg-type]
        )
        self.assertEqual(valid_int_cfg.delivery_min_send_budget_seconds, 25)

    def test_sg_p31_006_explicit_fingerprint_pepper_min_length_and_rotation(self) -> None:
        self.assertEqual(DEFAULT_OPERATIONAL_CONFIG.min_fingerprint_pepper_bytes, 16)

        # compute_fingerprint rejects empty, 1-byte, and 15-byte explicit peppers without leaking secret bytes
        for short_pepper in (b"", b"a", b"x" * 15):
            with self.assertRaises(CryptoConfigurationError) as cm:
                compute_fingerprint("SG-P31-006-CODE", pepper=short_pepper)
            if short_pepper:
                self.assertNotIn(short_pepper.decode("ascii"), str(cm.exception))

        # Exact 16-byte pepper is accepted
        exact_16_pepper = b"p" * 16
        fp_16 = compute_fingerprint("SG-P31-006-CODE", pepper=exact_16_pepper)
        self.assertEqual(len(fp_16), 64)

        # Seed inventory items and snapshot baseline fingerprints
        prod = self.catalog.create_voucher_product("V SG-P31-006", "TR", "TRY", status=ProductStatus.APPROVED)
        denom = self.catalog.add_voucher_denomination(prod.id, "TR", "TRY", 1000, 3000)
        self.inv.import_items(prod.id, ["PEPPER-CODE-1", "PEPPER-CODE-2"], InventoryType.VOUCHER_CODE, denom.id)

        baseline_fps = [
            (r["id"], r["fingerprint"])
            for r in self.conn.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC").fetchall()
        ]
        current_pepper = get_fingerprint_pepper()

        # Short new_pepper or short old_pepper must fail BEFORE starting any DB transaction
        with patch("src.db.repositories.transaction") as mock_tx:
            for bad_new in (b"", b"a", b"y" * 15):
                with self.assertRaises(CryptoConfigurationError):
                    self.inv.rotate_fingerprint_pepper(bad_new)
            for bad_old in (b"", b"b", b"z" * 15):
                with self.assertRaises(CryptoConfigurationError):
                    self.inv.rotate_fingerprint_pepper(b"valid_new_pepper_16", old_pepper=bad_old)
            mock_tx.assert_not_called()

        after_reject_fps = [
            (r["id"], r["fingerprint"])
            for r in self.conn.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC").fetchall()
        ]
        self.assertEqual(after_reject_fps, baseline_fps)

        # Rotation with exact 16-byte new_pepper and valid old_pepper succeeds
        new_16_pepper = b"n" * 16
        rotated = self.inv.rotate_fingerprint_pepper(new_16_pepper, old_pepper=current_pepper)
        self.assertEqual(rotated, 2)
        for item_id, fp in self.conn.execute("SELECT id, fingerprint FROM inventory_items ORDER BY id ASC").fetchall():
            raw_code = decrypt_payload(
                self.conn.execute("SELECT encrypted_payload FROM inventory_items WHERE id = ?", (item_id,)).fetchone()[
                    "encrypted_payload"
                ]
            )
            self.assertEqual(fp, compute_fingerprint(raw_code, pepper=new_16_pepper))

        # Fernet encryption/decryption remains unchanged
        ciphertext = encrypt_payload("FERNET-CHECK-006")
        self.assertEqual(decrypt_payload(ciphertext), "FERNET-CHECK-006")


if __name__ == "__main__":
    unittest.main()
