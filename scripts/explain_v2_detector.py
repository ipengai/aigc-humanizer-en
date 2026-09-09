#!/usr/bin/env python3
"""Explain turnitin_fit_detector_v2 predictions with tree path contributions.

This is a deterministic, dependency-free (beyond the detector's existing runtime)
local explanation. It follows every tree's actual decision path and attributes each
parent-to-child probability change to the feature used at that split. Averaging the
changes over all trees reconstructs RandomForestClassifier.predict_proba exactly.

The method is similar to TreeInterpreter/Saabas path attribution. It is useful for
diagnosis, but it is not exact TreeSHAP and must not be interpreted causally.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import joblib
import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ai_detector.vendor.turnitin_fit_detector_v2 import random_forest_detector as detector  # noqa: E402


DEFAULT_METADATA = (
    PROJECT_ROOT
    / "app"
    / "ai_detector"
    / "vendor"
    / "turnitin_fit_detector_v2"
    / "random_forest_detector.json"
)

FEATURE_LABELS = {
    "word_count": "段落词数",
    "sentence_count": "句子数量",
    "sentence_length_mean": "平均句长",
    "sentence_length_std": "句长标准差",
    "sentence_length_cv": "句长变异系数",
    "sentence_length_min": "最短句长度",
    "sentence_length_max": "最长句长度",
    "short_sentence_ratio": "短句占比（≤12词）",
    "long_sentence_ratio": "长句占比（≥30词）",
    "average_word_length": "平均词长",
    "long_word_ratio": "长词占比（≥7字母）",
    "type_token_ratio": "词汇多样性",
    "function_word_ratio": "功能词占比",
    "transition_ratio": "显式连接词占比",
    "hedge_ratio": "模糊/缓和词占比",
    "first_person_ratio": "第一人称占比",
    "number_ratio": "数字占比",
    "citation_ratio": "括号年份引用占比",
    "semicolon_per_sentence": "每句分号数",
    "colon_per_sentence": "每句冒号数",
    "dash_per_sentence": "每句破折号数",
    "question_per_sentence": "每句问号数",
    "repeated_bigram_ratio": "重复二元词组比例",
    "repeated_sentence_start_ratio": "重复句首比例",
    "hapax_ratio": "仅出现一次词占比",
    "most_common_word_ratio": "最高频词占比",
    "lexical_entropy": "词汇熵",
    "contraction_ratio": "英文缩写形式占比",
    "adverb_ly_ratio": "-ly副词占比",
    "nominalization_ratio": "名词化词尾占比",
    "passive_proxy_per_sentence": "被动语态近似频率",
    "comma_per_sentence": "每句逗号数",
    "parenthesis_per_sentence": "每句左括号数",
    "quote_per_sentence": "每句引号数",
    "exclamation_per_sentence": "每句感叹号数",
    "sentence_length_median": "句长中位数",
    "sentence_length_iqr": "句长四分位距",
    "demonstrative_sentence_start_ratio": "指示词句首占比",
    "modal_ratio": "情态动词占比",
    "negation_ratio": "否定词占比",
    "relative_paragraph_position": "段落相对位置",
    "neighbor_count": "参与上下文计算的邻段数",
    "section_paragraph_count": "可检测段落总数",
}


def _class_index(classes: np.ndarray, positive_class: int = 1) -> int:
    matches = np.flatnonzero(classes == positive_class)
    if len(matches) != 1:
        raise ValueError(f"模型类别中找不到唯一的正类 {positive_class}: {classes!r}")
    return int(matches[0])


def _node_probability(tree: Any, node_id: int, class_index: int) -> float:
    counts = tree.value[node_id][0]
    total = float(np.sum(counts))
    return float(counts[class_index] / total) if total else 0.0


def forest_path_contributions(
    classifier: Any,
    matrix: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return baseline, per-feature path contributions and positive probability.

    ``baseline + contributions.sum(axis=1)`` is checked against
    ``classifier.predict_proba`` and raises if the model is incompatible.
    """

    # sklearn's forest prediction validates dense input as float32 before each
    # tree is traversed. Mirroring that conversion matters for values extremely
    # close to a split threshold; float64 traversal can otherwise choose a
    # different child in an individual tree.
    traversal_matrix = np.asarray(matrix, dtype=np.float32)
    rows, features = traversal_matrix.shape
    baselines = np.zeros(rows, dtype=float)
    contributions = np.zeros((rows, features), dtype=float)
    positive_index = _class_index(classifier.classes_)

    for estimator in classifier.estimators_:
        tree = estimator.tree_
        tree_positive_index = _class_index(estimator.classes_)
        root_probability = _node_probability(tree, 0, tree_positive_index)
        baselines += root_probability

        for row_index, values in enumerate(traversal_matrix):
            node_id = 0
            parent_probability = root_probability
            while tree.feature[node_id] >= 0:
                feature_index = int(tree.feature[node_id])
                if values[feature_index] <= tree.threshold[node_id]:
                    child_id = int(tree.children_left[node_id])
                else:
                    child_id = int(tree.children_right[node_id])
                child_probability = _node_probability(tree, child_id, tree_positive_index)
                contributions[row_index, feature_index] += child_probability - parent_probability
                node_id = child_id
                parent_probability = child_probability

    tree_count = len(classifier.estimators_)
    baselines /= tree_count
    contributions /= tree_count
    probabilities = classifier.predict_proba(traversal_matrix)[:, positive_index]
    reconstructed = baselines + contributions.sum(axis=1)
    if not np.allclose(reconstructed, probabilities, atol=1e-10, rtol=1e-10):
        maximum_error = float(np.max(np.abs(reconstructed - probabilities)))
        raise RuntimeError(f"归因贡献无法重建模型概率，最大误差={maximum_error:.12g}")
    return baselines, contributions, probabilities


def _feature_label(name: str) -> str:
    if name.startswith("neighbor_mean_"):
        base = name.removeprefix("neighbor_mean_")
        return f"邻段平均·{FEATURE_LABELS.get(base, base)}"
    return FEATURE_LABELS.get(name, name)


def _grouped_features(names: list[str], contributions: np.ndarray, limit: int) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {"local": 0.0, "neighbor": 0.0})
    for name, contribution in zip(names, contributions):
        if name.startswith("neighbor_mean_"):
            base_name = name.removeprefix("neighbor_mean_")
            grouped[base_name]["neighbor"] += float(contribution)
        else:
            base_name = name
            grouped[base_name]["local"] += float(contribution)
    rows = [
        {
            "feature_group": name,
            "label_zh": FEATURE_LABELS.get(name, name),
            "contribution": values["local"] + values["neighbor"],
            "contribution_pp": (values["local"] + values["neighbor"]) * 100,
            "local_contribution_pp": values["local"] * 100,
            "neighbor_contribution_pp": values["neighbor"] * 100,
        }
        for name, values in grouped.items()
    ]
    return {
        "positive": sorted((row for row in rows if row["contribution"] > 0), key=lambda row: row["contribution"], reverse=True)[:limit],
        "negative": sorted((row for row in rows if row["contribution"] < 0), key=lambda row: row["contribution"])[:limit],
    }


def _ranked_features(
    names: list[str],
    values: np.ndarray,
    contributions: np.ndarray,
    *,
    descending: bool,
    limit: int,
) -> list[dict[str, Any]]:
    indexes = np.argsort(contributions)
    if descending:
        indexes = indexes[::-1]
        indexes = [index for index in indexes if contributions[index] > 0]
    else:
        indexes = [index for index in indexes if contributions[index] < 0]
    return [
        {
            "feature": names[index],
            "label_zh": _feature_label(names[index]),
            "value": float(values[index]),
            "contribution": float(contributions[index]),
            "contribution_pp": float(contributions[index] * 100),
        }
        for index in indexes[:limit]
    ]


def explain_text(
    text: str,
    metadata_path: Path = DEFAULT_METADATA,
    *,
    top: int = 8,
) -> dict[str, Any]:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    feature_names = list(metadata["feature_names"])
    if feature_names != list(detector.FEATURE_NAMES):
        raise ValueError("模型元数据与当前特征提取代码不一致")

    rows, total_words = detector.paragraph_rows(text)
    minimum_words = int(metadata["paragraph_rules"]["minimum_words"])
    supported = [row for row in rows if row["word_count"] >= minimum_words]
    if not supported:
        return {
            "summary": {
                "model_version": metadata.get("model_version", "unknown"),
                "risk_percent": None,
                "paragraphs": 0,
                "analyzed_words": 0,
                "input_words": total_words,
                "coverage": 0.0,
                "reason": f"没有达到最小 {minimum_words} 词的可检测段落",
            },
            "document_features": {"positive": [], "negative": []},
            "paragraphs": [],
        }

    classifier = joblib.load(metadata_path.with_name(metadata["model_file"]))
    matrix = detector.context_matrix(supported)
    baselines, contributions, probabilities = forest_path_contributions(classifier, matrix)
    weights = np.array([row["word_count"] for row in supported], dtype=float)
    weights /= weights.sum()
    document_baseline = float(np.dot(weights, baselines))
    document_contributions = np.average(contributions, axis=0, weights=weights)
    document_probability = float(np.dot(weights, probabilities))
    reconstructed = document_baseline + float(document_contributions.sum())

    paragraph_details = []
    for index, row in enumerate(supported):
        paragraph_details.append(
            {
                "paragraph_id": row["id"],
                "word_count": row["word_count"],
                "ai_probability": float(probabilities[index]),
                "ai_percent": float(probabilities[index] * 100),
                "baseline_probability": float(baselines[index]),
                "top_positive_features": _ranked_features(
                    feature_names, matrix[index], contributions[index], descending=True, limit=top
                ),
                "top_negative_features": _ranked_features(
                    feature_names, matrix[index], contributions[index], descending=False, limit=top
                ),
                "text_preview": row["text"][:240].replace("\n", " "),
            }
        )

    grouped_document_features = _grouped_features(feature_names, document_contributions, top)
    return {
        "summary": {
            "model_version": metadata.get("model_version", "unknown"),
            "method": "random_forest_decision_path_contribution",
            "method_note": "TreeInterpreter/Saabas-like path attribution; not exact TreeSHAP and not causal.",
            "risk_score": document_probability,
            "risk_percent": document_probability * 100,
            "baseline_probability": document_baseline,
            "baseline_percent": document_baseline * 100,
            "reconstructed_risk_score": reconstructed,
            "reconstruction_error": abs(reconstructed - document_probability),
            "paragraphs": len(supported),
            "analyzed_words": int(sum(row["word_count"] for row in supported)),
            "input_words": total_words,
            "coverage": float(sum(row["word_count"] for row in supported) / max(total_words, 1)),
        },
        "document_features": {
            "positive": _ranked_features(
                feature_names,
                np.average(matrix, axis=0, weights=weights),
                document_contributions,
                descending=True,
                limit=top,
            ),
            "negative": _ranked_features(
                feature_names,
                np.average(matrix, axis=0, weights=weights),
                document_contributions,
                descending=False,
                limit=top,
            ),
        },
        "document_feature_groups": grouped_document_features,
        "paragraphs": paragraph_details,
    }


def explain_files(
    inputs: Iterable[tuple[str, Path]],
    metadata_path: Path = DEFAULT_METADATA,
    *,
    top: int = 8,
) -> dict[str, Any]:
    results = []
    for label, path in inputs:
        explanation = explain_text(path.read_text(encoding="utf-8"), metadata_path, top=top)
        results.append({"label": label, "path": str(path.resolve()), **explanation})
    return {
        "detector": "turnitin_fit_detector_v2",
        "model_path": str(metadata_path.resolve()),
        "explanation_method": "random_forest_decision_path_contribution",
        "items": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# `turnitin_fit_detector_v2` 局部归因报告",
        "",
        "> 归因方法沿随机森林每棵树的实际决策路径分摊概率变化，贡献之和可精确重建模型分数。它类似 TreeInterpreter/Saabas，不是精确 TreeSHAP，也不表示因果关系。",
        "",
        "## 汇总",
        "",
        "| 文本 | v2风险分 | 覆盖率 | 可检测段落 | 模型基线 | 重建误差 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in report["items"]:
        summary = item["summary"]
        risk = "—" if summary["risk_percent"] is None else f'{summary["risk_percent"]:.1f}%'
        lines.append(
            f'| {item["label"]} | {risk} | {summary["coverage"]:.1%} | '
            f'{summary["paragraphs"]} | {summary.get("baseline_percent", 0):.1f}% | '
            f'{summary.get("reconstruction_error", 0):.2e} |'
        )

    for item in report["items"]:
        summary = item["summary"]
        lines.extend(["", f'## {item["label"]}', ""])
        if summary["risk_percent"] is None:
            lines.append(f'无法归因：{summary.get("reason", "没有可检测段落")}。')
            continue
        lines.extend([
            f'- v2 风险分：**{summary["risk_percent"]:.1f}%**；覆盖率：{summary["coverage"]:.1%}。',
            f'- 模型平均起点：{summary["baseline_percent"]:.1f}%；全部贡献合计后重建误差：{summary["reconstruction_error"]:.2e}。',
            "",
            "### 全文主要推高因素（本段与邻段同类特征合并）",
            "",
            "| 因素 | 合计贡献 | 本段部分 | 邻段部分 |",
            "|---|---:|---:|---:|",
        ])
        for feature in item["document_feature_groups"]["positive"]:
            lines.append(
                f'| {feature["label_zh"]} (`{feature["feature_group"]}`) | '
                f'+{feature["contribution_pp"]:.2f}pp | {feature["local_contribution_pp"]:+.2f}pp | '
                f'{feature["neighbor_contribution_pp"]:+.2f}pp |'
            )
        lines.extend(["", "### 全文主要压低因素（本段与邻段同类特征合并）", "", "| 因素 | 合计贡献 | 本段部分 | 邻段部分 |", "|---|---:|---:|---:|"])
        for feature in item["document_feature_groups"]["negative"]:
            lines.append(
                f'| {feature["label_zh"]} (`{feature["feature_group"]}`) | '
                f'{feature["contribution_pp"]:.2f}pp | {feature["local_contribution_pp"]:+.2f}pp | '
                f'{feature["neighbor_contribution_pp"]:+.2f}pp |'
            )
        lines.extend(["", "### 高风险段落", "", "| 段落 | 词数 | 风险分 | 最主要推高因素 | 文本开头 |", "|---|---:|---:|---|---|"])
        paragraphs = sorted(item["paragraphs"], key=lambda row: row["ai_probability"], reverse=True)
        for paragraph in paragraphs[: min(10, len(paragraphs))]:
            drivers = "；".join(
                f'{feature["label_zh"]} +{feature["contribution_pp"]:.1f}pp'
                for feature in paragraph["top_positive_features"][:3]
            )
            preview = paragraph["text_preview"].replace("|", "\\|")
            lines.append(
                f'| {paragraph["paragraph_id"]} | {paragraph["word_count"]} | '
                f'{paragraph["ai_percent"]:.1f}% | {drivers} | {preview} |'
            )
    lines.append("")
    return "\n".join(lines)


def _parse_inputs(values: list[str]) -> list[tuple[str, Path]]:
    parsed = []
    for value in values:
        if "=" in value:
            label, raw_path = value.split("=", 1)
        else:
            raw_path = value
            path = Path(raw_path)
            label = f"{path.parent.name}/{path.stem}"
        path = Path(raw_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        parsed.append((label, path))
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="解释 turnitin_fit_detector_v2 的特征贡献；输入可写成 标签=文件路径。"
    )
    parser.add_argument("inputs", nargs="+", help="UTF-8 文本路径，或 标签=文本路径")
    parser.add_argument("--model", type=Path, default=DEFAULT_METADATA, help="模型元数据 JSON")
    parser.add_argument("--top", type=int, default=8, help="每个方向保留的特征数")
    parser.add_argument("--json-out", type=Path, help="保存完整 JSON 归因")
    parser.add_argument("--markdown-out", type=Path, help="保存便于阅读的 Markdown 报告")
    args = parser.parse_args()

    report = explain_files(_parse_inputs(args.inputs), args.model, top=max(args.top, 1))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if not args.json_out and not args.markdown_out:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["items"]:
            risk = item["summary"]["risk_percent"]
            rendered_risk = "无法检测" if risk is None else f"{risk:.1f}%"
            print(f'{item["label"]}: {rendered_risk}, coverage={item["summary"]["coverage"]:.1%}')


if __name__ == "__main__":
    main()
