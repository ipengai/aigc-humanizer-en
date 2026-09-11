import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from app.extensions import limiter
from app.models import Order, User
from app.routes.payment import payment_bp
from app.routes.rewrite import rewrite_bp


class _PaymentAdapter:
    def create_prepay_form(self, order_id, amount, description):
        return {"form_html": "<form></form>", "expires_in": 600}


def _app_with_blueprint(blueprint):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
    limiter.init_app(app)
    app.register_blueprint(blueprint)
    return app


class PaymentOrderSwitchingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "payments.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        User.init_table(self.conn)
        Order.init_table(self.conn)
        self.conn.execute(
            """INSERT INTO users
               (id, email, password_hash, created_at, word_balance)
               VALUES (1, 'buyer@example.com', 'hash',
                       '2026-09-11T00:00:00+00:00', 0)"""
        )
        Order.create_payment_record(
            self.conn, 1, "OLD-PENDING", "word " * 100, "txt", None,
            100, 1.49, "median", 100, 0,
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_new_payment_option_expires_previous_pending_order(self):
        app = _app_with_blueprint(payment_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1

        with mock.patch("app.routes.payment.get_db", return_value=self.conn), \
             mock.patch("app.extensions.payment_adapter", _PaymentAdapter()):
            response = client.post(
                "/api/create-payment",
                json={
                    "text": "word " * 100,
                    "mode": "median",
                    "recharge_words": 2000,
                    "supersedes_order_id": "OLD-PENDING",
                },
            )

        self.assertEqual(response.status_code, 200, response.get_json())
        replacement_id = response.get_json()["order"]["order_id"]
        old = Order.get_by_order_id(self.conn, "OLD-PENDING")
        replacement = Order.get_by_order_id(self.conn, replacement_id)
        self.assertEqual(old["payment_status"], "expired")
        self.assertEqual(old["status"], "expired")
        self.assertEqual(old["failure_code"], "payment_option_changed")
        self.assertEqual(replacement["payment_status"], "pending")

    def test_cannot_expire_another_users_pending_order(self):
        self.conn.execute(
            """INSERT INTO users
               (id, email, password_hash, created_at, word_balance)
               VALUES (2, 'other@example.com', 'hash',
                       '2026-09-11T00:00:00+00:00', 0)"""
        )
        self.conn.execute(
            "UPDATE orders SET user_id=2 WHERE order_id='OLD-PENDING'"
        )
        self.conn.commit()

        changed = Order.supersede_pending_payment(
            self.conn, "OLD-PENDING", user_id=1
        )

        self.assertFalse(changed)
        old = Order.get_by_order_id(self.conn, "OLD-PENDING")
        self.assertEqual(old["payment_status"], "pending")

    def test_payment_rejects_short_input_without_creating_an_order(self):
        app = _app_with_blueprint(payment_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1
        before = self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

        with mock.patch("app.routes.payment.get_db", return_value=self.conn), \
             mock.patch("app.extensions.payment_adapter", _PaymentAdapter()):
            response = client.post(
                "/api/create-payment",
                json={"text": "word " * 40, "recharge_words": 2000},
            )

        after = self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error_code"], "rewrite_input_too_short"
        )
        self.assertEqual(after, before)


class RewriteMinimumInputTests(unittest.TestCase):
    def test_rewrite_rejects_short_input_before_balance_or_order_work(self):
        app = _app_with_blueprint(rewrite_bp)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = 1

        with mock.patch("app.routes.rewrite.get_db") as get_db:
            response = client.post(
                "/api/rewrite",
                json={"text": "word " * 40, "mode": "median"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error_code"], "rewrite_input_too_short"
        )
        get_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
