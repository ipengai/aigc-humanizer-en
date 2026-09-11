import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admin
from app.models import BalanceTransaction, Order, User


def _database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    User.init_table(conn)
    Order.init_table(conn)
    BalanceTransaction.init_table(conn)
    conn.execute(
        """INSERT INTO users
           (id, email, password_hash, created_at, word_balance)
           VALUES (1, 'detector-buyer@example.com', 'hash',
                   '2026-09-01T00:00:00+00:00', 0)"""
    )
    conn.commit()
    return conn


class AdminDetectionOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "admin-detect.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_admin_api_filters_and_summarizes_detection_recharges(self):
        conn = _database(self.db_path)
        try:
            Order.create_detection_recharge_order(
                conn, 1, "DET-PAID-1", words=2000, price=9.8
            )
            conn.execute(
                """UPDATE orders
                   SET payment_status='paid', status='completed',
                       alipay_trade_no='TRADE-DET-1',
                       paid_at='2026-09-09T02:00:00+00:00',
                       created_at='2026-09-09T01:00:00+00:00'
                   WHERE order_id='DET-PAID-1'"""
            )
            Order.create_detection_recharge_order(
                conn, 1, "DET-PENDING-1", words=1000, price=4.9
            )
            conn.execute(
                "UPDATE orders SET created_at='2026-09-09T03:00:00+00:00' "
                "WHERE order_id='DET-PENDING-1'"
            )
            Order.create_processing_order(
                conn, 1, "HUMA-1", "rewrite text", "txt", None,
                2, 0, "median",
            )
            conn.commit()

            indexes = {
                row[1] for row in conn.execute("PRAGMA index_list(orders)").fetchall()
            }
            self.assertIn("idx_orders_type_created_at", indexes)
        finally:
            conn.close()

        with mock.patch.object(admin, "DB_PATH", str(self.db_path)):
            admin.admin_app.config.update(TESTING=True)
            client = admin.admin_app.test_client()
            with client.session_transaction() as session:
                session["admin_authenticated"] = True
            response = client.get(
                "/admin/api/orders?start=2026-09-09&end=2026-09-09"
                "&type=detect_recharge"
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["summary"]["total_orders"], 2)
        self.assertEqual(data["summary"]["paid_orders"], 1)
        self.assertEqual(data["summary"]["pending_orders"], 1)
        self.assertEqual(data["summary"]["paid_recharge_words"], 2000)
        self.assertEqual(data["summary"]["total_revenue"], 9.8)
        self.assertEqual(
            {row["order_type"] for row in data["orders"]},
            {"detect_recharge"},
        )
        paid = next(row for row in data["orders"] if row["order_id"] == "DET-PAID-1")
        self.assertEqual(paid["user_email"], "detector-buyer@example.com")
        self.assertEqual(paid["recharge_words"], 2000)
        self.assertEqual(paid["alipay_trade_no"], "TRADE-DET-1")

    def test_dashboard_contains_detection_order_tab(self):
        template = admin.DASHBOARD_TEMPLATE
        self.assertIn('id="tab-detectorders"', template)
        self.assertIn('id="content-detectorders"', template)
        self.assertIn("type: 'detect_recharge'", template)
        self.assertIn("if (tab === 'detectorders') loadDetectOrders()", template)

    def test_dashboard_keeps_business_trend_tab_reachable(self):
        template = admin.DASHBOARD_TEMPLATE
        self.assertIn('id="tab-trends"', template)
        self.assertIn('id="content-trends"', template)
        self.assertIn("if (tab === 'trends') loadTrends()", template)

    def test_wide_order_table_is_scrollable_instead_of_clipped(self):
        template = admin.DASHBOARD_TEMPLATE
        self.assertIn('max-width: 1900px', template)
        self.assertIn('overflow-x: auto', template)

    def test_admin_users_include_detection_balance(self):
        conn = _database(self.db_path)
        try:
            conn.execute(
                "UPDATE users SET detection_free_words=477, detection_paid_words=2000 "
                "WHERE id=1"
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(admin, "DB_PATH", str(self.db_path)):
            admin.admin_app.config.update(TESTING=True)
            client = admin.admin_app.test_client()
            with client.session_transaction() as session:
                session["admin_authenticated"] = True
            response = client.get("/admin/api/users")

        self.assertEqual(response.status_code, 200)
        user = response.get_json()["users"][0]
        self.assertEqual(user["detection_free_words"], 477)
        self.assertEqual(user["detection_paid_words"], 2000)
        self.assertEqual(user["detection_balance"], 2477)

    def test_admin_users_count_unique_beijing_consumption_days(self):
        conn = _database(self.db_path)
        try:
            rows = [
                ('rewrite_consumption', -50, 950, '2026-09-09T15:30:00+00:00'),
                ('detection_consumption', -40, 910, '2026-09-09T16:30:00+00:00'),
                ('rewrite_consumption', -20, 890, '2026-09-10T03:00:00+00:00'),
                ('payment_recharge', 1000, 1890, '2026-09-11T03:00:00+00:00'),
            ]
            conn.executemany(
                """INSERT INTO balance_transactions
                   (user_id, transaction_type, words, balance_after, created_at)
                   VALUES (1, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(admin, "DB_PATH", str(self.db_path)):
            admin.admin_app.config.update(TESTING=True)
            client = admin.admin_app.test_client()
            with client.session_transaction() as session:
                session["admin_authenticated"] = True
            response = client.get("/admin/api/users")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["users"][0]["active_days"], 2)

    def test_dashboard_shows_active_days_column(self):
        self.assertIn('<th>活跃天数</th>', admin.DASHBOARD_TEMPLATE)

    def test_dashboard_explains_hybrid_route_steps(self):
        template = admin.DASHBOARD_TEMPLATE
        self.assertIn('function rewriteRouteSummary(order)', template)
        self.assertIn('混合明细:', template)
        self.assertIn('step.rewrite_backends', template)
        self.assertIn('targetedBackends', template)
        self.assertIn('定向二改：${targetedBackends}', template)


if __name__ == "__main__":
    unittest.main()
