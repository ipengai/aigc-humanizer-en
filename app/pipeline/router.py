"""Pure routing decisions for detector/humanizer composition.

This module deliberately does not call detectors, humanizer providers, Flask,
or the database. It converts detector output and document context into a
stable route decision so the execution layer can be tested independently.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

LEGACY_POLICY = "legacy_whole_document"
SEGMENTED_POLICY = "risk_band_segmented"


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def choose_document_policy(
    risk_percent: Any,
    coverage: Any,
    word_count: Any,
    block_count: Any = 0,
    *,
    configured_policy: str = SEGMENTED_POLICY,
    long_document_words: int = 1000,
    long_document_blocks: int = 6,
    min_coverage: float = 0.60,
) -> Dict[str, Any]:
    """Choose document-level handling without invoking any provider.

    ``legacy_whole_document`` always preserves the existing Sapling-style
    orchestration. The segmented policy only enables block routing for a
    trustworthy, long, high-risk document; short or uncertain inputs use the
    legacy whole-document path.
    """
    score = _number(risk_percent)
    coverage_value = _number(coverage)
    words = int(_number(word_count) or 0)
    blocks = int(_number(block_count) or 0)

    if configured_policy == LEGACY_POLICY:
        return {
            "policy": LEGACY_POLICY,
            "risk_band": "legacy",
            "action": "configured",
            "use_block_routing": False,
            "reason": "legacy policy explicitly selected",
        }
    if configured_policy != SEGMENTED_POLICY:
        raise ValueError(f"Unknown routing policy: {configured_policy}")
    if score is None or coverage_value is None or coverage_value < min_coverage:
        return {
            "policy": LEGACY_POLICY,
            "risk_band": "needs_review",
            "action": "needs_review",
            "use_block_routing": False,
            "reason": "missing or low-confidence detector result",
        }
    if score < 20:
        return {
            "policy": SEGMENTED_POLICY,
            "risk_band": "protect",
            "action": "protect",
            "use_block_routing": False,
            "reason": "whole-document risk below 20",
        }
    if score <= 60:
        return {
            "policy": SEGMENTED_POLICY,
            "risk_band": "low_cost_first",
            "action": "translation",
            "use_block_routing": False,
            "reason": "whole-document risk in 20-60 band",
        }
    is_long = words >= long_document_words or blocks >= long_document_blocks
    return {
        "policy": SEGMENTED_POLICY,
        "risk_band": "segment_route" if is_long else "huma_first",
        "action": "block_routing" if is_long else "huma",
        "use_block_routing": is_long,
        "reason": "high-risk long document" if is_long else "high-risk short document",
    }


def choose_rewrite_route(
    risk_percent: Any,
    coverage: Any,
    word_count: Any = 0,
    *,
    low: float = 20,
    high: float = 60,
    min_coverage: float = 0.60,
) -> Dict[str, Any]:
    """Map a document/block detector result to a rewrite action."""
    score = _number(risk_percent)
    coverage_value = _number(coverage)
    if score is None or coverage_value is None or coverage_value < min_coverage:
        return {
            "action": "needs_review",
            "risk_band": "needs_review",
            "requires_recheck": False,
            "max_paid_attempts": 0,
            "reason": "missing or low-confidence detector result",
        }
    if score < low:
        return {
            "action": "protect",
            "risk_band": "protect",
            "requires_recheck": False,
            "max_paid_attempts": 0,
            "reason": "risk below low threshold",
        }
    if score <= high:
        return {
            "action": "translation",
            "risk_band": "low_cost_first",
            "requires_recheck": True,
            "upgrade_action": "huma",
            "max_paid_attempts": 1,
            "reason": "risk in low-cost-first band",
        }
    return {
        "action": "huma",
        "risk_band": "huma_first",
        "requires_recheck": True,
        "max_paid_attempts": 1,
        "reason": "risk above high threshold",
    }
