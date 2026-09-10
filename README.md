# AI Humanizer - 降低 AI 检测率，让论文更自然

基于 AI 检测与文本改写服务的 Web 应用。支持上传文档或粘贴文本，检测 AI 率后按原文结构改写，并下载尽量保留原始格式的结果。

## 功能

| 功能 | 详情 |
|------|------|
| AI 文本检测 | 默认使用本地 `turnitin_fit_detector_v2`；保留规则、Sapling、Originality 及 Mock 适配器 |
| AI Detector 页面 | `/ai-detect/` 支持英文粘贴及 `.txt/.docx` 上传，展示全文风险、覆盖率、段落定位与随机森林路径归因 |
| AI 检测订单后台 | 管理后台独立展示检测充值订单、已售词数、实收营收与支付宝流水 |
| 风险路由 | 全文 `<20` 保留原文、`20–60` 低成本翻译优先、`>60` 按篇幅决定 Huma 或块级风险路由 |
| 降 AI 改写 | 支持 DeepSeek/OpenCode、付费 API 与本地规则，提供 low / median / high 三种聚合粒度 |
| 主备容灾 | 每个改写块都优先调用主服务；当前块失败才调用备用服务，下一块重新尝试主服务 |
| 异步处理 | 后台执行改写与复检，前端展示真实处理进度 |
| 多格式支持 | 上传 `.docx`、`.pdf`、`.txt`、`.md`，按文件类型生成下载结果 |
| Word 格式回填 | 基于原文副本和 `source_body_indexes` 定位替换正文，尽量保留原始样式 |
| 订单与余额 | 历史订单、结果下载、词数余额、充值和激活码兑换 |
| 支付宝支付 | 当面付二维码、异步通知及支付状态查询 |
| 结果反馈 | 改写完成后可多选问题类型、填写外部实测 AI 率并上传截图，用于定位问题和优化效果 |
| 订单分析维度 | 订单同时记录改写链路、文档结构、效果、性能与获客来源，详见[订单分析维度](docs/订单分析维度.md) |

## 定价

| 方案 | 价格 | 说明 |
|------|------|------|
| 注册赠送 | 改写 200 词 + 检测 1000 词 | 新用户注册即送；检测额度（免费桶）每月重置 |
| 改写前检测 | 免费 | 主页改写流程的一部分，不扣独立检测额度 |
| AI Detector 检测 | 每月 1000 词免费 | `/ai-detect/` 独立检测；扣减先免费后充值 |
| AI Detector 充值 | ¥4.9/千词 | 超出后按需购买，充值额度不清零；页面提供 1000/2000/5000 词档与自定义 |
| 改写付费 | ¥14.9/1000 词 | 按比例计费，使用词数余额支付 |
| 改写充值包 | 2000/5000/10000 词 | 可自动充值本次改写所需差额 |
| 激活码 | 渠道定制 | 兑换词数余额 |

## 快速开始

### 环境要求

- Python 3.8+
- pip / pip3

### 安装

```bash
cd aigc-humanizer-en
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.py config.py
```

编辑 `config.py`，填写 `SECRET_KEY`、改写服务、机器翻译和支付密钥等配置。本地 V2 检测器不需要 API Key，模型文件随项目发布。使用 `risk_band_segmented` 时，translation、Huma、LLM 三类 provider 都要可用，否则对应风险档无法完成。

推荐适配器配置：

- 本地开发：`AI_DETECTOR_ADAPTER='turnitin_fit_detector_v2'`、`HUMANIZER_ADAPTER='rule_based'`、`PAYMENT_ADAPTER='mock'`
- 流程测试：使用 `sapling_mock`、`rule_based_mock` 或 `ai_text_humanizer_mock`
- 线上生产：`AI_DETECTOR_ADAPTER='turnitin_fit_detector_v2'`、`REWRITE_ROUTING_POLICY='risk_band_segmented'`、`HUMANIZER_ADAPTER='ai_text_humanizer'`、`PAYMENT_ADAPTER='alipay'`

付费 API 作为主服务、DeepSeek 作为备用服务的示例：

```python
HUMANIZER_ADAPTER = 'ai_text_humanizer'
HUMANIZER_FALLBACK_ADAPTER = 'llm_based'
AI_DETECTOR_ADAPTER = 'turnitin_fit_detector_v2'
REWRITE_ROUTING_POLICY = 'risk_band_segmented'
REWRITE_TRANSLATION_ADAPTER = 'lynote'
REWRITE_LLM_ADAPTER = 'llm_based'
LLM_PROVIDER = 'deepseek'  # deepseek | opencode
LLM_API_KEY = 'your-api-key'
LLM_MODEL = ''             # 留空时使用 Provider 默认模型

REWRITE_MEDIAN_PARAS = 3
REWRITE_HIGH_PARAS = 5
REWRITE_MIN_CHARS = 300
REWRITE_MAX_WORDS = 2000
REWRITE_CROSS_BOUNDARIES = True
REWRITE_MEDIAN_TARGET_WORDS = 1500
REWRITE_HIGH_TARGET_WORDS = 2000
REWRITE_BATCH_SHORT_BLOCKS = True
REWRITE_PROTECT_SHORT_PARAGRAPHS = False
REWRITE_PROTECT_SHORT_LISTS = False
```

生产环境还需设置 `PAYMENT_ADAPTER='alipay'`、`ALLOW_MOCK_PAYMENT=False`。文档正文会发送给所选改写供应商处理，部署前应核对供应商的数据使用和保留政策。

`config.py` 包含密钥且已加入 `.gitignore`，不要提交到代码仓库。

### 运行

```bash
python3 app.py
```

默认访问地址：<http://127.0.0.1:5100>

### 使用流程

1. 粘贴英文文本，或上传 `.docx`、`.pdf`、`.txt`、`.md` 文件。
2. 登录后执行 AI 检测。
3. 选择改写粒度并提交改写；余额不足时先完成充值。
4. 等待后台改写和复检完成，查看前后对比并下载结果。

独立检测页面访问 `/ai-detect/`。登录后可以粘贴英文文本或上传 `.txt/.docx`；检测成功才扣除检测词数，失败不扣额度。免费桶每个北京时间自然月重置为 1000 词，充值桶永久保留，扣减顺序为免费额度优先。

## V2 检测与风险路由

`turnitin_fit_detector_v2` 是项目根据真实 Turnitin 标签拟合的本地随机森林风险模型，并非 Turnitin 官方服务。线上主检测、改写路由、改写后复检和 `/ai-detect/` 页面共用 `app.extensions.ai_detector` 中的同一个检测实例：

```text
输入全文
  → V2 全文检测（低覆盖率时执行一次检测专用短段重聚合）
  → <20：保留原文，不调用改写服务
  → 20–60：翻译链优先，复检不合格时升级 Huma
  → >60 短文：Huma 首轮
  → >60 长文：沿用 low/median/high 结构块，按块风险选择改写器
  → 拼回全文复检
  → 仍 >=20：最多两轮归因定向 LLM，只有全文分数下降才接受
```

模型、特征代码和元数据固定在 `app/ai_detector/vendor/turnitin_fit_detector_v2/`。分类器在每个 Web worker 内首次使用时加载一次；同一 worker 的全文检测、块检测、归因和复检复用该实例。`coverage < 0.60` 时会按相邻短正文重聚合一次，仍不足时不进入强风险路由或自动定向二改。

V2 分数只用于当前产品的风险估计和路由。历史 Sapling 订单继续保留原检测器字段，两种分数不混合、不平均。

## 文档处理流程

结构化文件的主链路如下：

```text
原始文件 → 有序结构节点 → 改写任务块 → 主服务/单块兜底 → 结构化结果 → 原文件位置回填
```

### 1. 原始内容如何解析

上传文件统一由 `app/text_extract` 按原始顺序解析，返回节点列表。每个文本节点至少包含 `text`、`word_count`、`node_id`、`content_index` 和 `source_format`；能取得源位置时，还会记录 `paragraph_index`、`body_index`、Word 样式、标题标记、列表标记等信息。这些稳定标识用于后续聚合和结果回填，不能在中途重新编号。

DOCX 按 Word 文档 Body 中的真实顺序遍历段落和表格：

- 普通段落保留 `style`、`is_heading`、`word_count` 和原始 `body_index`。
- Word 编号和项目符号解析为 `list_text`、`list_level`、`indent` 元数据。
- `Heading`、`Title`、`TOC` 样式会标记为标题；`References` 等标题后的内容标记为参考文献，直到遇到下一个标题。
- 含图片或超链接的文本段会增加保护标记。
- 表格目前只生成保持原位置的占位节点，不提取或改写单元格文字，也不计入检测和改写词数。下载 DOCX 时，原表格依靠源文件副本保留。

PDF、Markdown、TXT 也会整理为同一套有序节点；但只有 DOCX 能利用原始 `body_index` 在源文件副本中做精确位置替换。直接粘贴的文本当前只保存为完整字符串，没有 Word 样式和原始节点结构，因此走整段/通用切块流程。

首次 AI 检测不经过段落聚合器：系统把所有带 `text` 的已解析节点按顺序拼成全文后检测。表格占位等没有 `text` 的节点不会进入检测。

### 2. 解析后如何聚合段落

聚合前先判断整篇解析结果是否存在 `is_heading=True` 的标题信息，然后确定哪些节点需要保护：

- 标题、参考文献、代码块、含图片段和含超链接段始终原样保护，不发送给改写服务。
- 表格占位不发送给改写服务；当前实现也不打断表格前后正文的聚合。
- 普通短段默认仍是正文。只有文档完全没有标题信息，并且 `REWRITE_PROTECT_SHORT_PARAGRAPHS=True` 时，才启用少于 `min_words` 的无格式文档标题兜底。
- 短列表使用独立开关。默认 `REWRITE_PROTECT_SHORT_LISTS=False`，短列表会参与改写；有前置正文时强制黏到前面的正文块，不因段落数软上限单独切块。

正文节点按 mode 生成改写任务：

| mode | 基础粒度 | 最小字符规则 |
|------|----------|--------------|
| `low` | 原则上每个正文段单独一块；连续短列表仍黏到上一段 | 不为满足300字符主动跨段聚合；不足300字符由 API 调用前检查和备用服务处理 |
| `median` | 按完整段落聚合到约 1500 词 | 可跨标题和表格位置合并网络请求，回填时仍恢复原位置 |
| `high` | 按完整段落聚合到约 2000 词 | 可跨标题和表格位置合并网络请求，回填时仍恢复原位置 |

启用 `REWRITE_CROSS_BOUNDARIES=True` 后，median/high 分别使用 `REWRITE_MEDIAN_TARGET_WORDS=1500` 和 `REWRITE_HIGH_TARGET_WORDS=2000` 作为聚合目标；只合并完整段落，不会截断段落凑词数。关闭该开关时才回退 `REWRITE_MEDIAN_PARAS=3`、`REWRITE_HIGH_PARAS=5` 的旧段数软上限。`REWRITE_MIN_CHARS=300` 是上游请求的最小字符目标，`REWRITE_MAX_WORDS=2000` 是硬上限。

参考文献、代码、图像段和超链接等保护节点仍是文档逻辑硬边界。为减少请求次数，median/high 的跨边界聚合允许标题和短结构标签随正文进入大块；返回后会根据源位置强制恢复原文。启用 `REWRITE_BATCH_SHORT_BLOCKS=True` 时，如果某个逻辑正文块仍不足300字符，系统只在网络请求层把相邻逻辑改写块临时拼成一个满足最小字符数的付费 API 请求；各逻辑块的原始段落数量、标题位置和 Word 源位置映射保持独立。

批量返回后按空行拆段，只有返回段落总数与输入段落总数完全一致时，才按每个逻辑块原来的段落数量拆回结果，再按照原任务序列插回受保护标题。若主 API 把多段压成一个大段、增减了段落或请求失败，整批结果会被丢弃，不根据字符比例猜测标题位置：其中可独立满足300字符的块重新单独尝试主 API，不足300字符的块才交给备用服务。关闭该开关时，短块直接沿用单块主备逻辑。

每个任务最终记录独立的 `task_id`；改写任务还记录 `block_id`、`source_node_ids` 和 `source_body_indexes`。主服务在第 N 块失败时，前 N-1 块结果保留，备用服务只处理第 N 块；第 N+1 块再次优先尝试主服务。结构化文件不会再从第1块重跑；没有段落结构的纯粘贴文本暂时仍只能进行整篇主备切换。

### 3. 改写后如何拼接回结果

改写过程始终按任务顺序生成两份结果：

- 展示文本：改写块结果和受保护原文依次加入 `parts`，最后使用两个换行符拼成完整文本。
- 结构化结果：每个改写块保存改写文本、`block_id`、对应的 `source_node_ids` 和 `source_body_indexes`；受保护段保存原文、标题级别和原样式。

DOCX 下载不是从零创建文档，而是复制原始 Word 后只替换被改写的正文范围：

1. 根据 `source_body_indexes` 找到每个改写块对应的原始段落。
2. Huma 返回后先做顺序约束的段落对齐，允许 `1→1`、`1→多`、连续正文 `多→1`，以及恢复被 Huma 删除的标题、图注和 `Key Observations:` 一类短结构标签。
3. `1→多` 会合并回一个源段落；连续正文 `多→1` 按原段落词数比例在句子边界重新分配，最终仍生成一个源段落槽位对应一个结果。
4. `body_index` 不连续表示中间存在图片、表格或其他 Word 节点，禁止跨越该间隙合并。普通正文缺失、数字覆盖不足或对齐置信度不足时，只对原区域执行既有安全回退。
5. 从文档尾部向前替换并保留原段落属性、已有 run 格式和片段边界空格；标题、参考文献、表格和图片继续保留在原位置。

如果 DOCX 源文件副本不可用，系统根据结构化结果重新生成 DOCX；PDF 输入也输出这种重建的 DOCX，可以恢复已识别的标题级别和正文顺序，但不能完整还原原版式。TXT 和 Markdown 则直接按最终展示文本输出对应文本文件。

## 测试主入口

使用项目虚拟环境运行全部标准回归测试：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

账户余额与路由策略的端到端测试位于 `tests/test_rewrite_routing_account_scenarios.py`。它覆盖已有余额用户、新注册用户和零余额低风险用户，并分别验证 `legacy_whole_document`、`risk_band_segmented` 的检测、分段、改写器选择、余额扣减、订单落库和实际链路记录。

单独运行该测试：

```bash
.venv/bin/python -m unittest tests.test_rewrite_routing_account_scenarios -v
```

输出每个测试用户、文本词数、分段数量、每段检测分数和实际改写链路：

```bash
.venv/bin/python -m tests.test_rewrite_routing_account_scenarios --report
```

路由规则和 v2 检测边界分别沉淀在 `tests/test_pipeline_router.py` 与 `tests/test_v2_pipeline_scenarios.py`，后续增加新档位、provider、失败兜底或账户场景时继续在对应测试文件中扩展。

Huma 可变段落数和 Word 结构回填测试位于 `tests/test_docx_huma_alignment.py`。还可以用任意真实 Word 离线生成“少一段”和“多一段”结果；该脚本只使用 Mock，不请求 Huma：

```bash
.venv/bin/python tests/run_docx_alignment_fixture.py INPUT.docx OUTPUT_DIR
```

AI Detector 页面与主检测服务的标准回归入口：

```bash
.venv/bin/python -m unittest \
  tests.test_detector_page_model \
  tests.test_detect_routes_regression \
  tests.test_detect_page_ui -v
```

它覆盖主模型实例复用、低覆盖率重聚合、精确归因、登录与额度、成功后扣费、失败不扣费、文本和文件检测以及前端分档。

需要分析某篇文本为什么被 `turnitin_fit_detector_v2` 判高时，使用
[`scripts/explain_v2_detector.py`](scripts/explain_v2_detector.py)，命令和输出字段见
[`scripts/README.md`](scripts/README.md#v2-检测结果归因)。

## 文档

- [技术方案](docs/技术方案.md)：系统架构、模块划分、数据模型、接口、核心流程、配置和扩展方式。
- [开发规范](docs/开发规范.md)：AI 辅助开发与代码修改约定。
- [订单分析维度](docs/订单分析维度.md)：订单表各分析字段的口径、埋点时机与分析建议。
- [`turnitin_fit_detector_v2` 分档与改写路由改造方案](docs/v2检测分档与改写路由改造方案.md)：基于本地 `turnitin_fit_detector_v2` 风险分的分档、低成本优先路由、分段复检与分期实施方案。
- [`turnitin_fit_detector_v2` 归因方法与5篇失败样本分析](docs/v2归因方法与5篇失败样本分析.md)：解释模型判高原因、覆盖率限制，以及定向二次改写的优先场景。
- [V2 检测与 AI Detector 上线说明](docs/V2检测与AI-Detector上线说明.md)：生产配置、数据库迁移、发布检查、冒烟验证与回滚方案。

## TODO

优先级含义：`P0` 上线前核心效果或主链路问题；`P1` 稳定性、成本或重要体验问题；`P2` 能力完善和规模化优化。

### 改写效果与检测成本

- [x] **P0 - 高 AI 率自动定向二次改写**：改写结果不达标时，按 V2 决策路径归因选择高贡献块，默认最多两轮；每轮全文复检，只接受风险下降的候选。
- [x] **P0 - 改写上游自动兜底**：每块优先调用主改写 API，失败时仅当前块调用独立备用服务，下一块恢复主服务优先；已完成结果不重跑。
- [ ] **P1 - 改写故障告警**：主服务切换、主备全部失败及服务恢复时发送飞书通知；主备全部失败时增加电话加急。
- [x] **P1 - 超长单句处理**：单句超过上游词数限制时按词安全切块。
- [x] **P1 - 改写 API 进程级全局频控**：限制当前进程内跨订单、跨线程的最大并发数和请求启动间隔；多 worker 部署后迁移到 Redis 分布式限流。
- [ ] **P2 - 选择性改写**：支持只改写用户选中的段落。

### 文档解析与格式还原

- [ ] **P1 - DOCX 复杂内容识别**：完善表格、代码块、图表、公式及图片周围正文的识别和格式回填。
- [ ] **P2 - PDF 复杂内容识别**：后续补充 PDF 表格、代码、图表和公式处理；当前优先保证页眉页脚过滤与正文段落提取。
- [ ] **P2 - PDF 依赖升级**：将已弃用的 `fitz` 导入方式迁移到 `pymupdf`。

### 稳定性与性能

- [ ] **P1 - 生产多 worker 适配**：将改写进度和原文检测缓存完整迁移到数据库或 Redis。
- [x] **P1 - 前端轮询串行化**：改写进度与支付状态均改为串行 `setTimeout` 轮询，避免慢请求叠加。
- [ ] **P2 - 数据库索引**：为 `user_id`、`order_id`、`payment_status` 等高频查询字段增加索引。
- [ ] **P2 - 监控与可观测性**：增强健康检查、结构化日志，并评估接入 Sentry。

## 更新日志

- **20260909 · V2 检测、风险路由与 AI Detector**
  - `turnitin_fit_detector_v2` 模型与特征代码收敛到 `app/ai_detector/vendor/`，不依赖外部实验仓库；每个 worker 只加载一次模型
  - 新增 `<20 / 20–60 / >60` 风险路由，保留原有结构保护、low/median/high 分块、主备兜底与 DOCX 回填
  - 首轮未达标时支持随机森林路径归因和最多两轮定向 LLM；输出校验或全文升分时回滚
  - 新增 `/ai-detect/` 页面、独立月度免费额度和充值额度、段落定位与因素归因；检测页直接复用主检测服务
  - 管理后台新增 AI 检测付费订单 Tab；复用 `orders.order_type='detect_recharge'`，并增加订单类型与创建时间索引
  - 新增账户、路由、V2、定向改写、检测页面和检测额度标准回归测试

- **20260901 · 订单分析与结果反馈**
  - 订单表新增改写链路、文档结构、效果、性能与获客来源五类分析维度；改写前先记录计划链路，改写失败也能按方法和版本归因
  - 订单详情页支持提交结果反馈：多选问题类型、填写外部实测 AI 率、上传截图（PNG / JPG / WEBP，5MB 内），截图仅登录管理员可查看
  - 管理后台「改写效果统计」扩展为效果诊断：新增改写后升高占比、平均篇幅变动、标题结构异常、平均处理耗时、用户反馈分类与外部实测达标率
  - 后台订单筛选拆分为「改写方法」（api / llm / rule / hybrid）与「改写强度」（low / median / high），订单明细展示实际链路、主备引擎、降级块数与获客来源
  - 降级埋点改为结构化事件上报（保留旧文案兜底），改写文案调整不再导致后台降级统计静默失效
  - 订单列表的「是否已反馈」改为单次批量查询，消除逐单查询（N+1）
- **20260825 · 管理后台**
  - 新增「改写效果统计」：按时间范围查看改写完成样本中改写后 AI 率降至 20% 以下的比例、达标数与平均降幅
  - 订单明细支持按支付方式 / 任务状态 / 改写方法 / 检测方法筛选
  - 订单新增展示改写方法、检测方法、前后 AI 率（原→改写）；订单表记录实际使用的检测后端（历史订单显示「未知」）
- **20260825 · 前端页面**
  - 使用帮助页改版：左侧目录导航 + 时间线式步骤，整体更大气；新增「产品更新日志」板块
  - 帮助文案补全改写预期（下载后人工核对专有名词、Word 建议只传正文、可选改写模式等）
  - 首页上传区增加「详细使用方法见使用帮助」提示；定价调整为「高达标率」软宣传，去掉不达标退款承诺
- **20260824**：改写引擎优化 —— 短段落自动聚合、逐块主备兜底、300 字符最小聚合；修复超长文档改写对比超高亮；新增 DOCX 段落切分明细导出脚本

## License

MIT
