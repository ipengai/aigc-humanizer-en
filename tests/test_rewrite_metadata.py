import unittest
from unittest import mock

from app.helpers.tasks import rewrite_and_analyze
from app.humanizer.events import REWRITE_FALLBACK_EVENT


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


class _EventOnlyHumanizer:
    """只上报结构化 event、文案里不含历史兜底标记的适配器。"""

    def __init__(self):
        self.primary = _Engine('ai_text_humanizer')
        self.fallback = _Engine(
            'llm_based', provider='deepseek', model='deepseek-v4-flash'
        )

    def humanize_structured(self, text, mode=None, paragraphs=None, progress_cb=None):
        progress_cb(
            stage='rewrite', block=2, total_blocks=2,
            message='block 2 handled by backup engine',
            event=REWRITE_FALLBACK_EVENT,
        )
        return 'Natural rewritten text.', [
            {'text': 'Natural rewritten text.', 'was_rewritten': True}
        ]


class _EventOnlyWholeDocumentHumanizer(_EventOnlyHumanizer):
    """整篇降级：event 上报且不带 block。"""

    def humanize_structured(self, text, mode=None, paragraphs=None, progress_cb=None):
        progress_cb(
            stage='rewrite', message='switching to backup engine',
            event=REWRITE_FALLBACK_EVENT,
        )
        return 'Natural rewritten text.', [
            {'text': 'Natural rewritten text.', 'was_rewritten': True}
        ]


def _run_with_engine(engine):
    import app.extensions as extensions

    detector = lambda text, stage=None: {'ai_score': 18, 'backend': 'sapling'}
    with mock.patch.object(extensions, 'humanizer_adapter', engine), \
            mock.patch.object(extensions, 'ai_detector', detector):
        return rewrite_and_analyze(
            'Original text for rewrite.',
            mode='median',
            paragraphs=[{'text': 'Original text for rewrite.'}],
            original_analysis={'ai_score': 75, 'backend': 'sapling'},
        )


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


    def test_fallback_is_detected_from_structured_event_without_message(self):
        """文案改掉后，降级埋点仍应通过 event 命中（不依赖中文文案）。"""
        metadata = _run_with_engine(_EventOnlyHumanizer())['rewrite_metadata']

        self.assertTrue(metadata['fallback_used'])
        self.assertEqual(metadata['fallback_block_count'], 1)
        self.assertEqual(metadata['rewrite_block_count'], 2)
        self.assertEqual(metadata['rewrite_method'], 'hybrid')
        self.assertEqual(metadata['humanizer_backend'], 'ai_text_humanizer->llm_based')

    def test_whole_document_fallback_is_detected_from_structured_event(self):
        metadata = _run_with_engine(
            _EventOnlyWholeDocumentHumanizer())['rewrite_metadata']

        self.assertTrue(metadata['fallback_used'])
        self.assertEqual(metadata['fallback_block_count'], 1)
        self.assertEqual(metadata['rewrite_method'], 'llm')
        self.assertEqual(metadata['humanizer_backend'], 'ai_text_humanizer->llm_based')


if __name__ == '__main__':
    unittest.main()
