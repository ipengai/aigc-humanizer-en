#!/usr/bin/env python3
"""巡检订单改写是否走了兜底（fallback），发现则发飞书告警。

线上主改写服务正常时，改写链路应直接走主引擎；一旦出现主->备切换
（humanizer_backend 形如 'primary->fallback' 或 fallback_used=1），说明
主服务可能异常（熔断 / 超时 / 限流），需要人工关注。

部署（阿里云服务器，配合 cron 每日巡检）：
    export HUMANIZER_DB_PATH=/path/to/instance/aigc_humanizer.db
    export FEISHU_APP_ID=cli_xxx
    export FEISHU_APP_SECRET=xxx
    export FEISHU_ALERT_OPEN_ID=ou_xxx
    python3 scripts/check_fallback_orders.py --since-hours 24

建议 cron：
    0 8 * * * cd /opt/aigc-humanizer-en && python3 scripts/check_fallback_orders.py
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from feishu_alert import send_alert
except ImportError:
    sys.path.insert(0, SCRIPT_DIR)
    from feishu_alert import send_alert

DEFAULT_DB = os.environ.get("HUMANIZER_DB_PATH") or os.path.join(
    "instance", "aigc_humanizer.db"
)
STATE_FILE = os.path.join(SCRIPT_DIR, ".fallback_alerted.json")


def query_fallback_orders(db_path, since_hours):
    """返回走兜底的已完成订单（按时间倒序）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        params = []
        sql = (
            "SELECT order_id, humanizer_backend, fallback_used, created_at "
            "FROM orders "
            "WHERE status = 'completed' "
            "AND (fallback_used = 1 OR humanizer_backend LIKE '%->%')"
        )
        if since_hours and since_hours > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=since_hours)
            ).isoformat()
            sql += " AND created_at >= ?"
            params.append(cutoff)
        sql += " ORDER BY created_at DESC"
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def load_alerted():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_alerted(order_ids):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(order_ids), f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="检测走兜底的改写订单并通过飞书告警"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB,
        help="SQLite 库路径（默认 HUMANIZER_DB_PATH 或 instance/aigc_humanizer.db）",
    )
    parser.add_argument(
        "--since-hours", type=int, default=24,
        help="只扫描最近 N 小时（默认 24；与 --all 互斥时 --all 优先）",
    )
    parser.add_argument(
        "--all", action="store_true", help="忽略时间窗，扫描全部历史订单",
    )
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="关闭增量去重，每次都把全量兜底订单发出去",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印告警内容，不发送飞书",
    )
    parser.add_argument(
        "--urgent", choices=("none", "app", "phone"), default="none",
    )
    args = parser.parse_args()

    window = "全部历史" if args.all else f"近 {args.since_hours} 小时"
    rows = query_fallback_orders(args.db, 0 if args.all else args.since_hours)
    if not rows:
        print(f"OK: {window}内未检测到走兜底的订单")
        return 0

    if args.no_dedup:
        new_rows = rows
        alerted = set()
    else:
        alerted = load_alerted()
        new_rows = [r for r in rows if r["order_id"] not in alerted]

    if not new_rows:
        print(
            f"OK: {window}内无新增兜底订单"
            f"（已告警 {len(alerted)} 笔，跳过重复）"
        )
        return 0

    lines = [
        f"⚠️ [Huma] 改写兜底告警：{window}检测到 {len(rows)} 笔兜底订单，"
        f"本次新增 {len(new_rows)} 笔"
    ]
    for r in new_rows[:30]:
        lines.append(
            f"- {r['order_id']} | 链路={r['humanizer_backend']} "
            f"| fallback_used={r['fallback_used']} | {r['created_at']}"
        )
    if len(new_rows) > 30:
        lines.append(f"... 其余 {len(new_rows) - 30} 笔省略")
    message = "\n".join(lines)

    if args.dry_run:
        print("[dry-run] 将发送飞书告警：\n" + message)
        return 0

    try:
        send_alert(message, urgency=args.urgent)
    except Exception as exc:  # noqa: BLE001 - 运维脚本需要把异常转成非零退出码
        print(f"feishu alert failed: {exc}", file=sys.stderr)
        return 1

    save_alerted(alerted | {r["order_id"] for r in new_rows})
    print(f"已发送飞书告警，新增兜底订单 {len(new_rows)} 笔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
