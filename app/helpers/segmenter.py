"""Document segmenter: split an ordered paragraph list into rewrite tasks.

按文档结构把段落分组，供改写时决定"每次送 API 的文本块大小"。

核心能力：
    1. 结构保护（should_protect）—— 标题/目录等不改写；短段保护按配置兜底
    3. 三种 mode 粒度：
        - low   : 单段（兼容旧值 paragraph）
        - median: 按二级标题(Heading 2)分块
        - high  : 按一级标题(Heading 1)分块
        无对应级别标题时退化为"段落块"（按动态 M 段一组）
    4. 返回有序的重建任务列表，含每个块需要送 API 的文本与应保护的段落。
"""

import re


# 聚合配置：相邻正文段聚合为一次改写请求的段落数与字数上限
DEFAULT_MEDIAN_PARAS = 3      # median：最多聚合 3 个连续正文段
DEFAULT_HIGH_PARAS = 5        # high：最多聚合 5 个连续正文段
DEFAULT_MAX_WORDS = 2000      # 单次请求最大字数（聚合超过即切新 part）
DEFAULT_MIN_CHARS = 300       # 单次请求最小字符数；不足时继续吸收正文段


def _looks_like_title(text, words):
    """启发式判断一个 Normal 段是否像标题（无样式时的兜底）。"""
    if words > 15:
        return False
    stripped = text.strip()
    # 以数字/编号开头：1. 1.1 (1) 第一章 等
    if re.match(r'^(?:[\d]+[\s.、)）]+|[（(]\s*[\d]+[）)]\s*|第[一二三四五六七八九十百千0-9]+[章节部分篇])', stripped):
        return True
    # 无结尾句号（标题通常无句号）
    if not stripped.endswith(('.', '!', '?', '。', '！', '？')):
        # 且较短，视为标题
        if words <= 8:
            return True
    return False


class _StructureGuard:
    """段落保护判定器。

    基于 extract_text 阶段已标记好的段落属性做判断：
        - 标题/目录类（is_heading=True）
        - 参考文献条目（is_reference=True，在 extract_text 阶段已标记）
        - 无标题格式文档中的短正文（需显式开启保护）
        - 无标题格式文档中的启发式"伪标题"识别（需显式开启保护）
        - 短列表（使用独立配置，不受普通短段保护影响）

    无需维护前后文状态（参考文献标记已在 extract_text 解析时完成）。
    """

    def __init__(self, min_words=10, has_heading_info=False,
                 protect_short_paragraphs=False,
                 protect_short_lists=False):
        self.min_words = min_words
        self.has_heading_info = has_heading_info
        # Word 已提供标题结构时，绝不再用长度猜标题。
        self.protect_short_paragraphs = (
            bool(protect_short_paragraphs) and not has_heading_info
        )
        self.protect_short_lists = bool(protect_short_lists)

    def is_short_list(self, para):
        """Return whether para is a short, explicitly-marked list item."""
        return bool(
            para.get("list_text") and _count_words(para) < self.min_words
        )

    def should_attach_short_list(self, para):
        """Unprotected short list items stay attached to preceding body text."""
        return self.is_short_list(para) and not self.protect_short_lists

    def should_protect(self, para):
        if not para:
            return True
        # Tables are layout-only nodes at this stage. They are neither sent to
        # rewriting nor treated as boundaries between surrounding paragraphs.
        if "table" in para:
            return False
        text = (para.get("text") or "").strip()
        if not text:
            return True

        # 结构化内容由 extract_text 预先标记，统一跳过改写。
        protected_flags = (
            "is_reference", "is_code_block", "has_image",
            "has_hyperlink", "is_heading",
        )
        if any(para.get(flag) for flag in protected_flags):
            return True

        words = para.get("word_count", len(text.split()))
        # 短列表使用独立开关，避免被普通短段开关连带保护。
        if self.is_short_list(para):
            return self.protect_short_lists

        # 只有文档没有标题结构且配置显式开启时，才用长度猜测标题。
        if self.protect_short_paragraphs and words < self.min_words:
            return True
        if self.protect_short_paragraphs and _looks_like_title(text, words):
            return True
        return False


def should_protect(para, min_words=10, has_heading_info=False,
                   protect_short_paragraphs=False,
                   protect_short_lists=False):
    """无状态的段落保护判定（供外部单段调用 / 测试用）。"""
    guard = _StructureGuard(
        min_words=min_words,
        has_heading_info=has_heading_info,
        protect_short_paragraphs=protect_short_paragraphs,
        protect_short_lists=protect_short_lists,
    )
    return guard.should_protect(para)


# ---------- mode 分块 ----------

def segment(paragraphs, mode="low", min_words=10,
            median_paras=DEFAULT_MEDIAN_PARAS, high_paras=DEFAULT_HIGH_PARAS,
            max_words=DEFAULT_MAX_WORDS, min_chars=DEFAULT_MIN_CHARS,
            protect_short_paragraphs=False,
            protect_short_lists=False):
    """按 mode 把有序段落切分为"改写任务"。

    mode 枚举：
        low   = 单段（逐段改写，兼容旧值 paragraph）
        median= 连续正文段聚合（默认最多 3 段 / 总字数<max_words）
        high  = 连续正文段聚合（默认最多 5 段 / 总字数<max_words）

    聚合规则（median/high 共用，仅可聚合段落数不同）：
        - 标题 / 参考文献等真实结构是硬边界，不聚合进 part，原样保留
        - 短正文默认参与聚合；仅无标题格式文档且开关启用时才保护
        - 未保护的短列表强制黏到上一正文块，不受段数上限影响
        - 表格暂时跳过，不送审，也不打断表格前后的正文聚合
        - max_paras 是软上限：达到后仅当块字符数已达到 min_chars 才切块
        - max_words 是硬上限：达到后必须开启新的 part
        - 连续正文末尾不足 min_chars 时，尽量向前合并到上一改写块
        - 当 max_paras == 1 时，等价于 low（每段独立一次请求）

    Args:
        median_paras: median 模式最多聚合的连续正文段数（可配置）。
        high_paras:   high 模式最多聚合的连续正文段数（可配置）。
        max_words:    单次请求最大字数（聚合超过即切新 part）。
        min_chars:    单次请求期望的最小字符数；不足时允许超过段数软上限。
        protect_short_paragraphs: 是否在无标题格式文档中保护短正文。
        protect_short_lists: 是否保护短列表；False 时短列表黏到上一正文块。

    Returns:
        list[dict]: 每个元素：
            {
                "type": "protected" | "rewrite" | "table",
                "text": 送 API 的文本（protected 时为原样保留文本）,
                "paragraphs": 该块涉及的段落 dict 列表,
            }
        按文档原顺序排列。
    """
    mode = (mode or "low").lower()
    # 兼容旧值 paragraph（等价于 low）
    if mode == "paragraph":
        mode = "low"
    has_heading_info = any(
        para.get("is_heading", False) for para in paragraphs if "table" not in para
    )
    guard = _StructureGuard(
        min_words=min_words,
        has_heading_info=has_heading_info,
        protect_short_paragraphs=protect_short_paragraphs,
        protect_short_lists=protect_short_lists,
    )

    # 1) 单段模式：每段独立判断，保护段原样、正文段单独送
    if mode == "low":
        tasks = _segment_paragraph(paragraphs, guard)
        return _finalize_tasks(tasks)

    # 2) median/high：先逐段打标记，再在连续正文之间按 N 段聚合
    max_paras = high_paras if mode == "high" else median_paras
    if max_paras <= 1:
        tasks = _segment_paragraph(paragraphs, guard)
    else:
        tasks = _segment_aggregate(
            paragraphs, guard, max_paras, max_words, min_chars
        )
    return _finalize_tasks(tasks)


def _finalize_tasks(tasks):
    """Attach aggregate identity without discarding source-node identity."""
    rewrite_index = 0
    for task_index, task in enumerate(tasks):
        source_nodes = task.get("paragraphs") or []
        task["task_id"] = f"segment-{task_index:04d}"
        task["source_node_ids"] = [
            node["node_id"] for node in source_nodes if node.get("node_id")
        ]
        task["source_body_indexes"] = [
            node["body_index"] for node in source_nodes
            if node.get("body_index") is not None
        ]
        if task["type"] == "rewrite":
            task["block_id"] = f"rewrite-block-{rewrite_index:04d}"
            rewrite_index += 1
    return tasks


def _segment_paragraph(paragraphs, guard):
    tasks = []
    for para in paragraphs:
        if "table" in para:
            continue
        elif guard.should_protect(para):
            tasks.append({"type": "protected", "text": para["text"],
                          "paragraphs": [para]})
        else:
            if (guard.should_attach_short_list(para) and tasks and
                    tasks[-1]["type"] == "rewrite"):
                previous = tasks[-1]
                previous["paragraphs"].append(para)
                previous["text"] += "\n\n" + para["text"]
                continue
            tasks.append({"type": "rewrite", "text": para["text"],
                          "paragraphs": [para]})
    return tasks


def _count_words(para):
    return para.get("word_count", len((para.get("text") or "").split()))


def _segment_aggregate(paragraphs, guard, max_paras, max_words, min_chars):
    """连续正文段按 max_paras 段 + max_words 字聚合为一个 rewrite part。

    硬边界（标题/参考文献等真实结构）作为分割点，不聚合进 part。
    未保护的短列表视为上一段的附属内容，不触发 max_paras 分块。
    max_paras 是软上限；当前块不足 min_chars 时继续聚合后续正文。
    连续正文的尾块不足 min_chars 时，在不突破 max_words 的前提下向前合并。
    表格节点暂时忽略，前后正文仍可进入同一个聚合块。
    """
    tasks = []
    buffer = []      # 当前聚合的正文段
    buffer_words = 0
    buffer_chars = 0

    def flush():
        nonlocal buffer, buffer_words, buffer_chars
        if buffer:
            body_text = "\n\n".join(p["text"] for p in buffer)
            # 同一连续正文区域的尾块不足最小字符数时，向前合并，避免
            # median 的最后一两个段落形成过短请求。
            previous = tasks[-1] if tasks else None
            previous_words = (
                sum(_count_words(p) for p in previous["paragraphs"])
                if previous and previous["type"] == "rewrite" else 0
            )
            if (
                min_chars > 0 and len(body_text) < min_chars and
                previous and previous["type"] == "rewrite" and
                previous_words + buffer_words <= max_words
            ):
                previous["text"] += "\n\n" + body_text
                previous["paragraphs"].extend(buffer)
            else:
                tasks.append({"type": "rewrite", "text": body_text,
                              "paragraphs": buffer})
            buffer = []
            buffer_words = 0
            buffer_chars = 0

    for para in paragraphs:
        if "table" in para:
            continue
        elif guard.should_protect(para):
            # 标题 / 参考文献，以及按配置启用的短段保护，是硬边界。
            flush()
            tasks.append({"type": "protected", "text": para["text"],
                          "paragraphs": [para]})
        else:
            w = _count_words(para)
            text_chars = len((para.get("text") or "").strip())
            sticky_short_list = guard.should_attach_short_list(para)
            reached_soft_limit = (
                len(buffer) >= max_paras and
                (min_chars <= 0 or buffer_chars >= min_chars)
            )
            # 达到段数软上限且已满足最小字符数，或加入本段将超过单次请求
            # 最大词数时，开启新块。短列表不触发段数软上限。
            if buffer and (
                (not sticky_short_list and reached_soft_limit) or
                buffer_words + w > max_words
            ):
                flush()
            if buffer:
                buffer_chars += 2  # 与最终块文本中的段落分隔符 \n\n 一致
            buffer.append(para)
            buffer_words += w
            buffer_chars += text_chars

    flush()
    return tasks
