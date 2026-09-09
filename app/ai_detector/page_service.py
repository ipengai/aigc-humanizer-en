"""Product-page view adapter over the application's primary AI detector."""

from __future__ import annotations

from collections import defaultdict


FEATURE_GROUPS = {
    "lexical_diversity": {
        "label": "词汇多样性",
        "features": {"type_token_ratio", "hapax_ratio", "most_common_word_ratio", "lexical_entropy"},
    },
    "word_complexity": {
        "label": "词长与用词复杂度",
        "features": {"average_word_length", "long_word_ratio", "adverb_ly_ratio", "nominalization_ratio"},
    },
    "sentence_structure": {
        "label": "句式结构",
        "features": {
            "sentence_length_mean", "sentence_length_std", "sentence_length_cv",
            "sentence_length_min", "sentence_length_max", "sentence_length_median",
            "sentence_length_iqr", "short_sentence_ratio", "long_sentence_ratio",
        },
    },
    "function_words": {
        "label": "功能词密度",
        "features": {"function_word_ratio", "first_person_ratio", "modal_ratio", "negation_ratio", "contraction_ratio"},
    },
    "hedges_transitions": {
        "label": "过渡与模糊语",
        "features": {"transition_ratio", "hedge_ratio"},
    },
    "repetition": {
        "label": "重复模式",
        "features": {"repeated_bigram_ratio", "repeated_sentence_start_ratio", "demonstrative_sentence_start_ratio"},
    },
    "punctuation": {
        "label": "标点与节奏",
        "features": {
            "comma_per_sentence", "semicolon_per_sentence", "colon_per_sentence",
            "dash_per_sentence", "parenthesis_per_sentence", "quote_per_sentence",
            "question_per_sentence", "exclamation_per_sentence",
        },
    },
    "citations_numbers": {
        "label": "引用与数字",
        "features": {"citation_ratio", "number_ratio"},
    },
    "passive": {
        "label": "被动语态",
        "features": {"passive_proxy_per_sentence"},
    },
    "length": {
        "label": "篇幅与段落",
        "features": {
            "word_count", "sentence_count", "neighbor_count",
            "section_paragraph_count", "relative_paragraph_position",
        },
    },
}

FEATURE_TO_GROUP = {
    feature: key
    for key, spec in FEATURE_GROUPS.items()
    for feature in spec["features"]
}

DISCLAIMER = (
    "本检测模型以 Turnitin 真实评分数据为训练基准、拟合其 AI 文本判定口径，"
    "结果仅供预估参考，并非 Turnitin 官方检测，不替代学校或机构的正式判定。"
)


class DetectorPageService:
    """Convert the shared detector result into the secondary page contract."""

    def __init__(self, detect_fn):
        if detect_fn is None:
            raise RuntimeError("主检测服务尚未初始化")
        if not callable(getattr(detect_fn, "explain_blocks", None)):
            raise RuntimeError("AI 检测二级页需要 turnitin_fit_detector_v2 主检测服务")
        self._detect = detect_fn

    def detect(self, text: str) -> dict:
        result = self._detect(text, stage="detector_page")
        error_code = result.get("error_code")
        if error_code == "v2_insufficient_supported_text":
            raise ValueError("需要至少一个含 40 个英文单词的可检测文本块。")
        if error_code:
            raise RuntimeError(result.get("error") or error_code)

        risk = float(result["risk_percent"])
        raw_paragraphs = result.get("paragraphs") or []
        paragraphs = [self._paragraph(row) for row in raw_paragraphs]
        attribution = self._attribution(paragraphs, risk)
        input_words = int(result.get("input_words") or sum(p["word_count"] for p in paragraphs))
        analyzed_words = int(result.get("analyzed_words") or sum(p["word_count"] for p in paragraphs))

        return {
            "model_version": result.get("model_version", "unknown"),
            "summary": {
                "model_version": result.get("model_version", "unknown"),
                "risk_percent": round(risk, 1),
                "input_words": input_words,
                "analyzed_words": analyzed_words,
                "coverage": round(float(result.get("coverage") or 0.0) * 100, 1),
                "raw_coverage": round(float(result.get("raw_coverage") or 0.0) * 100, 1),
                "aggregation_fallback": result.get("aggregation_fallback"),
                "paragraphs": len(paragraphs),
                "high_risk_paragraphs": sum(p["risk_level"] == "高" for p in paragraphs),
                "attribution": attribution,
                "disclaimer": DISCLAIMER,
            },
            "paragraphs": paragraphs,
        }

    @staticmethod
    def _paragraph(row: dict) -> dict:
        probability = float(row.get("ai_probability") or 0.0)
        return {
            "id": row.get("paragraph_id"),
            "word_count": int(row.get("word_count") or 0),
            "risk_percent": round(probability * 100, 1),
            "risk_level": "高" if int(row.get("label") or 0) else "较低",
            "text": row.get("text") or "",
        }

    def _attribution(self, paragraphs: list[dict], risk: float) -> dict:
        texts = [row["text"] for row in paragraphs]
        explanations = self._detect.explain_blocks(texts, top=100)
        weighted = defaultdict(float)
        baseline = 0.0
        total_words = 0
        max_error = 0.0
        for explanation in explanations:
            words = int(explanation.get("word_count") or 0)
            total_words += words
            baseline += float(explanation.get("baseline_probability") or 0.0) * words
            max_error = max(max_error, float(explanation.get("reconstruction_error") or 0.0))
            for item in explanation.get("top_feature_groups", []):
                weighted[item["feature_group"]] += float(item["contribution"]) * words
            for item in explanation.get("protect_feature_groups", []):
                weighted[item["feature_group"]] += float(item["contribution"]) * words

        if total_words:
            weighted = {key: value / total_words for key, value in weighted.items()}
            baseline /= total_words
        grouped = defaultdict(float)
        for feature, value in weighted.items():
            key = FEATURE_TO_GROUP.get(feature)
            if key:
                grouped[key] += value

        ordered = sorted(grouped.items(), key=lambda item: abs(item[1]), reverse=True)[:5]
        denominator = sum(abs(value) for _, value in ordered) or 1.0
        groups = [{
            "key": key,
            "label": FEATURE_GROUPS[key]["label"],
            "contribution_percent": round(abs(value) / denominator * 100, 1),
            "direction": "raise" if value > 0 else "lower",
        } for key, value in ordered]
        reconstructed = (baseline + sum(weighted.values())) * 100 if total_words else risk
        return {
            "summary_text": self._attribution_sentence(risk, groups),
            "groups": groups,
            "method": "exact_random_forest_decision_path",
            "reconstruction_error": max(max_error * 100, abs(reconstructed - risk)),
        }

    @staticmethod
    def _attribution_sentence(risk: float, groups: list[dict]) -> str:
        raisers = [group for group in groups if group["direction"] == "raise"]
        lowers = [group for group in groups if group["direction"] == "lower"]
        if raisers:
            parts = "、".join(
                f"{group['label']}（约 {group['contribution_percent']:.0f}%）"
                for group in raisers[:3]
            )
            sentence = f"整体 AI 风险 {risk:.1f}%，主要推高风险的因素为：{parts}。"
            if lowers:
                sentence += f"其中{lowers[0]['label']}偏低对风险有一定缓冲作用。"
            else:
                sentence += "各维度整体偏向 AI 文本特征。"
            return sentence
        top = lowers[0]["label"] if lowers else "无明显主导维度"
        return (
            f"整体 AI 风险 {risk:.1f}%，未识别到显著抬升因素；"
            f"相对偏向人类写作的维度包括{top}等，建议结合段落明细综合判断。"
        )
