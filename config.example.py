"""
配置示例 — 复制为 config.py 后填写真实凭证。
配置顺序与 config.py 保持一致。
"""

import os

# ── 项目根路径 ─────────────────────────────────
PROJ_ROOT = os.path.dirname(os.path.abspath(__file__))

# ── 基础配置 ──────────────────────────────────
SECRET_KEY = 'your-secret-key-here'

# ── 检测器 ────────────────────────────────────
# 检测定价/额度常量（DETECTION_PRICE_PER_1000、DETECTION_SIGNUP_BONUS 等）
# 位于独立的 config_detector.py（非敏感，随项目发布，无需复制到 config.py）。
AI_DETECTOR_ADAPTER = 'turnitin_fit_detector_v2'  # rule_based | sapling | originality | sapling_mock | rule_based_mock | turnitin_fit_detector_v2

### 检测器-v2 Turnitin Fit Detector 配置
V2_REJECT_COVERAGE = 0.60
### 检测器-sapling配置
SAPLING_API_KEY = ''
### 检测器-originality配置
ORIGINALITY_API_KEY = ''


# ── 改写器与路由 ──────────────────────────────
REWRITE_ROUTING_POLICY = 'risk_band_segmented'  # legacy_whole_document | risk_band_segmented
HUMANIZER_ADAPTER = 'ai_text_humanizer'  # ai_text_humanizer | ai_text_humanizer_mock | llm_based | rule_based
HUMANIZER_FALLBACK_ADAPTER = 'llm_based'

REWRITE_TRANSLATION_ADAPTER = 'lynote'  # risk_band_segmented 的 20–60 档必需；EN→ZH→EN 均为 NMT
REWRITE_HUMA_ADAPTER = 'ai_text_humanizer'  # >60% 档及翻译复检未达标时使用
REWRITE_LLM_ADAPTER = 'llm_based'  # 首轮未达标后的低成本定向二改

### 二次定向改写
REWRITE_TARGETED_SECOND_PASS = True
REWRITE_TARGET_RISK_PERCENT = 20
REWRITE_TARGETED_BATCH_SIZE = 2
REWRITE_TARGETED_MAX_ROUNDS = 2

### 改写0：全局改写器 ai-text-humanizer.com
AI_TEXT_HUMANIZER_EMAIL = ''
AI_TEXT_HUMANIZER_PASSWORD = ''

### 改写1：翻译链路改写器
BAIDU_TRANSLATE_APPID = ''
BAIDU_TRANSLATE_KEY = ''

# 阿里云机器翻译兜底（百度失败/余额不足时切换）
ALIYUN_ACCESS_KEY_ID = ''
ALIYUN_ACCESS_KEY_SECRET = ''
ALIYUN_MT_REGION = 'cn-hangzhou'  # 机器翻译服务地域，可选 cn-hangzhou / cn-shanghai


### 改写2：大模型改写
LLM_PROVIDER = 'deepseek'  # opencode | deepseek
LLM_API_KEY = ''
LLM_MODEL = ''  # 留空时使用 provider 默认模型
LLM_TEMPERATURE = 0.7
LLM_MAX_TOKENS = 8192
LLM_TIMEOUT = 90
LLM_MAX_RETRIES = 3
LLM_RETRY_DELAY = 2.0

# ── 定价 ─────────────────────────────────────
PRICE_PER_1000_WORDS = 14.9
RECHARGE_PACKAGE_WORDS = [2000, 5000, 10000]
SIGNUP_BONUS_WORDS = 200

# ── 数字产品交付（网盘直链）─────────────────────
PRODUCT_DELIVERY_LINKS = {
    # "agentteam_kit": "https://pan.baidu.com/s/xxxx 提取码: xxxx",
}

# ── 改写模式（mode）与频控 ────────────────────
# 档位语义（P1 提速后）：
#   low    = 逐段改写，标题/表格/短段全为硬边界（最大保真，接受慢）
#   median = 跨标题/跨表格聚合，目标 ~REWRITE_MEDIAN_TARGET_WORDS 词/块
#   high   = 跨标题/跨表格聚合，目标 ~REWRITE_HIGH_TARGET_WORDS 词/块
# 聚合一律按"完整段落"：整段并入，加入下一段会超过目标词量就封块，
# 绝不截取段落的部分文字凑字数（宁少勿超）。
REWRITE_MODE_DEFAULT = 'median'
# 旧版（REWRITE_CROSS_BOUNDARIES=False 回滚时）的段数软上限
REWRITE_MEDIAN_PARAS = 3
REWRITE_HIGH_PARAS = 5
REWRITE_MAX_WORDS = 2000
REWRITE_MIN_CHARS = 300
# P1 跨边界聚合（True=median/high 跨标题+跨表格；False=回退旧行为）
REWRITE_CROSS_BOUNDARIES = True
# median/high 档的目标块词数（整段聚合，加入下段将超过即封块）
REWRITE_MEDIAN_TARGET_WORDS = 1500
REWRITE_HIGH_TARGET_WORDS = 2000
REWRITE_BATCH_SHORT_BLOCKS = True
REWRITE_PROTECT_SHORT_PARAGRAPHS = False
REWRITE_PROTECT_SHORT_LISTS = False
RATE_LIMIT_MAX_REQUESTS = 30
RATE_LIMIT_SLEEP = 1.0
HUMANIZER_GLOBAL_MAX_CONCURRENCY = 2
HUMANIZER_GLOBAL_MIN_INTERVAL = 1.0

# ── 上传 ─────────────────────────────────────
ALLOWED_UPLOAD_MIMETYPES = {
    'text/plain',
    'text/markdown',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
DELETE_UPLOADED_FILE = True

# ── 管理员 ───────────────────────────────────
ADMIN_PASSWORD = 'change-me'

# ── 支付 ─────────────────────────────────────
PAYMENT_ADAPTER = 'mock'  # mock | alipay
ALLOW_MOCK_PAYMENT = True  # 生产环境使用 alipay 时必须为 False

# ── 支付宝 ───────────────────────────────────
ALIPAY_APP_ID = ''
ALIPAY_PID = ''
ALIPAY_PRIVATE_KEY = ''
ALIPAY_PUBLIC_KEY = ''
ALIPAY_GATEWAY_URL = 'https://openapi.alipay.com/gateway.do'
ALIPAY_NOTIFY_URL = ''
ALIPAY_RETURN_URL = ''

# ── 数据库路径 ────────────────────────────────
DB_PATH = os.path.join(PROJ_ROOT, 'instance', 'aigc_humanizer.db')

# ── 飞书告警 ─────────────────────────────────
FEISHU_ALERT_CHAT_ID = ''
LARK_CHANNEL_PROFILE = 'qinglan'
