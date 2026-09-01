import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admin
from flask import Flask
from app.extensions import limiter
from app.helpers.db import close_db
from app.models import Order, RewriteFeedback, User
from app.routes.orders import orders_bp


def _init_database(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    User.init_table(conn)
    Order.init_table(conn)
    RewriteFeedback.init_table(conn)
    conn.execute(
        """INSERT INTO users (id, email, password_hash, created_at, word_balance)
           VALUES (1, 'customer@example.com', 'hash', '2026-08-31T00:00:00+00:00', 1000)"""
    )
    conn.commit()
    return conn


def _create_completed_order(conn):
    Order.create_processing_order(
        conn,
        user_id=1,
        order_id='HUMA-TEST-1',
        original_text='Heading Original text has five words.',
        original_format='docx',
        original_filename='paper.docx',
        word_count=6,
        price=0,
        mode='median',
        paragraphs=[
            {'text': 'Heading', 'is_heading': True},
            {'text': 'Original text has five words.', 'is_heading': False},
        ],
    )
    Order.update_result(
        conn,
        'HUMA-TEST-1',
        'Rewritten body is deliberately longer than before.',
        rewritten_score=28,
        original_score=75,
        rewritten_paragraphs=[
            {'text': 'Rewritten body is deliberately longer than before.', 'is_heading': False}
        ],
        detector_backend='sapling',
        humanizer_backend='LLMBasedHumanizer',
    )


class RewriteFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / 'huma.db'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_order_result_records_quality_monitoring_fields(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
            order = Order.get_by_order_id(conn, 'HUMA-TEST-1')

            self.assertEqual(order['status'], 'completed')
            self.assertEqual(order['humanizer_backend'], 'LLMBasedHumanizer')
            self.assertEqual(order['rewritten_word_count'], 7)
            self.assertAlmostEqual(order['word_count_change_ratio'], 1 / 6)
            self.assertEqual(order['original_heading_count'], 1)
            self.assertEqual(order['rewritten_heading_count'], 0)
            self.assertEqual(order['heading_count_changed'], 1)
            self.assertTrue(order['completed_at'])
        finally:
            conn.close()

    def test_feedback_is_updated_per_order_instead_of_duplicated(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
            RewriteFeedback.upsert(
                conn, 1, 'HUMA-TEST-1', ['high_ai_score', 'details_lost'],
                detector_platform='Turnitin', external_score=46,
                comment='Still high', contact_allowed=True,
            )
            RewriteFeedback.upsert(
                conn, 1, 'HUMA-TEST-1', ['content_disorder', 'meaning_changed'],
                detector_platform='Turnitin', external_score=42,
                comment='The opening structure changed', contact_allowed=False,
            )

            feedback = RewriteFeedback.get_by_order_id(conn, 'HUMA-TEST-1')
            count = conn.execute('SELECT COUNT(*) FROM rewrite_feedback').fetchone()[0]
            self.assertEqual(count, 1)
            self.assertEqual(feedback['issue_type'], 'content_disorder')
            self.assertEqual(
                RewriteFeedback.get_issue_types(feedback),
                ['content_disorder', 'meaning_changed'],
            )
            self.assertEqual(feedback['external_score'], 42)
            self.assertEqual(feedback['contact_allowed'], 0)
        finally:
            conn.close()

    def test_admin_stats_include_system_and_user_reported_results(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
            RewriteFeedback.upsert(
                conn, 1, 'HUMA-TEST-1', ['high_ai_score', 'content_disorder'],
                detector_platform='Turnitin', external_score=42,
                comment='External result is still high', contact_allowed=True,
            )
        finally:
            conn.close()

        with mock.patch.object(admin, 'DB_PATH', str(self.db_path)):
            admin.admin_app.config.update(TESTING=True)
            client = admin.admin_app.test_client()
            with client.session_transaction() as session:
                session['admin_authenticated'] = True

            response = client.get(
                '/admin/api/rewrite-stats?start=2026-01-01&end=2026-12-31'
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['sample_count'], 1)
        self.assertEqual(data['below20_count'], 0)
        self.assertEqual(data['improved_count'], 1)
        self.assertEqual(data['heading_warning_count'], 1)
        self.assertEqual(data['feedback_count'], 1)
        self.assertEqual(data['external_score_count'], 1)
        self.assertEqual(data['external_below20_ratio'], 0)
        self.assertEqual(data['recent_feedback'][0]['issue_type'], 'high_ai_score')
        self.assertEqual(
            data['recent_feedback'][0]['issue_types'],
            ['high_ai_score', 'content_disorder'],
        )
        self.assertEqual(data['feedback_issue_counts']['high_ai_score'], 1)
        self.assertEqual(data['feedback_issue_counts']['content_disorder'], 1)

    def test_feedback_endpoint_saves_structured_report_and_private_screenshot(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
        finally:
            conn.close()

        upload_dir = Path(self.temp_dir.name) / 'feedback_uploads'
        upload_dir.mkdir()
        flask_app = Flask(__name__)
        flask_app.secret_key = 'test-secret'
        flask_app.config.update(
            TESTING=True,
            RATELIMIT_ENABLED=False,
            FEEDBACK_UPLOAD_FOLDER=str(upload_dir),
        )
        flask_app.register_blueprint(orders_bp)
        flask_app.teardown_appcontext(close_db)
        limiter.init_app(flask_app)
        client = flask_app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = 1

        screenshot = b'\x89PNG\r\n\x1a\n' + b'feedback-image'
        with mock.patch('app.models.DB_PATH', str(self.db_path)):
            response = client.post(
                '/api/orders/HUMA-TEST-1/feedback',
                data={
                    'issue_types': ['content_disorder', 'meaning_changed'],
                    'external_score': '42',
                    'comment': 'Opening paragraphs are disordered',
                    'contact_allowed': 'true',
                    'screenshot': (io.BytesIO(screenshot), 'turnitin.png'),
                },
                content_type='multipart/form-data',
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        with mock.patch('app.models.DB_PATH', str(self.db_path)):
            detail_response = client.get('/api/orders/HUMA-TEST-1')
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            detail_response.get_json()['feedback']['issue_types'],
            ['content_disorder', 'meaning_changed'],
        )
        saved_files = list(upload_dir.iterdir())
        self.assertEqual(len(saved_files), 1)
        self.assertEqual(saved_files[0].suffix, '.png')

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            feedback = RewriteFeedback.get_by_order_id(conn, 'HUMA-TEST-1')
            self.assertEqual(feedback['issue_type'], 'content_disorder')
            self.assertEqual(
                RewriteFeedback.get_issue_types(feedback),
                ['content_disorder', 'meaning_changed'],
            )
            self.assertEqual(feedback['external_score'], 42)
            self.assertEqual(feedback['contact_allowed'], 1)
            self.assertEqual(feedback['screenshot_file_key'], saved_files[0].name)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
