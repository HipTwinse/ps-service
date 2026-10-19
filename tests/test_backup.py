"""Unit tests for SQLite online backup and restore utilities."""

import os
import tempfile
import unittest

from src.db.connection import (
    backup_database,
    create_connection,
    restore_database,
)
from src.db.migrations import run_migrations
from src.db.models import ProductStatus
from src.db.repositories import (
    CatalogRepository,
    OrderRepository,
    UserRepository,
)


class TestDatabaseBackupAndRestore(unittest.TestCase):
    """Tests for SQLite online backup and point-in-time restore functions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.src_db_path = os.path.join(self.temp_dir.name, "live.db")
        self.backup_path = os.path.join(self.temp_dir.name, "backup.db")
        self.restored_path = os.path.join(self.temp_dir.name, "restored.db")

        self.conn = create_connection(self.src_db_path)
        run_migrations(self.conn)

        # Seed data
        self.catalog_repo = CatalogRepository(self.conn)
        self.user_repo = UserRepository(self.conn)
        self.order_repo = OrderRepository(self.conn)

        self.user, _ = self.user_repo.get_or_create_user("tg_backup_test", "backup_user")
        self.product = self.catalog_repo.create_voucher_product(
            display_name="PSN Backup Test Card",
            region="TR",
            currency="TRY",
            status=ProductStatus.APPROVED,
        )
        self.denom = self.catalog_repo.add_voucher_denomination(
            product_id=self.product.id,
            region="TR",
            currency="TRY",
            face_value_minor=25000,
            cost_rub_kopecks=80000,
        )
        self.order = self.order_repo.create_order(
            user_id=self.user.id,
            total_amount_minor=80000,
            currency="RUB",
            items=[
                {
                    "product_id": self.product.id,
                    "product_type_snapshot": "VOUCHER",
                    "quantity": 1,
                    "unit_price_minor": 80000,
                    "metadata_json": None,
                }
            ],
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_backup_and_restore_cycle(self):
        """Verify full backup and restore cycle preserves data integrity and records."""
        # 1. Perform online backup
        backup_database(self.conn, self.backup_path)
        self.assertTrue(os.path.exists(self.backup_path))
        self.assertGreater(os.path.getsize(self.backup_path), 0)

        # 2. Restore to new file
        restore_database(self.backup_path, self.restored_path)
        self.assertTrue(os.path.exists(self.restored_path))

        # 3. Open restored DB and inspect data
        restored_conn = create_connection(self.restored_path)
        try:
            cur = restored_conn.cursor()
            cur.execute("PRAGMA integrity_check;")
            integrity_result = cur.fetchone()[0]
            self.assertEqual(integrity_result, "ok")

            restored_user_repo = UserRepository(restored_conn)
            restored_order_repo = OrderRepository(restored_conn)

            user = restored_user_repo.get_user(self.user.id)
            self.assertEqual(user.external_id, "tg_backup_test")

            order = restored_order_repo.get_order(self.order.id)
            self.assertEqual(order.total_amount_minor, 80000)
            self.assertEqual(order.user_id, self.user.id)

            items = restored_order_repo.get_order_items(order.id)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].unit_price_minor, 80000)
        finally:
            restored_conn.close()


if __name__ == "__main__":
    unittest.main()
