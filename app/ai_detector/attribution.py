"""Local decision-path attribution for turnitin_fit_detector_v2."""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from app.ai_detector.aggregation import aggregate_short_paragraphs
from app.ai_detector.vendor.turnitin_fit_detector_v2 import random_forest_detector as detector


def _class_index(classes, positive=1):
    matches = np.flatnonzero(classes == positive)
    if len(matches) != 1:
        raise ValueError(f"模型类别中找不到唯一正类 {positive}")
    return int(matches[0])


def _node_probability(tree, node_id, class_index):
    values = tree.value[node_id][0]
    total = float(np.sum(values))
    return float(values[class_index] / total) if total else 0.0


def forest_path_contributions(classifier, matrix):
    """Return contributions that exactly reconstruct forest probabilities."""
    values = np.asarray(matrix, dtype=np.float32)
    baselines = np.zeros(len(values), dtype=float)
    contributions = np.zeros_like(values, dtype=float)
    positive_index = _class_index(classifier.classes_)
    for estimator in classifier.estimators_:
        tree = estimator.tree_
        tree_positive = _class_index(estimator.classes_)
        root = _node_probability(tree, 0, tree_positive)
        baselines += root
        for row_index, row in enumerate(values):
            node = 0
            parent = root
            while tree.feature[node] >= 0:
                feature_index = int(tree.feature[node])
                node = int(
                    tree.children_left[node]
                    if row[feature_index] <= tree.threshold[node]
                    else tree.children_right[node]
                )
                child = _node_probability(tree, node, tree_positive)
                contributions[row_index, feature_index] += child - parent
                parent = child
    tree_count = len(classifier.estimators_)
    baselines /= tree_count
    contributions /= tree_count
    probabilities = classifier.predict_proba(values)[:, positive_index]
    if not np.allclose(
        baselines + contributions.sum(axis=1), probabilities,
        atol=1e-10, rtol=1e-10,
    ):
        raise RuntimeError("v2 归因贡献无法重建模型分数")
    return baselines, contributions, probabilities


def _group_contributions(feature_names, contributions, *, direction="positive"):
    grouped = defaultdict(float)
    for name, contribution in zip(feature_names, contributions):
        base = name.removeprefix("neighbor_mean_")
        grouped[base] += float(contribution)
    values = [
        {"feature_group": name, "contribution": value, "contribution_pp": value * 100}
        for name, value in grouped.items()
        if (value > 0 if direction == "positive" else value < 0)
    ]
    return sorted(
        values,
        key=lambda item: item["contribution"],
        reverse=direction == "positive",
    )


def explain_blocks(texts, runtime, top=3):
    """Explain ordered rewrite blocks in one shared detector context."""
    minimum = int(runtime.metadata["paragraph_rules"]["minimum_words"])
    supported = []
    for source_index, text in enumerate(texts):
        rows, _ = detector.paragraph_rows(text or "")
        analyzed_words = sum(row["word_count"] for row in rows if row["word_count"] >= minimum)
        total_words = len(detector.WORDS.findall(text or ""))
        if total_words and analyzed_words / total_words < 0.60:
            rows, _ = detector.paragraph_rows(aggregate_short_paragraphs(text or ""))
        for row in rows:
            if row["word_count"] >= minimum:
                row = dict(row)
                row["source_index"] = source_index
                supported.append(row)
    if not supported:
        return []
    matrix = detector.context_matrix(supported)
    baselines, contributions, probabilities = forest_path_contributions(
        runtime.classifier, matrix
    )
    feature_names = list(runtime.metadata["feature_names"])
    chunks = defaultdict(list)
    for row, baseline, probability, row_contributions in zip(
        supported, baselines, probabilities, contributions
    ):
        source_index = row["source_index"]
        chunks[source_index].append(
            (row, float(baseline), float(probability), row_contributions)
        )

    output = []
    for source_index, text in enumerate(texts):
        block_chunks = chunks.get(source_index, [])
        if not block_chunks:
            continue
        words = sum(item[0]["word_count"] for item in block_chunks)
        baseline = sum(
            item[1] * item[0]["word_count"] for item in block_chunks
        ) / words
        probability = sum(
            item[2] * item[0]["word_count"] for item in block_chunks
        ) / words
        aggregate = sum(
            item[3] * item[0]["word_count"] for item in block_chunks
        ) / words
        output.append({
            "source_index": source_index,
            "word_count": words,
            "risk_percent": probability * 100,
            "baseline_probability": baseline,
            "reconstruction_error": abs(
                baseline + float(np.sum(aggregate)) - probability
            ),
            "top_feature_groups": _group_contributions(
                feature_names, aggregate, direction="positive"
            )[:top],
            # Negative contributions are features currently keeping this block's
            # risk down. The rewrite prompt must preserve them while correcting
            # the positive contributors.
            "protect_feature_groups": _group_contributions(
                feature_names, aggregate, direction="negative"
            )[:top],
            "text": text,
        })
    return output


def feature_profile(text, runtime):
    """Return word-weighted local feature values for prompt feedback."""
    minimum = int(runtime.metadata["paragraph_rules"]["minimum_words"])
    rows, total_words = detector.paragraph_rows(text or "")
    supported = [row for row in rows if row["word_count"] >= minimum]
    analyzed_words = sum(row["word_count"] for row in supported)
    if total_words and analyzed_words / total_words < 0.60:
        rows, _ = detector.paragraph_rows(aggregate_short_paragraphs(text or ""))
        supported = [row for row in rows if row["word_count"] >= minimum]
    if not supported:
        return {}
    matrix = detector.context_matrix(supported)
    weights = np.array([row["word_count"] for row in supported], dtype=float)
    local = np.average(
        matrix[:, :len(detector.LOCAL_FEATURES)], axis=0, weights=weights
    )
    return {
        name: float(value)
        for name, value in zip(detector.LOCAL_FEATURES, local)
    }
