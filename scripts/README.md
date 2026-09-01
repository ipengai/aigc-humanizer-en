# 运维脚本

## 导出检测前后对比数据

`export_detector_comparisons.py` 以只读方式解析 `orders` 表，将完整的改写前后
文本、AI 率、分数变化、长度、去重哈希及检测后端可信度导出为 JSONL。输出不会
包含用户 ID、邮箱、文件名或支付信息，默认写入已被 Git 忽略的 `instance/` 目录。

```bash
# 使用默认数据库 instance/aigc_humanizer.db
python3 scripts/export_detector_comparisons.py

# 指定线上数据库、时间范围和输出文件
python3 scripts/export_detector_comparisons.py \
  --db /path/to/aigc_humanizer.db \
  --since 2026-08-01T00:00:00+00:00 \
  --output /secure/path/detector_comparisons.jsonl
```

旧订单没有保存检测后端，因此默认标记为 `unknown_legacy`，不会被标成可直接使用
的 Sapling 弱标签。人工核实指定时间范围内线上始终使用 Sapling 后，可以显式使用：

```bash
python3 scripts/export_detector_comparisons.py --trust-legacy-sapling
```

如果只需要统计、哈希和分数，不希望输出正文，可增加 `--redact-text`。

## 飞书告警（AgentTeam 群）

`feishu_alert.py` 复用 zzzheng 既有的飞书告警通道（与 work-utils / biz-coach 的
`send_redlight` 同源）：通过本地 `lark-cli` 以 `qinglan` 机器人身份，把消息发到
**AgentTeam 飞书群**（`oc_9a35aedfe15f5196ab6afee78a583f9f`）。

- 无需个人 `open_id`，也无需在 `config.py` 里放飞书应用凭证——凭证由本机
  `~/.lark-channel` 的 profile 管理（同 `send_redlight` 的机制）。
- 这避开了「open_id 跨应用不认」的问题：早期版本用应用 IM API 发给个人，需要
  取应用级 open_id 并开通通讯录权限；群发通道直接复用已配好的机器人，零额外授权。

### 配置

默认就发到 AgentTeam 群、用 `qinglan` profile，**通常无需任何配置**。仅在要换
目标群或机器人时才在 `config.py`（或环境变量）覆盖：

```python
# config.py
FEISHU_ALERT_CHAT_ID = 'oc_9a35aedfe15f5196ab6afee78a583f9f'  # 目标群
LARK_CHANNEL_PROFILE = 'qinglan'                                # lark-cli profile
```

读取优先级：**环境变量 > config.py > 默认值**。环境变量名为
`FEISHU_ALERT_CHAT_ID` / `LARK_CHANNEL_PROFILE`。

前置依赖：本机已安装 `lark-cli`，且 `~/.lark-channel/profiles/qinglan` 已配置
（与 `send_redlight` 共用）。

### 调用

```bash
# 普通消息
python3 scripts/feishu_alert.py '[Huma] 测试告警'

# --urgent 为兼容保留参数：当前 lark-cli 群发接口不支持应用内/电话加急，
# 非 none 时仅打印提示并按普通消息发送
python3 scripts/feishu_alert.py '[Huma] 测试' --urgent app
```

## 改写兜底巡检

`check_fallback_orders.py` 只读扫描 `orders` 表，找出实际改写链路走了兜底的订单
（`humanizer_backend` 形如 `primary->fallback`，或 `fallback_used=1`），通过
`feishu_alert` 发送飞书告警。正常线上主服务异常时会触发主备切换，持续出现兜底
订单即代表主服务可能出问题，需要人工排查。

```bash
# 依赖上文的飞书环境变量，扫描最近 24 小时
export HUMANIZER_DB_PATH=/path/to/instance/aigc_humanizer.db
python3 scripts/check_fallback_orders.py --since-hours 24

# 全量扫描、每次都告警（不增量去重）
python3 scripts/check_fallback_orders.py --all --no-dedup

# 只打印告警内容、不真正发送
python3 scripts/check_fallback_orders.py --dry-run
```

默认开启增量去重：已告警过的订单号记录在 `scripts/.fallback_alerted.json`，
下次只报新增；`--no-dedup` 可关闭。配合 cron 每日巡检即可稳定盯住主服务健康度。

### 配置（统一在 config.py）

飞书部分默认无需配置（复用 qinglan 机器人 + AgentTeam 群）。如需覆盖目标群或
机器人，在 `config.py`（与 `ALIPAY_*` / `LLM_*` 并列）填 `FEISHU_ALERT_CHAT_ID` /
`LARK_CHANNEL_PROFILE`，参照 `config.example.py`。数据库路径：

```python
# config.py
DB_PATH = os.path.join(PROJ_ROOT, 'instance', 'aigc_humanizer.db')
```

读取优先级：**环境变量 > config.py > 默认值**（`HUMANIZER_DB_PATH` 用于覆盖库路径，
`FEISHU_ALERT_CHAT_ID` / `LARK_CHANNEL_PROFILE` 用于覆盖告警目标）。脚本会把项目根
目录加入 `sys.path` 后 `import config`，所以 cron **直接运行即可，无需手动注入环境变量**。

### cron 部署

脚本仅依赖 Python 标准库，直接用系统的 `python3` 即可，无需激活 venv。

```cron
# 每天 08:00 巡检前一日兜底订单
0 8 * * * cd /opt/aigc-humanizer-en && /usr/bin/python3 scripts/check_fallback_orders.py --since-hours 24 >> /var/log/huma_fallback.log 2>&1
```

- 默认增量去重，每天只报新增兜底订单，不会刷屏。
- 想更高频盯主服务健康度，可加一条每 6 小时巡检：`0 */6 * * * ...`。
- 告警发到 AgentTeam 飞书群（lark-cli 群发，当前不支持加急）。

## 文档文件清理

`cleanup_document_files.sh` 默认删除 `instance/source_docs/` 和 `instance/output_docs/` 中超过 7 天的文件。通过 `RETENTION_DAYS` 调整保留天数。

## 独立测试改写 API

`test_humanizer_api_standalone.py` 不导入项目代码，也不读取 `config.py`。它使用脚本内的测试文本发送一次请求，并打印 HTTP 状态和返回正文。

```bash
export AI_TEXT_HUMANIZER_EMAIL='your-email'
export AI_TEXT_HUMANIZER_PASSWORD='your-password'

python3 scripts/test_humanizer_api_standalone.py
```
