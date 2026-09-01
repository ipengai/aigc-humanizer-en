#!/usr/bin/env python3
"""Send a Feishu alert to the AgentTeam group via the local ``lark-cli``.

复用 zzzheng 既有的飞书告警通道（work-utils / biz-coach 同款）：
- 通过 ``lark-cli`` 以 ``qinglan`` 机器人身份发到 AgentTeam 群
  ``oc_9a35aedfe15f5196ab6afee78a583f9f``，无需个人 open_id，也无需在
  config.py 里放飞书应用凭证（凭证由 ``~/.lark-channel`` 的 profile 管理）。
- 目标群 / 机器人 profile 可用环境变量或 config.py 覆盖：
  ``FEISHU_ALERT_CHAT_ID``、``LARK_CHANNEL_PROFILE``。

依赖：本机已安装 ``lark-cli`` 且 ``~/.lark-channel/profiles/<profile>`` 已配置。
"""

import argparse
import json
import os
import subprocess
import sys


class FeishuAlertError(RuntimeError):
    pass


DEFAULT_PROFILE = "qinglan"
DEFAULT_CHAT_ID = "oc_9a35aedfe15f5196ab6afee78a583f9f"
LARK_CLI = os.environ.get("LARK_CLI", "lark-cli").strip() or "lark-cli"


def _resolve_target():
    """返回 (profile, chat_id)，优先级：环境变量 > config.py > 默认值。"""
    profile = (os.environ.get("LARK_CHANNEL_PROFILE") or "").strip()
    chat_id = (os.environ.get("FEISHU_ALERT_CHAT_ID") or "").strip()
    if not (profile and chat_id):
        try:
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in sys.path:
                sys.path.insert(0, _root)
            import config  # noqa: F401 - 真实配置，已被 .gitignore 忽略
            profile = profile or (getattr(config, "LARK_CHANNEL_PROFILE", "") or "").strip()
            chat_id = chat_id or (getattr(config, "FEISHU_ALERT_CHAT_ID", "") or "").strip()
        except Exception:
            pass
    return (
        profile or DEFAULT_PROFILE,
        chat_id or DEFAULT_CHAT_ID,
    )


def _lark_cli_env(profile):
    home = os.path.expanduser(
        os.environ.get("LARK_CHANNEL_HOME", "~/.lark-channel")
    )
    config_path = f"{home}/profiles/{profile}/lark-cli-source/config.json"
    cli_config_dir = f"{home}/profiles/{profile}/lark-cli/lark-channel"
    env = os.environ.copy()
    env.update(
        {
            "LARK_CHANNEL": "1",
            "LARK_CHANNEL_HOME": home,
            "LARK_CHANNEL_PROFILE": profile,
            "LARK_CHANNEL_CONFIG": config_path,
            "LARKSUITE_CLI_CONFIG_DIR": cli_config_dir,
        }
    )
    return env


def send_alert(message, urgency="none"):
    """Send ``message`` to the AgentTeam group via lark-cli.

    ``urgency`` 当前为兼容保留参数；本机 lark-cli (1.0.x) 的群发接口不支持
    应用内/电话加急，非 ``none`` 时仅打印提示并按普通消息发送。
    """
    profile, chat_id = _resolve_target()
    env = _lark_cli_env(profile)
    key = "huma-alert-" + __import__("datetime").datetime.now().strftime(
        "%Y%m%d%H%M%S%f"
    )
    command = [
        LARK_CLI,
        "im",
        "+messages-send",
        "--as",
        "bot",
        "--chat-id",
        chat_id,
        "--text",
        message,
        "--idempotency-key",
        key,
    ]
    if urgency not in (None, "none", ""):
        print(
            f"[feishu_alert] 警告：当前 lark-cli 群发不支持加急(urgency={urgency})，"
            "已按普通消息发送。",
            file=sys.stderr,
        )
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, env=env
        )
    except FileNotFoundError as exc:
        raise FeishuAlertError(
            f"lark-cli 未找到（LARK_CLI={LARK_CLI}）。请确认已安装并在 PATH 中。"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise FeishuAlertError(f"lark-cli 发送失败: {detail}")
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if parsed.get("ok") is False:
        raise FeishuAlertError(f"lark-cli 返回错误: {result.stdout}")
    return (parsed.get("data") or {}).get("message_id")


def main():
    parser = argparse.ArgumentParser(
        description="Send a Feishu alert to the AgentTeam group via lark-cli."
    )
    parser.add_argument("message", help="Alert text")
    parser.add_argument(
        "--urgent",
        choices=("none", "app", "phone"),
        default="none",
        help="兼容性保留参数，当前 lark-cli 群发不支持加急（默认 none）",
    )
    args = parser.parse_args()
    try:
        message_id = send_alert(args.message, urgency=args.urgent)
    except FeishuAlertError as exc:
        print(f"feishu alert failed: {exc}", file=sys.stderr)
        return 1
    print(f"feishu alert sent: message_id={message_id}, urgent={args.urgent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
