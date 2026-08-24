import unittest

from app.helpers.segmenter import segment


def paragraph(text, *, heading=False, list_text=None):
    item = {
        "text": text,
        "word_count": len(text.split()),
        "is_heading": heading,
    }
    if list_text is not None:
        item["list_text"] = list_text
    return item


class SegmenterShortParagraphTests(unittest.TestCase):
    def test_short_body_is_aggregated_by_default(self):
        paragraphs = [
            paragraph("A normal body paragraph containing enough words for rewriting safely."),
            paragraph("Common specifications include:"),
        ]

        tasks = segment(paragraphs, mode="median", median_paras=3)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["type"], "rewrite")
        self.assertEqual(tasks[0]["paragraphs"], paragraphs)

    def test_heading_metadata_disables_enabled_short_paragraph_fallback(self):
        paragraphs = [
            paragraph("Formatted section", heading=True),
            paragraph("Short body text."),
        ]

        tasks = segment(
            paragraphs,
            mode="median",
            protect_short_paragraphs=True,
        )

        self.assertEqual([task["type"] for task in tasks], ["protected", "rewrite"])

    def test_enabled_short_paragraph_fallback_applies_without_headings(self):
        paragraphs = [paragraph("Possible title")]

        tasks = segment(
            paragraphs,
            mode="median",
            protect_short_paragraphs=True,
        )

        self.assertEqual(tasks[0]["type"], "protected")


class SegmenterMinimumCharactersTests(unittest.TestCase):
    @staticmethod
    def body(label):
        return paragraph(f"{label} " + ("word " * 15))

    def test_median_exceeds_three_paragraphs_until_minimum_chars(self):
        paragraphs = [self.body(f"body-{index}") for index in range(4)]

        tasks = segment(
            paragraphs,
            mode="median",
            median_paras=3,
            min_chars=300,
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["paragraphs"], paragraphs)
        self.assertGreaterEqual(len(tasks[0]["text"]), 300)

    def test_short_tail_merges_back_into_previous_rewrite_block(self):
        paragraphs = [self.body(f"paragraph-{index}") for index in range(9)]

        tasks = segment(
            paragraphs,
            mode="median",
            median_paras=3,
            min_chars=300,
        )

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["paragraphs"], paragraphs[:4])
        self.assertEqual(tasks[1]["paragraphs"], paragraphs[4:])
        self.assertTrue(all(len(task["text"]) >= 300 for task in tasks))


class SegmenterShortListTests(unittest.TestCase):
    def test_short_lists_attach_to_previous_block_past_median_limit(self):
        paragraphs = [
            paragraph("First body paragraph contains enough words to be rewritten normally."),
            paragraph("Second body paragraph contains enough words to be rewritten normally."),
            paragraph("Third body paragraph contains enough words to be rewritten normally."),
            paragraph("48.3 x 2.5 mm", list_text="•"),
            paragraph("48.3 x 3.0 mm", list_text="•"),
            paragraph("Following body paragraph contains enough words to start another block."),
        ]

        tasks = segment(
            paragraphs,
            mode="median",
            median_paras=3,
            min_chars=0,
            protect_short_lists=False,
        )

        self.assertEqual([task["type"] for task in tasks], ["rewrite", "rewrite"])
        self.assertEqual(tasks[0]["paragraphs"], paragraphs[:5])
        self.assertEqual(tasks[1]["paragraphs"], paragraphs[5:])

    def test_short_list_protection_can_be_enabled_independently(self):
        paragraphs = [
            paragraph("A normal body paragraph containing enough words for rewriting safely."),
            paragraph("48.3 x 2.5 mm", list_text="•"),
        ]

        tasks = segment(
            paragraphs,
            mode="median",
            protect_short_lists=True,
        )

        self.assertEqual([task["type"] for task in tasks], ["rewrite", "protected"])

    def test_general_short_protection_does_not_capture_short_lists(self):
        paragraphs = [
            paragraph("A normal body paragraph containing enough words for rewriting safely."),
            paragraph("48.3 x 2.5 mm", list_text="•"),
        ]

        tasks = segment(
            paragraphs,
            mode="median",
            protect_short_paragraphs=True,
            protect_short_lists=False,
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["paragraphs"], paragraphs)

    def test_low_mode_also_attaches_short_list_to_previous_paragraph(self):
        paragraphs = [
            paragraph("A normal body paragraph containing enough words for rewriting safely."),
            paragraph("48.3 x 2.5 mm", list_text="•"),
        ]

        tasks = segment(paragraphs, mode="low", protect_short_lists=False)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["paragraphs"], paragraphs)


if __name__ == "__main__":
    unittest.main()
