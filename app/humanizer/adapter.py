#!/usr/bin/env python3
"""Shared humanizer interface and segmented rewrite orchestration."""

import time
import re
import logging
import threading

from abc import ABC, abstractmethod

from app.helpers.segmenter import segment as segment_paragraphs
from app.humanizer.events import REWRITE_FALLBACK_EVENT

logger = logging.getLogger("app.humanizer")

_UPSTREAM_SEMAPHORE = None
_UPSTREAM_SEMAPHORE_SIZE = None
_UPSTREAM_SEMAPHORE_LOCK = threading.Lock()
_UPSTREAM_RATE_LOCK = threading.Lock()
_UPSTREAM_LAST_REQUEST_AT = 0.0


def _cfg(name, default):
    """从 config 安全读取配置项（本地 config.py 可能缺新配置时用默认值）。"""
    import config as _config
    return getattr(_config, name, default)


def _heading_level_from_style(style):
    """从样式名解析标题级别：Heading 1->1, Title->0, toc->None；非标题返回 None。"""
    if not style:
        return None
    sn = style.lower().strip()
    m = re.search(r'heading\s*(\d+)', sn)
    if m:
        return int(m.group(1))
    if sn == 'title':
        return 0
    return None


class _BatchParagraphMismatch(RuntimeError):
    """Raised when a batched rewrite no longer preserves paragraph count."""


def _split_blank_line_paragraphs(text):
    """Split text on blank lines while tolerating CRLF and whitespace."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    return [
        value.strip()
        for value in re.split(r"\n[ \t]*\n+", normalized)
        if value.strip()
    ]


def _build_rewrite_request_groups(rewrite_tasks, min_chars, max_words):
    """Group short logical blocks into physical requests without merging tasks."""
    indexed_tasks = list(enumerate(rewrite_tasks, 1))
    if min_chars <= 0:
        return [[item] for item in indexed_tasks]

    groups = []
    pending = []
    pending_chars = 0
    pending_words = 0

    def flush_pending():
        nonlocal pending, pending_chars, pending_words
        if pending:
            groups.append(pending)
        pending = []
        pending_chars = 0
        pending_words = 0

    for item in indexed_tasks:
        _, task = item
        text = (task.get("text") or "").strip()
        chars = len(text)
        words = len(text.split())

        if not pending and chars >= min_chars:
            groups.append([item])
            continue

        if pending and pending_words + words > max_words:
            flush_pending()
            if chars >= min_chars:
                groups.append([item])
                continue

        if pending:
            pending_chars += 2  # physical request joins logical blocks with \n\n
        pending.append(item)
        pending_chars += chars
        pending_words += words
        if pending_chars >= min_chars:
            flush_pending()

    if pending:
        # A short document tail can safely share the preceding physical request.
        previous_words = (
            sum(len((task.get("text") or "").split()) for _, task in groups[-1])
            if groups else 0
        )
        if groups and previous_words + pending_words <= max_words:
            groups[-1].extend(pending)
        else:
            flush_pending()

    return groups


class HumanizerAdapter(ABC):
    """Interface for text humanization adapters."""

    @abstractmethod
    def humanize(self, text, mode='low', paragraphs=None):
        """
        Humanize the given text.
        Args:
            text: The text to humanize.
            mode: 'low'/'median'/'high' — controls segmentation granularity.
            paragraphs: Optional ordered list[dict] with paragraph structure,
                        used for structure protection.
        Returns:
            Humanized text string.
        """
        pass

    @abstractmethod
    def humanize_structured(self, text, mode='low', paragraphs=None):
        """
        Humanize and return (text_str, structured_paragraphs).

        structured_paragraphs: list[dict] 每个元素含
            {'text', 'is_heading', 'heading_level', 'style'}，
            用于下载 Word 时按标题级别重建格式。
        """
        pass

    def _humanize_segmented(self, mode, paragraphs, block_rewriter):
        """
        按段落结构分段改写，保护标题和参考文献，并按配置处理短段，保持原文顺序。

        通用骨架，供各引擎复用。改写正文块的具体逻辑由 block_rewriter 提供：
            block_rewriter(body_text) -> str

        Args:
            mode: 分段粒度（low/median/high）
            paragraphs: 有序段落 dict 列表
            block_rewriter: 改写单个正文块的回调函数
        Returns:
            str
        """
        text, _ = self._humanize_segmented_structured(mode, paragraphs, block_rewriter)
        return text

    def _humanize_segmented_structured(
        self, mode, paragraphs, block_rewriter, progress_cb=None,
        fallback_rewriter=None, primary_label=None, fallback_label=None,
        batch_short_blocks=False,
    ):
        """
        分段改写并返回结构化结果：(text_str, structured_paragraphs)。

        structured_paragraphs 为 list[dict]：
            {'text': str, 'heading_level': int|None, 'is_heading': bool, 'style': str|None}
        用于下载 Word 时按标题级别重建格式（Heading 1/2/3...、Title、正文）。

        与 _humanize_segmented 走同一套 segmenter/结构保护逻辑，
        仅额外记录每个输出段落的结构标记：
            - protected 段：保留原始段落结构（标题级别从原始 style 解析）
            - rewrite 段：一个聚合结果映射到一个或多个源 node

        Args:
            progress_cb: 可选进度回调 progress_cb(stage, block, total_blocks)，
                每个 rewrite 块完成后调用，用于前端展示"改写 x/total"真实进度。
            fallback_rewriter: 可选的单块备用改写回调。主回调抛异常时，
                仅使用备用回调处理当前块；下一块仍重新调用主回调。
            batch_short_blocks: 是否只在网络请求层把不足最小字符数的逻辑块
                临时拼成一个请求。返回段落数必须与输入严格一致，否则放弃
                整批结果并按逻辑块恢复主备处理。
        """
        _start = time.time()
        tasks = segment_paragraphs(
            paragraphs,
            mode=mode,
            median_paras=_cfg('REWRITE_MEDIAN_PARAS', 3),
            high_paras=_cfg('REWRITE_HIGH_PARAS', 5),
            max_words=_cfg('REWRITE_MAX_WORDS', 2000),
            min_chars=_cfg('REWRITE_MIN_CHARS', 300),
            protect_short_paragraphs=_cfg(
                'REWRITE_PROTECT_SHORT_PARAGRAPHS', False
            ),
            protect_short_lists=_cfg('REWRITE_PROTECT_SHORT_LISTS', False),
        )

        parts = []
        structured = []
        rewrite_tasks = [t for t in tasks if t["type"] == "rewrite"]
        if rewrite_tasks:
            logger.info(
                "rewrite stage=rewrite backend=%s action=segment mode=%s blocks=%d protected=%d",
                _cfg('HUMANIZER_ADAPTER', 'rule_based'), mode,
                len(rewrite_tasks), len(tasks) - len(rewrite_tasks),
            )

        min_chars = int(_cfg('REWRITE_MIN_CHARS', 300))
        max_words = int(_cfg('REWRITE_MAX_WORDS', 2000))
        request_groups = (
            _build_rewrite_request_groups(rewrite_tasks, min_chars, max_words)
            if batch_short_blocks and len(rewrite_tasks) > 1
            else [[item] for item in enumerate(rewrite_tasks, 1)]
        )

        # 频控：实际请求组数超过阈值时，每组请求后 sleep，防止超 60 次/分钟
        rate_limit_max = _cfg('RATE_LIMIT_MAX_REQUESTS', 30)
        rate_limit_sleep = _cfg('RATE_LIMIT_SLEEP', 1.0)
        rate_limit_enabled = len(request_groups) > rate_limit_max

        rewritten_by_task_id = {}

        def use_fallback(task, current_block, reason):
            if fallback_rewriter is None:
                raise RuntimeError(
                    f"主改写服务在第{current_block}块不可用，且未配置备用服务"
                )
            logger.warning(
                "rewrite stage=rewrite action=fallback block=%d reason=%s "
                "primary=%s fallback=%s",
                current_block, reason, primary_label or "primary",
                fallback_label or "fallback",
            )
            if progress_cb:
                progress_cb(
                    stage="rewrite",
                    block=current_block,
                    total_blocks=len(rewrite_tasks),
                    message=f"第{current_block}块使用备用改写服务",
                    event=REWRITE_FALLBACK_EVENT,
                )
            try:
                return fallback_rewriter(task["text"])
            except Exception as fallback_error:
                raise RuntimeError(
                    f"备用改写服务在第{current_block}块不可用"
                ) from fallback_error

        def rewrite_one(task, current_block):
            try:
                # 每个独立块都重新优先调用主改写服务。
                return block_rewriter(task["text"])
            except Exception:
                if fallback_rewriter is None:
                    raise
                logger.exception(
                    "Primary humanizer %s failed at block=%d; "
                    "using %s for current block",
                    primary_label or "primary", current_block,
                    fallback_label or "fallback",
                )
                return use_fallback(task, current_block, "primary_failed")

        def recover_invalid_batch(group, reason):
            recovered = {}
            for current_block, task in group:
                block_chars = len((task.get("text") or "").strip())
                if min_chars > 0 and block_chars < min_chars:
                    # 该短块已经随批次尝试过主服务；结构校验失败后不能猜测
                    # 标题位置，直接交给备用服务处理当前逻辑块。
                    rewritten = use_fallback(
                        task, current_block, f"batch_{reason}"
                    )
                else:
                    # 批次中本可独立请求的块重新单独尝试主服务。
                    rewritten = rewrite_one(task, current_block)
                recovered[task["task_id"]] = rewritten
            return recovered

        for group_index, group in enumerate(request_groups):
            if len(group) == 1:
                current_block, task = group[0]
                rewritten_by_task_id[task["task_id"]] = rewrite_one(
                    task, current_block
                )
            else:
                input_paragraph_counts = [
                    len(_split_blank_line_paragraphs(task.get("text")))
                    for _, task in group
                ]
                batch_text = "\n\n".join(
                    (task.get("text") or "").strip() for _, task in group
                )
                block_numbers = [number for number, _ in group]
                logger.info(
                    "rewrite stage=rewrite backend=%s action=batch_start "
                    "blocks=%s chars=%d paragraphs=%d",
                    primary_label or "primary", block_numbers, len(batch_text),
                    sum(input_paragraph_counts),
                )
                try:
                    batch_result = block_rewriter(batch_text)
                    output_paragraphs = _split_blank_line_paragraphs(batch_result)
                    expected_paragraphs = sum(input_paragraph_counts)
                    if (
                        not all(input_paragraph_counts) or
                        len(output_paragraphs) != expected_paragraphs
                    ):
                        raise _BatchParagraphMismatch(
                            f"expected={expected_paragraphs} "
                            f"actual={len(output_paragraphs)}"
                        )

                    cursor = 0
                    for (_, task), paragraph_count in zip(
                        group, input_paragraph_counts
                    ):
                        rewritten_by_task_id[task["task_id"]] = "\n\n".join(
                            output_paragraphs[cursor:cursor + paragraph_count]
                        )
                        cursor += paragraph_count
                    logger.info(
                        "rewrite stage=rewrite backend=%s action=batch_ok "
                        "blocks=%s paragraphs=%d",
                        primary_label or "primary", block_numbers,
                        expected_paragraphs,
                    )
                except _BatchParagraphMismatch as mismatch:
                    logger.warning(
                        "rewrite stage=rewrite backend=%s action=batch_invalid "
                        "blocks=%s reason=paragraph_mismatch detail=%s",
                        primary_label or "primary", block_numbers, mismatch,
                    )
                    rewritten_by_task_id.update(
                        recover_invalid_batch(group, "paragraph_mismatch")
                    )
                except Exception:
                    logger.exception(
                        "rewrite stage=rewrite backend=%s action=batch_failed "
                        "blocks=%s",
                        primary_label or "primary", block_numbers,
                    )
                    rewritten_by_task_id.update(
                        recover_invalid_batch(group, "request_failed")
                    )

            if progress_cb:
                for current_block, _ in group:
                    progress_cb(
                        stage="rewrite", block=current_block,
                        total_blocks=len(rewrite_tasks)
                    )
            if rate_limit_enabled and group_index < len(request_groups) - 1:
                time.sleep(rate_limit_sleep)

        for task in tasks:
            if task["type"] == "rewrite":
                rewritten = rewritten_by_task_id[task["task_id"]]
                source_paragraphs = task.get("paragraphs") or []
                parts.append(rewritten)
                item = {
                    "text": rewritten.strip(),
                    "was_rewritten": True,
                    "is_heading": False,
                    "heading_level": None,
                    "style": None,
                    "block_id": task.get("block_id"),
                    "source_node_ids": task.get("source_node_ids", []),
                    "source_body_indexes": task.get("source_body_indexes", []),
                }
                if source_paragraphs:
                    item["source_format"] = source_paragraphs[0].get("source_format")
                structured.append(item)
            else:
                # protected：原样保留
                text = task.get("text") or ""
                parts.append(text)
                # 记录结构：从该 task 涉及的原始段落继承标题级别
                for p in task.get("paragraphs") or []:
                    if "table" in p:
                        continue
                    ptext = (p.get("text") or "").strip()
                    if not ptext:
                        continue
                    style = p.get("style")
                    level = _heading_level_from_style(style)
                    item = {
                        "text": ptext,
                        "was_rewritten": False,
                        "is_heading": bool(level is not None or p.get("is_heading", False)),
                        "heading_level": level,
                        "style": style,
                    }
                    for key in ("node_id", "content_index", "paragraph_index",
                                "source_format", "body_index"):
                        if key in p:
                            item[key] = p[key]
                    structured.append(item)

        logger.info(
            "rewrite stage=rewrite backend=%s action=all_done blocks=%d protected=%d elapsed=%.0fms",
            _cfg('HUMANIZER_ADAPTER', 'rule_based'), len(rewrite_tasks),
            len(tasks) - len(rewrite_tasks), (time.time() - _start) * 1000,
        )
        return "\n\n".join(parts), structured

    def _rewrite_with_chunking(self, text, block_rewriter, max_words=None,
                               request_delay=0, backend_label=None,
                               use_global_limit=False):
        """按上游单次词数限制切块，并逐块调用具体改写接口。"""
        max_words = max_words or _cfg('REWRITE_MAX_WORDS', 2000)
        backend_label = backend_label or _cfg('HUMANIZER_ADAPTER', 'unknown')
        started = time.time()
        chunks = self._split_text_for_requests(text, max_words)

        if len(chunks) > 1:
            logger.info(
                "rewrite stage=rewrite backend=%s action=chunk_split words=%d chunks=%d",
                backend_label, self._count_words(text), len(chunks),
            )

        results = []
        for index, chunk in enumerate(chunks, 1):
            logger.info(
                "rewrite stage=rewrite backend=%s action=chunk_start chunk=%d/%d words=%d",
                backend_label, index, len(chunks), self._count_words(chunk),
            )
            if use_global_limit:
                results.append(self._call_with_global_limit(block_rewriter, chunk))
            else:
                results.append(block_rewriter(chunk))
            if request_delay and index < len(chunks):
                time.sleep(request_delay)

        logger.info(
            "rewrite stage=rewrite backend=%s action=done chunks=%d elapsed=%.0fms",
            backend_label, len(chunks), (time.time() - started) * 1000,
        )
        return "\n\n".join(results)

    @staticmethod
    def _call_with_global_limit(block_rewriter, chunk):
        """限制当前进程所有订单共享的上游并发数和请求启动间隔。"""
        global _UPSTREAM_SEMAPHORE, _UPSTREAM_SEMAPHORE_SIZE
        global _UPSTREAM_LAST_REQUEST_AT

        max_concurrency = max(1, int(_cfg('HUMANIZER_GLOBAL_MAX_CONCURRENCY', 2)))
        min_interval = max(0.0, float(_cfg('HUMANIZER_GLOBAL_MIN_INTERVAL', 1.0)))
        with _UPSTREAM_SEMAPHORE_LOCK:
            if (_UPSTREAM_SEMAPHORE is None or
                    _UPSTREAM_SEMAPHORE_SIZE != max_concurrency):
                _UPSTREAM_SEMAPHORE = threading.BoundedSemaphore(max_concurrency)
                _UPSTREAM_SEMAPHORE_SIZE = max_concurrency

        with _UPSTREAM_SEMAPHORE:
            with _UPSTREAM_RATE_LOCK:
                wait_seconds = min_interval - (time.monotonic() - _UPSTREAM_LAST_REQUEST_AT)
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
                _UPSTREAM_LAST_REQUEST_AT = time.monotonic()
            return block_rewriter(chunk)

    @classmethod
    def _split_text_for_requests(cls, text, max_words):
        """优先按段落和句子切分，必要时按词硬切，确保每块不超上限。"""
        if not text or not text.strip():
            return []
        if cls._count_words(text) <= max_words:
            return [text.strip()]

        units = []
        for paragraph in re.split(r'\n\s*\n', text.strip()):
            if cls._count_words(paragraph) <= max_words:
                units.append(paragraph)
                continue
            sentences = re.split(r'(?<=[.!?])\s+', paragraph)
            for sentence in sentences:
                words = sentence.split()
                units.extend(
                    " ".join(words[index:index + max_words])
                    for index in range(0, len(words), max_words)
                )

        chunks = []
        current = []
        current_words = 0
        for unit in units:
            unit_words = cls._count_words(unit)
            if current and current_words + unit_words > max_words:
                chunks.append("\n\n".join(current))
                current, current_words = [], 0
            current.append(unit)
            current_words += unit_words
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    @staticmethod
    def _count_words(text):
        return len(text.split())
