import unittest
from unittest import mock

from flask import Flask

from app.extensions import limiter
from app.pipeline.orchestrator import RewriteOrchestrator
from app.routes.analysis import analysis_bp
from app.humanizer.adapter import HumanizerAdapter
from app.humanizer.llm_based import LLMBasedHumanizer
from app.ai_detector import create_detector
from app.helpers.tasks import rewrite_and_analyze
from app.pipeline.attribution_policy import feature_retry_guidance


class _Provider(HumanizerAdapter):
    def __init__(self, label):
        self.backend_label = label
        self.calls = []

    def humanize_structured(self, text, mode=None, paragraphs=None, progress_cb=None):
        self.calls.append(("structured", text, mode))
        if paragraphs is not None:
            return self._humanize_segmented_structured(
                mode or "low", paragraphs,
                lambda block: self.humanize(block, mode=mode),
                progress_cb=progress_cb,
            )
        return self.humanize(text, mode=mode), paragraphs or []

    def humanize(self, text, mode=None, paragraphs=None):
        self.calls.append(("block", text, mode))
        return f"{self.backend_label}: {text}"


class PipelineScenarioTests(unittest.TestCase):
    def test_feature_retry_guidance_reports_the_actual_missed_ranges(self):
        guidance = feature_retry_guidance(
            {
                "function_word_ratio": 0.28,
                "type_token_ratio": 0.84,
                "hapax_ratio": 0.73,
                "average_word_length": 5.62,
                "dash_per_sentence": 0.5,
            },
            ["function_word_ratio", "type_token_ratio", "hapax_ratio"],
            ["average_word_length", "dash_per_sentence"],
            previous_profile={"average_word_length": 5.10},
        )

        joined = " ".join(guidance)
        self.assertIn("0.28", joined)
        self.assertIn("0.84", joined)
        self.assertIn("0.73", joined)
        self.assertIn("5.62", joined)
        self.assertIn("zero dashes", joined)

    def test_targeted_llm_prompt_contains_edit_protection_and_retry_sections(self):
        llm = LLMBasedHumanizer(api_key="test", provider="deepseek")
        captured = {}

        def fake_call(text, system_prompt=None):
            captured["prompt"] = system_prompt
            return text

        llm._call_api = fake_call
        llm._rewrite_with_chunking = lambda text, callback, **kwargs: callback(text)
        llm.humanize_targeted(
            "A sufficiently long source paragraph for the prompt contract test.",
            ["Vary sentence structure carefully."],
            ["2026"],
            protection_directives=["Do not introduce em dashes."],
            retry_guidance=["Produce a materially different revision."],
            target_features=["function_word_ratio", "type_token_ratio"],
        )

        prompt = captured["prompt"]
        self.assertIn("diagnosis-specific editing instructions", prompt)
        self.assertIn("Protect these characteristics", prompt)
        self.assertIn("Do not introduce em dashes", prompt)
        self.assertIn("Revision feedback", prompt)
        self.assertIn("materially different", prompt)
        self.assertIn("Measurable output constraints", prompt)
        self.assertIn("roughly one word in three", prompt)
        self.assertIn("compact source vocabulary as anchors", prompt)
        self.assertIn("long, source, paragraph, prompt", prompt)
        self.assertIn("source's atomic claims", prompt)
        self.assertIn("do not optimize for lexical variety", prompt)
        self.assertIn("2026", prompt)

    def test_legacy_policy_does_not_route_blocks(self):
        provider = _Provider("legacy")
        detector = mock.Mock()
        orchestrator = RewriteOrchestrator(provider, detector)
        result = orchestrator.run(
            "original", mode="low", paragraphs=[{"text": "original"}],
            original_analysis={"ai_score": 70},
            policy="legacy_whole_document",
        )
        self.assertEqual(result["routing_policy"], "legacy_whole_document")
        self.assertEqual(provider.calls[0][0], "structured")
        detector.assert_not_called()

    def test_long_high_risk_document_routes_each_block(self):
        huma = _Provider("huma")
        translation = _Provider("translation")

        def detect(text, stage=None):
            if stage == "route_block_recheck":
                return {"backend": "turnitin_fit_detector_v2", "risk_percent": 10, "coverage": 1}
            if stage == "route_block" and "MEDIUM" in text:
                return {"backend": "turnitin_fit_detector_v2", "risk_percent": 30, "coverage": 1}
            return {"backend": "turnitin_fit_detector_v2", "risk_percent": 70, "coverage": 1}

        block_a = "HIGH " + ("word " * 500)
        block_b = "MEDIUM " + ("word " * 500)
        orchestrator = RewriteOrchestrator(
            huma, detect, providers={"huma": huma, "translation": translation}
        )
        result = orchestrator.run(
            block_a + "\n\n" + block_b,
            mode="low",
            paragraphs=[{"text": block_a}, {"text": block_b}],
            original_analysis={"backend": "turnitin_fit_detector_v2", "risk_percent": 70, "coverage": 1},
            policy="risk_band_segmented",
        )
        self.assertEqual(result["route"]["risk_band"], "segment_route")
        self.assertEqual(len(huma.calls), 1)
        self.assertEqual(len(translation.calls), 1)
        self.assertEqual([s["action"] for s in result["steps"]], ["huma", "translation"])

    def test_translation_recheck_upgrades_to_huma(self):
        huma = _Provider("huma")
        translation = _Provider("translation")

        def detect(text, stage=None):
            if stage == "route_block_recheck":
                return {"backend": "v2", "risk_percent": 65, "coverage": 1}
            return {"backend": "v2", "risk_percent": 30, "coverage": 1}

        text = "word " * 1100
        result = RewriteOrchestrator(
            huma, detect, providers={"huma": huma, "translation": translation}
        ).run(text, mode="low", paragraphs=[{"text": text}],
              original_analysis={"risk_percent": 70, "coverage": 1},
              policy="risk_band_segmented")
        self.assertEqual(len(translation.calls), 1)
        self.assertEqual(len(huma.calls), 1)
        self.assertEqual(result["steps"][0]["action"], "huma")
        self.assertEqual(result["steps"][0]["status"], "upgraded_after_recheck")

    def test_low_risk_block_is_preserved_without_a_provider(self):
        huma = _Provider("huma")

        def detect(text, stage=None):
            return {"backend": "v2", "risk_percent": 10, "coverage": 1}

        text = "word " * 1100
        result = RewriteOrchestrator(
            huma, detect, providers={"huma": huma}
        ).run(
            text, mode="low", paragraphs=[{"text": text}],
            original_analysis={"risk_percent": 70, "coverage": 1},
            policy="risk_band_segmented",
        )
        self.assertEqual(result["humanized"].strip(), text.strip())
        self.assertEqual(result["steps"][0]["action"], "protect")
        self.assertEqual(result["steps"][0]["status"], "protected_low_risk")
        self.assertFalse(huma.calls)

    def test_unscorable_short_block_inherits_document_score(self):
        huma = _Provider("huma")

        def detect(text, stage=None):
            return {
                "backend": "v2", "risk_percent": None, "coverage": 0,
                "error_code": "v2_insufficient_supported_text",
            }

        paragraphs = [{"text": f"short paragraph {index} with ordinary words"}
                      for index in range(6)]
        text = "\n\n".join(item["text"] for item in paragraphs)
        result = RewriteOrchestrator(
            huma, detect, providers={"huma": huma}
        ).run(
            text, mode="low", paragraphs=paragraphs,
            original_analysis={"risk_percent": 70, "coverage": 1},
            policy="risk_band_segmented",
        )
        self.assertEqual(len(huma.calls), 6)
        self.assertTrue(all(
            step["detector_source"] == "document_fallback"
            for step in result["steps"]
        ))

    def test_medium_document_upgrades_after_whole_document_recheck(self):
        huma = _Provider("huma")
        translation = _Provider("translation")

        def detect(text, stage=None):
            self.assertEqual(stage, "route_document_recheck")
            return {"backend": "v2", "risk_percent": 55, "coverage": 1}

        paragraphs = [
            {"text": "first paragraph " + "word " * 60},
            {"text": "second paragraph " + "word " * 60},
        ]
        text = "\n\n".join(item["text"] for item in paragraphs)
        result = RewriteOrchestrator(
            huma, detect, providers={"huma": huma, "translation": translation}
        ).run(
            text, mode="low", paragraphs=paragraphs,
            original_analysis={"backend": "v2", "risk_percent": 30, "coverage": 1},
            policy="risk_band_segmented",
        )
        self.assertEqual(len(translation.calls), 2)
        self.assertEqual(len(huma.calls), 2)
        self.assertEqual(result["route"]["recheck_score"], 55)
        self.assertTrue(all(
            step["action_chain"] == ["translation", "huma"]
            for step in result["steps"]
        ))

    def test_translation_fallback_attempt_does_not_skip_required_huma_upgrade(self):
        huma = _Provider("huma")

        class FailingTranslation(_Provider):
            def humanize(self, text, mode=None, paragraphs=None):
                self.calls.append(("block", text, mode))
                raise RuntimeError("translation markers were lost")

        translation = FailingTranslation("translation")

        def detect(text, stage=None):
            self.assertEqual(stage, "route_document_recheck")
            return {"backend": "v2", "risk_percent": 55, "coverage": 1}

        text = "source " + ("word " * 80)
        result = RewriteOrchestrator(
            huma, detect, providers={"huma": huma, "translation": translation}
        ).run(
            text, mode="median", paragraphs=[{"text": text}],
            original_analysis={"backend": "v2", "risk_percent": 30, "coverage": 1},
            policy="risk_band_segmented",
        )

        # One Huma call is the translation fallback; the second is the
        # mandatory post-recheck upgrade.  The old skip logic made only one.
        block_calls = [call for call in huma.calls if call[0] == "block"]
        self.assertEqual(len(block_calls), 2)
        self.assertEqual(result["steps"][0]["status"], "upgraded_after_recheck")

    def test_real_v2_joins_documents_made_of_short_paragraphs(self):
        paragraph = (
            "Writers compare evidence and explain the limits of each claim "
            "before they decide how strongly the conclusion should be stated."
        )
        result = create_detector("turnitin_fit_detector_v2")(
            "\n\n".join([paragraph] * 5), stage="test"
        )
        self.assertIsNotNone(result["risk_percent"])
        self.assertEqual(result["coverage"], 1.0)
        self.assertEqual(
            result["aggregation_fallback"],
            "structured_short_paragraph_join",
        )

    def test_real_v2_reaggregates_partial_coverage_below_sixty_percent(self):
        long_paragraph = " ".join(["evidence"] * 50)
        short_paragraph = " ".join(["context"] * 20)
        detect = create_detector("turnitin_fit_detector_v2")

        result = detect(
            "\n\n".join([long_paragraph] + [short_paragraph] * 4),
            stage="test_partial_coverage",
        )

        self.assertLess(result["raw_coverage"], 0.60)
        self.assertEqual(result["effective_coverage"], 1.0)
        self.assertEqual(
            result["aggregation_strategy"],
            "structured_short_paragraph_join",
        )

    def test_real_v2_model_is_loaded_once_for_repeated_detection(self):
        detect = create_detector("turnitin_fit_detector_v2")
        text = "Writers compare concrete evidence before reaching a careful conclusion. " * 8

        detect(text, stage="first")
        detect(text, stage="second")

        self.assertEqual(detect.runtime().load_count, 1)

    def test_real_v2_uses_bundled_model_without_path_configuration(self):
        import config

        with mock.patch.object(
            config, "V2_DETECTOR_MODEL", "/missing/legacy-model.json",
            create=True,
        ):
            detect = create_detector("turnitin_fit_detector_v2")
            result = detect(
                "Writers compare concrete evidence before reaching a careful conclusion. " * 8,
                stage="bundled_model_test",
            )

        self.assertEqual(result["backend"], "turnitin_fit_detector_v2")
        self.assertIsNotNone(result["risk_percent"])

    def test_targeted_second_pass_rewrites_two_blocks_and_stops(self):
        class _TargetDetector:
            def __init__(self):
                self.rechecks = 0

            def __call__(self, text, stage=None):
                self.rechecks += 1
                return {"backend": "v2", "risk_percent": 15, "coverage": 1}

            def explain_blocks(self, texts, top=3):
                scores = [85, 70, 30]
                return [
                    {
                        "source_index": index,
                        "word_count": len(text.split()),
                        "risk_percent": scores[index],
                        "top_feature_groups": [{
                            "feature_group": "sentence_length_std",
                            "contribution_pp": 5,
                        }],
                        "protect_feature_groups": [{
                            "feature_group": "dash_per_sentence",
                            "contribution_pp": -6,
                        }],
                        "text": text,
                    }
                    for index, text in enumerate(texts)
                ]

        class _TargetLLM(_Provider):
            def humanize_targeted(self, text, directives, protected_values,
                                  protection_directives=None, retry_guidance=None,
                                  target_features=None):
                self.calls.append((
                    "targeted", text, directives, protection_directives,
                    retry_guidance, target_features,
                ))
                return text.replace("ordinary", "specific")

        detector = _TargetDetector()
        llm = _TargetLLM("llm")
        orchestrator = RewriteOrchestrator(
            _Provider("huma"), detector, providers={"llm": llm}
        )
        paragraphs = [
            {
                "text": f"Block {index} contains ordinary language " + "word " * 55,
                "block_id": f"b{index}",
                "was_rewritten": True,
            }
            for index in range(3)
        ]
        progress = []

        result = orchestrator.run_targeted_second_pass(
            "\n\n".join(item["text"] for item in paragraphs),
            paragraphs,
            {"risk_percent": 35, "coverage": 1},
            progress_cb=lambda **value: progress.append(value),
        )

        self.assertEqual(len(llm.calls), 2)
        self.assertIn("Do not introduce em dashes", llm.calls[0][3][0])
        self.assertEqual(result["summary"]["rounds"], 1)
        self.assertTrue(result["summary"]["achieved"])
        self.assertEqual(result["analysis"]["risk_percent"], 15)
        self.assertEqual(progress[0]["stage"], "targeted_rewrite")

    def test_targeted_second_pass_rejects_a_candidate_that_scores_worse(self):
        class _TargetDetector:
            def __call__(self, text, stage=None):
                return {"backend": "v2", "risk_percent": 75, "coverage": 1}

            def explain_blocks(self, texts, top=3):
                return [{
                    "source_index": 0,
                    "word_count": len(texts[0].split()),
                    "risk_percent": 80,
                    "top_feature_groups": [{
                        "feature_group": "function_word_ratio",
                        "contribution_pp": 8,
                    }],
                    "text": texts[0],
                }]

        class _TargetLLM(_Provider):
            def humanize_targeted(self, text, directives, protected_values,
                                  protection_directives=None, retry_guidance=None,
                                  target_features=None):
                self.calls.append((
                    "targeted", text, directives, protection_directives,
                    retry_guidance, target_features,
                ))
                return text.replace("ordinary", "specific")

        detector = _TargetDetector()
        llm = _TargetLLM("llm")
        orchestrator = RewriteOrchestrator(
            _Provider("huma"), detector, providers={"llm": llm}
        )
        original = "This ordinary block contains " + "word " * 55
        structured = [{
            "text": original,
            "block_id": "b0",
            "was_rewritten": True,
        }]

        result = orchestrator.run_targeted_second_pass(
            original,
            structured,
            {"risk_percent": 32, "coverage": 1},
        )

        self.assertEqual(len(llm.calls), 2)
        self.assertIsNone(llm.calls[0][4])
        self.assertIn("materially different", llm.calls[1][4][0])
        self.assertEqual(result["humanized"], original)
        self.assertEqual(result["analysis"]["risk_percent"], 32)
        self.assertEqual(result["steps"][0]["status"], "rejected_no_improvement")
        self.assertEqual(result["summary"]["actual_gain"], 0)


class AnalyzeResponseScenarioTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.config["TESTING"] = True
        self.app.config["RATELIMIT_ENABLED"] = False
        limiter.init_app(self.app)
        self.app.register_blueprint(analysis_bp)
        self.client = self.app.test_client()

    def test_analysis_exposes_balance_and_v2_score(self):
        class _Conn:
            def close(self):
                pass

        with self.client.session_transaction() as sess:
            sess["user_id"] = 7

        import app.extensions as extensions
        with mock.patch.object(extensions, "ai_detector", return_value={
            "backend": "turnitin_fit_detector_v2",
            "risk_percent": 31.5,
            "ai_score": 31.5,
            "coverage": 1.0,
        }), mock.patch("app.models.get_connection", return_value=_Conn()), \
             mock.patch("app.models.User.get_balance", return_value=500), \
             mock.patch("app.models.User.get_detection_quota", return_value=(1000, 0)):
            response = self.client.post(
                "/api/analyze",
                json={"text": "This is a sufficiently long text. " * 20},
            )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["analysis"]["backend"], "turnitin_fit_detector_v2")
        self.assertEqual(data["analysis"]["risk_percent"], 31.5)
        self.assertEqual(data["rewrite_balance"], 500)
        self.assertTrue(data["balance_sufficient"])
        self.assertEqual(data["detection_balance"], 1000)


class TargetedRewriteIntegrationTests(unittest.TestCase):
    def test_rewrite_and_analyze_records_targeted_progress_and_metadata(self):
        class _Detector:
            def __call__(self, text, stage=None):
                score = {
                    "route_block": 75,
                    "rewrite_detect_rewritten": 35,
                    "targeted_document_recheck": 15,
                }.get(stage, 70)
                return {
                    "backend": "turnitin_fit_detector_v2_test",
                    "risk_percent": score,
                    "ai_score": score,
                    "coverage": 1,
                }

            def explain_blocks(self, texts, top=3):
                return [
                    {
                        "source_index": index,
                        "word_count": len(text.split()),
                        "risk_percent": 80 - index * 5,
                        "top_feature_groups": [{
                            "feature_group": "function_word_ratio",
                            "contribution_pp": 5,
                        }],
                        "text": text,
                    }
                    for index, text in enumerate(texts)
                ]

        class _LLM(_Provider):
            def humanize_targeted(self, text, directives, protected_values,
                                  protection_directives=None, retry_guidance=None,
                                  target_features=None):
                self.calls.append((
                    "targeted", text, directives, protection_directives,
                    retry_guidance, target_features,
                ))
                return text.replace("ordinary", "specific")

        import app.extensions as extensions
        huma = _Provider("huma")
        llm = _LLM("llm")
        detector = _Detector()
        paragraphs = [
            {"text": f"HIGH{index} ordinary " + "word " * 520}
            for index in range(2)
        ]
        text = "\n\n".join(item["text"] for item in paragraphs)
        progress = []

        with mock.patch.object(extensions, "humanizer_adapter", huma), \
             mock.patch.object(extensions, "ai_detector", detector), \
             mock.patch.object(
                 extensions, "rewrite_providers", {"huma": huma, "llm": llm}
             ), mock.patch("config.REWRITE_ROUTING_POLICY", "risk_band_segmented"):
            result = rewrite_and_analyze(
                text,
                mode="low",
                paragraphs=paragraphs,
                original_analysis={
                    "backend": "turnitin_fit_detector_v2_test",
                    "risk_percent": 70,
                    "ai_score": 70,
                    "coverage": 1,
                },
                progress_cb=lambda **value: progress.append(value),
            )

        metadata = result["rewrite_metadata"]
        self.assertEqual(result["rewritten_analysis"]["risk_percent"], 15)
        self.assertTrue(metadata["second_pass_triggered"])
        self.assertEqual(metadata["second_pass_rounds"], 1)
        self.assertEqual(metadata["second_pass_blocks"], 2)
        self.assertIn("targeted_rewrite", [row["stage"] for row in progress])
        self.assertEqual(metadata["rewrite_method"], "hybrid")


if __name__ == "__main__":
    unittest.main()
