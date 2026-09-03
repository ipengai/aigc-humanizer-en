"""Preview helper: strip cover/non-body noise and take leading body words.

Used by the free "first 200 words" rewrite preview so users see a *real*
rewrite of their own text before paying for the full document.

The preview must start from actual body content, not the cover page,
title page, table-of-contents or other front-matter noise that many
uploaded papers/reports carry at the very beginning.
"""

import copy
import re
import threading
import time
from collections import OrderedDict

# Lines that are clearly not body prose and should be skipped while we hunt
# for the real start of the document.
_COVER_HINTS = re.compile(
    r'\b(table of contents|contents|目录|abstract|摘要|keywords|关键词|'
    r'doi|issn|cc by|creative commons|university|college|journal|'
    r'volume|issue|published|©|copyright)\b',
    re.IGNORECASE,
)

_FRONT_MATTER_HEADING = re.compile(
    r'^(table of contents|contents|目录|abstract|摘要|keywords|关键词|'
    r'doi|issn|copyright)\s*[:.-]?$',
    re.IGNORECASE,
)

_BODY_HEADING = re.compile(
    r'^(?:chapter\s+\d+\s*[:.-]?\s*|\d+(?:\.\d+)*\s*[:.-]?\s*)?'
    r'(introduction|background|overview|main text|正文|引言|绪论)\s*[:.-]?$',
    re.IGNORECASE,
)

_PREVIEW_CACHE = OrderedDict()
_PREVIEW_CACHE_LOCK = threading.Lock()
_PREVIEW_CACHE_MAX = 256
_PREVIEW_CACHE_TTL_SECONDS = 2 * 60 * 60


def get_cached_preview(cache_key: str):
    """Return a defensive copy of a recent preview result, if available."""
    now = time.monotonic()
    with _PREVIEW_CACHE_LOCK:
        cached = _PREVIEW_CACHE.get(cache_key)
        if not cached:
            return None
        value, created_at = cached
        if now - created_at > _PREVIEW_CACHE_TTL_SECONDS:
            _PREVIEW_CACHE.pop(cache_key, None)
            return None
        _PREVIEW_CACHE.move_to_end(cache_key)
        return copy.deepcopy(value)


def cache_preview(cache_key: str, value: dict):
    """Cache a preview without mutating the user's filesystem session."""
    with _PREVIEW_CACHE_LOCK:
        _PREVIEW_CACHE[cache_key] = (copy.deepcopy(value), time.monotonic())
        _PREVIEW_CACHE.move_to_end(cache_key)
        while len(_PREVIEW_CACHE) > _PREVIEW_CACHE_MAX:
            _PREVIEW_CACHE.popitem(last=False)


def clear_preview_cache():
    """Clear the process-local cache (primarily for deterministic tests)."""
    with _PREVIEW_CACHE_LOCK:
        _PREVIEW_CACHE.clear()


def _looks_like_front_matter(paragraph: str) -> bool:
    """Identify leading metadata, title-page and table-of-contents blocks."""
    lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
    if not lines:
        return True

    if _FRONT_MATTER_HEADING.match(lines[0]):
        return True

    # Title pages and flattened TOCs often contain many short lines but no
    # prose-like sentences. Do not let their combined word count masquerade
    # as a long body paragraph.
    words = paragraph.split()
    leading_text = ' '.join(lines[:4])
    if len(lines) >= 2 and _COVER_HINTS.search(leading_text) \
            and len(words) / len(lines) < 12:
        return True
    if len(lines) >= 3 and len(words) / len(lines) < 8:
        return True
    return False


def _looks_like_body(paragraph: str) -> bool:
    """Heuristic: is this paragraph the start of real body prose?"""
    if _looks_like_front_matter(paragraph):
        return False
    words = paragraph.split()
    if len(words) < 6:
        return False
    # A real body paragraph is a complete sentence (ends with punctuation)
    # or is reasonably long even without terminal punctuation.
    if paragraph.rstrip().endswith(('.', '。', '!', '?', '！', '？')):
        return True
    return len(words) >= 20


def _structured_preview(paragraphs, max_words):
    """Prefer extractor structure over guessing from flattened PDF text."""
    candidates = []
    body_heading_seen = False
    in_front_matter_section = False
    for node in paragraphs or []:
        text = (node.get('text') or '').strip()
        if not text:
            continue
        if node.get('is_heading'):
            if _BODY_HEADING.match(text) or _BODY_HEADING.match(
                    re.sub(r'^\d+(?:\.\d+)*\s+', '', text)):
                body_heading_seen = True
                in_front_matter_section = False
            elif _FRONT_MATTER_HEADING.match(text):
                # An Abstract/Keywords heading is not enough on its own: its
                # following prose must also remain outside a body preview.
                in_front_matter_section = True
            continue
        if any(node.get(flag) for flag in (
            'is_toc', 'is_front_matter', 'is_reference', 'is_caption',
            'is_code_block',
        )):
            continue
        if in_front_matter_section and not body_heading_seen:
            continue
        if not candidates and not (_looks_like_body(text) or body_heading_seen):
            continue
        candidates.append(text)

    words = '\n\n'.join(candidates).split()
    return ' '.join(words[:max_words])


def extract_body_preview(full_text: str, max_words: int = 200,
                         paragraphs=None) -> str:
    """Return the first ``max_words`` words of the document *body*.

    Skips leading cover/title/TOC noise by advancing past short non-prose
    lines until the first paragraph that looks like real body text.
    """
    if paragraphs:
        structured = _structured_preview(paragraphs, max_words)
        if structured:
            return structured

    if not full_text or not full_text.strip():
        return ''

    # Split into paragraphs: prefer hard breaks (blank lines), fall back to
    # single newlines (common when PDF/DOCX text is flattened to one line each).
    paras = re.split(r'\n\s*\n', full_text)
    if len(paras) <= 1:
        paras = full_text.split('\n')
    paras = [p.strip() for p in paras if p and p.strip()]

    if not paras:
        return ''

    # Find the first body paragraph; skip cover/title/TOC blocks before it.
    # A recognized body heading lets the following paragraph start the preview
    # even when that first paragraph happens to be short.
    start_idx = None
    body_heading_seen = False
    first_non_front_matter = None
    for i, p in enumerate(paras):
        if _BODY_HEADING.match(p.strip()):
            body_heading_seen = True
            continue
        if _looks_like_front_matter(p):
            continue
        if first_non_front_matter is None:
            first_non_front_matter = i
        if _looks_like_body(p) or (body_heading_seen and len(p.split()) >= 6):
            start_idx = i
            break

    # Very short documents may contain no sentence-like paragraph. Prefer the
    # first non-front-matter block; only fall back to the top when every block
    # looked like front matter.
    if start_idx is None:
        if first_non_front_matter is None:
            return ''
        start_idx = first_non_front_matter

    body = '\n\n'.join(paras[start_idx:])
    words = body.split()
    if len(words) <= max_words:
        return body
    return ' '.join(words[:max_words])
