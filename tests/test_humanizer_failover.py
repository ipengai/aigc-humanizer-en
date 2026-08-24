import unittest

from app.humanizer.failover import FailoverHumanizer


class StubHumanizer:
    def __init__(self, label, fail_on=None):
        self.label = label
        self.fail_on = fail_on
        self.calls = []

    def humanize(self, text, mode=None, paragraphs=None):
        self.calls.append(text)
        if text == self.fail_on:
            raise RuntimeError(f"{self.label} failed")
        return f"{self.label}:{text}"

    def humanize_structured(self, text, mode=None, paragraphs=None,
                            progress_cb=None):
        return self.humanize(text, mode=mode, paragraphs=paragraphs), []


class ParagraphBatchStub(StubHumanizer):
    supports_short_block_batching = True

    def __init__(self, label, collapse_batch=False):
        super().__init__(label)
        self.collapse_batch = collapse_batch

    def humanize(self, text, mode=None, paragraphs=None):
        self.calls.append(text)
        values = text.split("\n\n")
        if self.collapse_batch and len(values) > 1:
            return f"{self.label}:collapsed output"
        return "\n\n".join(f"{self.label}:{value}" for value in values)


class BlockFailoverTests(unittest.TestCase):
    def test_fallback_handles_only_failed_block_then_primary_resumes(self):
        primary = StubHumanizer("primary", fail_on="block 16")
        fallback = StubHumanizer("deepseek")
        humanizer = FailoverHumanizer(primary, fallback)
        paragraphs = [
            {"text": f"block {index:02d}", "word_count": 2}
            for index in range(1, 19)
        ]
        progress = []

        output, structured = humanizer.humanize_structured(
            "ignored",
            mode="low",
            paragraphs=paragraphs,
            progress_cb=lambda **values: progress.append(values),
        )

        self.assertEqual(primary.calls, [f"block {index:02d}" for index in range(1, 19)])
        self.assertEqual(fallback.calls, ["block 16"])
        output_blocks = output.split("\n\n")
        self.assertEqual(output_blocks[14], "primary:block 15")
        self.assertEqual(output_blocks[15], "deepseek:block 16")
        self.assertEqual(output_blocks[16], "primary:block 17")
        self.assertEqual(len(structured), 18)
        switch_events = [item for item in progress if item.get("message")]
        self.assertEqual(switch_events[0]["block"], 16)

    @staticmethod
    def _short_sections_with_heading():
        return [
            {
                "text": "First paragraph " + ("alpha " * 20).strip(),
                "word_count": 22,
            },
            {
                "text": "Second paragraph " + ("beta " * 20).strip(),
                "word_count": 22,
            },
            {
                "text": "Protected section title",
                "word_count": 3,
                "is_heading": True,
                "style": "Heading 2",
            },
            {
                "text": "Third paragraph " + ("gamma " * 18).strip(),
                "word_count": 20,
            },
        ]

    def test_short_blocks_share_primary_request_but_heading_stays_between(self):
        primary = ParagraphBatchStub("primary")
        fallback = StubHumanizer("deepseek")
        humanizer = FailoverHumanizer(primary, fallback)

        output, structured = humanizer.humanize_structured(
            "ignored",
            mode="median",
            paragraphs=self._short_sections_with_heading(),
        )

        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(primary.calls[0].split("\n\n")), 3)
        self.assertEqual(fallback.calls, [])
        self.assertEqual(len(structured), 3)
        self.assertEqual(structured[0]["text"].count("primary:"), 2)
        self.assertTrue(structured[1]["is_heading"])
        self.assertEqual(structured[1]["text"], "Protected section title")
        self.assertEqual(structured[2]["text"].count("primary:"), 1)
        output_parts = output.split("\n\n")
        self.assertEqual(output_parts[2], "Protected section title")

    def test_collapsed_batch_is_discarded_and_short_blocks_fall_back(self):
        primary = ParagraphBatchStub("primary", collapse_batch=True)
        fallback = StubHumanizer("deepseek")
        humanizer = FailoverHumanizer(primary, fallback)

        output, structured = humanizer.humanize_structured(
            "ignored",
            mode="median",
            paragraphs=self._short_sections_with_heading(),
        )

        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(len(fallback.calls), 2)
        self.assertEqual(len(structured), 3)
        self.assertTrue(structured[1]["is_heading"])
        self.assertEqual(structured[1]["text"], "Protected section title")
        output_parts = output.split("\n\n")
        self.assertEqual(output_parts[2], "Protected section title")


if __name__ == "__main__":
    unittest.main()
