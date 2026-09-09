"""AI 检测子页面（/ai-detect）路由级回归测试。

覆盖 code review 固化的关键行为：
- 未登录 401；空文本/超短文本 400 且不扣费
- 注册送检测额度；额度明细接口结构
- 检测成功才扣费（模型报错 400/503 均不扣词）
- words_charged 与实际扣费一致（含短段被过滤场景）
- 402 响应体携带 need_words/price（前端自动充值依赖）
- 文件上传：仅 .txt/.docx，.doc 明确拒绝

运行：.venv/bin/python -m unittest tests.test_detect_routes_regression -v

与仓库其它路由级场景测试一致：真实 sqlite + 真实会话/订单层，
页面服务用本地假实现（patch _get_detector），不触网不加载模型。
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from unittest import mock

from flask import Flask

import config_detector
import app.models as models
from app.extensions import limiter
from app.helpers import close_db
from app.routes.auth import auth_bp
from app.routes.detect import detect_bp

MIN_WORDS = 40


def _alpha_words(text: str) -> int:
    return len([w for w in text.split() if any(c.isalpha() for c in w)])


class FakeDetector:
    """shape 对齐 DetectorPageService.detect()，可注入异常。

    段落级行为对齐真实模型：按空行分段，短段（< MIN_WORDS）被过滤、
    不进入 paragraphs（但全文仍计费）——用于回归「扣费=全文、展示=分析段」
    的口径差异不被再次引入。
    """

    def __init__(self):
        self.fail_with = None  # "too_short" | "server_error" | None

    def detect(self, text):
        words = _alpha_words(text)
        if self.fail_with == "too_short" or words < MIN_WORDS:
            raise ValueError("文本过短，请补充更多内容后再检测。")
        if self.fail_with == "server_error":
            raise RuntimeError("boom")
        paras_out = []
        analyzed = 0
        for raw in text.split("\n\n") or [text]:
            w = _alpha_words(raw)
            if w >= MIN_WORDS:
                paras_out.append({
                    "id": len(paras_out) + 1,
                    "text": raw,
                    "word_count": w,
                    "risk_percent": 42.0,
                })
                analyzed += w
        if not paras_out:
            paras_out.append({
                "id": 1,
                "text": text,
                "word_count": words,
                "risk_percent": 42.0,
            })
            analyzed = words
        coverage = round(100.0 * analyzed / words, 1) if words else 100.0
        return {
            "model_version": "test-fake",
            "summary": {
                "risk_percent": 42.0,
                "coverage": coverage,
                "paragraphs": len(paras_out),
                "high_risk_paragraphs": 0,
                "attribution": {
                    "summary_text": "fake attribution",
                    "groups": [],
                },
                "disclaimer": "test",
            },
            "paragraphs": paras_out,
        }


class DetectRoutesHarness:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="detect-routes-")
        self.original_db_dir = models.DB_DIR
        self.original_db_path = models.DB_PATH
        models.DB_DIR = self.temp.name
        models.DB_PATH = os.path.join(self.temp.name, "test.db")
        models.init_db()

        self.app = Flask(__name__)
        self.app.secret_key = "detect-routes-secret"
        self.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        limiter.init_app(self.app)
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(detect_bp)
        self.app.teardown_appcontext(close_db)

    def close(self):
        models.DB_DIR = self.original_db_dir
        models.DB_PATH = self.original_db_path
        self.temp.cleanup()


_BASE_TOKENS = (
    "The evaluation of machine writing quality has become a central concern for "
    "educators across every discipline in recent years since automated systems now "
    "produce fluent text that is difficult to distinguish from careful human prose "
    "by casual readers who skim quickly"
).split()


def _words_exact(n: int) -> str:
    """构造恰好 n 个英文字母词的文本（count_words 与 split 口径一致）。"""
    pool = _BASE_TOKENS * (n // len(_BASE_TOKENS) + 1)
    return " ".join(pool[:n])


class DetectRoutesRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = DetectRoutesHarness()
        cls.app = cls.harness.app
        cls.fake = FakeDetector()
        patcher = mock.patch(
            "app.routes.detect._get_detector",
            return_value=cls.fake,
        )
        patcher.start()
        cls._patcher = patcher
        cls.signup_bonus = config_detector.DETECTION_SIGNUP_BONUS

    @classmethod
    def tearDownClass(cls):
        cls._patcher.stop()
        cls.harness.close()

    def setUp(self):
        self.fake.fail_with = None
        self.client = self.app.test_client()
        resp = self.client.post("/api/register", json={
            "email": f"det-reg-{id(self)}@example.com",
            "password": "test1234",
            "confirm_password": "test1234",
        })
        self.assertEqual(resp.status_code, 201, resp.get_json())
        self.user_id = resp.get_json()["user"]["id"]

    # ── 访问控制 ──────────────────────────────────────────────
    def test_unauth_analyze_returns_401(self):
        anon = self.app.test_client()
        resp = anon.post("/ai-detect/api/analyze",
                         json={"text": _words_exact(60)})
        self.assertEqual(resp.status_code, 401)

    # ── 额度结构 ──────────────────────────────────────────────
    def test_register_grants_detection_bonus(self):
        resp = self.client.get("/ai-detect/api/quota")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["free_words"], self.signup_bonus)
        self.assertEqual(data["paid_words"], 0)
        self.assertEqual(data["detection_words"], self.signup_bonus)
        self.assertEqual(data["price_per_1000"], config_detector.DETECTION_PRICE_PER_1000)

    # ── 输入校验：不扣费 ───────────────────────────────────────
    def test_empty_text_400_no_charge(self):
        resp = self.client.post("/ai-detect/api/analyze", json={"text": "   "})
        self.assertEqual(resp.status_code, 400)
        quota = self.client.get("/ai-detect/api/quota").get_json()
        self.assertEqual(quota["detection_words"], self.signup_bonus)

    def test_too_short_text_400_no_charge(self):
        resp = self.client.post("/ai-detect/api/analyze",
                                json={"text": "only a few words here."})
        self.assertEqual(resp.status_code, 400)
        quota = self.client.get("/ai-detect/api/quota").get_json()
        self.assertEqual(quota["detection_words"], self.signup_bonus)

    # ── 成功路径：扣费 + 口径 ──────────────────────────────────
    def test_analyze_success_deducts_and_reports_words_charged(self):
        text = _words_exact(250)
        resp = self.client.post("/ai-detect/api/analyze", json={"text": text})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body["success"])
        summary = body["analysis"]["summary"]
        charged = summary["words_charged"]
        self.assertEqual(charged, 250)
        self.assertEqual(
            summary["remaining_words"],
            self.signup_bonus - charged,
        )
        self.assertEqual(
            summary["quota_detail"]["free_words"],
            self.signup_bonus - charged,
        )

    def test_short_paragraphs_filtered_still_charge_full_input(self):
        """短自然段被模型过滤（coverage<100%）时，扣费=全文词数=words_charged。
        这是既定计费口径（按输入计费），回归防止展示与扣费再次脱钩。"""
        text = _words_exact(180) + "\n\n" + "\n\n".join(
            f"Short para number {i} with a few words." for i in range(6)
        )  # 6 × 7 词短段（< MIN_WORDS 被过滤）
        charged = 180 + 6 * 7
        resp = self.client.post("/ai-detect/api/analyze", json={"text": text})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        summary = body["analysis"]["summary"]
        para_sum = sum(p.get("word_count", 0) for p in body["analysis"]["paragraphs"])
        self.assertEqual(para_sum, 180)
        self.assertLess(summary["coverage"], 100.0)
        self.assertEqual(summary["words_charged"], charged)
        self.assertEqual(
            summary["quota_detail"]["free_words"],
            self.signup_bonus - charged,
        )

    def test_server_error_503_no_charge(self):
        self.fake.fail_with = "server_error"
        resp = self.client.post("/ai-detect/api/analyze", json={"text": _words_exact(60)})
        self.assertEqual(resp.status_code, 503)
        quota = self.client.get("/ai-detect/api/quota").get_json()
        self.assertEqual(quota["detection_words"], self.signup_bonus)

    # ── 402 充值拦截体 ─────────────────────────────────────────
    def test_quota_exceeded_402_body_carries_need_words_price(self):
        need = 300
        text = _words_exact(self.signup_bonus + need)
        resp = self.client.post("/ai-detect/api/analyze", json={"text": text})
        self.assertEqual(resp.status_code, 402)
        data = resp.get_json()
        self.assertEqual(data["code"], "quota_exceeded")
        self.assertEqual(data["need_words"], need)
        self.assertEqual(data["remaining_words"], self.signup_bonus)
        self.assertGreater(data["price"], 0)
        # 402 不扣费
        quota = self.client.get("/ai-detect/api/quota").get_json()
        self.assertEqual(quota["detection_words"], self.signup_bonus)

    # ── 文件上传 ───────────────────────────────────────────────
    def test_doc_extension_rejected_with_clear_message(self):
        resp = self.client.post(
            "/ai-detect/api/analyze-file",
            data={"file": (io.BytesIO(b"\xd0\xcf\x11\xe0binary"), "legacy.doc")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("仅支持", resp.get_json()["error"])

    def test_txt_upload_detects_and_deducts(self):
        content = _words_exact(120)
        resp = self.client.post(
            "/ai-detect/api/analyze-file",
            data={"file": (io.BytesIO(content.encode()), "paper.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        summary = resp.get_json()["analysis"]["summary"]
        self.assertEqual(summary["words_charged"], 120)


if __name__ == "__main__":
    unittest.main()
