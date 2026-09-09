import unittest
from app.ai_detector import create_detector
from app.ai_detector.page_service import DetectorPageService


class DetectorPageModelTests(unittest.TestCase):
    def test_low_coverage_aggregation_matches_pipeline_detector(self):
        long_paragraph = " ".join(["evidence"] * 50)
        short_paragraph = " ".join(["context"] * 20)
        text = "\n\n".join([long_paragraph] + [short_paragraph] * 4)

        primary_detector = create_detector("turnitin_fit_detector_v2")
        page_result = DetectorPageService(primary_detector).detect(text)
        pipeline_result = primary_detector(text)

        summary = page_result["summary"]
        self.assertLess(summary["raw_coverage"], 60)
        self.assertEqual(summary["coverage"], 100)
        self.assertEqual(
            summary["aggregation_fallback"],
            "structured_short_paragraph_join",
        )
        self.assertAlmostEqual(
            summary["risk_percent"], pipeline_result["risk_percent"], places=1
        )

    def test_page_attribution_exactly_reconstructs_score(self):
        text = (
            "The research team compares each event with the observed record and "
            "checks where the model works well. The same team then reviews the "
            "water data, soil data, and flow data before it uses the model in a "
            "new area because the average result can hide an important error."
        )

        result = DetectorPageService(
            create_detector("turnitin_fit_detector_v2")
        ).detect(text)
        attribution = result["summary"]["attribution"]

        self.assertEqual(
            attribution["method"], "exact_random_forest_decision_path"
        )
        self.assertLess(attribution["reconstruction_error"], 1e-10)
        self.assertTrue(attribution["groups"])

    def test_page_uses_the_injected_primary_detector(self):
        primary_detector = create_detector("turnitin_fit_detector_v2")
        service = DetectorPageService(primary_detector)
        self.assertIs(service._detect, primary_detector)
        runtime = primary_detector.runtime()
        service.detect(
            "The team checks each event against the field record and reviews "
            "the water data before it trusts the model in a new area. This "
            "process lets the team find errors that an average result can hide "
            "and shows when the model still needs more evidence from the field."
        )
        primary_detector(
            "The team checks each event against the field record and reviews "
            "the water data before it trusts the model in a new area. This "
            "process lets the team find errors that an average result can hide "
            "and shows when the model still needs more evidence from the field."
        )
        self.assertEqual(runtime.load_count, 1)


if __name__ == "__main__":
    unittest.main()
