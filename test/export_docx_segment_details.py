#!/usr/bin/env python3
"""Export DOCX paragraph routing details without calling external services.

Usage:
    python test/export_docx_segment_details.py /path/to/input.docx
    python test/export_docx_segment_details.py /path/to/input.docx -o details.tsv
    python test/export_docx_segment_details.py /path/to/input.docx --mode high

The generated UTF-8 TSV contains one row per extracted paragraph or table
placeholder. It records whether each paragraph is protected or sent to a
rewrite block. This script performs local parsing and segmentation only; it
does not call the detector or any humanizer API.
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
from app.text_extract import extract_text  # noqa: E402


HEADERS = [
    "原始段落编号",
    "文档Body位置",
    "节点编号",
    "段落类型",
    "是否送改写",
    "所属改写块",
    "Word样式",
    "列表标记",
    "单词数",
    "字符数（含空白）",
    "字符数（非空白）",
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


def _build_node_routes(tasks):
    """Map every source node to its protection/rewrite route."""
    routes = {}
    rewrite_number = 0

    for task in tasks:
        if task["type"] == "rewrite":
            rewrite_number += 1
            block = f"改写块{rewrite_number:02d}"
            for node in task.get("paragraphs") or []:
                routes[node["node_id"]] = ("正文（送改写）", "是", block)
        else:
            for node in task.get("paragraphs") or []:
                routes[node["node_id"]] = (None, "否", "")

    return routes, rewrite_number


def _single_line(text):
    """Keep one physical TSV line per source node."""
    return (
        (text or "")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def build_rows(nodes, tasks, min_words):
    routes, rewrite_count = _build_node_routes(tasks)
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
                    0,
                    0,
                    0,
                    f"[表格{table_number}：单元格内容当前未解析]",
                ]
            )
            continue

        raw_text = node.get("text") or ""
        route_type, sent, block = routes.get(
            node.get("node_id"), (None, "否", "")
        )
        paragraph_type = route_type or _protected_type(node, min_words)
        rows.append(
            [
                node.get("paragraph_index", 0) + 1,
                node.get("body_index", ""),
                node.get("node_id", ""),
                paragraph_type,
                sent,
                block,
                node.get("style") or "",
                node.get("list_text") or "",
                node.get("word_count", len(raw_text.split())),
                len(raw_text),
                sum(not character.isspace() for character in raw_text),
                _single_line(raw_text),
            ]
        )

    return rows, rewrite_count


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
    rows, rewrite_count = build_rows(nodes, tasks, min_words)

    with output_path.open("w", encoding="utf-8-sig", newline="") as output_file:
        writer = csv.writer(output_file, delimiter="\t", lineterminator="\n")
        writer.writerow(HEADERS)
        writer.writerows(rows)

    type_counts = Counter(row[3] for row in rows)
    return {
        "output": output_path,
        "rows": len(rows),
        "paragraphs": sum("table" not in node for node in nodes),
        "tables": sum("table" in node for node in nodes),
        "rewrite_blocks": rewrite_count,
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
        )
    except (FileNotFoundError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2

    print(f"已生成：{result['output']}")
    print(
        f"数据行：{result['rows']}（文本段落 {result['paragraphs']}，"
        f"表格占位 {result['tables']}）"
    )
    print(f"改写块：{result['rewrite_blocks']}")
    print("类型统计：")
    for paragraph_type, count in result["type_counts"].most_common():
        print(f"  - {paragraph_type}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
