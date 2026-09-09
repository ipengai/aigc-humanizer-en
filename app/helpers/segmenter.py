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


def _is_layout_node(para):
    """Tables/images stay in document flow but never enter rewrite text."""
    return bool(
        para and (
            para.get("node_type") in ("table", "image") or
            "table" in para or "image" in para
        ) and not para.get("text")
    )


def _is_heading(para):
    """Whether a paragraph carries real heading semantics (style or flag)."""
    if not para:
        return False
    if para.get("is_heading"):
        return True
    level = para.get("heading_level")
    if level is not None and level != "":
        return True
    style = (para.get("style") or "").lower()
    return bool(
        (style and "heading" in style) or style in ("title", "subtitle")
    )


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
        if _is_layout_node(para):
            return True
        text = (para.get("text") or "").strip()
        if not text:
            return True

        # 结构化内容由 extract_text 预先标记，统一跳过改写。
        protected_flags = (
            "is_reference", "is_code_block", "has_image",
            "has_hyperlink", "is_heading", "is_toc",
            "is_front_matter", "is_caption",
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
            protect_short_lists=False,
            cross_boundaries=False, block_target_words=None):
    """按 mode 把有序段落切分为"改写任务"。

    mode 枚举：
        low   = 单段（逐段改写，兼容旧值 paragraph）
        median= 连续正文段聚合（默认最多 3 段 / 总字数<max_words）
        high  = 连续正文段聚合（默认最多 5 段 / 总字数<max_words）

    聚合规则（median/high 共用，仅可聚合段落数不同）：
        - 标题 / 参考文献等真实结构默认是硬边界，不聚合进 part，原样保留
        - 短正文默认参与聚合；仅无标题格式文档且开关启用时才保护
        - 未保护的短列表强制黏到上一正文块，不受段数上限影响
        - 表格默认跳过，不送审，也不打断表格前后的正文聚合
        - max_paras 是软上限：达到后仅当块字符数已达到 min_chars 才切块
        - max_words 是硬上限：达到后必须开启新的 part
        - 连续正文末尾不足 min_chars 时，尽量向前合并到上一改写块
        - 当 max_paras == 1 时，等价于 low（每段独立一次请求）

    cross_boundaries=True（P1 提速档）时聚合规则变为：
        - 标题不再作硬边界：作为"受保护行"随正文一起进入改写块（改写后
          按段位强制还原为原标题文本，由 adapter 层保证）；参考文献、
          代码块、题注等仍为硬边界
        - 表格/纯图占位（docx 源）直接跳过：不 flush、不占位、不进文本，
          前后正文可同块（docx 回填只替换段落节点，表格节点原位不动）
        - 忽略段数软上限，仅按 block_target_words（缺省取 max_words）
          切块 —— 供 median/high 档以目标词量表达差异

    Args:
        median_paras: median 模式最多聚合的连续正文段数（可配置）。
        high_paras:   high 模式最多聚合的连续正文段数（可配置）。
        max_words:    单次请求最大字数（聚合超过即切新 part）。
        min_chars:    单次请求期望的最小字符数；不足时允许超过段数软上限。
        protect_short_paragraphs: 是否在无标题格式文档中保护短正文。
        protect_short_lists: 是否保护短列表；False 时短列表黏到上一正文块。
        cross_boundaries: 是否启用跨标题/跨表格聚合（见上）。
        block_target_words: 跨边界模式的目标块词数（None 时用 max_words）。

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
        para.get("is_heading", False) for para in paragraphs
        if not _is_layout_node(para)
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
    # docx 源中无文本的表格/图片占位才可安全跳过（回填只替换段落节点）；
    # 其它来源（如 PDF 重建占位）保持旧行为，避免破坏格式重建。
    source_format = (
        paragraphs[0].get("source_format") if paragraphs else None
    )
    layout_skip = bool(
        cross_boundaries and source_format == "docx"
    )
    if max_paras <= 1 and not cross_boundaries:
        tasks = _segment_paragraph(paragraphs, guard)
    else:
        tasks = _segment_aggregate(
            paragraphs, guard, max_paras, max_words, min_chars,
            cross_boundaries=cross_boundaries,
            block_target_words=block_target_words,
            layout_skip=layout_skip,
        )
    return _finalize_tasks(tasks)


def _finalize_tasks(tasks):
    """Attach aggregate identity without discarding source-node identity."""
    rewrite_index = 0
    for task_index, task in enumerate(tasks):
        source_nodes = task.get("paragraphs") or []
        task["task_id"] = f"segment-{task_index:04d}"
        node_ids = []
        body_indexes = []
        for node in source_nodes:
            nested_node_ids = node.get("source_node_ids") or []
            if node.get("node_id"):
                nested_node_ids = [node["node_id"]]
            for node_id in nested_node_ids:
                if node_id not in node_ids:
                    node_ids.append(node_id)

            nested_body_indexes = node.get("source_body_indexes") or []
            if node.get("body_index") is not None:
                nested_body_indexes = [node["body_index"]]
            for body_index in nested_body_indexes:
                if body_index not in body_indexes:
                    body_indexes.append(body_index)

        task["source_node_ids"] = node_ids
        task["source_body_indexes"] = body_indexes
        if task["type"] == "rewrite":
            task["block_id"] = f"rewrite-block-{rewrite_index:04d}"
            rewrite_index += 1
    return tasks


def _segment_paragraph(paragraphs, guard):
    tasks = []
    for para in paragraphs:
        if _is_layout_node(para):
            tasks.append({"type": "layout", "text": "", "paragraphs": [para]})
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


def _segment_aggregate(paragraphs, guard, max_paras, max_words, min_chars,
                       cross_boundaries=False, block_target_words=None,
                       layout_skip=False):
    """连续正文段按 max_paras 段 + max_words 字聚合为一个 rewrite part。

    默认（cross_boundaries=False）：
        硬边界（标题/参考文献等真实结构）作为分割点，不聚合进 part。
        未保护的短列表视为上一段的附属内容，不触发 max_paras 分块。
        max_paras 是软上限；当前块不足 min_chars 时继续聚合后续正文。
        连续正文的尾块不足 min_chars 时，在不突破 max_words 的前提下向前合并。

    cross_boundaries=True（P1 提速）：
        标题作为"受保护行"随正文进块（不再切块）；仅当块词量达到
        target_words 才切块（忽略段数软上限）。layout_skip=True（docx 源）
        时表格/纯图占位直接跳过：不 flush、不占位、不进文本，前后正文
        可同块。参考文献 / 代码块 / 题注 / 超链接段等仍为硬边界。
    """
    tasks = []
    buffer = []      # 当前聚合的段落（正文段，cross 模式可含标题行）
    buffer_words = 0
    buffer_chars = 0
    target_words = (
        (block_target_words or max_words) if cross_boundaries else max_words
    )

    def flush():
        nonlocal buffer, buffer_words, buffer_chars
        if buffer:
            body_text = "\n\n".join(p["text"] for p in buffer)
            contains_headings = any(_is_heading(p) for p in buffer)
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
                previous_words + buffer_words <= target_words
            ):
                previous["text"] += "\n\n" + body_text
                previous["paragraphs"].extend(buffer)
                if contains_headings:
                    previous["contains_headings"] = True
            else:
                task = {"type": "rewrite", "text": body_text,
                        "paragraphs": buffer}
                if contains_headings:
                    task["contains_headings"] = True
                tasks.append(task)
            buffer = []
            buffer_words = 0
            buffer_chars = 0

    for para in paragraphs:
        if _is_layout_node(para):
            if layout_skip:
                # docx 表格/纯图占位：无文本、不参与改写。跨边界模式直接
                # 跳过 —— 不打断 buffer 聚合；其 body_index 不在改写块的
                # source_body_indexes 内，docx 回填只替换段落节点，表格
                # XML 节点原位不动。
                continue
            flush()
            tasks.append({"type": "layout", "text": "", "paragraphs": [para]})
        elif guard.should_protect(para):
            # 只有"纯标题"允许跨边界随正文进块；References / 题注 / 代码块
            # / TOC 等真实保护结构即使带标题样式也必须保持硬边界，否则
            # 会被改写或让回填错位。
            protected_attrs = (
                "is_reference", "is_code_block", "is_toc",
                "is_front_matter", "is_caption",
                "has_image", "has_hyperlink",
            )
            heading_crossable = (
                cross_boundaries and _is_heading(para) and
                not any(para.get(flag) for flag in protected_attrs)
            )
            if heading_crossable:
                # 标题作为"受保护行"随正文进块：文本随块发送（上游实测
                # 对标题式短行保留率 1.0），改写后由 adapter 层按段位
                # 强制还原为原标题文本。标题极短，不参与词量切块判断。
                w = _count_words(para)
                text_chars = len((para.get("text") or "").strip())
                if buffer:
                    buffer_chars += 2
                buffer.append(para)
                buffer_words += w
                buffer_chars += text_chars
                continue
            # 参考文献 / 代码块 / 题注等（以及 low 档下的标题）是硬边界。
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
            if cross_boundaries:
                # P1：忽略段数软上限，仅按目标词量切块。
                hard = buffer_words + w > target_words
            else:
                hard = (
                    (not sticky_short_list and reached_soft_limit) or
                    buffer_words + w > max_words
                )
            if buffer and hard:
                flush()
            if buffer:
                buffer_chars += 2  # 与最终块文本中的段落分隔符 \n\n 一致
            buffer.append(para)
            buffer_words += w
            buffer_chars += text_chars

    flush()
    return tasks
