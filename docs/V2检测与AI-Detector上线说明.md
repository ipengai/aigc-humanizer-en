# V2 检测与 AI Detector 上线说明

> 更新时间：2026-09-09
> 上线范围：`turnitin_fit_detector_v2` 主检测、风险分档路由、改写后复检、归因定向二改、`/ai-detect/` 页面及独立检测额度。

## 1. 本次上线内容

主改写页和 AI Detector 页面共用 `app.extensions.ai_detector` 中已初始化的检测器。`/ai-detect/` 只增加产品返回结构、额度和支付逻辑，不保存第二份模型或特征实现。

| 模块 | 代码位置 | 上线行为 |
|---|---|---|
| 检测适配器 | `app/ai_detector/adapter.py` | 创建 V2 检测 callable，进程内缓存模型 |
| 模型运行时 | `app/ai_detector/runtime.py` | 校验元数据与特征版本，加载随机森林 |
| 模型资产 | `app/ai_detector/vendor/turnitin_fit_detector_v2/` | Python 特征代码、JSON 元数据、约 1.5MB joblib 模型 |
| 覆盖率处理 | `app/ai_detector/aggregation.py` | 首次覆盖率不足 60% 时执行一次检测专用短段重聚合 |
| 路由与编排 | `app/pipeline/` | 全文分档、块级选择、复检、归因二改和回滚 |
| AI Detector | `app/routes/detect.py` | 页面检测、独立额度、检测充值订单及支付状态 |
| 页面适配 | `app/ai_detector/page_service.py` | 把主检测结果转换为全文、段落和归因展示结构 |
| 前端 | `templates/ai_detect.html`、`static/detector.js`、`static/detector.css` | `/ai-detect/` 页面交互与结果展示 |

## 2. 生产配置

推荐配置如下：

```python
AI_DETECTOR_ADAPTER = 'turnitin_fit_detector_v2'
V2_REJECT_COVERAGE = 0.60

REWRITE_ROUTING_POLICY = 'risk_band_segmented'
HUMANIZER_ADAPTER = 'ai_text_humanizer'
HUMANIZER_FALLBACK_ADAPTER = 'llm_based'
REWRITE_TRANSLATION_ADAPTER = 'lynote'
REWRITE_HUMA_ADAPTER = 'ai_text_humanizer'
REWRITE_LLM_ADAPTER = 'llm_based'

REWRITE_TARGETED_SECOND_PASS = True
REWRITE_TARGET_RISK_PERCENT = 20
REWRITE_TARGETED_BATCH_SIZE = 2
REWRITE_TARGETED_MAX_ROUNDS = 2

PAYMENT_ADAPTER = 'alipay'
ALLOW_MOCK_PAYMENT = False
```

`REWRITE_TARGETED_MAX_ROUNDS=2` 表示最多两轮；第 2 轮还有内置收益门槛，仅在第 1 轮结束后的当前最佳全文分数处于 `[20%, 30%)` 时执行。每轮耗时、候选分数和采用/回退状态均写入订单路由记录。

建议显式设置 `REWRITE_HUMA_ADAPTER='ai_text_humanizer'`，让风险路由使用的 Huma 服务一眼可见。即使全局主改写器以后改成其他适配器，`>60%` 档也不会随之丢失 Huma provider。

生产环境还必须具备有效的 `SECRET_KEY`、`AI_TEXT_HUMANIZER_EMAIL`、`AI_TEXT_HUMANIZER_PASSWORD`、`LLM_API_KEY` 以及支付宝正式环境配置。启用 `REWRITE_TRANSLATION_ADAPTER='lynote'` 时，百度或阿里云机器翻译至少有一组可用的生产凭证；未配置翻译 provider 时，20–60 档无法执行。V2 检测本身不需要 Sapling 或 Originality Key。

检测页的非敏感定价配置位于 `config_detector.py`：

| 配置 | 当前值 | 含义 |
|---|---:|---|
| `DETECTION_PRICE_PER_1000` | 4.9 | 充值价格，元/1000词 |
| `DETECTION_SIGNUP_BONUS` | 1000 | 新注册用户初始检测词数 |
| `DETECTION_MONTHLY_QUOTA` | 1000 | 北京时间每自然月免费桶重置值 |
| `DETECTION_RECHARGE_PACKAGES` | 1000/2000/5000 | 页面充值档位 |

本机 `config.py` 当前可能用于开发测试。发布前只核对服务器上的受保护配置，不要把本机 `config.py` 上传或提交。

## 3. 数据库与文件

应用启动时 `init_db()` 会对现有 SQLite 库做兼容迁移，在 `users` 增加：

- `detection_free_words`
- `detection_paid_words`
- `detection_free_reset_at`

检测充值继续复用 `orders`，订单类型为 `detect_recharge`；支付成功后只增加 `detection_paid_words`，不会增加改写余额或启动改写任务。检测消费与充值分别写入 `balance_transactions` 的 `detection_consumption`、`detection_recharge` 类型。

管理后台的“AI检测订单”Tab 直接读取 `orders.order_type='detect_recharge'`，展示充值词数、金额、支付/到账状态、支付宝流水号和时间。数据库不新增检测订单表；启动迁移只增加 `idx_orders_type_created_at` 索引，加快按订单类型和日期查询。

上线前必须备份整个 `instance/`，至少包含数据库、WAL/SHM 文件、文件系统 Session、原始文档和输出文档。更新代码后正常启动一次即可执行迁移，不需要单独运行 SQL。

确认以下模型资产已随发布包或 Git 提交进入服务器：

```text
app/ai_detector/vendor/turnitin_fit_detector_v2/random_forest_detector.py
app/ai_detector/vendor/turnitin_fit_detector_v2/random_forest_detector.json
app/ai_detector/vendor/turnitin_fit_detector_v2/random_forest_detector.joblib
```

依赖新增 `numpy`、`joblib`、`scikit-learn`，部署时必须重新安装 `requirements.txt`。多 worker 部署会让每个 worker 各加载一份约 1.5MB 模型；同一 worker 内只加载一次。

## 4. 发布前检查

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app tests
git diff --check
```

当前标准回归覆盖：原有文件解析与回填、账户余额、新用户、两种路由策略、V2 模型缓存与覆盖率、归因重建、定向二改回滚、AI Detector 登录/额度/文件检测和前端交互。

发布前逐项确认：

- Git 变更中包含全部新增的 `app/ai_detector/`、`app/pipeline/`、`app/routes/detect.py`、页面静态资源、测试和模型文件。
- 服务器配置没有使用 `ai_text_humanizer_mock`、`sapling_mock`、`rule_based_mock` 或 `PAYMENT_ADAPTER='mock'`。
- `risk_band_segmented` 所需的 translation、Huma、LLM 三类 provider 均已创建成功；翻译主服务账户有余额，且没有依赖实验用的 Google `gtx` 匿名接口。
- `ALLOW_MOCK_PAYMENT=False`；支付宝通知地址可以从公网访问。
- SQLite 和 `instance/` 已备份，部署用户对 `instance/`、`uploads/` 有读写权限。
- 重启前检查数据库中的 `status='processing'` 订单；应用启动会自动恢复它们，并按本次启动后的 provider 与路由配置继续执行。
- 服务器 Python 环境成功安装 scikit-learn，并能读取 joblib 模型。
- 若使用多个 worker，现有改写进度和检测缓存仍是进程内状态；上线阶段继续沿用当前已验证的 worker 数，不在本次发布同时调整并发架构。

## 5. 上线后冒烟验证

1. 访问 `/api/health`，确认返回 `status=ok`。
2. 访问 `/ai-detect/`，注册新测试账户，确认同时获得 200 词改写额度和 1000 词检测额度。
3. 粘贴至少 40 个英文单词并检测，确认展示整体风险、覆盖率、归因和段落明细；检测额度按全文英文词数下降。
4. 分别上传 `.txt` 和 `.docx`，确认检测成功；上传其他扩展名应明确拒绝。
5. 制造一次额度不足，确认返回差额并能创建检测充值订单；支付成功后只增加检测充值桶。
6. 在主页依次验证 `<20`、`20–60`、`>60` 三档：低风险不改写，中风险翻译优先，高风险使用 Huma 或块级路由。
7. 用已有余额账户提交改写，确认第一次点击即进入处理状态，进度可见，订单最终保存检测器、路由策略和实际改写链。
8. 检查日志中没有模型文件缺失、特征版本不一致、检测持续低覆盖或支付交付错误。

## 6. 回滚

只回滚新路由、保留 V2 检测和 AI Detector 页面：

```python
AI_DETECTOR_ADAPTER = 'turnitin_fit_detector_v2'
REWRITE_ROUTING_POLICY = 'legacy_whole_document'
REWRITE_TARGETED_SECOND_PASS = False
```

这样会恢复“配置的主改写器处理全文”的旧编排方式，同时 `/ai-detect/` 仍可使用。修改配置后必须重启 Web 进程，使检测器和进程内缓存重新初始化。

若把 `AI_DETECTOR_ADAPTER` 完全切回 `sapling`，当前 `/ai-detect/` 的段落归因依赖 V2，会返回检测服务不可用。因此完整回退旧检测器时，应同时从导航隐藏 AI Detector 页面或回退本次页面路由代码。

数据库新增字段可以保留，不需要执行破坏性降级；旧代码会忽略这些字段。已产生的 V2 订单必须保留原有 `detector_backend` 和路由记录，不能改写为 Sapling 历史口径。
