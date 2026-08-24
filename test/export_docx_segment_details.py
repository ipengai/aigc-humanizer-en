#!/usr/bin/env python3
"""Export DOCX paragraph routing details without calling external services.

Usage:
    python test/export_docx_segment_details.py /path/to/input.docx
    python test/export_docx_segment_details.py /path/to/input.docx -o details.tsv
    python test/export_docx_segment_details.py /path/to/input.docx --mode high

The script generates two UTF-8 TSV files:
    1. one row per extracted paragraph or table placeholder, including its
       logical rewrite block and planned physical API request;
    2. one row per logical rewrite block, including words, characters, source
       paragraph numbers, and physical API request totals.
It performs local parsing and segmentation only; it does not call the detector
or any humanizer API.
"""

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.helpers.segmenter import _looks_like_title, segment  # noqa: E402
from app.humanizer.adapter import _build_rewrite_request_groups  # noqa: E402
from app.text_extract import extract_text  # noqa: E402


HEADERS = [
    "原始段落编号",
    "文档Body位置",
    "节点编号",
    "段落类型",
    "是否送改写",
    "所属改写块",
    "聚合块段落数",
    "聚合块单词数",
    "聚合块字符数（含空白）",
    "聚合块字符数（非空白）",
    "主API请求",
    "同请求改写块",
    "请求单词数",
    "请求字符数（含空白）",
    "Word样式",
    "列表标记",
    "单词数",
    "字符数（含空白）",
    "字符数（非空白）",
    "具体内容",
]

AGGREGATE_HEADERS = [
    "所属改写块",
    "主API请求",
    "同请求改写块",
    "是否不足最小字符数",
    "原始段落编号",
    "原始段落数",
    "单词数",
    "字符数（含空白）",
    "字符数（非空白）",
    "请求单词数",
    "请求字符数（含空白）",
    "具体内容",
]


def parse_args():
    parser = argparse.ArgumentParser(
        description="逐段导出 DOCX 解析及改写切分明细（不调用任何外部接口）"
    )
    parser.add_argument("input", type=Path, help="输入 DOCX 文件")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 TSV；默认写到输入文件旁，文件名追加 _段落切分明细",
    )
    parser.add_argument(
        "--mode",
        choices=("low", "median", "high", "paragraph"),
        default="median",
        help="改写分块模式，默认 median",
    )
    parser.add_argument("--min-words", type=int, default=10, help="短段保护阈值")
    parser.add_argument(
        "--median-paras", type=int, default=3, help="median 模式每块最多段落数"
    )
    parser.add_argument(
        "--high-paras", type=int, default=5, help="high 模式每块最多段落数"
    )
    parser.add_argument(
        "--max-words", type=int, default=2000, help="每个改写块最大单词数"
    )
    parser.add_argument(
        "--min-chars",
        type=int,
        default=300,
        help="每个聚合块期望的最小字符数，默认300",
    )
    parser.add_argument(
        "--protect-short-paragraphs",
        action="store_true",
        help="无标题格式文档中，把短正文作为疑似标题保护（默认关闭）",
    )
    parser.add_argument(
        "--protect-short-lists",
        action="store_true",
        help="保护短列表，不并入上一正文块（默认关闭）",
    )
    parser.add_argument(
        "--no-batch-short-blocks",
        dest="batch_short_blocks",
        action="store_false",
        help="关闭短逻辑块的主API批量请求规划（默认开启）",
    )
    parser.set_defaults(batch_short_blocks=True)
    return parser.parse_args()


def _protected_type(node, min_words):
    """Return the first protection reason using segmenter's decision order."""
    text = (node.get("text") or "").strip()
    words = node.get("word_count", len(text.split()))

    if node.get("is_reference"):
        return "参考文献保护"
    if node.get("is_code_block"):
        return "代码块保护"
    if node.get("has_image"):
        return "图片段保护"
    if node.get("has_hyperlink"):
        return "超链接段保护"
    if node.get("is_heading"):
        return "标题保护"
    if words < min_words and node.get("list_text"):
        return "列表短段保护"
    if words < min_words:
        return "短段保护"
    if _looks_like_title(text, words):
        return "疑似标题保护"
    return "其他保护"


def _build_node_routes(tasks, min_chars, max_words, batch_short_blocks):
    """Map source nodes to logical blocks and physical API request groups."""
    routes = {}
    aggregate_rows = []
    rewrite_tasks = [task for task in tasks if task["type"] == "rewrite"]
    request_groups = (
        _build_rewrite_request_groups(rewrite_tasks, min_chars, max_words)
        if batch_short_blocks and len(rewrite_tasks) > 1
        else [[item] for item in enumerate(rewrite_tasks, 1)]
    )
    request_by_task_id = {}
    for request_number, group in enumerate(request_groups, 1):
        request_text = "\n\n".join(
            (task.get("text") or "").strip() for _, task in group
        )
        block_labels = [f"改写块{block_number:02d}" for block_number, _ in group]
        request_meta = {
            "request": f"主API请求{request_number:02d}",
            "request_blocks": "+".join(block_labels),
            "request_words": len(request_text.split()),
            "request_chars": len(request_text),
        }
        for _, task in group:
            request_by_task_id[task["task_id"]] = request_meta

    for rewrite_number, task in enumerate(rewrite_tasks, 1):
        block = f"改写块{rewrite_number:02d}"
        block_text = (task.get("text") or "").strip()
        source_nodes = task.get("paragraphs") or []
        request_meta = request_by_task_id[task["task_id"]]
        block_meta = {
            "route_type": "正文（送改写）",
            "sent": "是",
            "block": block,
            "block_paragraphs": len(source_nodes),
            "block_words": len(block_text.split()),
            "block_chars": len(block_text),
            "block_nonspace_chars": sum(
                not character.isspace() for character in block_text
            ),
            **request_meta,
        }
        for node in source_nodes:
            routes[node["node_id"]] = block_meta
        paragraph_numbers = [
            str(node.get("paragraph_index", 0) + 1)
            for node in source_nodes
            if node.get("paragraph_index") is not None
        ]
        aggregate_rows.append([
            block,
            request_meta["request"],
            request_meta["request_blocks"],
            "是" if min_chars > 0 and len(block_text) < min_chars else "否",
            ",".join(paragraph_numbers),
            len(source_nodes),
            len(block_text.split()),
            len(block_text),
            sum(not character.isspace() for character in block_text),
            request_meta["request_words"],
            request_meta["request_chars"],
            _single_line(block_text),
        ])

    for task in tasks:
        if task["type"] != "rewrite":
            for node in task.get("paragraphs") or []:
                routes[node["node_id"]] = {
                    "route_type": None,
                    "sent": "否",
                    "block": "",
                }

    return routes, aggregate_rows, len(request_groups)


def _single_line(text):
    """Keep one physical TSV line per source node."""
    return (
        (text or "")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def build_rows(nodes, tasks, min_words, min_chars, max_words,
               batch_short_blocks):
    routes, aggregate_rows, request_count = _build_node_routes(
        tasks, min_chars, max_words, batch_short_blocks
    )
    rows = []

    for node in nodes:
        if "table" in node:
            table_number = node["table"]
            rows.append(
                [
                    f"表格{table_number}",
                    node.get("body_index", ""),
                    node.get("node_id", ""),
                    "表格占位保护",
                    "否",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    0,
                    0,
                    0,
                    f"[表格{table_number}：单元格内容当前未解析]",
                ]
            )
            continue

        raw_text = node.get("text") or ""
        route = routes.get(
            node.get("node_id"),
            {"route_type": None, "sent": "否", "block": ""},
        )
        paragraph_type = route["route_type"] or _protected_type(node, min_words)
        rows.append(
            [
                node.get("paragraph_index", 0) + 1,
                node.get("body_index", ""),
                node.get("node_id", ""),
                paragraph_type,
                route["sent"],
                route["block"],
                route.get("block_paragraphs", ""),
                route.get("block_words", ""),
                route.get("block_chars", ""),
                route.get("block_nonspace_chars", ""),
                route.get("request", ""),
                route.get("request_blocks", ""),
                route.get("request_words", ""),
                route.get("request_chars", ""),
                node.get("style") or "",
                node.get("list_text") or "",
                node.get("word_count", len(raw_text.split())),
                len(raw_text),
                sum(not character.isspace() for character in raw_text),
                _single_line(raw_text),
            ]
        )

    return rows, aggregate_rows, len(aggregate_rows), request_count


def export_details(
    input_path,
    output_path=None,
    mode="median",
    min_words=10,
    median_paras=3,
    high_paras=5,
    max_words=2000,
    min_chars=300,
    protect_short_paragraphs=False,
    protect_short_lists=False,
    batch_short_blocks=True,
):
    input_path = Path(input_path).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"输入文件不存在：{input_path}")
    if input_path.suffix.lower() != ".docx":
        raise ValueError(f"只支持 DOCX 文件：{input_path}")

    if output_path is None:
        output_path = input_path.with_name(input_path.stem + "_段落切分明细.tsv")
    else:
        output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    nodes = extract_text(str(input_path))
    tasks = segment(
        nodes,
        mode=mode,
        min_words=min_words,
        median_paras=median_paras,
        high_paras=high_paras,
        max_words=max_words,
        min_chars=min_chars,
        protect_short_paragraphs=protect_short_paragraphs,
        protect_short_lists=protect_short_lists,
    )
    rows, aggregate_rows, rewrite_count, request_count = build_rows(
        nodes, tasks, min_words, min_chars, max_words, batch_short_blocks
    )

    with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file, delimiter="\t", lineterminator="\n")
        writer.writerow(HEADERS)
        writer.writerows(rows)

    aggregate_output = output_path.with_name(
        output_path.stem.replace("_段落切分明细", "") + "_聚合段落明细.tsv"
    )
    with aggregate_output.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file, delimiter="\t", lineterminator="\n")
        writer.writerow(AGGREGATE_HEADERS)
        writer.writerows(aggregate_rows)

    type_counts = Counter(row[3] for row in rows)
    return {
        "output": output_path,
        "aggregate_output": aggregate_output,
        "rows": len(rows),
        "paragraphs": sum("table" not in node for node in nodes),
        "tables": sum("table" in node for node in nodes),
        "rewrite_blocks": rewrite_count,
        "api_requests": request_count,
        "type_counts": type_counts,
    }


def main():
    args = parse_args()
    try:
        result = export_details(
            input_path=args.input,
            output_path=args.output,
            mode=args.mode,
            min_words=args.min_words,
            median_paras=args.median_paras,
            high_paras=args.high_paras,
            max_words=args.max_words,
            min_chars=args.min_chars,
            protect_short_paragraphs=args.protect_short_paragraphs,
            protect_short_lists=args.protect_short_lists,
            batch_short_blocks=args.batch_short_blocks,
        )
    except (FileNotFoundError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    print(f"已生成：{result['output']}")
    print(f"聚合明细：{result['aggregate_output']}")
    print(
        f"数据行：{result['rows']}（文本段落 {result['paragraphs']}，"
        f"表格占位 {result['tables']}）"
    )
    print(f"改写块：{result['rewrite_blocks']}")
    print(f"主API请求：{result['api_requests']}")
    print("类型统计：")
    for paragraph_type, count in result["type_counts"].most_common():
        print(f"  - {paragraph_type}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
