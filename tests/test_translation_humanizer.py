import unittest
from unittest import mock

from app.humanizer.rewrite_methods import LynoteTranslationHumanizer
from app.humanizer.translation_nmt import baidu_translate, translate_many


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

        def paragraph_nmt(values, source, target):
            calls.append((source, target, values))
            return list(values), "fake"

        source = "\n\n".join([
            "1. Introduction",
            "The first body paragraph contains evidence and a concrete claim.",
            "2. Method",
            "The second body paragraph explains how the evidence was collected.",
        ])
        with mock.patch(
            "app.humanizer.rewrite_methods.nmt_translate_many",
            side_effect=paragraph_nmt,
        ):
            rewritten = LynoteTranslationHumanizer().humanize(source)

        self.assertEqual(rewritten.split("\n\n"), source.split("\n\n"))
        self.assertEqual(len(calls), 2)

    def test_missing_paragraph_raises_so_router_can_upgrade_to_huma(self):
        source = "First paragraph with content.\n\nSecond paragraph with content."

        with mock.patch(
            "app.humanizer.rewrite_methods.nmt_translate_many",
            return_value=(["only one translated paragraph"], "fake"),
        ):
            with self.assertRaisesRegex(RuntimeError, "paragraph boundaries"):
                LynoteTranslationHumanizer().humanize(source)

    def test_translate_many_uses_baidu_line_results_as_paragraphs(self):
        with mock.patch(
            "app.humanizer.translation_nmt.baidu_translate",
            return_value="first translated\nsecond translated",
        ) as translate_mock:
            values, engine = translate_many(
                ["first source", "second source"], "en", "zh"
            )

        self.assertEqual(values, ["first translated", "second translated"])
        self.assertEqual(engine, "baidu")
        translate_mock.assert_called_once_with(
            "first source\nsecond source", "en", "zh"
        )


if __name__ == "__main__":
    unittest.main()
