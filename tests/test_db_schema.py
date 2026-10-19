"""Unit and integration tests for SQLite schema, migrations, connection, and catalog."""

import os
import sqlite3
import tempfile
import unittest

from src.db.connection import create_connection, get_connection_context
from src.db.migrations import get_applied_migrations, run_migrations
from src.db.models import (
    ApprovalStatus,
    ConflictError,
    DeliveryMethod,
    ProductStatus,
    ProductType,
    ValidationError,
)
from src.db.repositories import CatalogRepository


class TestDatabaseSchemaAndMigrations(unittest.TestCase):
    """Tests for database initialization, WAL pragma, migrations, and schema constraints."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_schema.db")
        self.conn = create_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_connection_pragmas(self):
        """Verify foreign keys, WAL mode, and busy timeout are configured."""
        cur = self.conn.cursor()
        cur.execute("PRAGMA foreign_keys;")
        fk_status = cur.fetchone()[0]
        self.assertEqual(fk_status, 1)

        cur.execute("PRAGMA journal_mode;")
        journal_mode = cur.fetchone()[0].lower()
        self.assertEqual(journal_mode, "wal")

        cur.execute("PRAGMA busy_timeout;")
        busy_timeout = cur.fetchone()[0]
        self.assertEqual(busy_timeout, 5000)

    def test_migration_runner_applied_cleanly(self):
        """Verify migrations run and are tracked in schema_migrations table."""
        applied = run_migrations(self.conn)
        self.assertIn(1, applied)

        history = get_applied_migrations(self.conn)
        self.assertIn(1, history)

        # Idempotency check: running again applies 0 new migrations
        applied_again = run_migrations(self.conn)
        self.assertEqual(len(applied_again), 0)

    def test_foreign_key_enforcement(self):
        """Verify foreign key constraint violations are rejected."""
        run_migrations(self.conn)
        cur = self.conn.cursor()

        # Attempt to insert voucher_details with non-existent product_id
        with self.assertRaises(sqlite3.IntegrityError):
            cur.execute(
                "INSERT INTO voucher_details (product_id, region, currency) VALUES (?, ?, ?)",
                (999999, "TR", "TRY"),
            )
            self.conn.commit()

    def test_polymorphic_catalog_voucher(self):
        """Verify creation of voucher product and its denominations."""
        run_migrations(self.conn)
        repo = CatalogRepository(self.conn)

        prod = repo.create_voucher_product(
            display_name="PSN TR Wallet Card",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.assertEqual(prod.product_type, ProductType.VOUCHER)
        self.assertEqual(prod.display_name, "PSN TR Wallet Card")

        # Add denominations
        denom100 = repo.add_voucher_denomination(
            product_id=prod.id,
            region="TR",
            currency="TRY",
            face_value_minor=10000,
            cost_rub_kopecks=32000,
        )
        self.assertEqual(denom100.face_value_minor, 10000)
        self.assertEqual(denom100.cost_rub_kopecks, 32000)
        self.assertTrue(denom100.is_active)

        # Duplicate denomination should raise ConflictError
        with self.assertRaises(ConflictError):
            repo.add_voucher_denomination(
                product_id=prod.id,
                region="TR",
                currency="TRY",
                face_value_minor=10000,
                cost_rub_kopecks=33000,
            )

        # Validation: negative cost or non-positive face value
        with self.assertRaises(ValidationError):
            repo.add_voucher_denomination(
                product_id=prod.id,
                region="TR",
                currency="TRY",
                face_value_minor=0,
                cost_rub_kopecks=1000,
            )

        with self.assertRaises(ValidationError):
            repo.add_voucher_denomination(
                product_id=prod.id,
                region="TR",
                currency="TRY",
                face_value_minor=5000,
                cost_rub_kopecks=-10,
            )

    def test_polymorphic_catalog_fixed_product(self):
        """Verify fixed product lifecycle, supply approval check, and status update."""
        run_migrations(self.conn)
        repo = CatalogRepository(self.conn)

        fixed_prod = repo.create_fixed_product(
            display_name="Gemini Pro 1 Month",
            price_rub_kopecks=14900,
            delivery_method=DeliveryMethod.MANUAL_REVIEW,
            supply_method="WHOLESALE_READY_ACCOUNT",
            duration_days=30,
            approval_status=ApprovalStatus.DRAFT,
            status=ProductStatus.DRAFT,
        )
        self.assertEqual(fixed_prod.product_type, ProductType.FIXED_PRODUCT)

        # Attempting to activate product before approval should raise ValidationError
        with self.assertRaises(ValidationError):
            repo.update_product_status(fixed_prod.id, ProductStatus.APPROVED)

        # Approve supply method
        repo.update_fixed_product_approval(fixed_prod.id, ApprovalStatus.APPROVED)

        # Now activating should succeed
        repo.update_product_status(fixed_prod.id, ProductStatus.APPROVED)
        updated = repo.get_product(fixed_prod.id)
        self.assertEqual(updated.status, ProductStatus.APPROVED)


if __name__ == "__main__":
    unittest.main()
