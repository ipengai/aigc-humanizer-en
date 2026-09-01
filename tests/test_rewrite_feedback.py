import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admin
from flask import Flask, session
from app.extensions import limiter
from app.helpers.db import close_db
from app.models import Order, RewriteFeedback, User
from app.routes.main import _capture_order_attribution
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


def _create_completed_order(conn, order_id='HUMA-TEST-1'):
    Order.create_processing_order(
        conn,
        user_id=1,
        order_id=order_id,
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
        analysis_context={
            'input_type': 'upload',
            'traffic_source': 'zhihu.com',
            'utm_source': 'zhihu',
            'utm_medium': 'answer',
            'utm_campaign': 'huma-natural',
            'referrer_domain': 'zhihu.com',
        },
    )
    Order.update_result(
        conn,
        order_id,
        'Rewritten body is deliberately longer than before.',
        rewritten_score=28,
        original_score=75,
        rewritten_paragraphs=[
            {'text': 'Rewritten body is deliberately longer than before.', 'is_heading': False}
        ],
        detector_backend='sapling',
        humanizer_backend='llm_based',
        rewrite_metadata={
            'humanizer_backend': 'llm_based',
            'rewrite_method': 'llm',
            'rewrite_provider': 'deepseek',
            'rewrite_model': 'deepseek-v4-flash',
            'humanizer_primary': 'llm_based',
            'humanizer_fallback': 'ai_text_humanizer',
            'fallback_used': False,
            'fallback_block_count': 0,
            'rewrite_block_count': 1,
            'rewrite_pipeline_version': 'test-v1',
        },
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
            self.assertEqual(order['humanizer_backend'], 'llm_based')
            self.assertEqual(order['rewrite_method'], 'llm')
            self.assertEqual(order['rewrite_provider'], 'deepseek')
            self.assertEqual(order['rewrite_model'], 'deepseek-v4-flash')
            self.assertEqual(order['humanizer_primary'], 'llm_based')
            self.assertEqual(order['humanizer_fallback'], 'ai_text_humanizer')
            self.assertEqual(order['fallback_used'], 0)
            self.assertEqual(order['rewrite_block_count'], 1)
            self.assertEqual(order['rewrite_pipeline_version'], 'test-v1')
            self.assertEqual(order['input_type'], 'upload')
            self.assertEqual(order['traffic_source'], 'zhihu.com')
            self.assertEqual(order['utm_source'], 'zhihu')
            self.assertEqual(order['original_paragraph_count'], 2)
            self.assertEqual(order['rewritten_paragraph_count'], 1)
            self.assertEqual(order['protected_paragraph_count'], 0)
            self.assertIsNotNone(order['processing_duration_ms'])
            self.assertEqual(order['rewritten_word_count'], 7)
            self.assertAlmostEqual(order['word_count_change_ratio'], 1 / 6)
            self.assertEqual(order['original_heading_count'], 1)
            self.assertEqual(order['rewritten_heading_count'], 0)
            self.assertEqual(order['heading_count_changed'], 1)
            self.assertTrue(order['completed_at'])
        finally:
            conn.close()

    def test_landing_attribution_keeps_utm_and_only_referrer_domain(self):
        flask_app = Flask(__name__)
        flask_app.secret_key = 'test-secret'
        with flask_app.test_request_context(
            '/?utm_source=zhihu&utm_medium=answer&utm_campaign=huma',
            headers={'Referer': 'https://www.zhihu.com/question/123?private=value'},
        ):
            _capture_order_attribution()
            attribution = dict(session['order_attribution'])

        self.assertEqual(attribution['traffic_source'], 'zhihu')
        self.assertEqual(attribution['utm_medium'], 'answer')
        self.assertEqual(attribution['utm_campaign'], 'huma')
        self.assertEqual(attribution['referrer_domain'], 'www.zhihu.com')
        self.assertNotIn('private', str(attribution))

    def test_failed_order_keeps_planned_rewrite_dimensions(self):
        conn = _init_database(self.db_path)
        try:
            Order.create_processing_order(
                conn,
                user_id=1,
                order_id='HUMA-FAILED-1',
                original_text='A paragraph that is long enough for a rewrite attempt.',
                original_format='txt',
                original_filename=None,
                word_count=10,
                price=0,
                mode='low',
                paragraphs=[{'text': 'A paragraph that is long enough for a rewrite attempt.'}],
            )
            Order.update_rewrite_plan(
                conn,
                'HUMA-FAILED-1',
                {
                    'rewrite_method': 'api',
                    'rewrite_provider': 'ai-text-humanizer.com',
                    'humanizer_primary': 'ai_text_humanizer',
                    'humanizer_fallback': 'llm_based',
                    'rewrite_pipeline_version': 'test-v1',
                },
            )
            Order.mark_failed(
                conn, 'HUMA-FAILED-1',
                failure_stage='rewrite', failure_code='TimeoutError',
            )
            order = Order.get_by_order_id(conn, 'HUMA-FAILED-1')

            self.assertEqual(order['status'], 'failed')
            self.assertEqual(order['rewrite_method'], 'api')
            self.assertEqual(order['rewrite_provider'], 'ai-text-humanizer.com')
            self.assertEqual(order['humanizer_primary'], 'ai_text_humanizer')
            self.assertEqual(order['failure_stage'], 'rewrite')
            self.assertEqual(order['failure_code'], 'TimeoutError')
        finally:
            conn.close()

    def test_feedback_is_updated_per_order_instead_of_duplicated(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
            RewriteFeedback.upsert(
                conn, 1, 'HUMA-TEST-1', ['high_ai_score', 'details_lost'],
                external_score=46,
                comment='Still high', contact_allowed=True,
            )
            RewriteFeedback.upsert(
                conn, 1, 'HUMA-TEST-1', ['content_disorder', 'meaning_changed'],
                external_score=42,
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

    def test_order_list_marks_feedback_with_a_single_batched_query(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn, 'HUMA-TEST-1')
            _create_completed_order(conn, 'HUMA-TEST-2')
            RewriteFeedback.upsert(conn, 1, 'HUMA-TEST-2', ['high_ai_score'])

            # 批量查询只返回已有反馈的订单，空入参不触发 SQL。
            self.assertEqual(
                RewriteFeedback.get_order_ids_with_feedback(
                    conn, ['HUMA-TEST-1', 'HUMA-TEST-2']),
                {'HUMA-TEST-2'},
            )
            self.assertEqual(
                RewriteFeedback.get_order_ids_with_feedback(conn, []), set()
            )
        finally:
            conn.close()

        flask_app = Flask(__name__)
        flask_app.secret_key = 'test-secret'
        flask_app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        flask_app.register_blueprint(orders_bp)
        flask_app.teardown_appcontext(close_db)
        limiter.init_app(flask_app)
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = 1

        with mock.patch('app.models.DB_PATH', str(self.db_path)):
            response = client.get('/api/orders?per_page=50')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['total'], 2)
        flags = {o['order_id']: o['has_feedback'] for o in payload['orders']}
        self.assertEqual(flags, {'HUMA-TEST-1': False, 'HUMA-TEST-2': True})

    def test_admin_stats_include_system_and_user_reported_results(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
            RewriteFeedback.upsert(
                conn, 1, 'HUMA-TEST-1', ['high_ai_score', 'content_disorder'],
                external_score=42,
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

    def test_admin_orders_filter_rewrite_method_and_strength_separately(self):
        conn = _init_database(self.db_path)
        try:
            _create_completed_order(conn)
        finally:
            conn.close()

        with mock.patch.object(admin, 'DB_PATH', str(self.db_path)):
            admin.admin_app.config.update(TESTING=True)
            client = admin.admin_app.test_client()
            with client.session_transaction() as session:
                session['admin_authenticated'] = True

            matching = client.get(
                '/admin/api/orders?start=2026-01-01&end=2026-12-31'
                '&method=llm&mode=median'
            )
            wrong_method = client.get(
                '/admin/api/orders?start=2026-01-01&end=2026-12-31'
                '&method=api&mode=median'
            )

        self.assertEqual(matching.status_code, 200)
        matching_data = matching.get_json()
        self.assertEqual(matching_data['summary']['total_orders'], 1)
        self.assertEqual(matching_data['orders'][0]['rewrite_method'], 'llm')
        self.assertEqual(matching_data['orders'][0]['mode'], 'median')
        self.assertEqual(wrong_method.status_code, 200)
        self.assertEqual(wrong_method.get_json()['summary']['total_orders'], 0)

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
