import unittest

import joblib
import numpy as np

from app.ai_detector.vendor.turnitin_fit_detector_v2 import random_forest_detector as detector
from scripts.explain_v2_detector import DEFAULT_METADATA, explain_text, forest_path_contributions


class V2AttributionTest(unittest.TestCase):
    @staticmethod
    def _sample_text(paragraphs=2):
        paragraph = (
            "This paragraph contains enough ordinary English words to exercise the local detector. "
            "It deliberately varies sentence length. Some sentences are short. Others include more "
            "detail so the model encounters several branches while calculating its probability. "
            "Readers can inspect punctuation choices, modal verbs, and repeated functional terms. "
            "The content itself is incidental because this test verifies numerical reconstruction."
        )
        return "\n\n".join(paragraph for _ in range(paragraphs))

    def test_path_contributions_exactly_reconstruct_forest_probability(self):
        text = self._sample_text()
        rows, _ = detector.paragraph_rows(text)
        supported = [row for row in rows if row["word_count"] >= 40]
        matrix = detector.context_matrix(supported)
        classifier = joblib.load(DEFAULT_METADATA.with_name("random_forest_detector.joblib"))

        baseline, contributions, probability = forest_path_contributions(classifier, matrix)

        np.testing.assert_allclose(baseline + contributions.sum(axis=1), probability, atol=1e-10)

    def test_document_contributions_reconstruct_weighted_detector_score(self):
        text = self._sample_text(3)
        result = explain_text(text, top=len(detector.FEATURE_NAMES))
        summary = result["summary"]
        detected = detector.detect(text, DEFAULT_METADATA)

        self.assertAlmostEqual(summary["risk_score"], detected["summary"]["risk_score"], places=12)
        self.assertLess(summary["reconstruction_error"], 1e-10)
        self.assertTrue(result["document_features"]["positive"])
        grouped_total = sum(
            row["contribution"]
            for direction in result["document_feature_groups"].values()
            for row in direction
        )
        self.assertAlmostEqual(
            summary["baseline_probability"] + grouped_total,
            summary["risk_score"],
            places=12,
        )
        self.assertTrue(result["paragraphs"])

    def test_long_document_uses_same_float_precision_as_sklearn(self):
        result = explain_text(self._sample_text(40), top=3)

        self.assertLess(result["summary"]["reconstruction_error"], 1e-10)

    def test_too_short_text_returns_explainable_empty_result(self):
        result = explain_text("This input is too short for the paragraph model.")

        self.assertIsNone(result["summary"]["risk_percent"])
        self.assertEqual(result["paragraphs"], [])


if __name__ == "__main__":
    unittest.main()
