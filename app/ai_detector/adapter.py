"""
AI Detector Adapter — 根据配置返回对应的 analyze_text 函数。

用法：在 create_app() 中调用 create_detector(name)，
然后把返回的函数注册到 app.extensions.ai_detector。

适配的 adapter_name：
  - "rule_based"  → 本地规则 (rule_based.py)
  - "sapling"     → Sapling.ai API
  - "originality"  → Originality.ai API
  - "sapling_mock" → 模拟 Sapling（随机 sleep 1-1.5s，不真实请求），用于测试等待界面
  - "rule_based_mock" → 模拟本地规则（随机 sleep），用于测试等待界面
"""

import random
import re
import time
import threading

from app.ai_detector.api import analyze_text as _api_detect
from app.ai_detector.aggregation import aggregate_short_paragraphs
from app.ai_detector.rule_based import analyze_text as _rule_detect


def _make_api_detect(backend: str):
    """Use the configured API detector, falling back to local rules on errors."""
    def _detect(text: str, stage: str = "analyze") -> dict:
        import logging
        _logger = logging.getLogger("app.ai_detector.adapter")
        api = _api_detect(text, backend=backend, stage=stage)
        if "error" in api:
            _logger.warning(
                "detect stage=%s backend=%s action=fallback_to_rule fallback=1 "
                "result=rule_estimate error=%s chars=%d",
                stage, backend, api.get("error", "unknown"), len(text),
            )
            # API 失败/超时时降级到规则，但保留规则的真实结果，
            # 不覆盖成 API 的占位值（ai_score=50 / Unknown），避免误导用户。
            rule = _rule_detect(text, stage=stage)
            rule["backend"] = f"{backend}_fallback"
            rule["error"] = api.get("error", "unknown")
            return rule
        _logger.info("detect stage=%s backend=%s action=ok ai_score=%.1f",
                     stage, backend, api.get("ai_score", 0))
        return api
    return _detect


def _make_mock_detect(label: str):
    """
    模拟检测：随机 sleep 1-1.5s，返回规则结果，不真实请求外部 API。
    用于测试"改写进行中"等待界面。
    """
    def _mock(text: str, stage: str = "analyze") -> dict:
        import logging
        _logger = logging.getLogger("app.ai_detector.adapter")
        duration = random.uniform(1.0, 1.5)
        _logger.info("detect stage=%s backend=%s_mock action=start chars=%d sleep=%.2fs",
                     stage, label, len(text), duration)
        time.sleep(duration)
        rule = _rule_detect(text, stage=stage)
        rule["backend"] = f"{label}_mock"
        rule["mock"] = True
        _logger.info("detect stage=%s backend=%s_mock action=done ai_score=%.1f elapsed=%.0fms",
                     stage, label, rule.get("ai_score", 0), duration * 1000)
        return rule
    return _mock


def _make_turnitin_fit_detect():
    """Create a v2 detector backed by one classifier per worker process."""
    import logging
    logger = logging.getLogger("app.ai_detector.turnitin_fit_detector_v2")
    module_cache = {}
    cache_lock = threading.Lock()

    def _load():
        if module_cache:
            return module_cache["runtime"]
        with cache_lock:
            if module_cache:
                return module_cache["runtime"]
            from app.ai_detector.runtime import (
                DEFAULT_METADATA_PATH,
                TurnitinFitDetectorRuntime,
            )
            runtime = TurnitinFitDetectorRuntime(DEFAULT_METADATA_PATH)
            runtime.warm_up()
            module_cache["runtime"] = runtime
            return runtime

    def _detect(text: str, stage: str = "analyze") -> dict:
        try:
            runtime = _load()
            result = runtime.detect(text)
            summary = result.get("summary", {})
            risk = summary.get("risk_percent")
            raw_coverage = float(summary.get("coverage") or 0.0)
            aggregation_fallback = None
            min_coverage = float(
                getattr(__import__("config"), "V2_REJECT_COVERAGE", 0.60)
            )
            # Retry all low-coverage results, including partial scores such as
            # P04. The joined text is detector-only and never changes rewrite
            # blocks or document reconstruction.
            if raw_coverage < min_coverage and re.search(r"\n\s*\n", text or ""):
                joined = aggregate_short_paragraphs(text)
                joined_result = runtime.detect(joined)
                joined_summary = joined_result.get("summary", {})
                joined_coverage = float(joined_summary.get("coverage") or 0.0)
                if joined_coverage > raw_coverage:
                    result = joined_result
                    summary = joined_summary
                    risk = summary.get("risk_percent")
                    aggregation_fallback = "structured_short_paragraph_join"
            coverage = float(summary.get("coverage") or 0.0)
            output = {
                "backend": "turnitin_fit_detector_v2",
                "model_version": summary.get("model_version", "v2.1.0"),
                "ai_score": risk,
                "risk_percent": risk,
                "coverage": coverage,
                "raw_coverage": raw_coverage,
                "effective_coverage": coverage,
                "confidence": "high" if coverage >= min_coverage else "low",
                "paragraphs": result.get("paragraphs", []),
                "input_words": summary.get("input_words"),
                "analyzed_words": summary.get("analyzed_words"),
                "stage": stage,
                "evidence_level": "experimental_local_estimate",
            }
            if aggregation_fallback:
                output["aggregation_fallback"] = aggregation_fallback
                output["aggregation_strategy"] = aggregation_fallback
            if risk is None:
                output.update({
                    "error": "v2 detector requires at least 40 English words in a scorable block",
                    "error_code": "v2_insufficient_supported_text",
                    "confidence": "none",
                })
            return output
        except Exception as exc:
            logger.exception("turnitin_fit_detector_v2 failed stage=%s", stage)
            return {
                "backend": "turnitin_fit_detector_v2",
                "model_version": "unknown",
                "error": str(exc),
                "error_code": "v2_detector_unavailable",
                "coverage": 0.0,
                "confidence": "none",
                "stage": stage,
                "evidence_level": "experimental_local_estimate",
            }
    def _explain_blocks(texts, top=3):
        from app.ai_detector.attribution import explain_blocks
        return explain_blocks(texts, _load(), top=top)

    def _feature_profile(text):
        from app.ai_detector.attribution import feature_profile
        return feature_profile(text, _load())

    _detect.runtime = _load
    _detect.explain_blocks = _explain_blocks
    _detect.feature_profile = _feature_profile
    return _detect


def create_detector(adapter_name: str = "rule_based"):
    """
    返回一个 analyze_text 可调用对象（仅整篇 AI 率检测）。

    adapter_name 取值：
      rule_based  → 本地规则检测
      sapling     → Sapling.ai
      originality → Originality.ai
      sapling_mock → 模拟 Sapling（随机 sleep，不请求外部），测试用
      rule_based_mock → 模拟本地规则（随机 sleep），测试用
      turnitin_fit_detector_v2 → 本地 Turnitin 导向随机森林实验检测器

    返回的函数签名：callable(text, stage="analyze") -> dict。
    """
    if adapter_name == "rule_based":
        return _rule_detect
    elif adapter_name in ("sapling", "originality"):
        return _make_api_detect(adapter_name)
    elif adapter_name == "sapling_mock":
        return _make_mock_detect("sapling")
    elif adapter_name == "rule_based_mock":
        return _make_mock_detect("rule_based")
    elif adapter_name == "turnitin_fit_detector_v2":
        return _make_turnitin_fit_detect()
    elif adapter_name == "turnitin_fit_detector_v2_mock":
        return _make_mock_detect("turnitin_fit_detector_v2")
    raise ValueError(f"Unknown AI_DETECTOR_ADAPTER: {adapter_name}")
