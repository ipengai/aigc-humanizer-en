import unittest
from unittest import mock


class AppFactoryRoutingTests(unittest.TestCase):
    def test_matching_huma_route_reuses_main_adapter_with_fallback(self):
        import app
        import app.extensions as extensions

        main_adapter = object()
        translation_adapter = object()
        calls = []

        def fake_create_humanizer(name, fallback_name=None):
            calls.append((name, fallback_name))
            if name == 'ai_text_humanizer':
                return main_adapter
            if name == 'lynote':
                return translation_adapter
            return object()

        with (
            mock.patch('app.models.init_db'),
            mock.patch('app.payment_adapter.create_payment_adapter', return_value=object()),
            mock.patch('app.humanizer.create_humanizer', side_effect=fake_create_humanizer),
            mock.patch('app.ai_detector.create_detector', return_value=lambda text, **kw: {}),
            mock.patch('app.helpers.recover_processing_orders'),
            mock.patch('threading.Thread.start'),
        ):
            flask_app = app.create_app()

        self.assertIsNotNone(flask_app)
        self.assertIs(extensions.rewrite_providers['huma'], main_adapter)
        self.assertIs(extensions.rewrite_providers['translation'], translation_adapter)
        self.assertEqual(calls.count(('ai_text_humanizer', 'llm_based')), 1)
        self.assertNotIn(('ai_text_humanizer', None), calls)


if __name__ == '__main__':
    unittest.main()
