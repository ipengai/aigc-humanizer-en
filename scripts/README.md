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

## 飞书个人告警

`feishu_alert.py` 使用企业自建应用机器人给指定人员发送单聊消息，并可追加应用内加急或电话加急。

### 配置

凭证统一写在项目 `config.py`（见 `config.example.py`）。也可通过环境变量临时
覆盖（优先级高于 config.py）：

```bash
export FEISHU_APP_ID='cli_xxx'
export FEISHU_APP_SECRET='xxx'
export FEISHU_ALERT_OPEN_ID='ou_xxx'
```

### 调用

```bash
# 普通消息
python3 scripts/feishu_alert.py '[Huma] 测试告警'

# 应用内加急
python3 scripts/feishu_alert.py '[Huma] 改写主服务已熔断' --urgent app

# 电话加急
python3 scripts/feishu_alert.py '[Huma] 主备改写服务全部失败' --urgent phone
```

应用需要启用机器人能力并发布版本，告警接收人必须位于应用可用范围内。应用还需要申请“以应用身份发送消息”以及对应的“发送应用内加急”或“发送电话加急”权限。电话加急会消耗企业额度。

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

四个变量统一写在项目 `config.py`（与 `ALIPAY_*` / `LLM_*` 并列），部署时参照
`config.example.py` 填入。该文件已被 `.gitignore` 忽略，凭证不会外泄。

```python
# config.py
FEISHU_APP_ID = 'cli_xxx'
FEISHU_APP_SECRET = 'xxx'
FEISHU_ALERT_OPEN_ID = 'ou_xxx'
DB_PATH = os.path.join(PROJ_ROOT, 'instance', 'aigc_humanizer.db')
```

读取优先级：**环境变量 > config.py**。因此也可通过 `FEISHU_*` /
`HUMANIZER_DB_PATH` 环境变量临时覆盖（便于多部署或测试），无需改动 config.py。
脚本会把项目根目录加入 `sys.path` 后 `import config`，所以 cron **直接运行即可，
无需手动注入环境变量**。

### cron 部署

脚本仅依赖 Python 标准库，直接用系统的 `python3` 即可，无需激活 venv。

```cron
# 每天 08:00 巡检前一日兜底订单
0 8 * * * cd /opt/aigc-humanizer-en && /usr/bin/python3 scripts/check_fallback_orders.py --since-hours 24 >> /var/log/huma_fallback.log 2>&1
```

- 默认增量去重，每天只报新增兜底订单，不会刷屏。
- 想更高频盯主服务健康度，可加一条每 6 小时巡检：`0 */6 * * * ...`。
- 需要电话/应用内加急时命令末尾加 `--urgent phone`（电话加急消耗企业额度）。

## 文档文件清理

`cleanup_document_files.sh` 默认删除 `instance/source_docs/` 和 `instance/output_docs/` 中超过 7 天的文件。通过 `RETENTION_DAYS` 调整保留天数。

## 独立测试改写 API

`test_humanizer_api_standalone.py` 不导入项目代码，也不读取 `config.py`。它使用脚本内的测试文本发送一次请求，并打印 HTTP 状态和返回正文。

```bash
export AI_TEXT_HUMANIZER_EMAIL='your-email'
export AI_TEXT_HUMANIZER_PASSWORD='your-password'

python3 scripts/test_humanizer_api_standalone.py
```
