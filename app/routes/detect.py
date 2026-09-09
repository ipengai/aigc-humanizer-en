"""AI Detector 子产品蓝图 — 挂载于 /ai-detect。

架构：检测页作为 Huma 的一个子页面，复用 Huma 的会话登录态、User 表与
支付宝适配器，不再自建账号体系。与改写侧共用 users 表但额度完全独立：

- detection_free_words：每自然月免费重置 1000 词（注册即送 1000）
- detection_paid_words：充值所得（¥4.9/千词），永不清零
- 扣减顺序：先免费后充值

检测直接复用 app.extensions.ai_detector 中已初始化的主检测服务，页面层只做
返回结构适配；模型缓存、短段聚合和随机森林归因均由主检测模块统一提供。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, jsonify, render_template, request, session

from config_detector import DETECTION_PRICE_PER_1000
from app.extensions import limiter
from app.ai_detector.page_service import DetectorPageService
from app.helpers import (
    generate_order_id, get_db, login_required, process_payment_success
)
from app.text_extract import extract_text, paragraph_list_to_text

_ALLOWED_EXTENSIONS = {".txt", ".docx"}
_MAX_CHARS = 200_000

# 英文词计数（与配额扣减一致：仅计英文字母词）
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


detect_bp = Blueprint("detect", __name__, url_prefix="/ai-detect")

def _get_detector() -> DetectorPageService:
    """Bind the page to the same process-local detector used by rewriting."""
    from app import extensions

    return DetectorPageService(extensions.ai_detector)


@detect_bp.route("/")
def detect_page():
    """AI 检测主页（/ai-detect/）。"""
    return render_template("ai_detect.html")


# ======================================================================
# 检测接口（CSRF 豁免，见 app/__init__.py；强制登录）
# ======================================================================

@detect_bp.route("/api/analyze", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def api_analyze():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text", "")).strip()
    if not text:
        return jsonify(error="请粘贴英文文本后再检测。"), 400
    if len(text) > _MAX_CHARS:
        return jsonify(error="单次最多检测 200,000 个字符。"), 400
    return _run_detect(text)


@detect_bp.route("/api/analyze-file", methods=["POST"])
@limiter.limit("10 per minute")
@login_required
def api_analyze_file():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="请选择要上传的 .txt 或 .docx 文件。"), 400
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return jsonify(error="仅支持 .txt 与 .docx 文件。"), 400
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"upload{ext}"
            upload.save(str(path))
            text = paragraph_list_to_text(extract_text(path)).strip()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("AI-detect file parsing failed")
        return jsonify(error="文件解析失败，请确认文件未损坏。"), 422
    if not text:
        return jsonify(error="未能从文件中提取到有效文本内容。"), 400
    if len(text) > _MAX_CHARS:
        return jsonify(error="单次最多检测 200,000 个字符。"), 400
    return _run_detect(text)


def _run_detect(text: str):
    """统一检测流程：额度校验 → 检测 → 成功才扣减并记账。"""
    from app.models import BalanceTransaction, User

    user_id = session.get("user_id")
    conn = get_db()
    free, paid = User.get_detection_quota(conn, user_id)
    total = free + paid
    words = count_words(text)

    if words > total:
        need = words - total
        return (
            jsonify(
                error=(
                    f"检测额度不足：本次需 {words} 词，当前可用 {total} 词"
                    f"（本月免费 {free} + 充值 {paid}），还差 {need} 词。"
                ),
                code="quota_exceeded",
                remaining_words=total,
                need_words=need,
                price=round(DETECTION_PRICE_PER_1000 * need / 1000, 2),
            ),
            402,
        )

    try:
        result = _get_detector().detect(text)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        current_app.logger.exception("AI-detect analysis failed")
        return jsonify(error="检测暂时不可用，请稍后重试。"), 503

    # 检测成功后才扣减额度（失败不扣词）
    try:
        det_result = User.deduct_detection_words(conn, user_id, words)
        if det_result is None:  # 极端并发下额度被抢先耗尽
            return jsonify(error="检测额度不足，请稍后重试或充值。", code="quota_exceeded"), 402
        free_after, paid_after = det_result
        BalanceTransaction.create(
            conn, user_id, "detection_consumption", -words,
            free_after + paid_after, description="AI 检测消耗",
        )
        conn.commit()
    except Exception:
        conn.rollback()
        current_app.logger.exception("AI-detect quota deduction failed")
        return jsonify(error="额度扣减失败，请重试。"), 503

    result["summary"]["remaining_words"] = free_after + paid_after
    result["summary"]["quota_detail"] = {
        "free_words": free_after,
        "paid_words": paid_after,
    }
    # 实际扣费词数 = 全文英文词数（短自然段可能被模型过滤不参与分析，但仍按输入计费）
    result["summary"]["words_charged"] = words
    return jsonify(success=True, analysis=result)


# ======================================================================
# 检测词充值（复用 Huma 支付宝适配器；order_type='detect_recharge'）
# ======================================================================

@detect_bp.route("/api/quota", methods=["GET"])
@login_required
def api_quota():
    """当前检测额度明细（充值弹窗/导航刷新用）。"""
    from app.models import User

    user_id = session.get("user_id")
    free, paid = User.get_detection_quota(get_db(), user_id)
    return jsonify({
        "free_words": free,
        "paid_words": paid,
        "detection_words": free + paid,
        "price_per_1000": DETECTION_PRICE_PER_1000,
    })


@detect_bp.route("/api/recharge", methods=["POST"])
@limiter.limit("30 per minute")
@login_required
def api_recharge():
    """创建检测词充值订单并返回支付宝二维码/表单。"""
    from app.extensions import payment_adapter as adapter
    from app.models import Order

    user_id = session.get("user_id")
    data = request.get_json(silent=True) or {}
    try:
        words = int(data.get("words") or 0)
    except (TypeError, ValueError):
        return jsonify(error="充值词数不正确。"), 400
    if words < 100 or words > 100000:
        return jsonify(error="单次充值词数需在 100 ~ 100000 之间。"), 400

    price = round(DETECTION_PRICE_PER_1000 * words / 1000, 2)
    order_id = generate_order_id()
    conn = get_db()
    try:
        Order.create_detection_recharge_order(conn, user_id, order_id, words, price)
    except Exception:
        conn.rollback()
        current_app.logger.exception("Failed to create detection recharge order")
        return jsonify(error="创建订单失败，请稍后重试。"), 500

    subject = f"AI Detector 词充值 - {words}词"
    try:
        result = adapter.create_prepay_form(order_id, price, subject)
        if result.get("error") and hasattr(adapter, "create_prepay_order"):
            logging.warning(
                "detect recharge create_prepay_form failed (%s), falling back",
                result.get("error"),
            )
            result = adapter.create_prepay_order(order_id, price, subject)
    except Exception:
        current_app.logger.exception("Payment adapter call failed for detect recharge")
        return jsonify(error="支付创建失败，请稍后重试。"), 500

    qr_code = result.get("qr_code")
    if qr_code:
        try:
            Order.save_qr_code(conn, order_id, qr_code)
        except Exception:
            conn.rollback()
            current_app.logger.exception("Failed to save qr code")

    if result.get("error"):
        logging.error(
            "Detect recharge adapter failed: error=%s code=%s sub_code=%s order=%s",
            result.get("error"), result.get("code"), result.get("sub_code"), order_id,
        )
        return jsonify(error="支付创建失败，请稍后重试。"), 500

    return jsonify({
        "success": True,
        "order": {
            "order_id": order_id,
            "words": words,
            "price": price,
            "qr_code": qr_code or "",
            "form_html": result.get("form_html") or "",
            "expires_in": result.get("expires_in", 600),
        },
    })


@detect_bp.route("/api/recharge-status/<order_id>")
@limiter.limit("20 per minute")
@login_required
def api_recharge_status(order_id):
    """轮询检测充值订单支付状态；webhook 未到达时主动查支付宝网关兜底。"""
    from app.extensions import payment_adapter as adapter
    from app.models import Order, User

    user_id = session.get("user_id")
    conn = get_db()
    order = Order.get_by_order_id(conn, order_id)
    if not order:
        return jsonify(error="订单不存在。"), 404
    if order.get("user_id") != user_id:
        return jsonify(error="无权访问该订单。"), 403
    if order.get("order_type") != "detect_recharge":
        return jsonify(error="非检测充值订单。"), 400

    payment_status = order.get("payment_status", "pending")
    if payment_status == "pending":
        try:
            query_result = adapter.query_payment(order_id)
            if query_result.get("status") == "paid":
                queried_order_id = query_result.get("order_id")
                trade_no = query_result.get("trade_no")
                amount = query_result.get("total_amount")
                if queried_order_id == order_id and trade_no and amount is not None:
                    process_payment_success(order_id, trade_no, amount)
                    order = Order.get_by_order_id(conn, order_id)
                    payment_status = order.get("payment_status", "pending")
        except Exception:
            current_app.logger.warning("Detect recharge payment query failed", exc_info=True)

        if payment_status == "pending":
            Order.expire_old_orders(conn)
            order = Order.get_by_order_id(conn, order_id)
            payment_status = order.get("payment_status", "pending")

    free, paid = User.get_detection_quota(conn, user_id)
    return jsonify({
        "order_id": order_id,
        "payment_status": payment_status,
        "status": order.get("status"),
        "price": order.get("price"),
        "words": order.get("recharge_words", 0),
        "free_words": free,
        "paid_words": paid,
        "detection_words": free + paid,
    })
