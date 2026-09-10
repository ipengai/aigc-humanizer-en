"""Detector/humanizer composition for rewrite jobs.

The legacy policy delegates to the existing HumanizerAdapter unchanged. The
segmented policy reuses the existing low/median/high segmenter, then chooses a
provider for each already-created rewrite block.
"""
from __future__ import annotations

import logging
import math
import re
from copy import deepcopy
from typing import Dict, Optional

from app.pipeline.router import (
    LEGACY_POLICY,
    choose_document_policy,
    choose_rewrite_route,
)

logger = logging.getLogger("app.pipeline.orchestrator")


def _score(analysis):
    analysis = analysis or {}
    value = analysis.get("risk_percent")
    return value if value is not None else analysis.get("ai_score")


def _min_coverage():
    import config as project_config
    return float(getattr(project_config, "V2_REJECT_COVERAGE", 0.60))


def _large_word_count_deviation(before, after, limit=0.25):
    before_words = len((before or "").split())
    after_words = len((after or "").split())
    return bool(
        before_words and abs(after_words - before_words) / before_words > limit
    )


def _translation_needs_upgrade(score_before, score_after, after_route,
                               before_text, after_text):
    if after_route.get("action") != "protect":
        return True
    if score_before is not None and score_after is not None:
        if float(score_before) - float(score_after) < 5:
            return True
    return _large_word_count_deviation(before_text, after_text)


def _plain_text_paragraphs(text):
    """Create minimal paragraph structure for pasted text.

    Risk routing needs the same segmenter used for uploaded documents. Pasted
    text has no Word styles, but blank-line paragraphs are still sufficient for
    the segmenter while keeping the legacy policy unchanged.
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    values = [value.strip() for value in re.split(r"\n[ \t]*\n+", normalized)
              if value.strip()]
    return [
        {
            "text": value,
            "style": None,
            "is_heading": False,
            "heading_level": None,
            "source_format": "txt",
        }
        for value in values
    ]


class RewriteOrchestrator:
    def __init__(self, humanizer, detector, providers: Optional[Dict[str, object]] = None):
        self.humanizer = humanizer
        self.detector = detector
        self.providers = providers or {}

    def run(self, text, mode=None, paragraphs=None, original_analysis=None,
            policy="legacy_whole_document", progress_cb=None):
        if policy == LEGACY_POLICY:
            humanized, structured = self._humanize_structured(
                self.humanizer,
                text, mode=mode, paragraphs=paragraphs, progress_cb=progress_cb
            )
            return {
                "humanized": humanized,
                "rewritten_paragraphs": structured,
                "routing_policy": LEGACY_POLICY,
                "route": {"risk_band": "legacy", "action": "configured"},
                "steps": [],
            }

        if paragraphs is None:
            paragraphs = _plain_text_paragraphs(text)

        # Keep compatibility with lightweight/legacy adapters that predate the
        # shared segmentation kernel. Production adapters inherit this method;
        # test and third-party adapters continue to use their own structured
        # implementation until they are upgraded.
        if not hasattr(self.humanizer, "_humanize_segmented_structured"):
            humanized, structured = self._humanize_structured(
                self.humanizer, text, mode, paragraphs, progress_cb
            )
            return {
                "humanized": humanized,
                "rewritten_paragraphs": structured,
                "routing_policy": policy,
                "route": {"risk_band": "compat", "action": "configured"},
                "steps": [],
            }

        analysis = original_analysis or self.detector(text, stage="route_document")
        word_count = len(text.split())
        block_count = sum(1 for p in (paragraphs or []) if p.get("text"))
        decision = choose_document_policy(
            _score(analysis),
            analysis.get("coverage", 1.0), word_count, block_count,
            configured_policy=policy,
            min_coverage=_min_coverage(),
        )
        if decision["risk_band"] == "protect":
            return {
                "humanized": text,
                "rewritten_paragraphs": self._protected_structure(paragraphs),
                "routing_policy": policy,
                "route": decision,
                "steps": [],
            }
        if decision["risk_band"] == "needs_review":
            return {
                "humanized": text,
                "rewritten_paragraphs": self._protected_structure(paragraphs),
                "routing_policy": policy,
                "route": decision,
                "steps": [],
            }
        if not decision["use_block_routing"]:
            # Keep the original segmenter/structure reconstruction kernel. Only
            # the provider used for each rewrite block changes here.
            action = "translation" if decision["risk_band"] == "low_cost_first" else "huma"
            provider = self._provider(action)
            if provider is None:
                raise RuntimeError(f"未配置 {action} 改写器，无法执行风险路由")
            translation_fallback = (
                self._provider("huma") if action == "translation" else None
            )
            humanized, structured, steps = self._run_segmented_provider(
                provider, action, mode, paragraphs, progress_cb,
                document_analysis=analysis,
                fallback_provider=translation_fallback,
            )
            if action == "translation":
                recheck = self.detector(
                    humanized, stage="route_document_recheck"
                )
                recheck_route = choose_rewrite_route(
                    _score(recheck), recheck.get("coverage", 1.0),
                    len(humanized.split()), min_coverage=_min_coverage(),
                )
                decision = dict(decision)
                decision["recheck_score"] = _score(recheck)
                if _translation_needs_upgrade(
                    _score(analysis), _score(recheck), recheck_route,
                    text, humanized,
                ):
                    huma = self._provider("huma")
                    if huma is None:
                        raise RuntimeError("翻译复检仍为高风险，但未配置 huma 改写器")
                    # Always apply the required document-level Huma upgrade.
                    # A translation exception may have tried Huma only for an
                    # intermediate aggregate whose output was later discarded
                    # by the DOCX structure fallback.  Treating that attempt as
                    # a completed block caused the real upgrade to be skipped.
                    humanized, structured, huma_steps = self._run_segmented_provider(
                        huma, "huma", mode, structured, progress_cb,
                        document_analysis=recheck,
                    )
                    for index, step in enumerate(steps):
                        step["score_after"] = _score(recheck)
                        if (
                            index < len(huma_steps)
                            and huma_steps[index].get("status")
                            != "preserved_existing_huma"
                        ):
                            step["action"] = "huma"
                            if "huma" not in step["action_chain"]:
                                step["action_chain"].append("huma")
                            step["rewrite_backend"] = huma_steps[index]["rewrite_backend"]
                            if huma_steps[index]["rewrite_backend"] not in step["rewrite_backends"]:
                                step["rewrite_backends"].append(
                                    huma_steps[index]["rewrite_backend"]
                                )
                            step["status"] = "upgraded_after_recheck"
            return {
                "humanized": humanized,
                "rewritten_paragraphs": structured,
                "routing_policy": policy,
                "route": decision,
                "steps": steps,
            }

        return self._run_block_routes(
            text, mode, paragraphs, policy, progress_cb,
            document_analysis=analysis, document_decision=decision,
        )

    def _run_block_routes(self, text, mode, paragraphs, policy, progress_cb,
                          document_analysis, document_decision):
        # Do not reimplement segment(), protection, batching, or structure
        # reconstruction here. The adapter owns that stable kernel.
        steps = []
        rewrite_index = 0

        def routed_rewriter(block_text):
            nonlocal rewrite_index
            analysis = self.detector(block_text, stage="route_block")
            detector_source = "block"
            route = choose_rewrite_route(
                _score(analysis),
                analysis.get("coverage", 1.0), len(block_text.split()),
                min_coverage=_min_coverage(),
            )
            block_score = _score(analysis)
            # v2 needs enough words inside one detector paragraph. Keep the
            # original rewrite block intact, but inherit the trustworthy full
            # document score when a short block itself cannot be scored.
            if route["action"] == "needs_review" and not document_analysis.get("error_code"):
                fallback_route = choose_rewrite_route(
                    _score(document_analysis),
                    document_analysis.get("coverage", 1.0),
                    len(block_text.split()),
                    min_coverage=_min_coverage(),
                )
                if fallback_route["action"] != "needs_review":
                    route = fallback_route
                    detector_source = "document_fallback"
            action = route["action"]
            planned_action = action
            action_chain = []
            rewrite_backends = []
            score_before = (
                block_score if detector_source == "block"
                else _score(document_analysis)
            )
            score_after = None
            if analysis.get("error_code") and detector_source == "block":
                action = "needs_review"
            if action in ("protect", "needs_review"):
                # An unavailable/low-confidence detector must not silently pay
                # for a rewrite. Preserve the block and surface the reason.
                output = block_text
                provider = None
                status = (
                    "protected_low_risk" if action == "protect"
                    else "protected_low_confidence"
                )
            else:
                provider = self._provider(action)
                if provider is None:
                    raise RuntimeError(f"未配置 {action} 改写器，无法执行风险路由")
                try:
                    output = provider.humanize(block_text, mode=mode, paragraphs=None)
                    action_chain.append(action)
                    rewrite_backends.append(self._label(provider))
                except Exception:
                    if action != "translation":
                        raise
                    huma = self._provider("huma")
                    if huma is None:
                        raise RuntimeError("翻译改写失败，且未配置 huma 改写器")
                    action_chain.append("translation")
                    rewrite_backends.append(self._label(provider))
                    output = huma.humanize(block_text, mode=mode, paragraphs=None)
                    provider = huma
                    action = "huma"
                    action_chain.append("huma")
                    rewrite_backends.append(self._label(huma))
                    status = "upgraded_after_translation_error"
                if action == "translation" and route.get("requires_recheck"):
                    after = self.detector(output, stage="route_block_recheck")
                    score_after = _score(after)
                    after_route = choose_rewrite_route(
                        score_after, after.get("coverage", 1.0), len(output.split()),
                        min_coverage=_min_coverage(),
                    )
                    if _translation_needs_upgrade(
                        score_before, score_after, after_route,
                        block_text, output,
                    ):
                        huma = self._provider("huma")
                        if huma is None:
                            raise RuntimeError("翻译复检仍为高风险，但未配置 huma 改写器")
                        output = huma.humanize(output, mode=mode, paragraphs=None)
                        provider = huma
                        action = "huma"
                        action_chain.append("huma")
                        rewrite_backends.append(self._label(huma))
                        status = "upgraded_after_recheck"
                    else:
                        status = "completed"
                elif action != "huma" or not action_chain or action_chain[0] != "translation":
                    status = "completed"
            steps.append({
                "step_id": len(steps) + 1,
                "scope": "block",
                "block_id": f"rewrite-block-{rewrite_index:04d}",
                "detector": analysis.get("backend", "unknown"),
                "detector_source": detector_source,
                "block_words": len(block_text.split()),
                "mode": mode or "median",
                "score_before": score_before,
                "block_score": block_score,
                "score_after": score_after,
                "planned_action": planned_action,
                "action": action,
                "action_chain": action_chain or [action],
                "rewrite_backend": self._label(provider) if provider else None,
                "rewrite_backends": rewrite_backends,
                "status": status,
                "reason": route.get("reason"),
            })
            rewrite_index += 1
            return output

        humanized, structured = self.humanizer._humanize_segmented_structured(
            mode or "median", paragraphs, routed_rewriter,
            progress_cb=progress_cb, batch_short_blocks=False,
        )
        return {
            "humanized": humanized,
            "rewritten_paragraphs": structured,
            "routing_policy": policy,
            "route": document_decision,
            "steps": steps,
        }

    def _run_segmented_provider(self, provider, action, mode, paragraphs, progress_cb,
                                document_analysis=None, fallback_provider=None,
                                skip_indexes=None):
        steps = []
        counter = 0

        def rewrite(block_text):
            nonlocal counter
            counter += 1
            if counter in (skip_indexes or set()):
                steps.append({
                    "step_id": counter,
                    "scope": "block",
                    "block_id": f"rewrite-block-{counter - 1:04d}",
                    "block_words": len(block_text.split()),
                    "mode": mode or "median",
                    "detector": (document_analysis or {}).get("backend", "unknown"),
                    "detector_source": "document",
                    "score_before": _score(document_analysis),
                    "score_after": None,
                    "planned_action": "preserve",
                    "action": "preserve",
                    "action_chain": [],
                    "rewrite_backend": None,
                    "rewrite_backends": [],
                    "status": "preserved_existing_huma",
                })
                return block_text
            effective_provider = provider
            action_chain = [action]
            rewrite_backends = [self._label(provider)]
            status = "completed"
            try:
                output = provider.humanize(block_text, mode=mode, paragraphs=None)
            except Exception:
                if action != "translation" or fallback_provider is None:
                    raise
                output = fallback_provider.humanize(
                    block_text, mode=mode, paragraphs=None
                )
                effective_provider = fallback_provider
                action_chain.append("huma")
                rewrite_backends.append(self._label(fallback_provider))
                status = "upgraded_after_translation_error"
            steps.append({
                "step_id": counter,
                "scope": "block",
                "block_id": f"rewrite-block-{counter - 1:04d}",
                "block_words": len(block_text.split()),
                "mode": mode or "median",
                "detector": (document_analysis or {}).get("backend", "unknown"),
                "detector_source": "document",
                "score_before": _score(document_analysis),
                "score_after": None,
                "planned_action": action,
                "action": action_chain[-1],
                "action_chain": action_chain,
                "rewrite_backend": self._label(effective_provider),
                "rewrite_backends": rewrite_backends,
                "status": status,
            })
            return output

        humanized, structured = self.humanizer._humanize_segmented_structured(
            mode or "median", paragraphs, rewrite,
            progress_cb=progress_cb, batch_short_blocks=False,
            primary_label=self._label(provider),
        )
        return humanized, structured, steps

    def run_targeted_second_pass(self, humanized, structured, analysis,
                                 mode=None, progress_cb=None):
        """Use local attribution to rewrite a few high-impact blocks and stop early."""
        import config as project_config
        from app.pipeline.attribution_policy import (
            POLICY_VERSION,
            directives_for,
            feature_retry_guidance,
            feature_target_guidance,
            protection_directives_for,
            protected_tokens,
            validate_targeted_output,
        )

        score = _score(analysis)
        target = float(getattr(project_config, "REWRITE_TARGET_RISK_PERCENT", 20))
        llm = self._provider("llm")
        explain = getattr(self.detector, "explain_blocks", None)
        profile = getattr(self.detector, "feature_profile", None)
        enabled = bool(getattr(project_config, "REWRITE_TARGETED_SECOND_PASS", True))
        if (
            not enabled or score is None or float(score) < target
            or llm is None or explain is None
            or float((analysis or {}).get("coverage", 0)) < _min_coverage()
        ):
            return {
                "humanized": humanized,
                "rewritten_paragraphs": structured,
                "analysis": analysis,
                "steps": [],
                "summary": {"triggered": False, "rounds": 0, "blocks": 0},
            }

        items = deepcopy(structured or [])
        eligible_indexes = [
            index for index, item in enumerate(items)
            if item.get("was_rewritten") is True and (item.get("text") or "").strip()
        ]
        if not eligible_indexes:
            return {
                "humanized": humanized,
                "rewritten_paragraphs": structured,
                "analysis": analysis,
                "steps": [],
                "summary": {"triggered": False, "rounds": 0, "blocks": 0},
            }

        batch_size = max(1, int(getattr(project_config, "REWRITE_TARGETED_BATCH_SIZE", 2)))
        max_rounds = max(1, int(getattr(project_config, "REWRITE_TARGETED_MAX_ROUNDS", 2)))
        attempt_counts = {}
        retry_guidance = {}
        steps = []
        current_analysis = analysis
        current_text = humanized
        rounds = 0

        for round_number in range(1, max_rounds + 1):
            current_score = _score(current_analysis)
            if current_score is None or float(current_score) < target:
                break
            block_texts = [items[index]["text"] for index in eligible_indexes]
            explanations = explain(block_texts, top=3)
            total_words = max(sum(row.get("word_count", 0) for row in explanations), 1)
            candidates = []
            for row in explanations:
                item_index = eligible_indexes[row["source_index"]]
                if (
                    attempt_counts.get(item_index, 0) >= max_rounds
                    or row["risk_percent"] <= target
                ):
                    continue
                expected_gain = (
                    (row["risk_percent"] - target)
                    * row["word_count"] / total_words
                )
                candidates.append((expected_gain, item_index, row))
            candidates.sort(key=lambda value: value[0], reverse=True)
            selected = candidates[:batch_size]
            if not selected:
                break

            rounds += 1
            if progress_cb:
                progress_cb(
                    stage="targeted_rewrite",
                    block=round_number,
                    total_blocks=max_rounds,
                    message=f"首次改写后仍有高风险内容，正在定向优化（第{round_number}/{max_rounds}轮）",
                )

            candidate_items = deepcopy(items)
            block_records = []
            for expected_gain, item_index, row in selected:
                attempt_counts[item_index] = attempt_counts.get(item_index, 0) + 1
                before = candidate_items[item_index]["text"]
                feature_names, directives = directives_for(row["top_feature_groups"])
                protect_names, protection_directives = protection_directives_for(
                    row.get("protect_feature_groups")
                )
                before_profile = profile(before) if profile else {}
                measured_guidance = feature_target_guidance(
                    before_profile,
                    feature_names,
                    protect_names,
                )
                attempt_guidance = list(retry_guidance.get(item_index) or [])
                attempt_guidance.extend(measured_guidance)
                protected = protected_tokens(before)
                try:
                    if hasattr(llm, "humanize_targeted"):
                        after = llm.humanize_targeted(
                            before,
                            directives,
                            protected,
                            protection_directives=protection_directives,
                            retry_guidance=attempt_guidance or None,
                            target_features=feature_names,
                        )
                    else:
                        after = llm.humanize(before, mode=mode, paragraphs=None)
                    valid, rejection_reason = validate_targeted_output(before, after)
                    after_profile = profile(after) if profile and after else {}
                except Exception as exc:
                    logger.exception("targeted rewrite failed block=%s", item_index)
                    after = before
                    valid, rejection_reason = False, type(exc).__name__
                    after_profile = {}
                if valid:
                    candidate_items[item_index]["text"] = after.strip()
                block_records.append({
                    "block_id": candidate_items[item_index].get("block_id") or f"structured-{item_index}",
                    "structured_index": item_index,
                    "attribution_source_index": row["source_index"],
                    "score_before": row["risk_percent"],
                    "word_count": row["word_count"],
                    "estimated_gain": expected_gain,
                    "top_feature_groups": feature_names,
                    "protected_feature_groups": protect_names,
                    "validation": "passed" if valid else "rejected",
                    "rejection_reason": rejection_reason,
                    "before_word_count": len(before.split()),
                    "after_word_count": len((after or "").split()),
                    "feature_retry_guidance": feature_retry_guidance(
                        after_profile,
                        feature_names,
                        protect_names,
                        previous_profile=before_profile,
                    ),
                })

            if not any(row["validation"] == "passed" for row in block_records):
                for record in block_records:
                    guidance = [
                        "The previous revision failed output validation. Produce a different revision and follow every measurable constraint literally."
                    ]
                    if record["rejection_reason"] == "word_count_deviation":
                        before_words = record["before_word_count"]
                        guidance.append(
                            f"The previous revision had {record['after_word_count']} words. Keep this attempt between {max(1, math.floor(before_words * 0.85))} and {max(1, math.ceil(before_words * 1.15))} words."
                        )
                    guidance.extend(record.get("feature_retry_guidance") or [])
                    retry_guidance[record["structured_index"]] = guidance
                steps.append({
                    "scope": "targeted_batch", "pass": 2, "round": round_number,
                    "action": "attribution_llm", "action_chain": ["llm"],
                    "rewrite_backend": self._label(llm),
                    "rewrite_backends": [self._label(llm)],
                    "status": "validation_rejected",
                    "blocks": block_records, "score_before": current_score,
                    "score_after": current_score,
                })
                continue

            candidate_text = "\n\n".join(
                (item.get("text") or "").strip() for item in candidate_items
                if (item.get("text") or "").strip()
            )
            candidate_analysis = self.detector(
                candidate_text, stage="targeted_document_recheck"
            )
            candidate_score = _score(candidate_analysis)
            accepted = bool(
                candidate_score is not None
                and not candidate_analysis.get("error_code")
                and float(candidate_analysis.get("coverage", 0)) >= _min_coverage()
                and float(candidate_score) < float(current_score)
            )
            if not accepted:
                for record in block_records:
                    if record["validation"] != "passed":
                        continue
                    retry_guidance[record["structured_index"]] = [
                        "The previous revision was rejected because the full passage did not improve. Produce a materially different revision rather than repeating the same wording.",
                        "Apply the requested changes conservatively and prioritize every protected characteristic over stylistic polishing.",
                    ] + list(record.get("feature_retry_guidance") or [])
            steps.append({
                "scope": "targeted_batch", "pass": 2, "round": round_number,
                "action": "attribution_llm", "rewrite_backend": self._label(llm),
                "action_chain": ["llm"], "rewrite_backends": [self._label(llm)],
                "attribution_policy_version": POLICY_VERSION,
                "status": "accepted" if accepted else "rejected_no_improvement",
                "score_before": current_score, "score_after": candidate_score,
                "actual_gain": (
                    float(current_score) - float(candidate_score)
                    if accepted else 0.0
                ),
                "blocks": block_records,
            })
            if accepted:
                updated_explanations = {
                    row["source_index"]: row
                    for row in explain(
                        [candidate_items[index]["text"] for index in eligible_indexes],
                        top=3,
                    )
                }
                updated_total_words = max(
                    sum(row.get("word_count", 0) for row in updated_explanations.values()),
                    1,
                )
                for record in block_records:
                    updated = updated_explanations.get(record["attribution_source_index"])
                    if updated:
                        record["score_after"] = updated["risk_percent"]
                        record["actual_weighted_gain"] = (
                            (record["score_before"] - updated["risk_percent"])
                            * updated["word_count"] / updated_total_words
                        )
                    retry_guidance[record["structured_index"]] = list(
                        record.get("feature_retry_guidance") or []
                    )
                items = candidate_items
                current_text = candidate_text
                current_analysis = candidate_analysis

        return {
            "humanized": current_text,
            "rewritten_paragraphs": items,
            "analysis": current_analysis,
            "steps": steps,
            "summary": {
                "triggered": bool(rounds),
                "rounds": rounds,
                "blocks": sum(attempt_counts.values()),
                "unique_blocks": len(attempt_counts),
                "required_reduction_before": max(float(score) - target, 0.0),
                "actual_gain": float(score) - float(_score(current_analysis)),
                "target": target,
                "achieved": float(_score(current_analysis)) < target,
                "attribution_policy_version": POLICY_VERSION,
            },
        }

    def _provider(self, action):
        if action in self.providers:
            return self.providers[action]
        # The main adapter is a valid fallback only for its matching route.
        primary = getattr(self.humanizer, "primary", self.humanizer)
        backend = getattr(primary, "backend_label", "")
        if action == "huma" and (backend.startswith("ai_text_humanizer") or
                                  type(primary).__name__ == "AITextHumanizer"):
            return self.humanizer
        if action == "llm" and type(primary).__name__ == "LLMBasedHumanizer":
            return self.humanizer
        return None

    @staticmethod
    def _humanize_structured(provider, text, mode, paragraphs, progress_cb):
        try:
            return provider.humanize_structured(
                text, mode=mode, paragraphs=paragraphs, progress_cb=progress_cb
            )
        except TypeError as exc:
            if "progress_cb" not in str(exc):
                raise
            return provider.humanize_structured(
                text, mode=mode, paragraphs=paragraphs
            )

    @staticmethod
    def _label(provider):
        return getattr(provider, "backend_label", type(provider).__name__)

    @staticmethod
    def _protected_structure(paragraphs):
        result = []
        for para in paragraphs or []:
            item = dict(para)
            item["was_rewritten"] = False
            result.append(item)
        return result
