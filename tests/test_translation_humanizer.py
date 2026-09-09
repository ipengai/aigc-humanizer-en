import unittest
from unittest import mock

from app.humanizer.rewrite_methods import LynoteTranslationHumanizer


class LynoteTranslationStructureTests(unittest.TestCase):
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
