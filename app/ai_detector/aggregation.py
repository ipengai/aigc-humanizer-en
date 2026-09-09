"""Detector-only paragraph aggregation for low-coverage v2 inputs."""

from __future__ import annotations

import re


WORDS = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def _word_count(text):
    return len(WORDS.findall(text or ""))


def aggregate_short_paragraphs(text, minimum_words=40, maximum_words=250):
    """Join adjacent short paragraphs without changing the delivered document.

    The result is used only as detector input. Long paragraphs remain intact;
    short tails merge backwards when doing so stays within the model limit.
    """
    blocks = [
        value.strip()
        for value in re.split(r"\n\s*\n+", text or "")
        if value.strip()
    ]
    if len(blocks) < 2:
        return text or ""

    groups = []
    pending = []
    pending_words = 0

    def flush():
        nonlocal pending, pending_words
        if pending:
            groups.append(" ".join(pending))
            pending = []
            pending_words = 0

    for block in blocks:
        words = _word_count(block)
        if words >= minimum_words:
            flush()
            groups.append(block)
            continue
        if pending and pending_words + words > maximum_words:
            flush()
        pending.append(block)
        pending_words += words
        if pending_words >= minimum_words:
            flush()
    flush()

    if len(groups) > 1 and _word_count(groups[-1]) < minimum_words:
        combined_words = _word_count(groups[-2]) + _word_count(groups[-1])
        if combined_words <= maximum_words:
            groups[-2:] = [groups[-2] + " " + groups[-1]]
    return "\n\n".join(groups)
