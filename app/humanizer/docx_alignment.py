"""Align variable-cardinality humanizer output back to DOCX paragraphs.

Upstream humanizers may merge, split, or omit structural labels.  The DOCX
renderer deliberately keeps a stable source skeleton, so this module converts
those variable output paragraphs back into one value per source paragraph.
Alignment is order-preserving and never permits a body merge across a layout
gap (image, table, or another skipped Word body node).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.helpers.segmenter import _is_heading


_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[.'-][A-Za-z0-9]+)*")
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)*(?:%|[kKmMbB])?\b")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "for",
    "from", "has", "have", "in", "is", "it", "of", "on", "or", "that",
    "the", "their", "this", "to", "was", "were", "which", "with",
}


@dataclass(frozen=True)
class AlignedParagraph:
    source: dict
    text: str
    was_rewritten: bool


@dataclass(frozen=True)
class DocxAlignment:
    paragraphs: list[AlignedParagraph]
    confidence: float
    operations: tuple[str, ...]


def looks_like_structural_label(paragraph: dict) -> bool:
    """Recognize short Word labels whose style is often incorrectly Normal."""
    text = (paragraph.get("text") or "").strip()
    words = _WORD_RE.findall(text)
    if not text or len(words) > 12 or len(text) > 100:
        return False
    if text.endswith(":"):
        return True
    return bool(re.match(r"^(?:figure|fig\.?|table|chart)\s+\d+\b", text, re.I))


def is_alignment_protected(paragraph: dict) -> bool:
    protected_flags = (
        "is_reference", "is_code_block", "has_image", "has_hyperlink",
        "is_toc", "is_front_matter", "is_caption",
    )
    return bool(
        _is_heading(paragraph)
        or any(paragraph.get(flag) for flag in protected_flags)
        or looks_like_structural_label(paragraph)
    )


def _normalized(text: str) -> str:
    return " ".join(_WORD_RE.findall(_normalize_number_spacing(text).lower()))


def _normalize_number_spacing(text: str) -> str:
    """Normalize comma/space thousands separators used by Huma."""
    value = text or ""
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"(?<=\d)[,\s](?=\d{3}\b)", "", value)
    return value


def _number_markers(text: str) -> set[str]:
    markers = set()
    for raw in _NUMBER_RE.findall(_normalize_number_spacing(text)):
        value = raw.lower().replace(",", "")
        suffix = value[-1:] if value[-1:] in {"k", "m", "b"} else ""
        if suffix:
            try:
                multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix]
                value = str(int(float(value[:-1]) * multiplier))
            except ValueError:
                pass
        markers.add(value)
    return markers


def _content_tokens(text: str) -> set[str]:
    return {
        token for token in _WORD_RE.findall((text or "").lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def _token_overlap(left: str, right: str) -> float:
    left_tokens = _content_tokens(left)
    right_tokens = _content_tokens(right)
    return (
        len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    )


def _has_alignment_evidence(left: str, right: str) -> bool:
    sequence = SequenceMatcher(None, _normalized(left), _normalized(right)).ratio()
    shared_numbers = _number_markers(left) & _number_markers(right)
    return bool(_token_overlap(left, right) >= 0.12 or sequence >= 0.32 or shared_numbers)


def _similarity(left: str, right: str) -> float:
    left_norm = _normalized(left)
    right_norm = _normalized(right)
    if not left_norm or not right_norm:
        return 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    overlap = _token_overlap(left, right)
    length_ratio = min(len(left_norm), len(right_norm)) / max(
        len(left_norm), len(right_norm)
    )
    left_numbers = _number_markers(left)
    right_numbers = _number_markers(right)
    number_score = (
        len(left_numbers & right_numbers) / len(left_numbers)
        if left_numbers else overlap
    )
    return 0.30 * sequence + 0.40 * overlap + 0.15 * length_ratio + 0.15 * number_score


def _critical_coverage(source: str, output: str) -> float:
    markers = _number_markers(source)
    if not markers:
        return 1.0
    output_markers = _number_markers(output)
    return len(markers & output_markers) / len(markers)


def _source_group_is_contiguous(paragraphs: list[dict]) -> bool:
    if any(is_alignment_protected(item) for item in paragraphs):
        return False
    indexes = [item.get("body_index") for item in paragraphs]
    if any(index is None for index in indexes):
        return True
    return all(right == left + 1 for left, right in zip(indexes, indexes[1:]))


def _split_by_weights(text: str, weights: list[int]) -> list[str]:
    """Split one merged result at sentence boundaries, then word boundaries."""
    count = len(weights)
    if count <= 1:
        return [text.strip()]
    sentences = [value.strip() for value in _SENTENCE_RE.split(text.strip()) if value.strip()]
    if len(sentences) >= count:
        total_weight = max(1, sum(weights))
        targets = []
        cumulative = 0
        for weight in weights[:-1]:
            cumulative += weight
            targets.append(len(text) * cumulative / total_weight)
        cumulative_chars = [0]
        for sentence in sentences:
            cumulative_chars.append(cumulative_chars[-1] + len(sentence) + 1)
        boundaries = []
        start = 0
        for boundary_index, target in enumerate(targets):
            remaining_groups = count - boundary_index - 1
            candidates = range(start + 1, len(sentences) - remaining_groups + 1)
            end = min(candidates, key=lambda value: abs(cumulative_chars[value] - target))
            boundaries.append(end)
            start = end
        groups = []
        start = 0
        for end in boundaries + [len(sentences)]:
            groups.append(sentences[start:end])
            start = end
        return [" ".join(group).strip() for group in groups]

    words = text.split()
    if len(words) < count:
        return []
    total_weight = max(1, sum(weights))
    boundaries = []
    cumulative = 0
    for weight in weights[:-1]:
        cumulative += weight
        boundary = round(len(words) * cumulative / total_weight)
        boundary = max(boundaries[-1] + 1 if boundaries else 1, boundary)
        boundary = min(boundary, len(words) - (count - len(boundaries) - 1))
        boundaries.append(boundary)
    result = []
    start = 0
    for boundary in boundaries + [len(words)]:
        result.append(" ".join(words[start:boundary]).strip())
        start = boundary
    return result


def align_docx_output(
    source_paragraphs: list[dict], output_paragraphs: list[str]
) -> DocxAlignment | None:
    """Return a safe one-item-per-source mapping, or ``None`` when uncertain."""
    sources = [item for item in source_paragraphs if (item.get("text") or "").strip()]
    outputs = [item.strip() for item in output_paragraphs if item and item.strip()]
    n, m = len(sources), len(outputs)
    if not sources or not outputs or abs(n - m) > max(4, math.ceil(n * 0.25)):
        return None

    # dp[(i, j)] = (score, operations), where each operation is
    # (source_count, output_count, local_similarity).
    dp: dict[tuple[int, int], tuple[float, list[tuple[int, int, float]]]] = {
        (0, 0): (0.0, [])
    }
    for i in range(n + 1):
        for j in range(m + 1):
            state = dp.get((i, j))
            if state is None:
                continue
            score, path = state

            if i < n and is_alignment_protected(sources[i]):
                candidate = (score + 0.40, path + [(1, 0, 1.0)])
                if candidate[0] > dp.get((i + 1, j), (-math.inf, []))[0]:
                    dp[(i + 1, j)] = candidate

            for source_count, output_count in ((1, 1), (1, 2), (1, 3), (2, 1), (3, 1)):
                if i + source_count > n or j + output_count > m:
                    continue
                source_group = sources[i:i + source_count]
                if source_count > 1 and not _source_group_is_contiguous(source_group):
                    continue
                if output_count > 1 and is_alignment_protected(source_group[0]):
                    continue
                source_text = " ".join(item.get("text") or "" for item in source_group)
                output_text = " ".join(outputs[j:j + output_count])
                local = _similarity(source_text, output_text)
                penalty = 0.10 * (source_count + output_count - 2)
                candidate = (
                    score + local - penalty,
                    path + [(source_count, output_count, local)],
                )
                key = (i + source_count, j + output_count)
                if candidate[0] > dp.get(key, (-math.inf, []))[0]:
                    dp[key] = candidate

    final = dp.get((n, m))
    if final is None:
        return None

    _, path = final
    aligned: list[AlignedParagraph] = []
    operations = []
    similarities = []
    source_cursor = 0
    output_cursor = 0
    for source_count, output_count, local in path:
        source_group = sources[source_cursor:source_cursor + source_count]
        output_group = outputs[output_cursor:output_cursor + output_count]
        source_cursor += source_count
        output_cursor += output_count

        if output_count == 0:
            if source_count != 1 or not is_alignment_protected(source_group[0]):
                return None
            source = source_group[0]
            aligned.append(AlignedParagraph(source, source["text"].strip(), False))
            operations.append("protected_restore")
            continue

        source_text = " ".join(item.get("text") or "" for item in source_group)
        output_text = " ".join(output_group)
        if _critical_coverage(source_text, output_text) < 0.75:
            return None
        if local < 0.20:
            return None

        if source_count > 1:
            # A merge is accepted only when every source paragraph has some
            # evidence in the combined output. This prevents a deleted body
            # paragraph from masquerading as a legitimate merge.
            if any(
                _similarity(item.get("text") or "", output_text) < 0.18
                or not _has_alignment_evidence(item.get("text") or "", output_text)
                   for item in source_group):
                return None
            values = _split_by_weights(
                output_text,
                [max(1, len((item.get("text") or "").split())) for item in source_group],
            )
            if len(values) != source_count:
                return None
            operations.append(f"{source_count}_to_1")
        elif output_count > 1:
            if any(
                _similarity(source_text, item) < 0.14
                or not _has_alignment_evidence(source_text, item)
                for item in output_group
            ):
                return None
            values = [output_text]
            operations.append(f"1_to_{output_count}")
        else:
            values = [output_text]
            operations.append("1_to_1")

        similarities.append(local)
        for source, value in zip(source_group, values):
            if is_alignment_protected(source):
                aligned.append(AlignedParagraph(source, source["text"].strip(), False))
            else:
                aligned.append(AlignedParagraph(source, value.strip(), True))

    if len(aligned) != len(sources):
        return None
    confidence = sum(similarities) / len(similarities) if similarities else 1.0
    if confidence < 0.28:
        return None
    return DocxAlignment(aligned, confidence, tuple(operations))
