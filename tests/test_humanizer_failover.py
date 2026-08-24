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


if __name__ == "__main__":
    unittest.main()
