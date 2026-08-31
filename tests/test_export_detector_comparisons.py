import json
import sqlite3
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_detector_comparisons.py"


def create_orders_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE orders (
                order_id TEXT,
                original_text TEXT,
                rewritten_text TEXT,
                original_score REAL,
                rewritten_score REAL,
                status TEXT,
                created_at TEXT,
                mode TEXT,
                original_format TEXT,
                word_count INTEGER
            )
            """
        )
        rows = [
            (
                "order-1",
                "This is the original text.",
                "This text was rewritten naturally.",
                82.5,
                21.25,
                "completed",
                "2026-08-24T10:00:00+00:00",
                "median",
                "docx",
                5,
            ),
            (
                "order-2",
                "This is the original text.",
                "This text was rewritten naturally.",
                82.5,
                21.25,
                "completed",
                "2026-08-24T10:01:00+00:00",
                "median",
                "docx",
                5,
            ),
            (
                "order-3",
                "Incomplete rewrite",
                None,
                50,
                None,
                "completed",
                "2026-08-24T10:02:00+00:00",
                "low",
                "txt",
                2,
            ),
            (
                "order-4",
                "Still processing",
                None,
                None,
                None,
                "processing",
                "2026-08-24T10:03:00+00:00",
                "low",
                "txt",
                2,
            ),
        ]
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def run_export(db_path, output_path, *extra_args):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(db_path),
            "--output",
            str(output_path),
            *extra_args,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_exports_valid_unique_pairs_and_marks_legacy_backend_unverified(tmp_path):
    db_path = tmp_path / "orders.db"
    output_path = tmp_path / "comparisons.jsonl"
    create_orders_db(db_path)

    result = run_export(db_path, output_path)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["scanned_completed_orders"] == 3
    assert summary["exported_pairs"] == 1
    assert summary["invalid_pairs_skipped"] == 1
    assert summary["exact_duplicates_skipped"] == 1
    assert summary["sapling_weak_label_eligible"] == 0

    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert "order-1" not in output_path.read_text(encoding="utf-8")
    assert record["backend_provenance"] == "missing"
    assert record["original"]["detector_backend"] == "unknown_legacy"
    assert record["original"]["text"] == "This is the original text."
    assert record["comparison"]["improvement"] == 61.25
    assert record["comparison"]["direction"] == "improved"
    assert record["quality"]["eligible_for_sapling_weak_label"] is False


def test_operator_can_explicitly_mark_legacy_rows_as_sapling(tmp_path):
    db_path = tmp_path / "orders.db"
    output_path = tmp_path / "comparisons.jsonl"
    create_orders_db(db_path)

    result = run_export(
        db_path,
        output_path,
        "--trust-legacy-sapling",
        "--redact-text",
    )

    assert result.returncode == 0, result.stderr
    record = json.loads(output_path.read_text(encoding="utf-8"))
    assert record["backend_provenance"] == "operator_assumption"
    assert record["quality"]["eligible_for_sapling_weak_label"] is True
    assert "text" not in record["original"]
    assert "text" not in record["rewritten"]
