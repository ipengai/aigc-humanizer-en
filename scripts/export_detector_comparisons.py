#!/usr/bin/env python3
"""Export before/after detector results from the production orders database.

The exporter is intentionally read-only.  It emits one JSON object per completed
rewrite order and never exports user ids, email addresses, payment information,
or filenames.

Historical orders do not contain detector backend columns.  Such rows are kept,
but are marked as unverified and are not considered eligible Sapling samples
unless the operator explicitly passes ``--trust-legacy-sapling``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "instance" / "aigc_humanizer.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "instance" / "detector_learning_exports"
WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")

REQUIRED_COLUMNS = {
    "order_id",
    "original_text",
    "rewritten_text",
    "original_score",
    "rewritten_score",
    "status",
    "created_at",
}


def normalize_text(text: str) -> str:
    """Normalize only for hashing/deduplication; exported text stays untouched."""
    return WHITESPACE_RE.sub(" ", text).strip()


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def safe_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 <= score <= 100:
        return None
    return round(score, 4)


def open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def detector_backends(
    row: sqlite3.Row,
    columns: set[str],
    trust_legacy_sapling: bool,
) -> tuple[str, str, str]:
    """Return original backend, rewritten backend, and provenance."""
    if {"original_detector_backend", "rewritten_detector_backend"} <= columns:
        return (
            str(row["original_detector_backend"] or "unknown").lower(),
            str(row["rewritten_detector_backend"] or "unknown").lower(),
            "stored",
        )
    if "detector_backend" in columns:
        backend = str(row["detector_backend"] or "unknown").lower()
        return backend, backend, "stored"
    if trust_legacy_sapling:
        return "sapling", "sapling", "operator_assumption"
    return "unknown_legacy", "unknown_legacy", "missing"


def selected_columns(columns: set[str]) -> list[str]:
    base = [
        "order_id",
        "original_text",
        "rewritten_text",
        "original_score",
        "rewritten_score",
        "status",
        "created_at",
    ]
    optional = [
        "mode",
        "original_format",
        "word_count",
        "original_detector_backend",
        "rewritten_detector_backend",
        "detector_backend",
    ]
    return base + [name for name in optional if name in columns]


def iter_rows(
    conn: sqlite3.Connection,
    columns: set[str],
    since: str | None,
    until: str | None,
    limit: int | None,
) -> Iterable[sqlite3.Row]:
    where = ["status = 'completed'"]
    params: list[Any] = []
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at < ?")
        params.append(until)

    sql = (
        f"SELECT {', '.join(selected_columns(columns))} FROM orders "
        f"WHERE {' AND '.join(where)} ORDER BY created_at, order_id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    yield from conn.execute(sql, params)


def build_record(
    row: sqlite3.Row,
    columns: set[str],
    trust_legacy_sapling: bool,
    redact_text: bool,
) -> dict[str, Any] | None:
    original_text = str(row["original_text"] or "").strip()
    rewritten_text = str(row["rewritten_text"] or "").strip()
    original_score = safe_score(row["original_score"])
    rewritten_score = safe_score(row["rewritten_score"])

    if not original_text or not rewritten_text:
        return None
    if original_score is None or rewritten_score is None:
        return None

    original_hash = text_sha256(original_text)
    rewritten_hash = text_sha256(rewritten_text)
    pair_hash = hashlib.sha256(
        f"{original_hash}:{rewritten_hash}".encode("ascii")
    ).hexdigest()
    order_id_hash = hashlib.sha256(str(row["order_id"]).encode("utf-8")).hexdigest()
    original_backend, rewritten_backend, provenance = detector_backends(
        row, columns, trust_legacy_sapling
    )
    sapling_eligible = (
        original_backend == "sapling" and rewritten_backend == "sapling"
    )
    exclusion_reasons = []
    if not sapling_eligible:
        exclusion_reasons.append("detector_backend_not_verified_as_sapling")
    if original_hash == rewritten_hash:
        exclusion_reasons.append("original_and_rewrite_are_identical")
        sapling_eligible = False

    improvement = round(original_score - rewritten_score, 4)
    direction = "improved" if improvement > 0 else "worse" if improvement < 0 else "unchanged"

    def document(text: str, score: float, digest: str, backend: str) -> dict[str, Any]:
        value: dict[str, Any] = {
            "ai_score": score,
            "detector_backend": backend,
            "word_count": count_words(text),
            "char_count": len(text),
            "normalized_text_sha256": digest,
        }
        if not redact_text:
            value["text"] = text
        return value

    return {
        "schema_version": "1.0",
        # Exact duplicate source documents share one group, preventing leakage
        # across train/evaluation splits even if they belong to different orders.
        "group_id": f"online-{original_hash}",
        "pair_id": f"pair-{pair_hash}",
        "order_id_sha256": order_id_hash,
        "created_at": row["created_at"],
        "mode": row["mode"] if "mode" in columns else None,
        "original_format": row["original_format"] if "original_format" in columns else None,
        "backend_provenance": provenance,
        "original": document(original_text, original_score, original_hash, original_backend),
        "rewritten": document(rewritten_text, rewritten_score, rewritten_hash, rewritten_backend),
        "comparison": {
            "improvement": improvement,
            "direction": direction,
            "score_change_rewritten_minus_original": round(-improvement, 4),
        },
        "quality": {
            "valid_before_after_pair": True,
            "eligible_for_sapling_weak_label": sapling_eligible,
            "exclusion_reasons": exclusion_reasons,
        },
    }


def default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_OUTPUT_DIR / f"detector_comparisons_{timestamp}.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读导出订单数据库中的改写前后文本与 AI 检测分数。"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--output", type=Path, help="JSONL 输出路径；默认写入 instance 下的私有目录")
    parser.add_argument("--since", help="仅导出 created_at >= 此值的订单（ISO 时间）")
    parser.add_argument("--until", help="仅导出 created_at < 此值的订单（ISO 时间）")
    parser.add_argument("--limit", type=int, help="最多读取多少条已完成订单")
    parser.add_argument(
        "--trust-legacy-sapling",
        action="store_true",
        help="把没有后端字段的历史分数视为 Sapling；仅在人工核实线上配置后使用",
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="保留原文+改写文完全相同的重复订单；默认按 pair_id 去重",
    )
    parser.add_argument(
        "--redact-text",
        action="store_true",
        help="不输出正文，只输出哈希、长度和分数",
    )
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    return args


def run(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"数据库不存在：{db_path}")

    output_path = (args.output or default_output_path()).expanduser().resolve()
    if output_path == db_path:
        raise ValueError("输出路径不能与数据库路径相同")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scanned = exported = invalid = duplicates = eligible = 0
    seen_pair_ids: set[str] = set()
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    try:
        with open_readonly(db_path) as conn:
            columns = table_columns(conn, "orders")
            missing = REQUIRED_COLUMNS - columns
            if missing:
                raise RuntimeError(f"orders 表缺少字段：{', '.join(sorted(missing))}")

            with tmp_path.open("w", encoding="utf-8") as output:
                for row in iter_rows(conn, columns, args.since, args.until, args.limit):
                    scanned += 1
                    record = build_record(
                        row,
                        columns,
                        args.trust_legacy_sapling,
                        args.redact_text,
                    )
                    if record is None:
                        invalid += 1
                        continue
                    if record["pair_id"] in seen_pair_ids and not args.keep_duplicates:
                        duplicates += 1
                        continue
                    seen_pair_ids.add(record["pair_id"])
                    if record["quality"]["eligible_for_sapling_weak_label"]:
                        eligible += 1
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    exported += 1

        os.replace(tmp_path, output_path)
        os.chmod(output_path, 0o600)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return {
        "database": str(db_path),
        "output": str(output_path),
        "scanned_completed_orders": scanned,
        "exported_pairs": exported,
        "invalid_pairs_skipped": invalid,
        "exact_duplicates_skipped": duplicates,
        "sapling_weak_label_eligible": eligible,
        "legacy_backend_assumption": bool(args.trust_legacy_sapling),
        "text_redacted": bool(args.redact_text),
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        summary = run(args)
    except (FileNotFoundError, RuntimeError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
