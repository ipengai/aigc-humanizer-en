import unittest
from unittest import mock

from app.helpers.tasks import rewrite_and_analyze


class _Engine:
    def __init__(self, backend_label, provider=None, model=None):
        self.backend_label = backend_label
        self.provider = provider
        self.model = model


class _MixedHumanizer:
    def __init__(self):
        self.primary = _Engine('ai_text_humanizer')
        self.fallback = _Engine(
            'llm_based', provider='deepseek', model='deepseek-v4-flash'
        )

    def humanize_structured(self, text, mode=None, paragraphs=None, progress_cb=None):
        progress_cb(stage='rewrite', block=1, total_blocks=2, message='')
        progress_cb(
            stage='rewrite', block=2, total_blocks=2,
            message='第2块使用备用改写服务',
        )
        return 'Natural rewritten text.', [
            {'text': 'Natural rewritten text.', 'was_rewritten': True}
        ]


class RewriteMetadataTests(unittest.TestCase):
    def test_mixed_api_and_llm_execution_is_recorded_as_hybrid(self):
        import app.extensions as extensions

        detector = lambda text, stage=None: {
            'ai_score': 18,
            'backend': 'sapling',
        }
        with mock.patch.object(extensions, 'humanizer_adapter', _MixedHumanizer()), \
                mock.patch.object(extensions, 'ai_detector', detector):
            result = rewrite_and_analyze(
                'Original text for rewrite.',
                mode='median',
                paragraphs=[{'text': 'Original text for rewrite.'}],
                original_analysis={'ai_score': 75, 'backend': 'sapling'},
            )

        metadata = result['rewrite_metadata']
        self.assertEqual(metadata['rewrite_method'], 'hybrid')
        self.assertEqual(metadata['humanizer_backend'], 'ai_text_humanizer->llm_based')
        self.assertEqual(metadata['humanizer_primary'], 'ai_text_humanizer')
        self.assertEqual(metadata['humanizer_fallback'], 'llm_based')
        self.assertTrue(metadata['fallback_used'])
        self.assertEqual(metadata['fallback_block_count'], 1)
        self.assertEqual(metadata['rewrite_block_count'], 2)
        self.assertEqual(
            metadata['rewrite_provider'], 'ai-text-humanizer.com+deepseek'
        )
        self.assertEqual(metadata['rewrite_model'], 'deepseek-v4-flash')


if __name__ == '__main__':
    unittest.main()
