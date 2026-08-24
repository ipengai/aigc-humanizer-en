import unittest
from unittest.mock import patch

from app.ai_detector.adapter import _make_api_detect


class ApiDetectorAdapterTests(unittest.TestCase):
    def setUp(self):
        self.text = "This is sufficiently long test content. " * 3

    def test_api_success_does_not_run_rule_detector(self):
        api_result = {
            "ai_score": 36.6,
            "risk_level": "Warning",
            "risk_description": "API result",
            "backend": "sapling",
            "details": {"raw_score": 0.366},
        }

        with patch(
            "app.ai_detector.adapter._api_detect", return_value=api_result
        ) as api_detect, patch(
            "app.ai_detector.adapter._rule_detect"
        ) as rule_detect:
            result = _make_api_detect("sapling")(self.text, stage="analyze")

        self.assertEqual(result, api_result)
        api_detect.assert_called_once_with(
            self.text, backend="sapling", stage="analyze"
        )
        rule_detect.assert_not_called()

    def test_api_failure_runs_rule_detector_as_fallback(self):
        api_error = {
            "error": "Timeout from sapling",
            "ai_score": 50,
            "risk_level": "Unknown",
            "backend": "sapling",
        }
        rule_result = {
            "ai_score": 23.6,
            "risk_level": "Warning",
            "risk_description": "Rule estimate",
        }

        with patch(
            "app.ai_detector.adapter._api_detect", return_value=api_error
        ) as api_detect, patch(
            "app.ai_detector.adapter._rule_detect", return_value=rule_result
        ) as rule_detect:
            result = _make_api_detect("sapling")(
                self.text, stage="rewrite_detect_rewritten"
            )

        self.assertEqual(result["ai_score"], 23.6)
        self.assertEqual(result["backend"], "sapling_fallback")
        self.assertEqual(result["error"], "Timeout from sapling")
        api_detect.assert_called_once_with(
            self.text, backend="sapling", stage="rewrite_detect_rewritten"
        )
        rule_detect.assert_called_once_with(
            self.text, stage="rewrite_detect_rewritten"
        )


if __name__ == "__main__":
    unittest.main()
