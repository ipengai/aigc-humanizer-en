import unittest
from unittest import mock

from flask import Flask

from app.extensions import limiter
from app.helpers.preview import clear_preview_cache, extract_body_preview
from app.routes.rewrite import rewrite_bp


class PreviewExtractionTests(unittest.TestCase):
    def test_long_table_of_contents_is_skipped(self):
        text = """Table of Contents
Introduction ........ 1
Literature Review ........ 4
Research Methodology ........ 9
Results and Discussion ........ 15
Conclusion and Recommendations ........ 21
References and Appendices ........ 24

Introduction

This is the actual opening paragraph of the document body. It contains enough words to qualify as prose and should be the first material shown in the preview.
"""

        preview = extract_body_preview(text, max_words=200)

        self.assertTrue(preview.startswith('This is the actual opening paragraph'))
        self.assertNotIn('Table of Contents', preview)

    def test_multiline_title_page_is_skipped(self):
        text = """A Study of Humanized Academic Writing
John Smith
Example University
Department of Computer Science
September 2026
Supervisor Jane Doe

This is the actual first body paragraph. It has enough words to be recognized as meaningful prose rather than title-page metadata.
"""

        preview = extract_body_preview(text, max_words=200)

        self.assertTrue(preview.startswith('This is the actual first body paragraph'))

    def test_preview_is_capped_at_requested_word_count(self):
        text = ' '.join(f'word{i}' for i in range(250))

        preview = extract_body_preview(text, max_words=200)

        self.assertEqual(len(preview.split()), 200)

    def test_body_sentence_that_mentions_university_is_not_skipped(self):
        text = (
            'University students increasingly use digital writing assistants '
            'to revise academic work, which creates new questions for teachers.'
        )

        preview = extract_body_preview(text, max_words=200)

        self.assertEqual(preview, text)

    def test_front_matter_only_document_has_no_preview(self):
        text = 'Table of Contents\n\nKeywords\n\nCopyright'

        preview = extract_body_preview(text, max_words=200)

        self.assertEqual(preview, '')

    def test_structured_preview_skips_abstract_until_introduction(self):
        paragraphs = [
            {'text': 'Abstract', 'is_heading': True},
            {
                'text': 'This abstract summarizes the paper but should not be '
                        'used as the paid rewrite preview.',
            },
            {'text': '1 Introduction', 'is_heading': True},
            {
                'text': 'This introduction is the real body opening and should '
                        'be the first material shown to the user.',
            },
        ]

        preview = extract_body_preview(
            'flattened fallback should not win', paragraphs=paragraphs
        )

        self.assertTrue(preview.startswith('This introduction is the real body'))
        self.assertNotIn('This abstract summarizes', preview)


class RewritePreviewRouteTests(unittest.TestCase):
    def setUp(self):
        clear_preview_cache()
        self.app = Flask(__name__)
        self.app.secret_key = 'preview-test-secret'
        self.app.config['TESTING'] = True
        self.app.config['RATELIMIT_ENABLED'] = False
        limiter.init_app(self.app)
        self.app.register_blueprint(rewrite_bp)
        self.client = self.app.test_client()

    def _login_with_text(self, text):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 42
            sess['last_text'] = text

    @staticmethod
    def _result(original_score=70, rewritten_score=15):
        return {
            'humanized': 'A more natural rewritten paragraph.',
            'original_analysis': {'ai_score': original_score},
            'rewritten_analysis': {'ai_score': rewritten_score},
        }

    def test_route_uses_analyzed_session_text_not_posted_text(self):
        analyzed = (
            'This analyzed paragraph contains enough words to qualify as body '
            'content and should be used by the preview endpoint.'
        )
        self._login_with_text(analyzed)

        with mock.patch(
            'app.helpers.tasks.rewrite_and_analyze',
            return_value=self._result(),
        ) as rewrite:
            response = self.client.post(
                '/api/rewrite-preview',
                json={'text': 'attacker supplied chunk', 'mode': 'high'},
            )

        self.assertEqual(response.status_code, 200)
        rewrite.assert_called_once()
        rewritten_text = rewrite.call_args.args[0]
        self.assertIn('This analyzed paragraph', rewritten_text)
        self.assertNotIn('attacker supplied chunk', rewritten_text)
        self.assertEqual(rewrite.call_args.kwargs['mode'], 'high')

    def test_same_document_and_mode_reuses_cached_preview(self):
        analyzed = (
            'This analyzed paragraph contains enough words to qualify as body '
            'content and should produce one cached preview response.'
        )
        self._login_with_text(analyzed)

        with mock.patch(
            'app.helpers.tasks.rewrite_and_analyze',
            return_value=self._result(),
        ) as rewrite:
            first = self.client.post('/api/rewrite-preview', json={'mode': 'median'})
            second = self.client.post('/api/rewrite-preview', json={'mode': 'median'})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.get_json()['cached'])
        self.assertTrue(second.get_json()['cached'])
        rewrite.assert_called_once()

    def test_preview_requires_prior_analysis(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 42

        response = self.client.post('/api/rewrite-preview', json={'mode': 'median'})

        self.assertEqual(response.status_code, 400)
        self.assertIn('请先上传', response.get_json()['error'])


if __name__ == '__main__':
    unittest.main()
