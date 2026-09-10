import unittest

from app.humanizer.docx_alignment import align_docx_output
from app.humanizer.failover import FailoverHumanizer


def _paragraph(text, body_index, *, heading=False, style="Normal"):
    return {
        "text": text,
        "word_count": len(text.split()),
        "source_format": "docx",
        "body_index": body_index,
        "is_heading": heading,
        "style": style,
    }


class _VariableOutputProvider:
    def __init__(self, transform):
        self.transform = transform
        self.calls = []

    def humanize(self, text, mode=None, paragraphs=None):
        self.calls.append(text)
        return self.transform(text)


class _UnexpectedFallback:
    def __init__(self):
        self.calls = []

    def humanize(self, text, mode=None, paragraphs=None):
        self.calls.append(text)
        return text


class DocxHumaAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.paragraphs = [
            _paragraph("1.0 Introduction", 0, heading=True, style="Heading 2"),
            _paragraph(
                "Streaming services expanded rapidly in 2023. Creators now compare "
                "views, likes, and comments when they assess audience engagement.", 1,
            ),
            _paragraph("Key Observations:", 2, style="Normal (Web)"),
            _paragraph(
                "The United States sample has a wider spread in view counts. Its "
                "standard deviation is 1,642,439 compared with 1,567,133 in the UK.", 3,
            ),
            _paragraph(
                "Both samples contain viral outliers that sit far above their median "
                "values. Those observations should remain in the analysis.", 4,
            ),
        ]

    def test_missing_normal_style_label_is_restored_without_another_call(self):
        def remove_label(text):
            return "\n\n".join(
                value for value in text.split("\n\n")
                if value != "Key Observations:"
            )

        primary = _VariableOutputProvider(remove_label)
        fallback = _UnexpectedFallback()
        humanizer = FailoverHumanizer(primary, fallback)

        output, structured = humanizer.humanize_structured(
            "ignored", mode="median", paragraphs=self.paragraphs,
        )

        self.assertEqual(len(primary.calls), 1)
        self.assertEqual(fallback.calls, [])
        self.assertEqual(len(structured), len(self.paragraphs))
        self.assertEqual(structured[2]["text"], "Key Observations:")
        self.assertFalse(structured[2]["was_rewritten"])
        self.assertEqual(len(output.split("\n\n")), len(self.paragraphs))

    def test_extra_output_paragraph_is_merged_back_into_its_source_slot(self):
        def split_first_body(text):
            values = text.split("\n\n")
            values[1:2] = values[1].split(". ", 1)
            values[1] += "."
            return "\n\n".join(values)

        result = align_docx_output(
            self.paragraphs,
            split_first_body("\n\n".join(item["text"] for item in self.paragraphs)).split("\n\n"),
        )

        self.assertIsNotNone(result)
        self.assertEqual(len(result.paragraphs), len(self.paragraphs))
        self.assertIn("1_to_2", result.operations)
        self.assertIn("Creators now compare", result.paragraphs[1].text)

    def test_two_body_paragraphs_can_merge_within_contiguous_word_range(self):
        values = [item["text"] for item in self.paragraphs]
        values[3:5] = [" ".join(values[3:5])]

        result = align_docx_output(self.paragraphs, values)

        self.assertIsNotNone(result)
        self.assertEqual(len(result.paragraphs), len(self.paragraphs))
        self.assertIn("2_to_1", result.operations)
        self.assertIn("United States sample", result.paragraphs[3].text)
        self.assertIn("viral outliers", result.paragraphs[4].text)

    def test_equal_total_count_can_still_restore_one_label_and_one_split(self):
        values = [item["text"] for item in self.paragraphs]
        del values[2]
        values[1:2] = values[1].split(". ", 1)
        values[1] += "."

        result = align_docx_output(self.paragraphs, values)

        self.assertIsNotNone(result)
        self.assertEqual(len(values), len(self.paragraphs))
        self.assertIn("protected_restore", result.operations)
        self.assertIn("1_to_2", result.operations)
        self.assertEqual(result.paragraphs[2].text, "Key Observations:")

    def test_huma_thousands_spacing_is_treated_as_the_same_number(self):
        paragraphs = [
            _paragraph(
                "The UK median was 628336 and the US median was 491229, while "
                "their maximum values were 6673271 and 6059697.", 5,
            ),
        ]
        outputs = [
            "The UK median was 628 336 in the UK and 491 229 in the US. "
            "Their maximum values were 6 673 271 and 6 059 697."
        ]

        self.assertIsNotNone(align_docx_output(paragraphs, outputs))

    def test_merge_across_layout_gap_is_rejected(self):
        paragraphs = [
            _paragraph(
                "The first chart compares monthly values across both markets and "
                "retains the original 2023 observations.", 10,
            ),
            _paragraph(
                "The paragraph after the image explains a separate pattern in the "
                "2024 observations and should remain below that image.", 13,
            ),
        ]
        merged = [" ".join(item["text"] for item in paragraphs)]

        self.assertIsNone(align_docx_output(paragraphs, merged))

    def test_deleted_body_paragraph_is_rejected(self):
        outputs = [item["text"] for index, item in enumerate(self.paragraphs) if index != 3]

        self.assertIsNone(align_docx_output(self.paragraphs, outputs))

    def test_unrelated_extra_paragraph_is_rejected(self):
        outputs = [item["text"] for item in self.paragraphs]
        outputs.insert(2, "A completely unrelated marketing claim was inserted here.")

        self.assertIsNone(align_docx_output(self.paragraphs, outputs))


if __name__ == "__main__":
    unittest.main()
