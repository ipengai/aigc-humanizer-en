import unittest
from unittest import mock

from app.humanizer.rewrite_methods import LynoteTranslationHumanizer
from app.humanizer.translation_nmt import baidu_translate


class LynoteTranslationStructureTests(unittest.TestCase):
    def test_baidu_preserves_all_multiline_translation_results(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "trans_result": [
                {"dst": "first translated line"},
                {"dst": "[[ZXQ_000001_QXZ]]"},
                {"dst": "second translated line"},
            ]
        }
        with (
            mock.patch("app.humanizer.translation_nmt._baidu_appid", return_value="id"),
            mock.patch("app.humanizer.translation_nmt._baidu_key", return_value="key"),
            mock.patch("app.humanizer.translation_nmt.requests.get", return_value=response),
            mock.patch("app.humanizer.translation_nmt.time.sleep"),
        ):
            translated = baidu_translate(
                "first\n[[ZXQ_000001_QXZ]]\nsecond", "en", "zh"
            )

        self.assertEqual(
            translated,
            "first translated line\n[[ZXQ_000001_QXZ]]\nsecond translated line",
        )

    def test_round_trip_preserves_blank_line_paragraph_boundaries(self):
        calls = []

        def collapsed_nmt(text, source, target):
            calls.append((source, target, text))
            # Simulate an NMT provider that collapses every line break while
            # leaving non-language marker tokens unchanged.
            return " ".join(text.split()), "fake"

        source = "\n\n".join([
            "1. Introduction",
            "The first body paragraph contains evidence and a concrete claim.",
            "2. Method",
            "The second body paragraph explains how the evidence was collected.",
        ])
        with mock.patch(
            "app.humanizer.rewrite_methods.nmt_translate",
            side_effect=collapsed_nmt,
        ):
            rewritten = LynoteTranslationHumanizer().humanize(source)

        self.assertEqual(rewritten.split("\n\n"), source.split("\n\n"))
        self.assertEqual(len(calls), 2)

    def test_missing_marker_raises_so_router_can_upgrade_to_huma(self):
        source = "First paragraph with content.\n\nSecond paragraph with content."

        with mock.patch(
            "app.humanizer.rewrite_methods.nmt_translate",
            return_value=("translation without the separator", "fake"),
        ):
            with self.assertRaisesRegex(RuntimeError, "paragraph markers"):
                LynoteTranslationHumanizer().humanize(source)


if __name__ == "__main__":
    unittest.main()
