import unittest

from app.pipeline.router import (
    LEGACY_POLICY,
    choose_document_policy,
    choose_rewrite_route,
)


class PipelineRouterTests(unittest.TestCase):
    def test_legacy_policy_preserves_whole_document_mode(self):
        result = choose_document_policy(80, 0.99, 3000, 20,
                                        configured_policy=LEGACY_POLICY)
        self.assertFalse(result["use_block_routing"])
        self.assertEqual(result["policy"], LEGACY_POLICY)

    def test_document_bands(self):
        self.assertEqual(choose_document_policy(10, .95, 100)["risk_band"], "protect")
        self.assertEqual(choose_document_policy(30, .95, 100)["risk_band"], "low_cost_first")
        self.assertEqual(choose_document_policy(60, .95, 2000, 8)["risk_band"], "low_cost_first")
        self.assertEqual(choose_document_policy(60.1, .95, 2000, 8)["risk_band"], "segment_route")
        self.assertEqual(choose_document_policy(60.1, .95, 200, 2)["risk_band"], "huma_first")

    def test_block_routes(self):
        self.assertEqual(choose_rewrite_route(10, .95)["action"], "protect")
        self.assertEqual(choose_rewrite_route(30, .95)["action"], "translation")
        self.assertEqual(choose_rewrite_route(60, .95)["action"], "translation")
        self.assertEqual(choose_rewrite_route(60.1, .95)["action"], "huma")
        self.assertEqual(choose_rewrite_route(60, .40)["action"], "needs_review")


if __name__ == "__main__":
    unittest.main()
