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

    def test_cross_block_sends_heading_in_stream_and_force_restores_it(self):
        # P1 语义（median/high 跨边界）：标题不再是硬边界，随正文进同一个
        # 改写块（一次请求），输出段数一致时按段位强制还原为原标题文本。
        primary = ParagraphBatchStub("primary")
        fallback = StubHumanizer("deepseek")
        humanizer = FailoverHumanizer(primary, fallback)

        output, structured = humanizer.humanize_structured(
            "ignored",
            mode="median",
            paragraphs=self._short_sections_with_heading(),
        )

        self.assertEqual(len(primary.calls), 1)
        # 标题行随块发送 → 请求含 4 个 \n\n 段（正文+标题+正文）
        self.assertEqual(len(primary.calls[0].split("\n\n")), 4)
        self.assertEqual(fallback.calls, [])
        # 单个跨边界改写块 → 1 个 structured 条目
        self.assertEqual(len(structured), 1)
        # 输出与 structured 里标题位置被强制还原为原文
        output_segs = output.split("\n\n")
        self.assertEqual(len(output_segs), 4)
        self.assertEqual(output_segs[2], "Protected section title")
        self.assertEqual(structured[0]["text"].split("\n\n")[2],
                         "Protected section title")
        self.assertTrue(structured[0]["was_rewritten"])

    def test_docx_cross_block_returns_one_structured_item_per_source_paragraph(self):
        primary = ParagraphBatchStub("primary")
        humanizer = FailoverHumanizer(primary, StubHumanizer("fallback"))
        paragraphs = self._short_sections_with_heading()
        for index, item in enumerate(paragraphs):
            item.update({
                "source_format": "docx",
                "body_index": index,
                "style": "Heading 2" if item.get("is_heading") else "Normal",
            })

        output, structured = humanizer.humanize_structured(
            "ignored", mode="median", paragraphs=paragraphs,
        )

        self.assertEqual(len(output.split("\n\n")), 4)
        self.assertEqual(len(structured), 4)
        self.assertEqual(
            [item["source_body_indexes"] for item in structured],
            [[0], [1], [2], [3]],
        )
        self.assertFalse(structured[2]["was_rewritten"])
        self.assertEqual(structured[2]["text"], "Protected section title")

    def test_collapsed_cross_block_degrades_to_median_blocks_heading_protected(self):
        # 主服务返回段数失配（折叠）→ 无法安全定位标题 → 该区间按 median 重切：
        # 标题恢复硬边界，正文按 3 段聚合重试主服务（最坏≈旧 median 行为，标题零改动）。
        primary = ParagraphBatchStub("primary", collapse_batch=True)
        fallback = StubHumanizer("deepseek")
        humanizer = FailoverHumanizer(primary, fallback)

        output, structured = humanizer.humanize_structured(
            "ignored",
            mode="median",
            paragraphs=self._short_sections_with_heading(),
        )

        # 1 次跨边界请求失败 + median 重切后 2 个正文块（标题段不改写）
        self.assertEqual(len(primary.calls), 3)
        self.assertEqual(fallback.calls, [])
        self.assertEqual(len(structured), 3)
        # 标题仍作为保护段原样保留在正确位置
        self.assertTrue(structured[1]["is_heading"])
        self.assertFalse(structured[1]["was_rewritten"])
        self.assertEqual(structured[1]["text"], "Protected section title")
        output_parts = output.split("\n\n")
        self.assertEqual(len(output_parts), 3)
        self.assertEqual(output_parts[1], "Protected section title")
        # 正文段已改写
        self.assertEqual(structured[0]["text"].count("primary:"), 1)
        self.assertEqual(structured[2]["text"].count("primary:"), 1)

    def test_docx_structure_fallback_preserves_too_short_body_without_failing(self):
        class RejectShortCollapsingProvider(ParagraphBatchStub):
            def humanize(self, text, mode=None, paragraphs=None):
                self.calls.append(text)
                if len(text.strip()) < 300:
                    raise RuntimeError("upstream rejects short input")
                if "\n\n" in text:
                    return "collapsed aggregate output"
                return f"primary:{text}"

        primary = RejectShortCollapsingProvider("primary")
        humanizer = FailoverHumanizer(primary, StubHumanizer("fallback"))
        paragraphs = [
            {
                "text": "Long paragraph " + ("evidence " * 40),
                "source_format": "docx", "body_index": 0,
                "style": "Normal", "was_rewritten": True,
            },
            {
                "text": "Short translated paragraph.",
                "source_format": "docx", "body_index": 1,
                "style": "Normal", "was_rewritten": True,
            },
        ]

        output, structured = humanizer.humanize_structured(
            "ignored", mode="median", paragraphs=paragraphs,
        )

        self.assertIn("primary:Long paragraph", output)
        self.assertIn("Short translated paragraph.", output)
        self.assertEqual(len(primary.calls), 2)
        self.assertEqual(len(structured), 2)
        self.assertTrue(structured[1]["was_rewritten"])


if __name__ == "__main__":
    unittest.main()
