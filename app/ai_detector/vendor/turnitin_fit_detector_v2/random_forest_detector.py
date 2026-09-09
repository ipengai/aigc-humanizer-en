#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import joblib
import numpy as np


WORDS = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
FUNCTION_WORDS = set("a an and are as at be been but by for from had has have he her his i in is it its of on or our she that the their they this to was we were which with you".split())
TRANSITIONS = set("additionally consequently conversely furthermore hence however moreover nevertheless therefore thus overall similarly specifically".split())
HEDGES = set("apparently approximately arguably could generally likely may might often perhaps probably relatively seems suggests typically".split())
FIRST_PERSON = {"i", "me", "my", "mine", "we", "us", "our", "ours"}
STRUCTURE_FEATURES = (
    "word_count", "sentence_count", "sentence_length_mean", "sentence_length_std",
    "sentence_length_cv", "sentence_length_min", "sentence_length_max",
    "short_sentence_ratio", "long_sentence_ratio",
)
EXPLICIT_FEATURES = (
    "average_word_length", "long_word_ratio", "type_token_ratio",
    "function_word_ratio", "transition_ratio", "hedge_ratio", "first_person_ratio",
    "number_ratio", "citation_ratio", "semicolon_per_sentence", "colon_per_sentence",
    "dash_per_sentence", "question_per_sentence", "repeated_bigram_ratio",
    "repeated_sentence_start_ratio",
)
ADDED_FEATURES = (
    "hapax_ratio", "most_common_word_ratio", "lexical_entropy", "contraction_ratio",
    "adverb_ly_ratio", "nominalization_ratio", "passive_proxy_per_sentence",
    "comma_per_sentence", "parenthesis_per_sentence", "quote_per_sentence",
    "exclamation_per_sentence", "sentence_length_median", "sentence_length_iqr",
    "demonstrative_sentence_start_ratio", "modal_ratio", "negation_ratio",
)
LOCAL_FEATURES = STRUCTURE_FEATURES + EXPLICIT_FEATURES + ADDED_FEATURES
FEATURE_NAMES = LOCAL_FEATURES + tuple(f"neighbor_mean_{name}" for name in LOCAL_FEATURES) + (
    "relative_paragraph_position", "neighbor_count", "section_paragraph_count",
)


def sentence_texts(text: str) -> list[str]:
    return [
        value for value in re.split(r"(?<=[.!?])\s+(?=[\"'(]*[A-Z0-9])", re.sub(r"\s+", " ", text).strip())
        if value
    ]


def split_long_block(block: str, maximum: int = 250) -> list[str]:
    sentences = sentence_texts(block)
    chunks: list[list[str]] = []
    current: list[str] = []
    words = 0
    for sentence in sentences:
        count = len(WORDS.findall(sentence))
        if current and words + count > maximum:
            chunks.append(current)
            current, words = [], 0
        current.append(sentence)
        words += count
    if current:
        chunks.append(current)
    return [" ".join(chunk) for chunk in chunks]


def paragraph_rows(text: str) -> tuple[list[dict], int]:
    total_words = len(WORDS.findall(text))
    blocks = [value.strip() for value in re.split(r"\n\s*\n+", text) if value.strip()]
    rows = []
    for source_index, block in enumerate(blocks, 1):
        for chunk_index, chunk in enumerate(split_long_block(block), 1):
            sentences = sentence_texts(chunk)
            lengths = [len(WORDS.findall(value)) for value in sentences if WORDS.findall(value)]
            if not lengths:
                continue
            mean = float(np.mean(lengths))
            std = float(np.std(lengths)) if len(lengths) > 1 else 0.0
            rows.append({
                "id": f"p{source_index:04d}_c{chunk_index:02d}", "text": chunk,
                "word_count": sum(lengths), "sentence_count": len(lengths),
                "sentence_length_mean": mean, "sentence_length_std": std,
                "sentence_length_cv": std / mean if mean else 0.0,
                "sentence_length_min": min(lengths), "sentence_length_max": max(lengths),
                "short_sentence_ratio": sum(value <= 12 for value in lengths) / len(lengths),
                "long_sentence_ratio": sum(value >= 30 for value in lengths) / len(lengths),
            })
    return rows, total_words


def explicit_features(text: str) -> np.ndarray:
    words = [value.lower() for value in WORDS.findall(text)]
    count = max(len(words), 1)
    sentences = max(len(sentence_texts(text)), 1)
    bigrams = list(zip(words, words[1:]))
    starts = re.findall(r"(?:^|[.!?]\s+)([A-Za-z]+)", text)
    return np.array([
        sum(map(len, words)) / count, sum(len(word) >= 7 for word in words) / count,
        len(set(words)) / count, sum(word in FUNCTION_WORDS for word in words) / count,
        sum(word in TRANSITIONS for word in words) / count, sum(word in HEDGES for word in words) / count,
        sum(word in FIRST_PERSON for word in words) / count,
        len(re.findall(r"\b\d+(?:\.\d+)?\b", text)) / count,
        len(re.findall(r"\([^)]*(?:19|20)\d{2}[^)]*\)", text)) / count,
        text.count(";") / sentences, text.count(":") / sentences,
        len(re.findall(r"[—–]", text)) / sentences, text.count("?") / sentences,
        1.0 - len(set(bigrams)) / max(len(bigrams), 1),
        1.0 - len(set(value.lower() for value in starts)) / max(len(starts), 1),
    ], dtype=float)


def added_features(text: str) -> np.ndarray:
    words = [value.lower() for value in WORDS.findall(text)]
    count = max(len(words), 1)
    counts = Counter(words)
    sentences = sentence_texts(text)
    sentence_count = max(len(sentences), 1)
    lengths = np.array([len(WORDS.findall(value)) for value in sentences]) if sentences else np.array([0])
    entropy = -sum((value / count) * math.log(value / count) for value in counts.values())
    return np.array([
        sum(value == 1 for value in counts.values()) / count, max(counts.values(), default=0) / count,
        entropy, len(re.findall(r"\b\w+'(?:t|s|re|ve|ll|d|m)\b", text.lower())) / count,
        len(re.findall(r"\b\w+ly\b", text.lower())) / count,
        len(re.findall(r"\b\w+(?:tion|ment|ness|ity|ance|ence)\b", text.lower())) / count,
        len(re.findall(r"\b(?:is|are|was|were|be|been|being)\s+\w+(?:ed|en)\b", text.lower())) / sentence_count,
        text.count(",") / sentence_count, text.count("(") / sentence_count,
        (text.count('"') + text.count("“") + text.count("”")) / sentence_count,
        text.count("!") / sentence_count, float(np.median(lengths)),
        float(np.percentile(lengths, 75) - np.percentile(lengths, 25)),
        sum(value.strip().lower().startswith(("the ", "this ", "these ", "it ", "there ")) for value in sentences) / sentence_count,
        len(re.findall(r"\b(?:can|could|may|might|must|shall|should|will|would)\b", text.lower())) / count,
        len(re.findall(r"\b(?:not|no|never|neither|nor|without)\b", text.lower())) / count,
    ], dtype=float)


def context_matrix(rows: list[dict], radius: int = 2) -> np.ndarray:
    local = np.vstack([
        np.concatenate([np.array([row[name] for name in STRUCTURE_FEATURES]), explicit_features(row["text"]), added_features(row["text"])])
        for row in rows
    ])
    output = []
    for index, vector in enumerate(local):
        neighbors = list(range(max(0, index - radius), index)) + list(range(index + 1, min(len(rows), index + radius + 1)))
        neighbor_mean = local[neighbors].mean(axis=0) if neighbors else vector
        output.append(np.concatenate([
            vector, neighbor_mean,
            np.array([index / max(len(rows) - 1, 1), len(neighbors), len(rows)]),
        ]))
    return np.vstack(output)


def detect(text: str, metadata_path: Path, *, metadata=None, classifier=None) -> dict:
    metadata = metadata or json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata["feature_names"] != list(FEATURE_NAMES):
        raise ValueError("模型与脚本的特征版本不一致")
    rows, total_words = paragraph_rows(text)
    minimum = int(metadata["paragraph_rules"]["minimum_words"])
    supported = [row for row in rows if row["word_count"] >= minimum]
    if not supported:
        return {"summary": {"risk_score": None, "paragraphs": 0, "analyzed_words": 0, "coverage": 0}, "paragraphs": []}
    if classifier is None:
        classifier = joblib.load(metadata_path.with_name(metadata["model_file"]))
    probabilities = classifier.predict_proba(context_matrix(supported))[:, 1]
    threshold = float(metadata["decision_threshold"])
    details = [{
        "paragraph_id": row["id"], "word_count": row["word_count"],
        "ai_probability": float(probability), "label": int(probability >= threshold),
        "text": row["text"],
    } for row, probability in zip(supported, probabilities)]
    analyzed_words = sum(row["word_count"] for row in supported)
    score = sum(row["ai_probability"] * row["word_count"] for row in details) / analyzed_words
    return {
        "summary": {
            "model_version": metadata.get("model_version", "unknown"),
            "risk_score": score,
            "risk_percent": score * 100,
            "aggregation": "word_weighted_continuous_probability",
            "paragraphs": len(details), "analyzed_words": analyzed_words,
            "input_words": total_words, "coverage": analyzed_words / max(total_words, 1),
            "high_risk_paragraphs": sum(row["label"] for row in details),
            "warning": "实验性本地风险分，不等于 Turnitin 官方 AI 百分比。段落标签仅用于定位。",
        },
        "paragraphs": details,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="英文段落随机森林 AI 风险检测器")
    parser.add_argument("--file", required=True, help="UTF-8 英文纯文本；空行分隔自然段")
    parser.add_argument("--model", default=str(Path(__file__).with_name("random_forest_detector.json")))
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    result = detect(Path(args.file).read_text(encoding="utf-8"), Path(args.model))
    print(json.dumps(result["summary"] if args.summary_only else result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
